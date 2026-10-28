"""
Social signals: which tokens people are talking about in your Discord servers.

Pure Python, no network (so it is testable offline):
  extract_mints()    pull pump.fun token addresses out of message text
  SocialTracker      per token: how many DIFFERENT people mentioned it, in which servers, when
  OutcomeTracker     after a token is first mentioned, record its price at +5/+15/+60 min, so
                     analyze_discord.py can show which servers / callers / caller-counts were worth following

Everything people post is untrusted: this module only counts and measures. It never trades by itself.
"""
import re
import time
from collections import OrderedDict

# pump.fun token addresses are base58 and end in "pump" (32-44 characters in total)
_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_IDX = {c: i for i, c in enumerate(_B58)}
MINT_RE = re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{28,40}pump(?![1-9A-HJ-NP-Za-km-z])")
MAX_MINTS_PER_MESSAGE = 5      # a message listing dozens of addresses is spam, not a call


def is_solana_address(s: str) -> bool:
    """True if s is base58 that decodes to exactly 32 bytes (a real public key shape)."""
    n = 0
    for ch in s:
        v = _IDX.get(ch)
        if v is None:
            return False
        n = n * 58 + v
    pad = len(s) - len(s.lstrip("1"))
    return (n.bit_length() + 7) // 8 + pad == 32


def extract_mints(text: str) -> list[str]:
    """pump.fun token addresses found in text, in order, no duplicates."""
    out = []
    for m in MINT_RE.finditer(text or ""):
        s = m.group(0)
        if s not in out and is_solana_address(s):
            out.append(s)
            if len(out) >= MAX_MINTS_PER_MESSAGE:
                break
    return out


class SocialTracker:
    """Who mentioned which token, and when. Keeps the last `window` seconds."""

    def __init__(self, window: float = 3600, max_mints: int = 3000):
        self.window, self.max_mints = window, max_mints
        self.mints: OrderedDict[str, dict] = OrderedDict()   # oldest activity first

    def record(self, mint: str, author: str, guild: str, now: float) -> tuple[bool, bool]:
        """Count a mention. Returns (first_time_this_token_was_seen, this_author_is_new_for_it)."""
        self._prune(now)
        rec = self.mints.get(mint)
        first = rec is None
        if first:
            rec = self.mints[mint] = dict(first=now, last=now, n=0, authors={}, guilds={})
        new_author = author not in rec["authors"]
        rec["authors"][author] = now
        rec["guilds"][guild] = rec["guilds"].get(guild, 0) + 1
        rec["n"] += 1
        rec["last"] = now
        self.mints.move_to_end(mint)
        while len(self.mints) > self.max_mints:
            self.mints.popitem(last=False)
        return first, new_author

    def authors(self, mint: str, now: float, window: float | None = None) -> int:
        """Different people who mentioned the token within the window."""
        rec = self.mints.get(mint)
        if not rec:
            return 0
        cutoff = now - (window or self.window)
        return sum(1 for t in rec["authors"].values() if t >= cutoff)

    def top(self, now: float, window: float | None = None, n: int = 8) -> list[dict]:
        cutoff = now - (window or self.window)
        rows = []
        for mint, r in self.mints.items():
            if r["last"] < cutoff:
                continue
            a = sum(1 for t in r["authors"].values() if t >= cutoff)
            rows.append(dict(mint=mint, authors=a, mentions=r["n"], first_s=int(now - r["first"]),
                             last_s=int(now - r["last"]), guilds=sorted(r["guilds"], key=r["guilds"].get, reverse=True)))
        rows.sort(key=lambda x: (-x["authors"], -x["mentions"], x["last_s"]))
        return rows[:n]

    def _prune(self, now: float):
        cutoff = now - self.window
        while self.mints:
            mint, r = next(iter(self.mints.items()))
            if r["last"] >= cutoff:
                break
            self.mints.popitem(last=False)


class OutcomeTracker:
    """
    Follows tokens after they are first mentioned and records what the price did.
    Price = bonding-curve price (SOL per token) from PumpPortal trade events.
      p0     first price seen after the mention (what you could roughly have bought at)
      ret    % change from p0 at +5 / +15 / +60 minutes (last price seen up to that moment)
      peak_x highest price seen / p0
    Tokens that finish the bonding curve and move to a DEX stop reporting curve prices; they are flagged
    `graduated` (a heuristic) because their later returns are not captured.
    """
    CHECKPOINTS = (300, 900, 3600)

    def __init__(self, max_follow: int = 150, authors_fn=None):
        self.max_follow = max_follow
        self.authors_fn = authors_fn      # (mint, now) -> number of different people so far
        self.active: dict[str, dict] = {}

    def following(self, mint: str) -> bool:
        return mint in self.active

    def start(self, mint: str, now: float, meta: dict) -> bool:
        """Begin following a token. False if we are at the limit (so the caller shouldn't subscribe)."""
        if mint in self.active:
            return True
        if len(self.active) >= self.max_follow:
            return False
        self.active[mint] = dict(mint=mint, t0=now, meta=dict(meta), p0=None, p0_t=None, last=None,
                                 peak=None, cp={}, graduated=False, authors5=None)
        return True

    def on_event(self, ev: dict, now: float):
        r = self.active.get(ev.get("mint"))
        if not r:
            return
        v_sol, v_tok = ev.get("vSolInBondingCurve"), ev.get("vTokensInBondingCurve")
        pool = ev.get("pool")
        if (pool and pool != "pump") or (v_sol and float(v_sol) >= 84):
            r["graduated"] = True
        if v_sol and v_tok:
            px = float(v_sol) / float(v_tok)
            if r["p0"] is None:
                r["p0"], r["p0_t"] = px, now
            r["last"] = px
            r["peak"] = px if r["peak"] is None else max(r["peak"], px)

    def tick(self, now: float) -> tuple[list[dict], list[str]]:
        """Call about once a second. Returns (finished rows ready for the CSV, mints no longer followed)."""
        rows, finished = [], []
        for mint, r in list(self.active.items()):
            age = now - r["t0"]
            for cp in self.CHECKPOINTS:
                if age >= cp and cp not in r["cp"]:
                    r["cp"][cp] = r["last"]                         # carry forward the last price seen
                    if cp == self.CHECKPOINTS[0] and self.authors_fn:
                        # people who called it in the first 5 minutes, measured from the call itself
                        # (not from `now`, so a late tick still reports the same number)
                        r["authors5"] = self.authors_fn(mint, r["t0"] + cp)
            if age >= self.CHECKPOINTS[-1]:
                del self.active[mint]
                finished.append(mint)
                if r["p0"]:
                    rows.append(self._row(r))
        return rows, finished

    @staticmethod
    def _row(r: dict) -> dict:
        p0, m = r["p0"], r["meta"]

        def ret(cp):
            px = r["cp"].get(cp)
            return round((px / p0 - 1) * 100, 1) if px else ""
        return dict(
            time=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["t0"])), mint=r["mint"],
            guild_id=m.get("guild_id", ""), guild=m.get("guild", ""), channel=m.get("channel", ""),
            first_author_id=m.get("first_author_id", ""), first_author=m.get("first_author", ""),
            authors_5m=r["authors5"] if r["authors5"] is not None else "",
            p0=f"{p0:.12f}", p0_delay_s=int(r["p0_t"] - r["t0"]),
            ret5m=ret(300), ret15m=ret(900), ret60m=ret(3600),
            peak_x=round(r["peak"] / p0, 2), graduated=int(r["graduated"]))
