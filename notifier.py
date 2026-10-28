"""
Discord alerts for the trading bot.

  Server channel (every buy and sell):  DISCORD_WEBHOOK_URL
  Direct message (big winners):         DISCORD_BOT_TOKEN + DISCORD_USER_ID
                                        sent when a trade closes at or above DISCORD_DM_MIN_PNL_PCT (default 100)

Alerts are sent in the background, so a slow or failing Discord never delays a trade.
"""
import asyncio
import logging
import os

log = logging.getLogger("bot")
API = "https://discord.com/api/v10"
GREEN, RED, BLUE, GOLD = 0x2ECC71, 0xE74C3C, 0x3498DB, 0xF1C40F


class Notifier:
    def __init__(self, http, live: bool):
        self.http = http
        self.live = live
        self.webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
        self.token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
        self.user_id = os.getenv("DISCORD_USER_ID", "").strip()
        self.dm_min_pct = float(os.getenv("DISCORD_DM_MIN_PNL_PCT", "100"))
        self._dm_channel = None
        self._tasks: set = set()

    @property
    def enabled(self) -> str:
        parts = []
        if self.webhook:
            parts.append("server channel")
        if self.token and self.user_id:
            parts.append(f"DMs for trades >= +{self.dm_min_pct:.0f}%")
        return " + ".join(parts)

    # ---------- public: called by the bot ----------
    def buy(self, symbol, mint, sol, why, sig=None):
        fields = [("Amount", f"{sol:g} SOL"), ("Reason", why), ("Mode", self._mode())]
        links = f"[pump.fun](https://pump.fun/coin/{mint})"
        if sig:
            links += f" · [transaction](https://solscan.io/tx/{sig})"
        self._fire(self._post_webhook(self._embed(f"🟢 BUY ${symbol}", BLUE, fields, links, mint)))

    def sell(self, t: dict, sig=None):
        pct, pnl = t["pct"], t["pnl"]
        color = GREEN if pnl > 0 else RED
        fields = [("P&L", f"{pnl:+.4f} SOL ({pct:+.1f}%)"),
                  ("In / out", f"{t['sol_in']} → {t['sol_out']} SOL"),
                  ("Held", _dur(t["held_s"])), ("Exit", t["reason"]),
                  ("Source", t.get("source", "")), ("Mode", self._mode())]
        links = f"[pump.fun](https://pump.fun/coin/{t['mint']})"
        if sig:
            links += f" · [transaction](https://solscan.io/tx/{sig})"
        icon = "💰" if pnl > 0 else "🔻"
        self._fire(self._post_webhook(self._embed(f"{icon} SELL ${t['symbol']}  {pct:+.1f}%", color,
                                                  fields, links, t["mint"])))
        if pct >= self.dm_min_pct:
            self._fire(self._send_dm(self._embed(
                f"🚀 +{pct:.0f}% on ${t['symbol']}", GOLD,
                [("Profit", f"{pnl:+.4f} SOL"), ("Held", _dur(t["held_s"])), ("Exit", t["reason"]),
                 ("Mode", self._mode())], links, t["mint"])))

    def text(self, msg: str):
        """Plain status message to the server channel (start-up etc.)."""
        if self.webhook:
            self._fire(self._post_webhook({"content": msg}))

    async def close(self):
        if self._tasks:
            await asyncio.wait(self._tasks, timeout=10)

    # ---------- internals ----------
    def _mode(self):
        return "🔴 LIVE" if self.live else "📝 paper"

    def _embed(self, title, color, fields, links, mint):
        return {"embeds": [{
            "title": title, "color": color,
            "description": links,
            "fields": [{"name": k, "value": str(v)[:1000] or "-", "inline": True} for k, v in fields],
            "footer": {"text": mint},
        }]}

    def _fire(self, coro):
        task = asyncio.get_running_loop().create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _request(self, url, payload, headers=None):
        for _ in range(3):
            try:
                r = await self.http.post(url, json=payload, headers=headers or {})
            except Exception as e:
                log.warning("discord: %s", e)
                await asyncio.sleep(2)
                continue
            if r.status_code == 429:  # rate limited: wait as told
                try:
                    wait = float(r.json().get("retry_after", 1))
                except Exception:
                    wait = 1.0
                await asyncio.sleep(min(wait, 30))
                continue
            if r.status_code >= 400:
                log.warning("discord error %s: %s", r.status_code, r.text[:200])
                return None
            return r
        return None

    async def _post_webhook(self, payload):
        if self.webhook:
            await self._request(self.webhook, payload)

    async def _send_dm(self, payload):
        if not (self.token and self.user_id):
            return
        auth = {"Authorization": f"Bot {self.token}"}
        if not self._dm_channel:
            r = await self._request(f"{API}/users/@me/channels", {"recipient_id": self.user_id}, auth)
            if not r:
                log.warning("discord: couldn't open a DM. The bot must share a server with you, "
                            "and your DMs from server members must be on.")
                return
            self._dm_channel = r.json()["id"]
        await self._request(f"{API}/channels/{self._dm_channel}/messages", payload, auth)


def _dur(s) -> str:
    s = int(s or 0)
    return f"{s // 3600}h {s % 3600 // 60}m" if s >= 3600 else f"{s // 60}m {s % 60}s"
