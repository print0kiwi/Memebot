"""
Reads your Discord servers and tells the bot which coins people are calling.

It connects to the Discord Gateway with YOUR bot token (the same DISCORD_BOT_TOKEN used for
DM alerts), listens to messages in the servers/channels you allow, pulls pump.fun token
addresses out of them, and:

  * logs every call to discord_calls.csv
  * counts how many DIFFERENT people called each token (SocialTracker)
  * follows each token's price for an hour and writes the result to discord_outcomes.csv
    so `python analyze_discord.py` can show you which servers and callers are actually worth following

The bot can also require Discord buzz before it buys (MIN_SOCIAL_AUTHORS in the dashboard).
Nothing posted in Discord ever triggers a trade on its own; it only adds to the existing filters.

Standalone use (logs calls, no trading):
    python discord_signals.py --watch
    python discord_signals.py --servers        # list the servers your bot can actually read

Requirements: your bot must be IN the server and have "Read Messages" there, and the
MESSAGE CONTENT INTENT must be ON at https://discord.com/developers/applications
(your app -> Bot -> Privileged Gateway Intents). You cannot read a server your bot isn't in.
"""
import asyncio
import json
import logging
import os
import time

from social import SocialTracker, extract_mints

log = logging.getLogger("bot")

GATEWAY = "wss://gateway.discord.gg/?v=10&encoding=json"
API = "https://discord.com/api/v10"
# GUILDS | GUILD_MESSAGES | MESSAGE_CONTENT  (message content is a privileged intent)
INTENTS = (1 << 0) | (1 << 9) | (1 << 15)
CALL_LOG = "discord_calls.csv"
OUTCOME_LOG = "discord_outcomes.csv"
SERVERS_FILE = "discord_servers.txt"


def load_filter(path: str, base_dir: str = "") -> set[str]:
    """
    discord_servers.txt: one server or channel ID per line (# comments ignored).
    Empty file/no file = read every server the bot is in.
    """
    p = os.path.join(base_dir, path) if base_dir and not os.path.isabs(path) else path
    if not os.path.exists(p):
        return set()
    out = set()
    for line in open(p, encoding="utf-8"):
        line = line.split("#")[0].strip()
        if line.isdigit():
            out.add(line)
    return out


