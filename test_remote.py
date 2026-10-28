"""Offline tests for remote control: keys, sessions, lockout, and the real HTTP listener.
Run: python test_remote.py   (no network, no wallet, no bot.py import)"""
import asyncio
import gzip
import http.client
import json
import os
import re
import stat
import tempfile

import remote
import walletkey
from dashboard import Dashboard, Telemetry
from remote import Auth
from strategy import Config, Engine

V_TOK = 1_000_000_000.0
KEY = "K7QD-3MWX-9PTC-BZ4N-HR2V-8FGA"
# a public test vector (RFC 8032 test 1), NOT a real wallet: 32-byte seed + its 32-byte public key
WALLET_SECRET = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60"
                              "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
WALLET_B58 = walletkey.b58encode(WALLET_SECRET)
WALLET_ADDR = walletkey.b58encode(WALLET_SECRET[32:])


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


# ------------------------------------------------------------------ keys and sessions
def test_keys():
    assert remote.normalize_key(" k7qd-3mwx 9ptc ") == "K7QD3MWX9PTC"
    assert remote.normalize_key("O0Il1") == "00111"                    # look-alikes map together
    for _ in range(20):
        k = remote.generate_key()
        assert re.fullmatch(r"([0-9A-HJKMNP-TV-Z]{4}-){5}[0-9A-HJKMNP-TV-Z]{4}", k), k
    assert len({remote.generate_key() for _ in range(50)}) == 50
    for weak in ("", "short", "aaaaaaaaaaaaaaaaaaaaaaaa", "1234-1234-1234"):
        try:
            remote.check_strength(weak)
            raise AssertionError(f"accepted weak key {weak!r}")
        except ValueError:
            pass
    remote.check_strength(KEY)
    print("  keys ok")


def test_key_file():
    with tempfile.TemporaryDirectory() as d:
        os.environ.pop("REMOTE_KEY", None)
        k1 = remote.load_key(d)
        path = os.path.join(d, remote.KEY_FILE)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert remote.load_key(d) == k1                                   # stable across restarts
        k2 = remote.rotate_key(d)
        assert k2 != k1 and remote.load_key(d) == k2
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        os.environ["REMOTE_KEY"] = "weak"
        try:
            remote.load_key(d)
            raise AssertionError("weak REMOTE_KEY accepted")
        except ValueError:
            pass
        os.environ["REMOTE_KEY"] = KEY
        assert remote.load_key(d) == KEY
        os.environ.pop("REMOTE_KEY")
        with open(path, "w") as f:
            f.write("weak\n")
        try:
            remote.load_key(d)                                            # a damaged key file is an error, not a silent reset
            raise AssertionError("weak key file accepted")
        except ValueError:
            pass
    print("  key file ok")


def test_auth():
    clk = Clock()
    a = Auth(KEY, clock=clk)
    tok, wait, fails = a.login(KEY, "1.1.1.1")
    assert tok and not wait and not fails
    sid = a.verify(f"x=1; {remote.COOKIE}={tok}; y=2")
    assert sid and a.verify(f"{remote.COOKIE}={tok}") == sid
    # typed sloppily is still accepted
    assert a.login(" k7qd 3mwx-9ptc-bz4n-hr2v-8fga ", "1.1.1.1")[0]
    assert a.login(KEY.replace("0", "O"), "1.1.1.1")[0]
    # tampering, garbage and a different server's key
    assert a.verify(f"{remote.COOKIE}={tok[:-1]}{'A' if tok[-1] != 'A' else 'B'}") is None
    assert a.verify(f"{remote.COOKIE}=") is None and a.verify("") is None and a.verify(None) is None
    assert a.verify(f"{remote.COOKIE}=a.b.c") is None and a.verify(f"{remote.COOKIE}=a.99999999999.c") is None
    other = Auth(remote.generate_key(), clock=clk)
    assert other.verify(f"{remote.COOKIE}={tok}") is None                  # new key = everyone signed out
    # expiry
    clk.t += remote.SESSION_TTL - 5
    assert a.verify(f"{remote.COOKIE}={tok}")
    clk.t += 10
    assert a.verify(f"{remote.COOKIE}={tok}") is None
    # revoke
    tok2 = a.login(KEY, "1.1.1.1")[0]
    s2 = a.verify(f"{remote.COOKIE}={tok2}")
    a.revoke(s2)
    assert a.verify(f"{remote.COOKIE}={tok2}") is None
    print("  sessions ok")


