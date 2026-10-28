"""
Solana meme coin bot (pump.fun tokens via PumpPortal).

  python bot.py            -> PAPER mode (default): simulates trades, no wallet needed
  LIVE_TRADING=true ...    -> LIVE mode: signs real trades with your bot wallet

Your private key is only used locally to sign transactions; it is never sent anywhere.
"""
import asyncio
import base64
import csv
import json
import logging
import os
import time
from dataclasses import fields

import httpx
import websockets
from dotenv import load_dotenv

from dashboard import Dashboard, Telemetry, skip_bucket
import discord_signals
from notifier import Notifier
import remote
import review
import settings as ui_settings
from social import OutcomeTracker, SocialTracker
from strategy import Config, Engine, price_from, short

load_dotenv()
log = logging.getLogger("bot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")

WS_URL = "wss://pumpportal.fun/api/data"
TRADE_URL = "https://pumpportal.fun/api/trade-local"

LIVE = os.getenv("LIVE_TRADING", "false").lower() == "true"
RPC_URL = os.getenv("RPC_URL", "https://api.mainnet-beta.solana.com")
PRIORITY_FEE = float(os.getenv("PRIORITY_FEE_SOL", "0.0005"))
SLIPPAGE = int(os.getenv("SLIPPAGE_PCT", "15"))
# Hard limits: enforced no matter what .env says
HARD_MAX_SOL_PER_TRADE = 0.5
HARD_MAX_POSITIONS = 5

TRADE_LOG = "trades.csv"
SETTINGS_FILE = "settings.json"   # written by the dashboard; overrides .env
WALLET_LOG = "wallet_trades.csv"
CONTEXT_LOG = review.CONTEXT        # entry conditions of each buy, for the self-review
HERE = os.path.dirname(os.path.abspath(__file__))


def load_config() -> Config:
    """Any Config field can be overridden in .env, e.g. BUY_SOL=0.02"""
    cfg = Config()
    for f in fields(Config):
        val = os.getenv(f.name.upper())
        if val is None:
            continue
        default = getattr(cfg, f.name)
        if isinstance(default, bool):
            setattr(cfg, f.name, val.strip().lower() in ("1", "true", "yes", "on"))
        else:
            setattr(cfg, f.name, type(default)(val))
    saved = ui_settings.load(SETTINGS_FILE)
    for k, v in saved.items():
        setattr(cfg, k, v)
    if saved:
        log.info("Loaded %d settings saved from the dashboard (%s); they override .env", len(saved), SETTINGS_FILE)
    for k in ("buy_sol", "copy_buy_sol"):
        if getattr(cfg, k) > HARD_MAX_SOL_PER_TRADE:
            log.warning("%s=%s is above the 0.5 SOL limit, using 0.5", k.upper(), getattr(cfg, k))
            setattr(cfg, k, HARD_MAX_SOL_PER_TRADE)
    if cfg.max_open_positions > HARD_MAX_POSITIONS:
        log.warning("MAX_OPEN_POSITIONS=%s is above the limit of 5, using 5", cfg.max_open_positions)
        cfg.max_open_positions = HARD_MAX_POSITIONS
    return cfg


def append_csv(path: str, row: dict):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if new:
            w.writeheader()
        w.writerow(row)


class Wallet:
    """Signs PumpPortal-built transactions locally and sends them to your RPC."""

    def __init__(self):
        from solders.keypair import Keypair
        key = os.getenv("BOT_WALLET_PRIVATE_KEY")
        if not key:
            raise SystemExit("LIVE_TRADING=true but BOT_WALLET_PRIVATE_KEY is not set in .env")
        self.kp = Keypair.from_base58_string(key.strip())
        self.pubkey = str(self.kp.pubkey())
        self.http = httpx.AsyncClient(timeout=15)

    async def trade(self, action: str, mint: str, amount, in_sol: bool) -> str:
        if action == "buy" and (not in_sol or float(amount) > HARD_MAX_SOL_PER_TRADE + 1e-9):
            raise RuntimeError(f"blocked: buy of {amount} exceeds 0.5 SOL limit")
        from solders.transaction import VersionedTransaction
        r = await self.http.post(TRADE_URL, json={
            "publicKey": self.pubkey, "action": action, "mint": mint,
            "amount": amount, "denominatedInSol": "true" if in_sol else "false",
            "slippage": SLIPPAGE, "priorityFee": PRIORITY_FEE, "pool": "auto",
        })
        r.raise_for_status()
        unsigned = VersionedTransaction.from_bytes(r.content)
        signed = VersionedTransaction(unsigned.message, [self.kp])
        rpc = await self.http.post(RPC_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "sendTransaction",
            "params": [base64.b64encode(bytes(signed)).decode(),
                       {"encoding": "base64", "maxRetries": 3}],
        })
        data = rpc.json()
        if "error" in data:
            raise RuntimeError(data["error"])
        return data["result"]  # transaction signature

    async def balance(self) -> float:
        r = await self.http.post(RPC_URL, json={
            "jsonrpc": "2.0", "id": 1, "method": "getBalance", "params": [self.pubkey]})
        return r.json()["result"]["value"] / 1e9