class DiscordListener:
    """
    Gateway client. Calls on_call(mint, info) for every pump.fun address posted in an allowed channel.
    Reconnects on its own; never raises into the bot.
    """

    def __init__(self, token: str, tracker: SocialTracker, on_call=None,
                 allow: set[str] | None = None, log_path: str = CALL_LOG, read_own: bool = False):
        self.token = (token or "").strip()
        self.tracker = tracker
        self.on_call = on_call
        self.allow = allow or set()
        self.log_path = log_path
        self.read_own = read_own
        self.guilds: dict[str, str] = {}       # id -> name
        self.channels: dict[str, str] = {}     # id -> name
        self.me = None
        self.connected = False
        self.messages = 0
        self.calls = 0
        self.last_error = ""
        self._seq = None
        self._session = None
        self._resume_url = None

    @property
    def enabled(self) -> bool:
        return bool(self.token)

    def allowed(self, guild_id: str, channel_id: str) -> bool:
        return not self.allow or guild_id in self.allow or channel_id in self.allow

    # ---------- message handling ----------
    def handle_message(self, d: dict, now: float) -> list[tuple[str, dict]]:
        """MESSAGE_CREATE payload -> list of (mint, info). Pure, so it can be tested offline."""
        author = d.get("author") or {}
        if author.get("bot") and not self.read_own:
            pass                                   # many call channels ARE bots (sniper feeds): keep them
        if self.me and author.get("id") == self.me and not self.read_own:
            return []                              # never react to our own alert messages
        gid, cid = str(d.get("guild_id") or ""), str(d.get("channel_id") or "")
        if not gid or not self.allowed(gid, cid):
            return []
        text = d.get("content") or ""
        for emb in (d.get("embeds") or [])[:4]:    # call bots put the address in an embed
            text += " " + " ".join(str(emb.get(k) or "") for k in ("title", "description", "url"))
            for f in (emb.get("fields") or [])[:12]:
                text += f" {f.get('name') or ''} {f.get('value') or ''}"
        mints = extract_mints(text)
        if not mints:
            self.messages += 1
            return []
        self.messages += 1
        uid = str(author.get("id") or "?")
        uname = author.get("global_name") or author.get("username") or uid
        out = []
        for mint in mints:
            first, new_author = self.tracker.record(mint, uid, gid, now)
            self.calls += 1
            info = dict(ts=now, mint=mint, guild_id=gid, guild=self.guilds.get(gid, gid),
                        channel_id=cid, channel=self.channels.get(cid, cid),
                        author_id=uid, author=uname, first=first, new_author=new_author,
                        authors=self.tracker.authors(mint, now),
                        url=f"https://discord.com/channels/{gid}/{cid}/{d.get('id', '')}")
            self._log(info)
            out.append((mint, info))
        return out

    def _log(self, info: dict):
        import csv
        row = {"time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(info["ts"])),
               "mint": info["mint"], "guild_id": info["guild_id"], "guild": info["guild"],
               "channel": info["channel"], "author_id": info["author_id"], "author": info["author"],
               "first": int(info["first"]), "authors_now": info["authors"], "link": info["url"]}
        try:
            new = not os.path.exists(self.log_path)
            with open(self.log_path, "a", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(row))
                if new:
                    w.writeheader()
                w.writerow(row)
        except OSError as e:
            log.debug("discord log failed: %s", e)

    # ---------- gateway ----------
    async def run(self):
        """Connect, listen, reconnect forever. Safe to run as a background task."""
        import websockets
        if not self.enabled:
            return
        backoff = 2
        while True:
            try:
                url = self._resume_url or GATEWAY
                async with websockets.connect(url, max_size=2 ** 22, ping_interval=None) as ws:
                    hello = json.loads(await ws.recv())
                    interval = hello["d"]["heartbeat_interval"] / 1000
                    hb = asyncio.create_task(self._heartbeat(ws, interval))
                    try:
                        if self._session and self._resume_url:
                            await ws.send(json.dumps({"op": 6, "d": {
                                "token": self.token, "session_id": self._session, "seq": self._seq}}))
                        else:
                            await ws.send(json.dumps({"op": 2, "d": {
                                "token": self.token, "intents": INTENTS,
                                "properties": {"os": "linux", "browser": "memebot", "device": "memebot"}}}))
                        backoff = 2
                        async for raw in ws:
                            await self._dispatch(json.loads(raw))
                    finally:
                        hb.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.connected = False
                msg = str(e)
                if "4014" in msg:
                    self.last_error = ("Discord rejected the connection: turn ON the MESSAGE CONTENT INTENT "
                                       "at discord.com/developers/applications -> your app -> Bot")
                    log.warning("Discord: %s", self.last_error)
                    return
                if "4004" in msg:
                    self.last_error = "Discord rejected DISCORD_BOT_TOKEN (invalid token)"
                    log.warning("Discord: %s", self.last_error)
                    return
                if "4013" in msg or "4012" in msg:
                    self.last_error = f"Discord rejected the gateway settings ({msg})"
                    log.warning("Discord: %s", self.last_error)
                    return
                self.last_error = msg[:140]
                log.warning("Discord gateway lost (%s), reconnecting in %ss", msg[:80], backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    async def _heartbeat(self, ws, interval):
        while True:
            await asyncio.sleep(interval)
            await ws.send(json.dumps({"op": 1, "d": self._seq}))

    async def _dispatch(self, msg: dict):
        op, t, d = msg.get("op"), msg.get("t"), msg.get("d") or {}
        if msg.get("s"):
            self._seq = msg["s"]
        if op == 7 or op == 9:                     # reconnect / invalid session
            if op == 9 and not d:
                self._session = self._resume_url = None
            raise ConnectionError("gateway asked us to reconnect")
        if op != 0:
            return
        if t == "READY":
            self.connected = True
            self.last_error = ""
            self.me = (d.get("user") or {}).get("id")
            self._session = d.get("session_id")
            ru = d.get("resume_gateway_url")
            self._resume_url = f"{ru}/?v=10&encoding=json" if ru else None
            log.info("Discord: connected as %s, waiting for servers…",
                     (d.get("user") or {}).get("username", "?"))
        elif t == "RESUMED":
            self.connected = True
        elif t == "GUILD_CREATE":
            gid = str(d.get("id"))
            self.guilds[gid] = d.get("name") or gid
            for ch in d.get("channels") or []:
                if ch.get("type") in (0, 5, 11, 15):      # text, announcement, thread, forum
                    self.channels[str(ch["id"])] = ch.get("name") or ""
            if self.allowed(gid, ""):
                log.info("Discord: reading #%s", self.guilds[gid])
        elif t == "CHANNEL_CREATE" and d.get("id"):
            self.channels[str(d["id"])] = d.get("name") or ""
        elif t in ("MESSAGE_CREATE", "MESSAGE_UPDATE"):
            now = time.time()
            for mint, info in self.handle_message(d, now):
                if self.on_call:
                    try:
                        r = self.on_call(mint, info)
                        if asyncio.iscoroutine(r):
                            await r
                    except Exception as e:
                        log.warning("discord handler failed: %s", e)

    def status(self) -> dict:
        return dict(enabled=self.enabled, connected=self.connected, guilds=len(self.guilds),
                    reading=sorted(n for g, n in self.guilds.items() if self.allowed(g, "")),
                    messages=self.messages, calls=self.calls, error=self.last_error)


# ------------------------------------------------------------------ standalone
async def _watch(minutes: float):
    from dotenv import load_dotenv
    load_dotenv()
    token = os.getenv("DISCORD_BOT_TOKEN", "")
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set in .env")
    here = os.path.dirname(os.path.abspath(__file__))
    tracker = SocialTracker()
    lis = DiscordListener(token, tracker, allow=load_filter(SERVERS_FILE, here),
                          log_path=os.path.join(here, CALL_LOG))

    def on_call(mint, info):
        n = info["authors"]
        tag = "NEW" if info["first"] else f"{n} callers"
        print(f"{time.strftime('%H:%M:%S')}  [{info['guild']}/#{info['channel']}] {info['author']}: "
              f"{mint}  ({tag})")

    lis.on_call = on_call
    task = asyncio.create_task(lis.run())
    end = time.time() + minutes * 60 if minutes else None
    try:
        while not end or time.time() < end:
            await asyncio.sleep(5)
            if lis.last_error and not lis.connected:
                break
    finally:
        task.cancel()
    s = lis.status()
    print(f"\nServers readable: {', '.join(s['reading']) or '(none)'}")
    print(f"{s['messages']} messages seen, {s['calls']} token calls logged to {CALL_LOG}")
    if s["error"]:
        print("Error:", s["error"])


async def _list_servers():
    from dotenv import load_dotenv
    load_dotenv()
    token = os.getenv("DISCORD_BOT_TOKEN", "")
    if not token:
        raise SystemExit("DISCORD_BOT_TOKEN is not set in .env")
    tracker = SocialTracker()
    lis = DiscordListener(token, tracker)
    task = asyncio.create_task(lis.run())
    for _ in range(30):
        await asyncio.sleep(0.5)
        if lis.guilds:
            break
        if lis.last_error and not lis.connected:
            break
    task.cancel()
    if lis.last_error and not lis.guilds:
        raise SystemExit(lis.last_error)
    if not lis.guilds:
        print("Your bot is not in any server yet.\n"
              "Invite it: discord.com/developers/applications -> your app -> OAuth2 -> URL Generator\n"
              "  scopes: bot   permissions: View Channels, Read Message History\n"
              "You must have 'Manage Server' in a server to add a bot to it.")
        return
    print(f"Your bot can read these {len(lis.guilds)} server(s). Put the IDs you want in {SERVERS_FILE}:\n")
    for gid, name in sorted(lis.guilds.items(), key=lambda x: x[1].lower()):
        print(f"  {gid}   {name}")


if __name__ == "__main__":
    import argparse
    logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--watch", action="store_true", help="print and log token calls as they happen")
    ap.add_argument("--minutes", type=float, default=0, help="stop after N minutes (0 = run until Ctrl+C)")
    ap.add_argument("--servers", action="store_true", help="list the servers your bot can read, with IDs")
    a = ap.parse_args()
    try:
        asyncio.run(_list_servers() if a.servers else _watch(a.minutes))
    except KeyboardInterrupt:
        pass