def test_lockout():
    # the policy itself is a security requirement, so it is pinned here rather than read from remote.py
    assert (remote.MAX_FAILS, remote.LOCKOUT, remote.FAIL_WINDOW) == (5, 900, 600)
    assert remote.SESSION_TTL == 30 * 86400 and remote.MIN_KEY_LEN >= 20
    clk = Clock()
    a = Auth(KEY, clock=clk)
    for i in range(1, remote.MAX_FAILS):
        assert a.login("wrong", "9.9.9.9") == (None, 0, i)
    tok, wait, fails = a.login("wrong", "9.9.9.9")                         # 5th wrong key
    assert tok is None and wait == remote.LOCKOUT and fails == remote.MAX_FAILS
    # even the right key is refused while locked, but only for that address
    assert a.login(KEY, "9.9.9.9")[0] is None and a.login(KEY, "9.9.9.9")[1] > 0
    assert a.login(KEY, "8.8.8.8")[0]
    clk.t += remote.LOCKOUT + 2
    assert a.login(KEY, "9.9.9.9")[0]                                      # lock expires
    # old failures age out instead of adding up forever
    b = Auth(KEY, clock=clk)
    for _ in range(remote.MAX_FAILS - 1):
        b.login("wrong", "7.7.7.7")
        clk.t += remote.FAIL_WINDOW / 3
    assert b.login("wrong", "7.7.7.7")[1] == 0
    # a good login clears the count
    c = Auth(KEY, clock=clk)
    for _ in range(remote.MAX_FAILS - 1):
        c.login("wrong", "6.6.6.6")
    assert c.login(KEY, "6.6.6.6")[0]
    for _ in range(remote.MAX_FAILS - 1):
        assert c.login("wrong", "6.6.6.6")[1] == 0
    # a flood of different addresses can't grow memory without bound
    d = Auth(KEY, clock=clk)
    for i in range(2500):
        d.login("wrong", f"10.{i // 250}.{i % 250}.1")
    assert len(d._fails) <= 2000
    print("  lockout ok")


def test_env_and_urls():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, ".env")
        with open(p, "w") as f:
            f.write("A=1\n# comment\nREMOTE_CONTROL=false\nB=two words\n")
        remote.set_env(p, "REMOTE_CONTROL", "true")
        remote.set_env(p, "NEWKEY", "x")
        assert open(p).read() == "A=1\n# comment\nREMOTE_CONTROL=true\nB=two words\nNEWKEY=x\n"
        assert remote.env_value(p, "REMOTE_CONTROL") == "true" and remote.env_value(p, "NOPE") == ""
        q = os.path.join(d, "new.env")
        remote.set_env(q, "X", "1")
        assert stat.S_IMODE(os.stat(q).st_mode) == 0o600
    assert remote.urls("100.64.1.2", 8788) == ["http://100.64.1.2:8788"]
    assert isinstance(remote.urls("0.0.0.0", 8788), list)
    assert remote.is_loopback("127.0.0.1") and remote.is_loopback("localhost") and not remote.is_loopback("0.0.0.0")
    print("  env helpers and addresses ok")


# ------------------------------------------------------------------ the real server
class FakeNotify:
    def __init__(self):
        self.msgs = []

    def text(self, m):
        self.msgs.append(m)


class FakeBot:
    live = False

    def __init__(self):
        self.cfg = Config()
        self.engine = Engine(self.cfg)
        self.tel = Telemetry(False, "/nonexistent/trades.csv")
        self.tel.connected = True
        self.notify = FakeNotify()
        self.handled = []
        self.settings_path = os.path.join(tempfile.gettempdir(), "test_remote_settings.json")

    async def handle(self, acts):
        self.handled.extend(acts)


def hold_a_position(bot):
    e = bot.engine
    e.on_event({"txType": "create", "mint": "good", "vSolInBondingCurve": 30, "vTokensInBondingCurve": V_TOK,
                "traderPublicKey": "dev", "solAmount": 0.5, "marketCapSol": 60, "symbol": "GOOD",
                "initialBuy": 0.05 * V_TOK}, 0)
    for i in range(10):
        e.on_event({"txType": "buy", "mint": "good", "vSolInBondingCurve": 30, "vTokensInBondingCurve": V_TOK,
                    "traderPublicKey": f"b{i}", "solAmount": 0.5, "marketCapSol": 60}, 1)
    for a in e.tick(21):
        if a[0] == "buy":
            bot.engine.record_buy(a[2], 0, bot.cfg.buy_sol)
    assert "good" in bot.engine.positions


