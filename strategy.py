"""
Strategy + risk engine. Pure Python, no network — so it can be tested offline.

Two ways the bot can enter a trade:
  LAUNCH - watch each new pump.fun token for WATCH_SECONDS, buy if it passes the filters
  COPY   - a wallet in TRACKED_WALLETS buys a token -> bot buys it too
           (and sells when that same wallet sells)

Every position exits on take-profit, stop-loss, trailing stop, or max hold time.
"""
from collections import deque
from dataclasses import dataclass, field
import os
import time

from wallets import load_wallets

TOTAL_SUPPLY = 1_000_000_000  # every pump.fun token launches with 1B supply


@dataclass
class Config:
    buy_sol: float = 0.5               # SOL spent per trade (hard max 0.5)
    max_open_positions: int = 5        # hard max 5
    max_daily_loss_sol: float = 1.5    # stop opening trades after losing this much today
    # launch sniping
    launch_trading: bool = True
    watch_seconds: int = 20            # MIN coin age (s) before the bot will buy
    max_age_seconds: int = 20          # MAX coin age (s): give up on a launch after this
    min_unique_buyers: int = 8         # holders: distinct non-dev buyers so far
    max_top_buyer_pct: float = 100     # holders: biggest single buyer's % of buy volume (100 = off)
    min_net_inflow_sol: float = 1.0
    max_dev_share: float = 0.10        # dev's initial buy as share of supply (0.10 = 10%)
    allow_dev_selling: bool = False    # False = skip a token once the dev sells
    min_mcap_sol: float = 30
    max_mcap_sol: float = 120
    bundle_window_ms: int = 1500       # buys this soon after launch count as "bundled"
    max_bundle_pct: float = 100        # max % of supply bought in that window by others (100 = off)
    # social (Discord servers read by discord_signals.py)
    min_social_authors: int = 0        # different people who must have posted the address (0 = off)
    social_window_min: int = 30        # only count calls from the last N minutes
    # wallet tracking / copy trading
    tracked_wallets: str = ""          # addresses and/or files (Axiom export .json, .txt): LOGGED
    copy_wallets: str = ""             # subset to COPY ("" = copy every tracked wallet)
    copy_trading: bool = True          # False = only log their trades, never copy
    copy_buy_sol: float = 0.5
    copy_min_sol: float = 0.2          # ignore tracked buys smaller than this
    copy_max_mcap_sol: float = 400     # don't chase tokens that already ran this far
    copy_sells: bool = True            # sell when the wallet we copied sells
    # exits
    take_profit_pct: float = 60
    stop_loss_pct: float = 25
    trailing_stop_pct: float = 20
    max_hold_seconds: int = 300
    fee_pct: float = 1.5               # assumed fees + slippage per side (paper mode)
    # self-review (review.py): after each session and every N hours
    review_mode: str = "apply"         # apply | suggest | off
    review_every_hours: float = 24
    review_min_trades: int = 20        # closed trades needed before it changes anything


@dataclass
class Watch:
    mint: str
    symbol: str
    dev: str
    created_at: float
    dev_share: float = 0.0
    buyers: set = field(default_factory=set)
    buy_sol: float = 0.0
    sell_sol: float = 0.0
    dev_sold: bool = False
    price: float = 0.0
    mcap_sol: float = 0.0
    buyer_sol: dict = field(default_factory=dict)
    bundle_tokens: float = 0.0
    bundle_buyers: set = field(default_factory=set)
    last_reason: str = ""


@dataclass
class Position:
    mint: str
    symbol: str
    entry_price: float
    tokens: float
    sol_in: float
    opened_at: float
    price: float
    peak: float
    source: str = "launch"             # "launch" or the wallet address we copied


def price_from(ev):
    """SOL per token, from the bonding-curve reserves in a PumpPortal event."""
    v_sol, v_tok = ev.get("vSolInBondingCurve"), ev.get("vTokensInBondingCurve")
    if v_sol and v_tok:
        return float(v_sol) / float(v_tok)
    return None


def short(addr: str) -> str:
    return f"{addr[:4]}..{addr[-4:]}" if len(addr) > 10 else addr


