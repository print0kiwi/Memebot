"""
Monitor X (Twitter) accounts for meme coin market signals.

  python x_monitor.py                  # runs forever, polls every 60 s
  python x_monitor.py --once           # one pass (for testing / scheduled jobs)
  python x_monitor.py --report         # per-account stats from what has been collected

What it extracts from every new post:
  - Solana contract addresses (also inside pump.fun / dexscreener / axiom / gmgn / birdeye links)
  - $TICKERS
  - market keywords you configure (e.g. "sol", "memecoin", "launch", "aped")

For every contract address posted, it records the token's price and market cap at the time
of the post, then re-checks at +15 min, +1 h and +4 h. So over time you learn which
accounts' calls actually go up, and how fast (x_calls.csv / --report).

Needs ONE of these in .env:
  TWITTERAPI_IO_KEY=...    twitterapi.io, ~$0.15 per 1,000 tweets (cheapest)
  X_BEARER_TOKEN=...       official X API, pay-per-use (~$0.005 per post read)
Prices come from GeckoTerminal's free API (no key).

Files: x_posts.csv (all posts), x_signals.csv (everything extracted), x_calls.csv
(contract-address calls with price follow-ups), x_state.json (resume state).
Pure standard library.
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

ACCOUNTS_FILE = "x_accounts.txt"
STATE = "x_state.json"
POSTS, SIGNALS, CALLS = "x_posts.csv", "x_signals.csv", "x_calls.csv"
FOLLOWUPS = [("15m", 900), ("1h", 3600), ("4h", 14400)]

BASE58 = r"[1-9A-HJ-NP-Za-km-z]{32,44}"
CA_RE = re.compile(rf"(?<![A-Za-z0-9/])({BASE58})(?![A-Za-z0-9])")
LINK_RE = re.compile(rf"(?:pump\.fun/(?:coin/)?|dexscreener\.com/solana/|axiom\.trade/(?:meme|t)/"
                     rf"|gmgn\.ai/sol/token/(?:\w+_)?|birdeye\.so/token/|photon-sol\.tinyastro\.io/\w+/lp/"
                     rf"|solscan\.io/token/)({BASE58})")
TICKER_RE = re.compile(r"(?<![\w$])\$([A-Za-z][A-Za-z0-9_]{1,14})\b")
NOT_TICKERS = {"SOL", "BTC", "ETH", "USD", "USDC", "USDT"}
DEFAULT_KEYWORDS = ("memecoin,meme coin,solana,$sol,pump.fun,pumpfun,launch,launching,ca:,contract,"
                    "aped,aping,bought,buying,sold,selling,rug,rugged,airdrop,doge,dogecoin,crypto,"
                    "bitcoin,token,coin,bonding curve,migrat,dex paid,cto")


def now() -> int:
    return int(time.time())


def iso(ts) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def append(path, row: dict):
    new = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new:
            w.writeheader()
        w.writerow(row)


def http_json(url, headers=None, tries=4):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "x-monitor/1.0", **(headers or {})})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise SystemExit(f"Access refused ({e.code}) for {url.split('?')[0]}. Check your key.")
            if e.code == 404:
                return None
            time.sleep(15 if e.code == 429 else 3 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError):
            time.sleep(3 * (attempt + 1))
    return None


# ---------------------------------------------------------------- X providers
class TwitterApiIo:
    name = "twitterapi.io"

    def __init__(self, key):
        self.h = {"X-API-Key": key}

    def latest(self, handle, state):
        d = http_json("https://api.twitterapi.io/twitter/user/last_tweets?"
                      + urllib.parse.urlencode({"userName": handle}), self.h)
        tweets = ((d or {}).get("data") or {}).get("tweets") or (d or {}).get("tweets") or []
        return [norm(t, handle) for t in tweets]


class OfficialX:
    name = "X API"

    def __init__(self, token):
        self.h = {"Authorization": f"Bearer {token}"}

    def latest(self, handle, state):
        ids = state.setdefault("user_ids", {})
        if handle not in ids:
            u = http_json(f"https://api.x.com/2/users/by/username/{handle}", self.h)
            if not u or "data" not in u:
                print(f"  ! could not find @{handle}")
                return []
            ids[handle] = u["data"]["id"]
        q = {"max_results": 10, "tweet.fields": "created_at,entities,referenced_tweets"}
        since = state.get("last_id", {}).get(handle)
        if since:
            q["since_id"] = since
        d = http_json(f"https://api.x.com/2/users/{ids[handle]}/tweets?{urllib.parse.urlencode(q)}", self.h)
        return [norm(t, handle) for t in (d or {}).get("data") or []]


def norm(t: dict, handle: str) -> dict:
    tid = str(t.get("id") or t.get("id_str") or "")
    created = t.get("createdAt") or t.get("created_at") or ""
    try:
        ts = int(parsedate_to_datetime(created).timestamp())
    except (TypeError, ValueError):
        try:
            ts = int(datetime.fromisoformat(created.replace("Z", "+00:00")).timestamp())
        except ValueError:
            ts = now()
    urls = []
    for u in ((t.get("entities") or {}).get("urls") or []):
        urls.append(u.get("expanded_url") or u.get("url") or "")
    text = t.get("text") or t.get("full_text") or ""
    return dict(id=tid, ts=ts, handle=handle, text=text, urls=urls,
                url=t.get("url") or f"https://x.com/{handle}/status/{tid}")


# ---------------------------------------------------------------- extraction
def extract(post: dict, keywords: list[str]) -> list[tuple[str, str]]:
    blob = post["text"] + " " + " ".join(post["urls"])
    found = []
    cas = set(LINK_RE.findall(blob))
    for m in CA_RE.findall(blob):
        if any(c.isdigit() for c in m) or m.endswith("pump"):  # skip plain long words
            cas.add(m)
    found += [("contract", c) for c in sorted(cas)]
    tick = {t.upper() for t in TICKER_RE.findall(post["text"])} - NOT_TICKERS
    found += [("ticker", "$" + t) for t in sorted(tick)]
    low = post["text"].lower()
    found += [("keyword", k) for k in keywords if k and k in low]
    return found


# ---------------------------------------------------------------- prices
def token_snapshot(mint: str) -> dict | None:
    d = http_json(f"https://api.geckoterminal.com/api/v2/networks/solana/tokens/{mint}",
                  {"Accept": "application/json"})
    a = ((d or {}).get("data") or {}).get("attributes")
    if not a:
        return None
    f = lambda k: float(a.get(k) or 0) or None
    return dict(symbol=a.get("symbol") or "", price=f("price_usd"),
                mcap=f("market_cap_usd") or f("fdv_usd"),
                vol24=float((a.get("volume_usd") or {}).get("h24") or 0))


def pct(a, b):
    return round((b / a - 1) * 100, 1) if a and b else ""


def run_followups(state):
    """Fill +15m/+1h/+4h prices for open calls whose time has come."""
    pending = state.setdefault("calls", [])
    changed = False
    for c in pending:
        for label, delay in FOLLOWUPS:
            if c.get(f"price_{label}") is None and now() >= c["ts"] + delay:
                snap = token_snapshot(c["mint"])
                c[f"price_{label}"] = snap["price"] if snap else 0
                changed = True
                print(f"  follow-up {label}: ${c['symbol'] or c['mint'][:6]} called by @{c['handle']} "
                      f"{pct(c['price_0'], c[f'price_{label}']) or 'n/a'}%")
    done = [c for c in pending if all(c.get(f"price_{l}") is not None for l, _ in FOLLOWUPS)]
    for c in done:
        append(CALLS, {
            "time": iso(c["ts"]), "account": c["handle"], "symbol": c["symbol"], "mint": c["mint"],
            "price_at_post": c["price_0"], "mcap_at_post": c["mcap_0"],
            **{f"chg_{l}_pct": pct(c["price_0"], c[f"price_{l}"]) for l, _ in FOLLOWUPS},
            "post": c["url"]})
        pending.remove(c)
    return changed or bool(done)


# ---------------------------------------------------------------- main loop
def load_accounts(path):
    out = []
    for line in open(path, encoding="utf-8"):
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(line.rstrip("/").split("/")[-1].lstrip("@"))
    return out


def load_env():
    if os.path.exists(".env"):
        for line in open(".env", encoding="utf-8"):
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def poll(provider, handles, state, keywords, first_run):
    last = state.setdefault("last_id", {})
    for h in handles:
        posts = provider.latest(h, state)
        prev = int(last.get(h) or 0)
        new = sorted((p for p in posts if p["id"].isdigit() and int(p["id"]) > prev), key=lambda p: int(p["id"]))
        if not new:
            continue
        last[h] = new[-1]["id"]
        backfill = first_run and not prev
        for p in new:
            sig = extract(p, keywords)
            append(POSTS, {"time": iso(p["ts"]), "account": h, "id": p["id"],
                           "signals": "; ".join(v for _, v in sig), "text": p["text"].replace("\n", " "),
                           "url": p["url"]})
            for kind, val in sig:
                append(SIGNALS, {"time": iso(p["ts"]), "account": h, "kind": kind, "value": val,
                                 "backfill": backfill, "url": p["url"]})
            cas = [v for k, v in sig if k == "contract"]
            if sig and not backfill:
                print(f"[{iso(p['ts'])}] @{h}: {p['text'][:110]!r}")
                print(f"   signals: {', '.join(v for _, v in sig)}")
            for mint in cas:
                if backfill and now() - p["ts"] > 3600:
                    continue                       # too old to measure "price at post"
                snap = token_snapshot(mint)
                if not snap or not snap["price"]:
                    print(f"   {mint[:8]}..: no market yet / not found")
                    continue
                state.setdefault("calls", []).append(dict(
                    ts=p["ts"], handle=h, mint=mint, symbol=snap["symbol"], url=p["url"],
                    price_0=snap["price"], mcap_0=snap["mcap"]))
                print(f"   CALL ${snap['symbol']}  mcap ${snap['mcap'] or 0:,.0f}  "
                      f"https://pump.fun/coin/{mint}")


def report():
    if not os.path.exists(CALLS):
        print("No completed calls yet (each needs 4 h of follow-up). Signals so far:")
        if os.path.exists(SIGNALS):
            counts = {}
            for r in csv.DictReader(open(SIGNALS, encoding="utf-8")):
                counts.setdefault(r["account"], {}).setdefault(r["kind"], 0)
                counts[r["account"]][r["kind"]] += 1
            for a, c in sorted(counts.items()):
                print(f"  @{a:<18} {c}")
        return
    by = {}
    for r in csv.DictReader(open(CALLS, encoding="utf-8")):
        by.setdefault(r["account"], []).append(r)
    print(f"{'account':<18}{'calls':>6}" + "".join(f"{'avg ' + l:>10}{'up% ' + l:>9}" for l, _ in FOLLOWUPS))
    for a, rows in sorted(by.items(), key=lambda kv: -len(kv[1])):
        line = f"@{a:<17}{len(rows):>6}"
        for l, _ in FOLLOWUPS:
            v = [float(r[f"chg_{l}_pct"]) for r in rows if r[f"chg_{l}_pct"] not in ("", None)]
            line += (f"{sum(v) / len(v):>9.1f}%{sum(x > 0 for x in v) / len(v) * 100:>8.0f}%" if v
                     else f"{'-':>10}{'-':>9}")
        print(line)
    print("\navg = average price change after the post; up% = share of calls that were higher.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--accounts", default=ACCOUNTS_FILE)
    ap.add_argument("--interval", type=int, default=60, help="seconds between polls")
    ap.add_argument("--keywords", default=None, help="comma-separated, overrides X_KEYWORDS")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--report", action="store_true")
    a = ap.parse_args()
    load_env()
    if a.report:
        return report()

    key, bearer = os.getenv("TWITTERAPI_IO_KEY"), os.getenv("X_BEARER_TOKEN")
    if key:
        provider = TwitterApiIo(key)
    elif bearer:
        provider = OfficialX(bearer)
    else:
        sys.exit("Set TWITTERAPI_IO_KEY (cheapest) or X_BEARER_TOKEN in .env")
    keywords = [k.strip().lower() for k in (a.keywords or os.getenv("X_KEYWORDS") or DEFAULT_KEYWORDS).split(",")]
    handles = load_accounts(a.accounts)
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    first_run = not state.get("last_id")
    print(f"Monitoring {len(handles)} accounts via {provider.name} every {a.interval}s: "
          + ", ".join("@" + h for h in handles))
    if first_run:
        print("First run: recent posts are logged as backfill, alerts start with new posts.")
    while True:
        try:
            poll(provider, handles, state, keywords, first_run)
            run_followups(state)
        except SystemExit:
            raise
        except Exception as e:  # keep the monitor alive through one-off errors
            print(f"  ! poll error: {e}")
        json.dump(state, open(STATE, "w"))
        first_run = False
        if a.once:
            break
        time.sleep(a.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Run with --report to see per-account call stats.")