async def server_tests():
    clk = Clock(remote.time.time())
    bot = FakeBot()
    auth = Auth(KEY, clock=clk)
    dash = Dashboard(bot, "127.0.0.1", 0, auth=auth)
    await dash.start()
    port = dash.port

    def _req(method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        h = dict(headers or {})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h["Content-Type"] = "application/json"
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        raw = r.read()
        hd = {k.lower(): v for k, v in r.getheaders()}
        hd["_set_cookies"] = [v for k, v in r.getheaders() if k.lower() == "set-cookie"]
        c.close()
        return r.status, hd, raw

    def req(*a, **k):
        return asyncio.to_thread(_req, *a, **k)

    # --- nothing works without a login
    st, h, _ = await req("GET", "/")
    assert st == 302 and h["location"] == "/login", (st, h)
    for method, path in (("GET", "/api/state"), ("GET", "/api/settings"), ("POST", "/api/pause"),
                         ("POST", "/api/panic"), ("POST", "/api/sell"), ("POST", "/api/settings"),
                         ("POST", "/logout"), ("GET", "/assets/bg.webp"), ("GET", "/dashboard.html"),
                         ("GET", "/.env"), ("GET", "/assets/../.env")):
        st, h, raw = await req(method, path, body={} if method == "POST" else None)
        assert st == 401, (method, path, st)
        assert b"positions" not in raw and b"TOKEN" not in raw
    assert not bot.engine.paused
    # --- the few public things
    st, h, raw = await req("GET", "/login")
    assert st == 200 and b"Access key" in raw and "content-security-policy" in h
    st, h, raw = await req("GET", "/manifest.webmanifest")
    m = json.loads(raw)
    assert st == 200 and m["display"] == "standalone" and len(m["icons"]) == 2
    for icon in ("icon-192.png", "icon-512.png", "favicon.png", "neet-cat.webp"):
        st, h, _ = await req("GET", f"/assets/{icon}")
        assert st == 200 and h["content-type"].startswith("image/"), icon
    st, h, raw = await req("GET", "/")
    assert st == 302
    # --- wrong key
    st, h, raw = await req("POST", "/login", {"key": "nope"})
    assert st == 401 and json.loads(raw)["ok"] is False and not h["_set_cookies"]
    st, h, raw = await req("POST", "/login", {"key": 12345})
    assert st == 401
    st, h, raw = await req("POST", "/login", None)
    assert st == 401
    assert any("wrong key" in e["text"] for e in bot.tel.events)
    # --- right key (sloppily typed)
    st, h, raw = await req("POST", "/login", {"key": KEY.lower().replace("-", " ")})
    assert st == 200 and json.loads(raw)["ok"] is True
    ck = h["_set_cookies"][0]
    assert ck.startswith(remote.COOKIE + "=") and "HttpOnly" in ck and "SameSite=Strict" in ck and "Path=/" in ck
    assert "Secure" not in ck
    cookie = {"Cookie": ck.split(";")[0]}
    st, h, _ = await req("POST", "/login", {"key": KEY}, {"X-Forwarded-Proto": "https"})
    assert "Secure" in h["_set_cookies"][0]
    assert any("Remote login from" in e["text"] for e in bot.tel.events)
    assert any("login" in m for m in bot.notify.msgs)
    assert KEY not in json.dumps(list(bot.tel.events)) and not any(KEY in m for m in bot.notify.msgs)
    # --- the dashboard itself
    st, h, raw = await req("GET", "/", headers=cookie)
    page = raw.decode()
    assert st == 200 and "const REMOTE = true;" in page and "__TOKEN__" not in page and "__REMOTE__" not in page
    assert "content-security-policy" in h and h["x-frame-options"] == "DENY" and h["referrer-policy"] == "no-referrer"
    token = re.search(r'const TOKEN = "([^"]+)"', page).group(1)
    st, h, raw = await req("GET", "/api/state", headers=cookie)
    state = json.loads(raw)
    assert st == 200 and state["mode"] == "PAPER" and "positions" in state
    st, h, raw = await req("GET", "/api/state", headers={**cookie, "Accept-Encoding": "gzip"})
    assert h.get("content-encoding") == "gzip" and set(json.loads(gzip.decompress(raw))) == set(state)
    st, h, raw = await req("GET", "/api/settings", headers=cookie)
    assert st == 200 and json.loads(raw)
    st, h, raw = await req("GET", "/assets/bg.webp", headers=cookie)
    assert st == 200 and h["content-type"] == "image/webp"
    for bad in ("/assets/../.env", "/assets/..%2f.env", "/assets/.env", "/assets/x.py"):
        st, _, _ = await req("GET", bad, headers=cookie)
        assert st == 404, bad
    # --- controls need the session AND the page's token
    st, _, _ = await req("POST", "/api/pause", {}, cookie)
    assert st == 403 and not bot.engine.paused
    st, _, _ = await req("POST", "/api/pause", {}, {**cookie, "X-Dash-Token": "wrong"})
    assert st == 403
    st, _, _ = await req("POST", "/api/pause", {}, {**cookie, "X-Dash-Token": "café"})   # non-ASCII must not crash
    assert st in (403, 400)
    ctl = {**cookie, "X-Dash-Token": token}
    st, _, raw = await req("POST", "/api/pause", {}, ctl)
    assert st == 200 and bot.engine.paused and json.loads(raw)["paused"] is True
    st, _, raw = await req("POST", "/api/resume", {}, ctl)
    assert st == 200 and not bot.engine.paused
    # --- sell all
    st, _, raw = await req("POST", "/api/panic", {}, ctl)
    assert json.loads(raw) == {"ok": True, "selling": 0, "paused": True} and bot.engine.paused
    bot.engine.paused = False
    hold_a_position(bot)
    st, _, raw = await req("POST", "/api/panic", {}, ctl)
    j = json.loads(raw)
    await asyncio.sleep(0.05)
    assert j["ok"] and j["selling"] == 1 and bot.engine.paused
    assert [a[0] for a in bot.handled] == ["sell"] and bot.handled[0][1] == "good"
    st, _, raw = await req("POST", "/api/panic", {}, ctl)
    assert json.loads(raw)["selling"] == 0                                 # already selling, not sold twice
    assert any("SELL ALL" in e["text"] for e in bot.tel.events) and any("Sell all" in m for m in bot.notify.msgs)
    # settings still validated and capped through the remote path
    st, _, raw = await req("POST", "/api/settings", {"buy_sol": 0.9}, ctl)
    assert json.loads(raw)["ok"] is False and bot.cfg.buy_sol <= 0.5
    # --- a wallet key can never be imported (or even looked up) through the phone listener
    with tempfile.TemporaryDirectory() as wd:
        walletkey.ENV_PATH = os.path.join(wd, ".env")
        st, _, raw = await req("GET", "/api/wallet", headers=cookie)
        assert st == 403
        st, _, raw = await req("POST", "/api/wallet", {"key": WALLET_B58}, ctl)
        j = json.loads(raw)
        assert st == 200 and j["ok"] is False and "computer running the bot" in j["error"] and WALLET_B58 not in raw.decode()
        assert not os.path.exists(walletkey.ENV_PATH)
        st, _, _ = await req("POST", "/api/wallet", {"key": WALLET_B58}, cookie)        # no page token either
        assert st == 403
    # --- tampered / expired / signed-out sessions
    bad = {"Cookie": cookie["Cookie"][:-2] + ("AA" if not cookie["Cookie"].endswith("AA") else "BB")}
    assert (await req("GET", "/api/state", headers=bad))[0] == 401
    st, h, _ = await req("POST", "/logout", {}, cookie)
    assert st == 403                                                       # logout also needs the token
    st, h, _ = await req("POST", "/logout", {}, ctl)
    assert st == 200 and "Max-Age=0" in h["_set_cookies"][0]
    assert (await req("GET", "/api/state", headers=cookie))[0] == 401      # that session is dead
    st, h, raw = await req("POST", "/login", {"key": KEY})
    cookie2 = {"Cookie": h["_set_cookies"][0].split(";")[0]}
    assert (await req("GET", "/api/state", headers=cookie2))[0] == 200
    clk.t += remote.SESSION_TTL + 5
    assert (await req("GET", "/api/state", headers=cookie2))[0] == 401     # 30 days later
    # --- lockout over real HTTP (last: it locks this address)
    clk.t += 1
    seen = []
    for _ in range(remote.MAX_FAILS + 1):
        st, h, raw = await req("POST", "/login", {"key": "wrong"})
        seen.append(st)
    assert seen[-1] == 429 and "retry-after" in h and seen.count(401) == remote.MAX_FAILS - 1, seen
    assert seen[remote.MAX_FAILS - 1] == 429
    st, _, _ = await req("POST", "/login", {"key": KEY})
    assert st == 429                                                       # right key, still locked
    assert any("locked out" in e["text"] for e in bot.tel.events) and any("Locked out" in m for m in bot.notify.msgs)
    dash.server.close()
    await dash.server.wait_closed()
    print("  remote listener ok")


async def local_tests():
    """The normal localhost dashboard is unchanged: no login, Host guard on, can't be opened to the network."""
    bot = FakeBot()
    dash = Dashboard(bot, "127.0.0.1", 0)
    await dash.start()
    port = dash.port

    def _req(path, host=None, method="GET", headers=None):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        h = {"Host": host} if host else {}
        h.update(headers or {})
        c.request(method, path, headers=h, body=b"{}" if method == "POST" else None)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, raw

    for host in (f"localhost:{port}", f"127.0.0.1:{port}", f"[::1]:{port}"):
        st, raw = await asyncio.to_thread(_req, "/api/state", host)
        assert st == 200, host
    st, raw = await asyncio.to_thread(_req, "/api/state", "evil.example.com")
    assert st == 403                                                       # DNS-rebinding guard
    st, raw = await asyncio.to_thread(_req, "/", f"localhost:{port}")
    page = raw.decode()
    assert "const REMOTE = false;" in page and "__REMOTE__" not in page
    st, _ = await asyncio.to_thread(_req, "/api/panic", f"localhost:{port}", "POST")
    assert st == 403                                                       # token still required
    st, _ = await asyncio.to_thread(_req, "/login", f"localhost:{port}")
    assert st == 404                                                       # no login page on the private port
    # --- wallet key import on the private dashboard: saved to .env (mode 600), never echoed back
    with tempfile.TemporaryDirectory() as wd:
        env = walletkey.ENV_PATH = os.path.join(wd, ".env")
        with open(env, "w") as fh:
            fh.write("LIVE_TRADING=false\nBOT_WALLET_PRIVATE_KEY=\nRPC_URL=https://example.invalid\n")
        os.chmod(env, 0o644)

        def call(method, path, body=None, token=None):
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            h = {"Host": f"localhost:{port}"}
            if token:
                h["X-Dash-Token"] = token
            c.request(method, path, headers=h, body=json.dumps(body).encode() if body is not None else None)
            r = c.getresponse()
            raw = r.read().decode()
            c.close()
            return r.status, raw

        st, raw = await asyncio.to_thread(call, "GET", "/")
        token = re.search(r'const TOKEN = "([^"]+)"', raw).group(1)
        st, raw = await asyncio.to_thread(call, "GET", "/api/wallet")
        assert st == 200 and json.loads(raw) == {"configured": False, "address": "", "live": False}, raw
        st, raw = await asyncio.to_thread(call, "POST", "/api/wallet", {"key": WALLET_B58})
        assert st == 403                                                      # needs the page token
        for bad in ("", "hello", "0OIl" * 22, WALLET_B58[:-4], "[1,2,3]", "seed phrase words here"):
            st, raw = await asyncio.to_thread(call, "POST", "/api/wallet", {"key": bad}, token)
            j = json.loads(raw)
            assert j["ok"] is False and j["error"] and bad not in j["error"] or bad == "", (bad, j)
        assert "BOT_WALLET_PRIVATE_KEY=\n" in open(env).read()                 # nothing saved by the bad tries
        st, raw = await asyncio.to_thread(call, "POST", "/api/wallet", {"key": WALLET_B58}, token)
        j = json.loads(raw)
        assert j["ok"] and j["address"] == WALLET_ADDR and WALLET_B58 not in raw, j
        text = open(env).read()
        assert f"BOT_WALLET_PRIVATE_KEY={WALLET_B58}" in text and "RPC_URL=https://example.invalid" in text
        assert stat.S_IMODE(os.stat(env).st_mode) == 0o600
        st, raw = await asyncio.to_thread(call, "GET", "/api/wallet")
        assert json.loads(raw)["address"] == WALLET_ADDR and WALLET_B58 not in raw
        for path in ("/api/state", "/api/settings"):
            st, raw = await asyncio.to_thread(call, "GET", path)
            assert WALLET_B58 not in raw, path
        assert WALLET_B58 not in json.dumps(list(bot.tel.events)) and any("Wallet key imported" in e["text"] for e in bot.tel.events)
        st, raw = await asyncio.to_thread(call, "POST", "/api/wallet", {"key": json.dumps(list(WALLET_SECRET))}, token)
        assert json.loads(raw)["ok"] is True                                  # the Solana CLI file format works too
    dash.server.close()
    await dash.server.wait_closed()
    # opening the dashboard to the network without a key is refused outright
    for host in ("0.0.0.0", "192.168.1.5", "::"):
        try:
            await Dashboard(FakeBot(), host, 0).start()
            raise AssertionError(f"listened on {host} without a key")
        except ValueError:
            pass
    print("  localhost dashboard unchanged ok")


def run():
    test_keys()
    test_key_file()
    test_auth()
    test_lockout()
    test_env_and_urls()
    asyncio.run(server_tests())
    asyncio.run(local_tests())
    print("all remote tests passed")


if __name__ == "__main__":
    run()