class Engine:
    """Turns market events into actions. The caller executes them."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        here = os.path.dirname(os.path.abspath(__file__))
        self.names = load_wallets(cfg.tracked_wallets, here)
        copy = load_wallets(cfg.copy_wallets, here) if cfg.copy_wallets else dict(self.names)
        for a, n in copy.items():               # copied wallets are always tracked too
            if not self.names.get(a):
                self.names[a] = n
        self.wallets = set(self.names)
        self.copy_from = set(copy)
        self.watching: dict[str, Watch] = {}
        self.positions: dict[str, Position] = {}
        self.busy: set[str] = set()       # mints with a buy/sell in flight
        self.paused = False               # dashboard: stop opening new trades
        # set by the bot to social.SocialTracker.authors; (mint, now, window_s) -> number of callers.
        # Kept as a plain callable so this module stays network-free and testable offline.
        self.social_authors = None
        self.realized_pnl_today = 0.0
        self.day = time.strftime("%Y-%m-%d")
        self.closed: list[dict] = []
        self._seen = deque(maxlen=5000)   # dedupe events delivered via 2 subscriptions
        self._seen_set: set = set()

    # ---------- events ----------
    def on_event(self, ev: dict, now: float) -> list[tuple]:
        tx, mint = ev.get("txType"), ev.get("mint")
        if not tx or not mint:
            return []
        sig = ev.get("signature")
        if sig:
            if sig in self._seen_set:
                return []
            if len(self._seen) == self._seen.maxlen:
                self._seen_set.discard(self._seen[0])
            self._seen.append(sig)
            self._seen_set.add(sig)

        price = price_from(ev)
        trader = ev.get("traderPublicKey", "")
        actions = []

        if trader in self.wallets and tx in ("buy", "sell"):
            actions += self._on_tracked(ev, trader, tx, mint, price, now)

        if tx == "create":
            if self.cfg.launch_trading:
                self.watching[mint] = Watch(
                    mint=mint, symbol=ev.get("symbol", "?"), dev=trader, created_at=now,
                    dev_share=float(ev.get("initialBuy") or 0) / TOTAL_SUPPLY,
                    price=price or 0.0, mcap_sol=float(ev.get("marketCapSol") or 0))
                actions.append(("subscribe", mint))
            return actions

        if mint in self.watching:
            w = self.watching[mint]
            sol = float(ev.get("solAmount") or 0)
            if tx == "buy":
                w.buy_sol += sol
                if trader != w.dev:
                    w.buyers.add(trader)
                    w.buyer_sol[trader] = w.buyer_sol.get(trader, 0.0) + sol
                    if (now - w.created_at) * 1000 <= self.cfg.bundle_window_ms:
                        w.bundle_tokens += float(ev.get("tokenAmount") or 0)
                        w.bundle_buyers.add(trader)
            elif tx == "sell":
                w.sell_sol += sol
                if trader == w.dev:
                    w.dev_sold = True
            if price:
                w.price = price
            w.mcap_sol = float(ev.get("marketCapSol") or w.mcap_sol)

        if mint in self.positions and price:
            p = self.positions[mint]
            p.price, p.peak = price, max(p.peak, price)
            actions += self._check_exit(p, now)

        return actions

    def _on_tracked(self, ev, trader, tx, mint, price, now) -> list[tuple]:
        c = self.cfg
        actions = [("wallet_trade", ev)]
        if not c.copy_trading:
            return actions
        if tx == "buy" and trader not in self.copy_from:
            return actions
        sol = float(ev.get("solAmount") or 0)
        mcap = float(ev.get("marketCapSol") or 0)

        if tx == "buy":
            if mint in self.positions or mint in self.busy:
                return actions
            if sol < c.copy_min_sol:
                reason = f"their buy {sol:.3f} SOL < {c.copy_min_sol}"
            elif mcap > c.copy_max_mcap_sol:
                reason = f"mcap {mcap:.0f} SOL too high to chase"
            elif not price:
                reason = "no bonding-curve price (token not on pump.fun curve)"
            elif not self.can_open():
                reason = "paused" if self.paused else "risk limits"
            else:
                self.watching.pop(mint, None)
                self.busy.add(mint)
                w = Watch(mint=mint, symbol=ev.get("symbol") or mint[:6], dev="",
                          created_at=now, price=price, mcap_sol=mcap)
                return actions + [("copy_buy", mint, w, trader)]
            actions.append(("copy_skip", mint, trader, reason))

        elif tx == "sell" and c.copy_sells:
            p = self.positions.get(mint)
            if p and p.source == trader and mint not in self.busy:
                if price:
                    p.price = price
                self.busy.add(mint)
                actions.append(("sell", mint, f"copied sell from {self.label(trader)}"))
        return actions

    def label(self, addr: str) -> str:
        name = self.names.get(addr)
        return f"{name} ({short(addr)})" if name else short(addr)

    # ---------- timer ----------
    def tick(self, now: float) -> list[tuple]:
        """Call ~once per second: decides on watched tokens and time-based exits."""
        self._roll_day()
        actions = []
        c = self.cfg
        for mint, w in list(self.watching.items()):
            age = now - w.created_at
            if mint in self.positions or mint in self.busy:
                del self.watching[mint]
                continue
            if age < c.watch_seconds:
                continue                                  # too young: keep watching
            ok, reason = self.passes_filters(w, now)
            if ok and self.can_open():
                del self.watching[mint]
                self.busy.add(mint)
                actions.append(("buy", mint, w, f"{reason}, age {age:.0f}s"))
            elif age >= max(c.max_age_seconds, c.watch_seconds):
                del self.watching[mint]                   # too old: give up
                actions.append(("skip", mint, w, reason if not ok else ("paused" if self.paused else "risk limits")))
                actions.append(("unsubscribe", mint))
            else:
                w.last_reason = reason
        for p in list(self.positions.values()):
            actions += self._check_exit(p, now)
        return actions

    # ---------- rules ----------
    def callers(self, mint: str, now: float) -> int:
        """How many different people posted this address in your Discord servers recently."""
        if not self.social_authors:
            return 0
        try:
            return int(self.social_authors(mint, now, self.cfg.social_window_min * 60))
        except Exception:
            return 0

    def passes_filters(self, w: Watch, now: float | None = None) -> tuple[bool, str]:
        c = self.cfg
        now = time.time() if now is None else now
        if w.dev_sold and not c.allow_dev_selling:
            return False, "dev sold"
        if w.dev_share > c.max_dev_share:
            return False, f"dev holds {w.dev_share:.0%}"
        if len(w.buyers) < c.min_unique_buyers:
            return False, f"only {len(w.buyers)} buyers"
        if c.max_top_buyer_pct < 100 and w.buyer_sol:
            share = max(w.buyer_sol.values()) / sum(w.buyer_sol.values()) * 100
            if share > c.max_top_buyer_pct:
                return False, f"top buyer {share:.0f}% of buys"
        if c.max_bundle_pct < 100:
            bundled = w.bundle_tokens / TOTAL_SUPPLY * 100
            if bundled > c.max_bundle_pct:
                return False, f"bundled {bundled:.1f}% of supply ({len(w.bundle_buyers)} wallets)"
        if c.min_social_authors > 0:
            n = self.callers(w.mint, now)
            if n < c.min_social_authors:
                return False, (f"only {n} Discord caller{'s' if n != 1 else ''} "
                               f"(need {c.min_social_authors})")
        if w.buy_sol - w.sell_sol < c.min_net_inflow_sol:
            return False, f"net inflow {w.buy_sol - w.sell_sol:.2f} SOL"
        if not (c.min_mcap_sol <= w.mcap_sol <= c.max_mcap_sol):
            return False, f"mcap {w.mcap_sol:.0f} SOL out of range"
        if w.price <= 0:
            return False, "no price"
        tracked_in = w.buyers & self.wallets
        extra = f", tracked wallet in: {', '.join(map(self.label, tracked_in))}" if tracked_in else ""
        callers = self.callers(w.mint, now)
        if callers:
            extra += f", {callers} Discord caller{'s' if callers != 1 else ''}"
        return True, f"{len(w.buyers)} buyers, +{w.buy_sol - w.sell_sol:.2f} SOL, mcap {w.mcap_sol:.0f}{extra}"

    def checklist(self, w: Watch, now: float | None = None) -> list[tuple[str, bool, str]]:
        """Every entry filter for one watched token as (name, passed, detail) - what the dashboard shows.
        passes_filters stops at the first failure; this lists them all. test_strategy.py checks the two
        always agree (all passed <=> passes_filters is True), so edit them together."""
        c = self.cfg
        now = time.time() if now is None else now
        out = []
        dev_bad = (w.dev_sold and not c.allow_dev_selling) or w.dev_share > c.max_dev_share
        out.append(("dev", not dev_bad,
                    "dev sold" if w.dev_sold and not c.allow_dev_selling else f"dev {w.dev_share:.0%}"))
        out.append(("buyers", len(w.buyers) >= c.min_unique_buyers, f"{len(w.buyers)}/{c.min_unique_buyers} buyers"))
        if c.max_top_buyer_pct < 100 and w.buyer_sol:
            share = max(w.buyer_sol.values()) / sum(w.buyer_sol.values()) * 100
            out.append(("top buyer", share <= c.max_top_buyer_pct, f"top {share:.0f}%"))
        if c.max_bundle_pct < 100:
            bundled = w.bundle_tokens / TOTAL_SUPPLY * 100
            out.append(("bundle", bundled <= c.max_bundle_pct, f"bundled {bundled:.1f}%"))
        if c.min_social_authors > 0:
            n = self.callers(w.mint, now)
            out.append(("callers", n >= c.min_social_authors, f"{n}/{c.min_social_authors} callers"))
        net = w.buy_sol - w.sell_sol
        out.append(("inflow", net >= c.min_net_inflow_sol, f"{net:+.2f}/{c.min_net_inflow_sol:g} SOL"))
        out.append(("mcap", c.min_mcap_sol <= w.mcap_sol <= c.max_mcap_sol, f"mcap {w.mcap_sol:.0f}"))
        out.append(("price", w.price > 0, "price" if w.price > 0 else "no price"))
        return out

    def can_open(self) -> bool:
        if self.paused:
            return False
        open_or_pending = len(self.positions) + len(self.busy - set(self.positions))
        return (open_or_pending < self.cfg.max_open_positions
                and self.realized_pnl_today > -self.cfg.max_daily_loss_sol)

    def _check_exit(self, p: Position, now: float) -> list[tuple]:
        if p.mint in self.busy:
            return []
        c = self.cfg
        change = (p.price / p.entry_price - 1) * 100
        drop_from_peak = (1 - p.price / p.peak) * 100 if p.peak else 0
        reason = None
        if change >= c.take_profit_pct:
            reason = f"take profit {change:+.0f}%"
        elif change <= -c.stop_loss_pct:
            reason = f"stop loss {change:+.0f}%"
        elif c.trailing_stop_pct > 0 and p.peak > p.entry_price and change > 0 and drop_from_peak >= c.trailing_stop_pct:
            reason = f"trailing stop ({drop_from_peak:.0f}% off peak, {change:+.0f}%)"
        elif now - p.opened_at >= c.max_hold_seconds:
            reason = f"max hold time ({change:+.0f}%)"
        if reason:
            self.busy.add(p.mint)
            return [("sell", p.mint, reason)]
        return []

    def manual_sell(self, mint: str) -> list[tuple]:
        """Dashboard 'Sell now' button."""
        if mint in self.positions and mint not in self.busy:
            self.busy.add(mint)
            return [("sell", mint, "manual sell (dashboard)")]
        return []

    # ---------- bookkeeping ----------
    def release(self, mint: str):
        """Call when a buy/sell failed so the mint can be traded again."""
        self.busy.discard(mint)

    def record_buy(self, w: Watch, now: float, sol_in: float, source: str = "launch"):
        self.busy.discard(w.mint)
        fee = 1 - self.cfg.fee_pct / 100
        self.positions[w.mint] = Position(
            mint=w.mint, symbol=w.symbol, entry_price=w.price,
            tokens=sol_in * fee / w.price, sol_in=sol_in,
            opened_at=now, price=w.price, peak=w.price, source=source)

    def record_sell(self, mint: str, now: float, reason: str) -> dict | None:
        self.busy.discard(mint)
        p = self.positions.pop(mint, None)
        if not p:
            return None
        sol_out = p.tokens * p.price * (1 - self.cfg.fee_pct / 100)
        pnl = sol_out - p.sol_in
        self.realized_pnl_today += pnl
        trade = dict(symbol=p.symbol, mint=p.mint,
                     source="launch" if p.source == "launch" else f"copy:{p.source}",
                     sol_in=round(p.sol_in, 4), sol_out=round(sol_out, 4),
                     pnl=round(pnl, 4), pct=round(pnl / p.sol_in * 100, 1),
                     held_s=int(now - p.opened_at), reason=reason)
        self.closed.append(trade)
        return trade

    def _roll_day(self):
        today = time.strftime("%Y-%m-%d")
        if today != self.day:
            self.day, self.realized_pnl_today = today, 0.0
