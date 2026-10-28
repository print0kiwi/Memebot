"""
Live dashboard for the bot: http://localhost:8787 (opens automatically with `python bot.py`).

  python dashboard.py --demo     # try the dashboard with simulated trades, no wallet, no internet

Shows open positions with live P&L, closed trades, a P&L chart, why tokens were skipped,
and an activity feed. Controls: pause/resume new buys, and "Sell" on any open position.

Security: listens on 127.0.0.1 only (this computer). Control buttons need a per-run token
that only the served page knows, so other websites can't trigger them.

Remote control (REMOTE_CONTROL=true, see remote.py): a second instance of this same server on its own
port, where EVERYTHING except the login page needs a signed session cookie from the access key.
"""
import asyncio
import gzip
import hmac
import json
import os
import re
import secrets
import time
from collections import Counter, deque

import remote
import settings as ui_settings
import review
import walletkey

HERE = os.path.dirname(os.path.abspath(__file__))


def skip_bucket(reason: str) -> str:
    r = reason.lower()
    # order matters: the first key found wins, so more specific phrases come first
    for key, label in (("discord caller", "not called in Discord"),
                       ("only", "too few buyers"), ("net inflow", "low net inflow"), ("mcap", "market cap out of range"),
                       ("dev holds", "dev holds too much"), ("dev sold", "dev sold"), ("paused", "paused"),
                       ("risk", "risk limits / full"), ("their buy", "copy: buy too small"),
                       ("too high", "copy: already pumped"), ("top buyer", "one buyer dominates"),
                       ("bundled", "bundled launch"), ("no bonding", "copy: not on pump.fun curve"),
                       ("no price", "no price")):
        if key in r:
            return label
    return re.sub(r"[\d.+%-]+", "", reason).strip() or "other"


class Telemetry:
    """Everything the dashboard shows that the engine doesn't already keep."""

    def __init__(self, live: bool, trade_log: str):
        self.started = time.time()
        self.connected = False
        self.events = deque(maxlen=400)
        self.tokens_seen = 0
        self.wallet_trades = 0
        self.skips = Counter()
        self.decisions = deque(maxlen=60)          # what the bot just did with the coins it was watching
        self.balance = None
        self.history = self._load_history(trade_log, "live" if live else "paper")
        self._seq = 0
        self.discord_calls = 0
        self.review = None                          # last self-review record (review.py)

    @staticmethod
    def _load_history(path, mode):
        import csv
        from datetime import datetime
        out = []
        if not os.path.exists(path):
            return out
        try:
            for r in csv.DictReader(open(path, encoding="utf-8")):
                if r.get("mode") != mode:
                    continue
                ts = datetime.strptime(r["time"], "%Y-%m-%d %H:%M:%S").timestamp()
                out.append(dict(ts=ts, symbol=r.get("symbol", ""), mint=r.get("mint", ""),
                                source=r.get("source", ""), sol_in=float(r.get("sol_in") or 0),
                                sol_out=float(r.get("sol_out") or 0), pnl=float(r.get("pnl") or 0),
                                pct=float(r.get("pct") or 0), held_s=int(float(r.get("held_s") or 0)),
                                reason=r.get("reason", "")))
        except Exception:
            pass
        return out[-2000:]

    def event(self, kind: str, text: str, mint: str = ""):
        self._seq += 1
        self.events.append(dict(id=self._seq, ts=time.time(), kind=kind, text=text, mint=mint))

    def decide(self, kind: str, w, reason: str):
        """Remember a decision (kind 'buy' or 'skip') about a watched token, for the Watching panel."""
        self.decisions.append(dict(ts=time.time(), kind=kind, symbol=w.symbol, mint=w.mint, reason=reason,
                                   buyers=len(w.buyers), inflow=round(w.buy_sol - w.sell_sol, 2),
                                   mcap=round(w.mcap_sol)))

    def social_event(self, mint: str, info: dict):
        n = info.get("authors", 1)
        who = info.get("author", "?")
        where = info.get("guild") or "Discord"
        self.event("discord", (f"{who} called {mint[:8]}… in {where}" if info.get("first")
                               else f"{who} also called {mint[:8]}… — now {n} callers in {where}"), mint)

    def closed(self, t: dict):
        self.history.append({**t, "ts": time.time()})