class Bot:
    def __init__(self):
        self.cfg = load_config()
        self.engine = Engine(self.cfg)
        self.wallet = Wallet() if LIVE else None
        self.notify = Notifier(httpx.AsyncClient(timeout=15), LIVE)
        self.live = LIVE
        self.settings_path = SETTINGS_FILE
        self.tel = Telemetry(LIVE, TRADE_LOG)
        self.ws = None
        self.session_start = time.time()
        self.review_root = os.path.dirname(os.path.abspath(TRADE_LOG))
        self.tel.review = review.last_review(self.review_root)
        # ---- Discord signals: which coins your servers are calling ----
        self.social = SocialTracker(window=max(3600, self.cfg.social_window_min * 60))
        self.engine.social_authors = self.social.authors
        self.outcomes = OutcomeTracker(authors_fn=lambda m, n: self.social.authors(m, n, 300))
        self.discord = discord_signals.DiscordListener(
            os.getenv("DISCORD_BOT_TOKEN", ""), self.social, on_call=self.on_discord_call,
            allow=discord_signals.load_filter(discord_signals.SERVERS_FILE, HERE),
            log_path=os.path.join(HERE, discord_signals.CALL_LOG))

    async def on_discord_call(self, mint: str, info: dict):
        """Someone posted a token address in one of your servers."""
        n = info["authors"]
        self.tel.discord_calls += 1
        if info["first"] or info["new_author"]:
            self.tel.social_event(mint, info)
        # follow its price for an hour so analyze_discord.py can score the server and the caller
        if self.outcomes.start(mint, info["ts"], dict(
                guild_id=info["guild_id"], guild=info["guild"], channel=info["channel"],
                first_author_id=info["author_id"], first_author=info["author"])) and self.ws:
            try:
                await self.send("subscribeTokenTrade", [mint])
            except Exception:
                pass
        if info["first"]:
            log.info("DISCORD %s called %s in %s", info["author"], short(mint), info["guild"])
        elif n >= max(2, self.cfg.min_social_authors):
            log.info("DISCORD %s now has %d callers (latest: %s)", short(mint), n, info["author"])

    async def send(self, method: str, mints: list[str] | None = None):
        msg = {"method": method}
        if mints:
            msg["keys"] = mints
        await self.ws.send(json.dumps(msg))

    async def handle(self, actions: list[tuple]):
        now = time.time()
        for a in actions:
            kind, mint = a[0], a[1]
            if kind == "subscribe":
                await self.send("subscribeTokenTrade", [mint])
            elif kind == "unsubscribe":
                await self.send("unsubscribeTokenTrade", [mint])
            elif kind == "skip":
                log.debug("skip  %-10s %s", a[2].symbol, a[3])
                self.tel.skips[skip_bucket(a[3])] += 1
                self.tel.decide("skip", a[2], a[3])
            elif kind == "wallet_trade":
                self.log_wallet_trade(a[1])
                ev = a[1]
                self.tel.wallet_trades += 1
                self.tel.event("wallet", f"{self.engine.label(ev.get('traderPublicKey', ''))} "
                               f"{ev.get('txType')} {float(ev.get('solAmount') or 0):.2f} SOL of "
                               f"{ev.get('symbol') or short(ev.get('mint') or '')}", ev.get("mint", ""))
            elif kind == "copy_skip":
                log.info("      not copying %s on %s: %s", self.engine.label(a[2]), short(mint), a[3])
                self.tel.skips[skip_bucket(a[3])] += 1
                self.tel.event("copyskip", f"Not copying {self.engine.label(a[2])}: {a[3]}", mint)
            elif kind in ("buy", "copy_buy"):
                w = a[2]
                is_copy = kind == "copy_buy"
                sol = self.cfg.copy_buy_sol if is_copy else self.cfg.buy_sol
                why = f"copying {self.engine.label(a[3])}" if is_copy else a[3]
                if is_copy:
                    await self.send("subscribeTokenTrade", [mint])
                sig = None
                if LIVE:
                    try:
                        sig = await self.wallet.trade("buy", mint, sol, in_sol=True)
                        log.info("LIVE BUY  %s  https://solscan.io/tx/%s", w.symbol, sig)
                    except Exception as e:
                        log.warning("buy failed %s: %s", w.symbol, e)
                        self.tel.event("error", f"Buy failed {w.symbol}: {e}", mint)
                        self.engine.release(mint)
                        await self.send("unsubscribeTokenTrade", [mint])
                        continue
                self.engine.record_buy(w, now, sol, source=a[3] if is_copy else "launch")
                self.log_context(w, now, "launch" if not is_copy else "copy")
                self.tel.decide("buy", w, why)
                log.info("BUY   %-10s %.3f SOL  (%s)  pump.fun/coin/%s", w.symbol, sol, why, mint)
                self.notify.buy(w.symbol, mint, sol, why, sig)
                self.tel.event("buy", f"BUY {w.symbol} {sol:g} SOL ({why})", mint)
            elif kind == "sell":
                reason = a[2]
                sig = None
                if LIVE:
                    try:
                        sig = await self.wallet.trade("sell", mint, "100%", in_sol=False)
                        log.info("LIVE SELL https://solscan.io/tx/%s", sig)
                    except Exception as e:
                        log.warning("sell failed %s: %s (will retry)", mint, e)
                        self.tel.event("error", f"Sell failed {short(mint)}: {e} (will retry)", mint)
                        self.engine.release(mint)
                        continue
                t = self.engine.record_sell(mint, now, reason)
                if t:
                    await self.send("unsubscribeTokenTrade", [mint])
                    self.log_trade(t)
                    log.info("SELL  %-10s %+.4f SOL (%+.1f%%)  %s  | today: %+.4f SOL",
                             t["symbol"], t["pnl"], t["pct"], reason,
                             self.engine.realized_pnl_today)
                    self.notify.sell(t, sig)
                    self.tel.closed(t)
                    self.tel.event("sell", f"SELL {t['symbol']} {t['pnl']:+.4f} SOL ({t['pct']:+.1f}%) {reason}", mint)

    def log_trade(self, t: dict):
        append_csv(TRADE_LOG, {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
                               "mode": "live" if LIVE else "paper", **t})

    def log_context(self, w, now: float, source: str):
        """What the coin looked like when we bought (review.py uses it to judge entry rules)."""
        try:
            append_csv(CONTEXT_LOG, {
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)), "mode": "live" if LIVE else "paper",
                "mint": w.mint, "symbol": w.symbol, "source": source, "age_s": int(now - w.created_at),
                "mcap_sol": round(w.mcap_sol, 1), "buyers": len(w.buyers),
                "net_inflow_sol": round(w.buy_sol - w.sell_sol, 3), "dev_share": round(w.dev_share, 4)})
        except (OSError, AttributeError, TypeError) as e:
            log.debug("context log failed: %s", e)

    def do_review(self, label: str, start: float | None = None, force_apply: bool | None = None) -> dict | None:
        """Look at this window's trades and adjust the exits/entry filters if they clearly need it."""
        mode = (self.cfg.review_mode or "apply").strip().lower()
        if mode == "off" and force_apply is None:
            return None
        apply = (mode == "apply") if force_apply is None else force_apply
        try:
            rec = review.run_review(self.cfg, self.review_root, self.settings_path, start=start,
                                    hours=self.cfg.review_every_hours, mode="live" if LIVE else "paper",
                                    apply=apply, min_trades=self.cfg.review_min_trades, label=label,
                                    files=(TRADE_LOG, CONTEXT_LOG))
        except Exception as e:                                  # a review must never stop the bot
            log.warning("self-review failed: %s", e)
            return None
        # hard limits, enforced again (review never touches them, this is belt and braces)
        self.cfg.buy_sol, self.cfg.copy_buy_sol = min(self.cfg.buy_sol, 0.5), min(self.cfg.copy_buy_sol, 0.5)
        self.cfg.max_open_positions = min(self.cfg.max_open_positions, 5)
        self.tel.review = rec
        text = review.summary(rec)
        log.info("%s", text)
        if rec["changes"]:
            verb = "Strategy updated" if rec["applied"] else "Strategy suggestion"
            self.tel.event("control", f"{verb} by self-review: " + "; ".join(
                f"{ui_settings.BY_KEY[c['key']]['label']} {c['old']:g}→{c['new']:g}" for c in rec["changes"]))
            if self.notify.enabled:
                self.notify.text("🧠 " + text)
        else:
            self.tel.event("control", f"Self-review ({label}): no change. " + " ".join(rec.get("notes", [])))
        return rec

    async def reviewer(self):
        """Every review_every_hours (default 24), review the trades from that window."""
        while True:
            await asyncio.sleep(600)
            last = review.load_state(self.review_root).get("last_review_ts") or self.session_start
            if time.time() - last >= self.cfg.review_every_hours * 3600:
                self.do_review("24h" if self.cfg.review_every_hours == 24 else f"{self.cfg.review_every_hours:g}h")

    def end_session(self):
        """Called when the bot stops: review this session's trades."""
        if time.time() - self.session_start > 60:
            self.do_review("session", start=self.session_start)

    def log_wallet_trade(self, ev: dict):
        """Every trade a tracked wallet makes -> wallet_trades.csv (your research data)."""
        price = price_from(ev)
        row = {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "wallet": ev.get("traderPublicKey"),
            "name": self.engine.names.get(ev.get("traderPublicKey"), ""),
            "side": ev.get("txType"),
            "mint": ev.get("mint"),
            "sol": round(float(ev.get("solAmount") or 0), 4),
            "tokens": ev.get("tokenAmount"),
            "price_sol": f"{price:.12f}" if price else "",
            "mcap_sol": round(float(ev.get("marketCapSol") or 0), 1),
            "pool": ev.get("pool", ""),
            "tx": ev.get("signature", ""),
        }
        append_csv(WALLET_LOG, row)
        log.info("WATCH %s %-4s %.3f SOL of %s (mcap %.0f SOL)",
                 self.engine.label(row["wallet"]), row["side"], row["sol"], short(row["mint"]), row["mcap_sol"])

    async def ticker(self):
        last_bal = 0.0
        while True:
            await asyncio.sleep(1)
            now = time.time()
            if self.ws:
                await self.handle(self.engine.tick(now))
            rows, done = self.outcomes.tick(now)
            for r in rows:
                append_csv(os.path.join(HERE, discord_signals.OUTCOME_LOG), r)
            for mint in done:
                if mint not in self.engine.positions and mint not in self.engine.watching and self.ws:
                    try:
                        await self.send("unsubscribeTokenTrade", [mint])
                    except Exception:
                        pass
            if LIVE and time.time() - last_bal > 30:
                last_bal = time.time()
                try:
                    self.tel.balance = await self.wallet.balance()
                except Exception:
                    pass

    async def start_remote(self):
        """Second dashboard port that needs the access key (see remote.py). Never prints the key."""
        try:
            key = remote.load_key(HERE)
            port = int(os.getenv("REMOTE_PORT", str(remote.DEFAULT_PORT)))
            bind = os.getenv("REMOTE_BIND", "").strip() or remote.default_bind()
            await Dashboard(self, bind, port, auth=remote.Auth(key)).start()
            addrs = remote.urls(bind, port) or [f"http://<this computer>:{port}"]
            log.info("Remote control ON: %s  (access key: ./memebot remote)", ", ".join(addrs))
            if bind in ("0.0.0.0", "::"):
                log.warning("Remote control is on plain HTTP over your local network. Use a network you trust, "
                            "or install Tailscale and it binds to that private address instead.")
        except (OSError, ValueError) as e:
            log.warning("Remote control couldn't start: %s", e)

    async def run(self):
        mode = "LIVE" if LIVE else "PAPER"
        log.info("Starting in %s mode | %.3f SOL/trade | max %d positions | daily loss cap %.2f SOL",
                 mode, self.cfg.buy_sol, self.cfg.max_open_positions, self.cfg.max_daily_loss_sol)
        wallets = sorted(self.engine.wallets)
        if wallets:
            n_copy = len(self.engine.copy_from) if self.cfg.copy_trading else 0
            log.info("Tracking %d wallets, copying %d", len(wallets), n_copy)
            if self.cfg.copy_trading and n_copy == 0:
                log.info("Copying 0 wallets: run analyze_wallets.py to fill wallets/best_wallets.txt")
            if n_copy > 25:
                log.warning("Copying %d wallets will fire constantly and burn fees. Run "
                            "analyze_wallets.py and set COPY_WALLETS=wallets/best_wallets.txt", n_copy)
        if not self.cfg.launch_trading:
            log.info("Launch sniping OFF: only copy trades will be made")
        if LIVE:
            log.info("Wallet %s  balance %.4f SOL", self.wallet.pubkey, await self.wallet.balance())
            if self.wallet.pubkey in self.engine.wallets:
                log.warning("Your bot wallet is also in TRACKED_WALLETS; it will react to its own trades. Removing it.")
                self.engine.wallets.discard(self.wallet.pubkey)
                self.engine.copy_from.discard(self.wallet.pubkey)
        if self.notify.enabled:
            log.info("Discord alerts: %s", self.notify.enabled)
            self.notify.text(f"🤖 Bot started in **{mode}** mode: {self.cfg.buy_sol:g} SOL per trade, "
                             f"max {self.cfg.max_open_positions} open")
        port = int(os.getenv("DASHBOARD_PORT", "8787"))
        if port:
            try:
                url = await Dashboard(self, os.getenv("DASHBOARD_HOST", "127.0.0.1"), port).start()
                log.info("Dashboard: %s", url)
                if os.getenv("DASHBOARD_OPEN", "true").lower() == "true":
                    import webbrowser
                    webbrowser.open(url)
            except (OSError, ValueError) as e:
                log.warning("Dashboard couldn't start on port %s (%s). Set DASHBOARD_PORT to another port.", port, e)
        if os.getenv("REMOTE_CONTROL", "false").lower() == "true":
            await self.start_remote()
        if self.discord.enabled:
            n = len(self.discord.allow)
            log.info("Discord signals: reading %s (bot must be in the server, with the "
                     "message content intent on)", f"{n} listed server(s)" if n else "every server it's in")
            asyncio.create_task(self.discord.run())
        else:
            log.info("Discord signals off: set DISCORD_BOT_TOKEN in .env to read your servers")
        self.tel.event("control", f"Bot started in {mode} mode")
        asyncio.create_task(self.ticker())
        if (self.cfg.review_mode or "").lower() != "off":
            asyncio.create_task(self.reviewer())
            log.info("Self-review: %s, after each session and every %gh (needs %d closed trades)",
                     self.cfg.review_mode, self.cfg.review_every_hours, self.cfg.review_min_trades)
        backoff = 1
        while True:
            try:
                async with websockets.connect(WS_URL, ping_interval=20) as ws:
                    self.ws, backoff = ws, 1
                    self.tel.connected = True
                    self.tel.event("control", "Connected to PumpPortal")
                    if self.cfg.launch_trading:
                        await self.send("subscribeNewToken")
                    if self.engine.wallets:
                        ws_list = sorted(self.engine.wallets)
                        for i in range(0, len(ws_list), 100):
                            await self.send("subscribeAccountTrade", ws_list[i:i + 100])
                    tracked = list(dict.fromkeys(list(self.engine.watching) + list(self.engine.positions)
                                                 + list(self.outcomes.active)))
                    for i in range(0, len(tracked), 100):
                        await self.send("subscribeTokenTrade", tracked[i:i + 100])
                    log.info("Connected to PumpPortal, watching new launches...")
                    async for raw in ws:
                        ev = json.loads(raw)
                        now = time.time()
                        if ev.get("txType") == "create":
                            self.tel.tokens_seen += 1
                        self.outcomes.on_event(ev, now)
                        await self.handle(self.engine.on_event(ev, now))
            except Exception as e:
                self.ws = None
                self.tel.connected = False
                self.tel.event("error", f"Connection lost ({e}), reconnecting")
                log.warning("connection lost (%s), reconnecting in %ss", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)


if __name__ == "__main__":
    import signal

    def _stop(*_):
        raise KeyboardInterrupt
    for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", None)):   # closing the window counts as Ctrl+C
        if sig:
            signal.signal(sig, _stop)
    bot = Bot()
    try:
        asyncio.run(bot.run())
    except KeyboardInterrupt:
        pass
    finally:
        bot.end_session()
        print("\nStopped. Trade history is in trades.csv")