# the only files the login page and the home-screen icon need; everything else waits for a login
PUBLIC_ASSETS = {"favicon.png", "icon-192.png", "icon-512.png", "neet-cat.webp"}
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
       "script-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; "
       "form-action 'self'; frame-ancestors 'none'")
MANIFEST = {"name": "Memebot", "short_name": "Memebot", "start_url": "/", "scope": "/",
            "display": "standalone", "orientation": "portrait", "background_color": "#1a1720",
            "theme_color": "#2a2531",
            "icons": [{"src": "/assets/icon-192.png", "sizes": "192x192", "type": "image/png"},
                      {"src": "/assets/icon-512.png", "sizes": "512x512", "type": "image/png"}]}
STATUS = {200: "OK", 302: "Found", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
          429: "Too Many Requests"}


class Dashboard:
    def __init__(self, bot, host="127.0.0.1", port=8787, auth=None):
        self.bot, self.host, self.port = bot, host, port
        self.auth = auth                       # remote.Auth -> this is the remote listener
        self.token = secrets.token_urlsafe(24)
        self.server = None

    async def start(self):
        if self.auth is None and not remote.is_loopback(self.host):
            raise ValueError("won't listen beyond this computer without an access key; "
                             "set REMOTE_CONTROL=true (see remote.py) instead of DASHBOARD_HOST")
        self.server = await asyncio.start_server(self._client, self.host, self.port)
        self.port = self.server.sockets[0].getsockname()[1]
        return f"http://{'localhost' if remote.is_loopback(self.host) else self.host}:{self.port}"

    # ---------- state ----------
    def state(self) -> dict:
        b, e, c, tel = self.bot, self.bot.engine, self.bot.cfg, self.bot.tel
        now = time.time()
        positions = []
        for p in e.positions.values():
            pct = (p.price / p.entry_price - 1) * 100 if p.entry_price else 0
            positions.append(dict(
                symbol=p.symbol, mint=p.mint, source=("launch" if p.source == "launch" else e.label(p.source)),
                sol_in=p.sol_in, pct=round(pct, 2), peak_pct=round((p.peak / p.entry_price - 1) * 100, 2),
                est_pnl=round(p.sol_in * pct / 100, 4), held_s=int(now - p.opened_at),
                max_hold_s=c.max_hold_seconds, tp=c.take_profit_pct, sl=-c.stop_loss_pct,
                selling=p.mint in e.busy))
        positions.sort(key=lambda x: -x["held_s"])
        day0 = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))
        today = [t for t in tel.history if t["ts"] >= day0]
        wins = sum(t["pnl"] > 0 for t in today)
        cum, curve = 0.0, []
        for t in tel.history[-500:]:
            cum += t["pnl"]
            curve.append([int(t["ts"]), round(cum, 4)])
        return dict(
            now=now, mode="LIVE" if b.live else "PAPER", started=tel.started, connected=tel.connected,
            paused=e.paused, balance=tel.balance,
            wallet=getattr(getattr(b, "wallet", None), "pubkey", None),
            limits=dict(buy_sol=c.buy_sol, copy_buy_sol=c.copy_buy_sol, max_positions=c.max_open_positions,
                        max_daily_loss=c.max_daily_loss_sol, tp=c.take_profit_pct, sl=c.stop_loss_pct,
                        trail=c.trailing_stop_pct, max_hold_s=c.max_hold_seconds),
            today=dict(pnl=round(e.realized_pnl_today, 4), trades=len(today), wins=wins,
                       win_rate=round(wins / len(today) * 100) if today else None,
                       open_pnl=round(sum(p["est_pnl"] for p in positions), 4)),
            alltime=dict(pnl=round(sum(t["pnl"] for t in tel.history), 4), trades=len(tel.history)),
            positions=positions,
            closed=list(reversed(tel.history[-60:])),
            curve=curve,
            events=list(tel.events)[-150:],
            stats=dict(tokens_seen=tel.tokens_seen, watching=len(e.watching), wallet_trades=tel.wallet_trades,
                       tracked=len(e.wallets), copying=len(e.copy_from) if c.copy_trading else 0,
                       launch=c.launch_trading, skips=tel.skips.most_common(8)),
            discord=self.discord_state(now),
            watch=self.watch_state(now),
            review=self.review_state(),
        )

    def review_state(self) -> dict:
        """Self-review status for the dashboard: when, what it saw, what it changed."""
        c, rec = self.bot.cfg, getattr(self.bot.tel, "review", None)
        out = dict(mode=getattr(c, "review_mode", "off"), every_h=getattr(c, "review_every_hours", 24),
                   min_trades=getattr(c, "review_min_trades", 20), last=None)
        if rec:
            s = rec.get("stats") or {}
            changes = []
            for ch in rec.get("changes", []):
                f = ui_settings.BY_KEY.get(ch["key"], {})
                sc = f.get("scale", 1)
                changes.append(dict(label=f.get("label", ch["key"]), old=round(ch["old"] * sc, 4),
                                    new=round(ch["new"] * sc, 4), unit=f.get("unit", ""), why=ch.get("why", "")))
            out["last"] = dict(ts=rec.get("ts"), window=rec.get("window"), n=s.get("n", 0),
                               win_rate=s.get("win_rate"), pnl=s.get("pnl"), avg_pct=s.get("avg_pct"),
                               changes=changes, notes=rec.get("notes", []), applied=bool(rec.get("applied")),
                               reverted=rec.get("reverted"), undo=bool(rec.get("applied") and rec.get("before")
                                                                        and rec.get("window") != "undo"))
        return out

    def watch_state(self, now: float, limit: int = 40) -> dict:
        """The coins the bot is evaluating right now, and why it hasn't bought each one yet."""
        b, e, c, tel = self.bot, self.bot.engine, self.bot.cfg, self.bot.tel
        rows = []
        for w in list(e.watching.values()):
            age = now - w.created_at
            checks = e.checklist(w, now)
            ok, why = e.passes_filters(w, now)
            if age < c.watch_seconds:
                status, state = f"too young ({age:.0f}s of {c.watch_seconds}s)", "young"
            elif not ok:
                status, state = why, "no"
            elif not e.can_open():
                status, state = ("paused" if e.paused else "risk limits"), "blocked"
            else:
                status, state = "passes, buying", "go"
            rows.append(dict(
                symbol=w.symbol, mint=w.mint, age_s=round(age, 1), buyers=len(w.buyers),
                inflow=round(w.buy_sol - w.sell_sol, 2), mcap=round(w.mcap_sol),
                dev=("sold" if w.dev_sold else f"{w.dev_share:.0%}"),
                passed=sum(ok for _, ok, _ in checks), total=len(checks),
                checks=[dict(k=k, ok=ok, t=t) for k, ok, t in checks], status=status, state=state))
        # closest to buying first, then the freshest
        rank = {"go": 0, "blocked": 1, "no": 2, "young": 3}
        rows.sort(key=lambda r: (rank[r["state"]], -r["passed"], r["age_s"]))
        recent = list(reversed(tel.decisions))[:25]
        return dict(total=len(rows), rows=rows[:limit], recent=recent, min_age=c.watch_seconds,
                    max_age=max(c.max_age_seconds, c.watch_seconds), launch=c.launch_trading)

    def discord_state(self, now: float) -> dict:
        """What your servers are calling right now."""
        b = self.bot
        lis, social = getattr(b, "discord", None), getattr(b, "social", None)
        st = lis.status() if lis else dict(enabled=False, connected=False, reading=[], error="", calls=0)
        window = b.cfg.social_window_min * 60
        names = lis.guilds if lis else {}
        hot = []
        if social:
            for r in social.top(now, window, n=6):
                mint = r["mint"]
                p = b.engine.positions.get(mint)
                hot.append(dict(mint=mint, authors=r["authors"], mentions=r["mentions"],
                                age_s=r["first_s"], last_s=r["last_s"],
                                guilds=[names.get(g, g) for g in r["guilds"][:2]],
                                held=bool(p), watching=mint in b.engine.watching,
                                following=b.outcomes.following(mint) if getattr(b, "outcomes", None) else False))
        return dict(enabled=st["enabled"], connected=st["connected"], reading=st.get("reading", []),
                    error=st.get("error", ""), calls=st.get("calls", 0),
                    need=b.cfg.min_social_authors, window_min=b.cfg.social_window_min,
                    following=len(b.outcomes.active) if getattr(b, "outcomes", None) else 0, hot=hot)

    # ---------- http ----------
    @staticmethod
    def _hostname(h: str) -> str:
        h = h.strip().lower()
        return h[:h.find("]") + 1] if h.startswith("[") and "]" in h else h.split(":")[0]

    def _tok_ok(self, hdr) -> bool:
        return hmac.compare_digest(hdr.get("x-dash-token", "").encode("latin-1"), self.token.encode())

    def _notify(self, msg: str):
        n = getattr(self.bot, "notify", None)
        if n:
            n.text(msg)

    @staticmethod
    def _page(name: str) -> bytes:
        with open(os.path.join(HERE, name), encoding="utf-8") as f:
            return f.read().encode()

    async def _client(self, reader, writer):
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
            lines = head.decode("latin-1").split("\r\n")
            method, path, _ = (lines[0].split(" ") + ["", ""])[:3]
            hdr = {k.strip().lower(): v.strip() for k, v in (l.split(":", 1) for l in lines[1:] if ":" in l)}
            body = b""
            n = int(hdr.get("content-length") or 0)
            if 0 < n < 10000:
                body = await asyncio.wait_for(reader.readexactly(n), 10)
            path = path.split("?")[0]
            gz = "gzip" in hdr.get("accept-encoding", "")
            if self.auth is None:
                if self._hostname(hdr.get("host", "")) not in ("localhost", "127.0.0.1", "[::1]"):
                    return await self._send(writer, 403, b"forbidden", "text/plain")  # DNS-rebinding guard
            else:
                ip = (writer.get_extra_info("peername") or ("?",))[0]
                if await self._gate(writer, method, path, hdr, body, ip, gz):
                    return
            await self._route(writer, method, path, hdr, body, gz)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                ConnectionError, ValueError):
            pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    async def _gate(self, writer, method, path, hdr, body, ip, gz) -> bool:
        """Remote listener only: nothing but the login page, the manifest and a few icons is
        reachable without a valid session. Returns True when this already sent the reply."""
        a = self.auth
        if path == "/login" and method == "GET":
            await self._send(writer, 200, self._page("login.html"), "text/html", gz=gz)
            return True
        if path == "/login" and method == "POST":
            await self._login(writer, body, hdr, ip)
            return True
        if method == "GET" and (path == "/manifest.webmanifest"
                                or (path.startswith("/assets/") and path[len("/assets/"):] in PUBLIC_ASSETS)):
            return False
        sid = a.verify(hdr.get("cookie", ""))
        if not sid:
            if method == "GET" and path == "/":
                await self._send(writer, 302, b"", "text/plain", extra=["Location: /login"])
            else:
                await self._send(writer, 401, b'{"error":"login required"}', "application/json")
            return True
        if method == "POST" and path == "/logout":
            if not self._tok_ok(hdr):
                await self._send(writer, 403, b'{"error":"bad token"}', "application/json")
                return True
            a.revoke(sid)
            self.bot.tel.event("control", f"Remote sign-out from {ip}")
            await self._send(writer, 200, b'{"ok":true}', "application/json",
                             extra=[f"Set-Cookie: {remote.COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"])
            return True
        return False

    async def _login(self, writer, body, hdr, ip):
        try:
            key = json.loads(body or b"{}").get("key", "")
        except (ValueError, AttributeError):
            key = ""
        if not isinstance(key, str) or len(key) > 200:
            key = ""
        cookie, wait, fails = self.auth.login(key, ip)
        tel = self.bot.tel
        if cookie:
            tel.event("control", f"Remote login from {ip}")
            self._notify(f"📱 Remote control login from {ip}")
            secure = "; Secure" if hdr.get("x-forwarded-proto") == "https" else ""
            ck = (f"{remote.COOKIE}={cookie}; Path=/; HttpOnly; SameSite=Strict; "
                  f"Max-Age={remote.SESSION_TTL}{secure}")
            return await self._send(writer, 200, b'{"ok":true}', "application/json", extra=[f"Set-Cookie: {ck}"])
        if wait:
            if fails:       # this attempt is the one that triggered the lockout
                tel.event("error", f"Remote login: {fails} wrong keys from {ip}, locked out for {wait // 60} min")
                self._notify(f"🔒 Remote control: {fails} wrong keys from {ip}. Locked out for {wait // 60} min.")
            msg = f"Too many wrong keys. Try again in {wait // 60 + 1} min."
            return await self._send(writer, 429, json.dumps({"ok": False, "error": msg}).encode(),
                                    "application/json", extra=[f"Retry-After: {wait}"])
        if fails == 1:
            tel.event("error", f"Remote login: wrong key from {ip}")
        await asyncio.sleep(0.5)                      # slows down guessing
        return await self._send(writer, 401, b'{"ok":false,"error":"That key isn\'t right."}', "application/json")

    async def _route(self, writer, method, path, hdr, body, gz):
        remote_on = self.auth is not None
        if method == "GET" and path == "/":
            page = self._page("dashboard.html").decode()
            page = page.replace("__TOKEN__", self.token).replace("__REMOTE__", "true" if remote_on else "false")
            return await self._send(writer, 200, page.encode(), "text/html", gz=gz)
        if method == "GET" and path == "/manifest.webmanifest":
            return await self._send(writer, 200, json.dumps(MANIFEST).encode(), "application/manifest+json")
        if method == "GET" and path.startswith("/assets/"):
            return await self._asset(writer, path[len("/assets/"):])
        if method == "GET" and path == "/api/settings":
            return await self._send(writer, 200, json.dumps(ui_settings.schema_with_values(
                self.bot.cfg, self.bot.settings_path)).encode(), "application/json", gz=gz)
        if method == "GET" and path == "/api/wallet":
            if self.auth is not None:
                return await self._send(writer, 403, b'{"error":"only on the computer running the bot"}', "application/json")
            info = walletkey.status()
            info["live"] = bool(self.bot.live)
            return await self._send(writer, 200, json.dumps(info).encode(), "application/json")
        if method == "GET" and path == "/api/state":
            return await self._send(writer, 200, json.dumps(self.state()).encode(), "application/json", gz=gz)
        if method == "POST" and path.startswith("/api/"):
            if not self._tok_ok(hdr):
                return await self._send(writer, 403, b'{"error":"bad token"}', "application/json")
            return await self._send(writer, 200, json.dumps(await self._control(path, body)).encode(),
                                    "application/json")
        await self._send(writer, 404, b"not found", "text/plain")

    async def _asset(self, writer, name: str):
        """Theme images from assets/. Only plain filenames, only known image types."""
        types = {".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg",
                 ".jpeg": "image/jpeg", ".gif": "image/gif", ".svg": "image/svg+xml"}
        ext = os.path.splitext(name)[1].lower()
        if "/" in name or "\\" in name or name.startswith(".") or ext not in types:
            return await self._send(writer, 404, b"not found", "text/plain")
        p = os.path.join(HERE, "assets", name)
        if not os.path.isfile(p):
            return await self._send(writer, 404, b"not found", "text/plain")
        with open(p, "rb") as f:
            return await self._send(writer, 200, f.read(), types[ext], cache="public, max-age=86400")

    async def _control(self, path, body) -> dict:
        e, tel = self.bot.engine, self.bot.tel
        if path == "/api/pause":
            e.paused = True
            tel.event("control", "New buys paused from dashboard")
            return {"ok": True, "paused": True}
        if path == "/api/resume":
            e.paused = False
            tel.event("control", "Buying resumed from dashboard")
            return {"ok": True, "paused": False}
        if path == "/api/panic":
            e.paused = True
            n = 0
            for mint in list(e.positions):
                acts = e.manual_sell(mint)
                if acts:
                    n += 1
                    asyncio.get_running_loop().create_task(self.bot.handle(acts))
            tel.event("control", f"SELL ALL: buys paused, selling {n} open position{'s' if n != 1 else ''}")
            self._notify(f"🛑 Sell all pressed: buying paused, selling {n} open position{'s' if n != 1 else ''}")
            return {"ok": True, "selling": n, "paused": True}
        if path == "/api/sell":
            mint = (json.loads(body or b"{}").get("mint") or "")
            acts = e.manual_sell(mint)
            if not acts:
                return {"ok": False, "error": "not an open position (or already selling)"}
            tel.event("control", f"Manual sell requested: {e.positions[mint].symbol}", mint)
            asyncio.get_running_loop().create_task(self.bot.handle(acts))
            return {"ok": True}
        if path == "/api/wallet":
            # Wallet keys are only ever accepted on the local dashboard: never over the phone/remote listener,
            # which may be plain HTTP on Wi-Fi. The key goes to .env and is never echoed back or logged.
            if self.auth is not None:
                return {"ok": False, "error": "For safety a wallet key can only be imported on the computer "
                                              "running the bot, not from the phone."}
            try:
                key = json.loads(body or b"{}").get("key", "")
                res = walletkey.import_key(key)
            except (ValueError, AttributeError) as e:
                return {"ok": False, "error": str(e) if isinstance(e, ValueError) and str(e) else "bad request"}
            except OSError:
                return {"ok": False, "error": "Couldn't write the .env file (is it read-only?)."}
            tel.event("control", f"Wallet key imported: {res['address'][:4]}…{res['address'][-4:]} "
                                 "(restart the bot to use it)")
            return {"ok": True, "address": res["address"], "replaced": res["replaced"],
                    "live": bool(self.bot.live)}
        if path in ("/api/review", "/api/review/undo"):
            if not hasattr(self.bot, "do_review"):
                return {"ok": False, "error": "self-review isn't available here"}
            if path == "/api/review/undo":
                notes = review.undo_last(self.bot.cfg, self.bot.review_root, self.bot.settings_path)
                if not notes:
                    return {"ok": False, "error": "Nothing to undo."}
                tel.event("control", "Self-review undone: " + "; ".join(notes))
                self._notify("↩️ Self-review undone: " + "; ".join(notes))
                tel.review = review.last_review(self.bot.review_root)
                return {"ok": True, "changed": notes, "review": self.review_state()}
            try:
                want = json.loads(body or b"{}").get("apply")
            except (ValueError, AttributeError):
                want = None
            mode = (self.bot.cfg.review_mode or "").lower()
            force = True if want is True else (None if mode in ("apply", "suggest") else False)
            rec = self.bot.do_review("now", start=time.time() - self.bot.cfg.review_every_hours * 3600,
                                     force_apply=force)
            if rec is None:
                return {"ok": False, "error": "The review failed; see the bot's log."}
            return {"ok": True, "review": self.review_state()}
        if path == "/api/settings":
            try:
                updates = json.loads(body or b"{}")
                if not isinstance(updates, dict):
                    raise ValueError
            except ValueError:
                return {"ok": False, "errors": ["bad request"]}
            cfg = self.bot.cfg
            clean, errors = ui_settings.validate(cfg, updates)
            if errors:
                return {"ok": False, "errors": errors}
            notes = ui_settings.apply(cfg, clean)
            # hard limits, enforced again (also re-checked before signing in bot.Wallet.trade)
            cfg.buy_sol, cfg.copy_buy_sol = min(cfg.buy_sol, 0.5), min(cfg.copy_buy_sol, 0.5)
            cfg.max_open_positions = min(cfg.max_open_positions, 5)
            if notes:
                ui_settings.save(self.bot.settings_path, cfg)
                tel.event("control", "Settings changed: " + "; ".join(notes))
                notify = getattr(self.bot, "notify", None)
                if notify:
                    notify.text("⚙️ Settings changed: " + "; ".join(notes))
            return {"ok": True, "changed": notes,
                    "settings": ui_settings.schema_with_values(cfg, self.bot.settings_path)}
        return {"ok": False, "error": "unknown action"}

    @staticmethod
    async def _send(writer, code, data: bytes, ctype, cache="no-store", gz=False, extra=()):
        charset = "" if ctype.startswith("image/") and "svg" not in ctype else "; charset=utf-8"
        head = [f"HTTP/1.1 {code} {STATUS.get(code, 'OK')}", f"Content-Type: {ctype}{charset}",
                f"Cache-Control: {cache}", "X-Content-Type-Options: nosniff", "X-Frame-Options: DENY",
                "Referrer-Policy: no-referrer", "Vary: Accept-Encoding", "Connection: close", *extra]
        if gz and len(data) > 600 and (ctype.startswith("text/") or "json" in ctype):
            data = gzip.compress(data, 4)
            head.append("Content-Encoding: gzip")
        if ctype.startswith("text/html"):
            head.append(f"Content-Security-Policy: {CSP}")
        head.append(f"Content-Length: {len(data)}")
        writer.write(("\r\n".join(head) + "\r\n\r\n").encode("latin-1") + data)
        await writer.drain()


# ---------------------------------------------------------------- demo mode
async def demo(port: int, with_remote: bool = False):
    """Real Bot + Engine, fed with simulated PumpPortal events. No network, no wallet."""
    import random
    os.environ["LIVE_TRADING"] = "false"
    os.environ.setdefault("TRACKED_WALLETS", "wallets/axiom_wallets.json")
    os.environ.setdefault("COPY_WALLETS", "")
    for k in ("DISCORD_WEBHOOK_URL", "DISCORD_BOT_TOKEN"):
        os.environ[k] = ""
    import bot as botmod
    botmod.TRADE_LOG = os.path.join(HERE, "demo_trades.csv")
    botmod.WALLET_LOG = os.path.join(HERE, "demo_wallet_trades.csv")
    botmod.SETTINGS_FILE = os.path.join(HERE, "demo_settings.json")
    botmod.CONTEXT_LOG = os.path.join(HERE, "demo_trade_context.csv")
    b = botmod.Bot()
    b.settings_path = botmod.SETTINGS_FILE
    import tempfile
    b.review_root = tempfile.mkdtemp(prefix="memebot-demo-review-")    # demo reviews never touch the real log
    b.cfg.review_min_trades = 5
    if not os.path.exists(b.settings_path):
        b.cfg.watch_seconds, b.cfg.max_age_seconds, b.cfg.max_hold_seconds = 6, 15, 90
    b.tel = Telemetry(False, botmod.TRADE_LOG)

    class FakeWS:
        async def send(self, msg):
            pass

    b.ws = FakeWS()
    b.tel.connected = True
    # pretend Discord is connected so the Discord panel has something to show
    b.discord.token, b.discord.connected = "demo", True
    b.discord.guilds = {"1": "Memecoins", "2": "Alpha Chat", "3": "Snipers"}
    CALLERS = ["orangie", "binzy", "qhoas", "kimchi", "mello", "LJC", "whale_alert", "dani"]
    dash = Dashboard(b, port=port)
    url = await dash.start()
    print(f"DEMO dashboard (simulated trades): {url}")
    if with_remote:                      # try the phone login with a throwaway key (not your real one)
        key = remote.generate_key()
        bind = os.getenv("REMOTE_BIND", "").strip() or remote.default_bind()
        rport = int(os.getenv("REMOTE_PORT", str(remote.DEFAULT_PORT)))
        await Dashboard(b, bind, rport, auth=remote.Auth(key)).start()
        print(f"DEMO remote login: {', '.join(remote.urls(bind, rport)) or f'port {rport}'}   key: {key}  (throwaway)")
    asyncio.get_running_loop().create_task(b.ticker())
    wallets = list(b.engine.wallets)[:40]
    tokens = {}
    rng = random.Random()
    V = 1_000_000_000.0
    n = 0
    while True:
        await asyncio.sleep(0.4)
        now = time.time()
        if rng.random() < 0.35:  # new launch
            n += 1
            mint = f"Demo{n:04d}{secrets.token_hex(6)}pump"
            sym = rng.choice(["PEPE", "CAT", "FROG", "DOGE", "AI", "MOON", "BONK", "WIF", "GOAT", "NUL"]) + str(n)
            tokens[mint] = dict(v=30.0, sym=sym, hype=rng.random(), born=now)
            b.tel.tokens_seen += 1
            await b.handle(b.engine.on_event(dict(txType="create", mint=mint, symbol=sym, traderPublicKey="dev" + mint,
                                                  initialBuy=rng.uniform(0.01, 0.15) * V, vSolInBondingCurve=30,
                                                  vTokensInBondingCurve=V, marketCapSol=30 + 30 * rng.random()), now))
        if tokens and rng.random() < 0.25:        # someone calls a coin in Discord
            mint = rng.choice(list(tokens))
            gid = rng.choice(list(b.discord.guilds))
            who = rng.choice(CALLERS)
            first, new_author = b.social.record(mint, who, gid, now)
            b.discord.calls += 1
            await b.on_discord_call(mint, dict(
                ts=now, mint=mint, guild_id=gid, guild=b.discord.guilds[gid], channel="calls",
                author_id=who, author=who, first=first, new_author=new_author,
                authors=b.social.authors(mint, now)))
        for mint, t in list(tokens.items()):
            if now - t["born"] > 180:
                tokens.pop(mint)
                continue
            if rng.random() < 0.6:
                age = now - t["born"]
                drift = (0.06 * t["hype"] if age < 10 else (0.02 if t["hype"] > 0.75 else -0.03))
                t["v"] = max(t["v"] * (1 + rng.gauss(drift, 0.08)), 5)
                side = "buy" if rng.random() < 0.5 + drift * 4 else "sell"
                trader = rng.choice(wallets) if (wallets and rng.random() < 0.05) else f"u{rng.randint(0, 999)}"
                amt = rng.uniform(0.1, 1.5)
                ev = dict(txType=side, mint=mint, traderPublicKey=trader, solAmount=amt, tokenAmount=amt * V / t["v"],
                          vSolInBondingCurve=t["v"], vTokensInBondingCurve=V, marketCapSol=t["v"] * 2,
                          signature=secrets.token_hex(8), symbol=t["sym"])
                await b.handle(b.engine.on_event(ev, now))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="simulated trades, no wallet or internet needed")
    ap.add_argument("--remote", action="store_true", help="with --demo: also open the key-protected phone port")
    ap.add_argument("--port", type=int, default=int(os.getenv("DASHBOARD_PORT", "8787")))
    a = ap.parse_args()
    if not a.demo:
        raise SystemExit("The dashboard starts automatically with `python bot.py`. Use --demo to try it alone.")
    try:
        asyncio.run(demo(a.port, a.remote))
    except KeyboardInterrupt:
        pass
