"""Offline tests for the Discord signal reader. Run: python test_social.py"""
import json
import time

from discord_signals import DiscordListener, load_filter
from social import OutcomeTracker, SocialTracker, extract_mints, is_solana_address
from strategy import Config, Engine

V = 1_000_000_000.0
# real-shaped pump.fun mints (base58, 32 bytes, ending in "pump")
M1 = "8rvAsDKeAcEjEkiZMug9k8v1y8mW6gQQiM7Ubd89pump"
M2 = "FM1YCKED2KaqB8Uat8aB1nsffR1vezr7s6FAEieXpump"


def test_extract():
    assert is_solana_address(M1) and is_solana_address(M2)
    assert not is_solana_address("notbase58_0OIl")
    assert extract_mints(f"aping {M1} now") == [M1]
    assert extract_mints(f"https://pump.fun/coin/{M1}") == [M1]
    assert extract_mints(f"{M1} and {M2} and {M1} again") == [M1, M2]
    assert extract_mints("no addresses here, just vibes $PEPE 100x") == []
    assert extract_mints("") == [] and extract_mints(None) == []
    # not a pump.fun mint (doesn't end in pump) and wrong length
    assert extract_mints("So11111111111111111111111111111111111111112") == []
    assert extract_mints("shortpump") == []
    # a wall of addresses is spam, capped
    assert len(extract_mints(" ".join([M1[:-5] + c + "pump" for c in "abcdefghij"]))) <= 5
    print("  extract_mints ok")


def test_tracker():
    t = SocialTracker(window=600)
    assert t.record(M1, "alice", "g1", 1000) == (True, True)
    assert t.record(M1, "alice", "g1", 1001) == (False, False)   # same person again
    assert t.record(M1, "bob", "g2", 1002) == (False, True)
    assert t.authors(M1, 1002) == 2
    assert t.authors(M2, 1002) == 0
    # window: alice's call ages out
    assert t.authors(M1, 1700, window=100) == 0
    assert t.authors(M1, 1050, window=100) == 2
    top = t.top(1002)
    assert top[0]["mint"] == M1 and top[0]["authors"] == 2 and top[0]["mentions"] == 3
    assert set(top[0]["guilds"]) == {"g1", "g2"}
    # old tokens are pruned
    t.record(M2, "carl", "g1", 5000)
    assert M1 not in t.mints and t.authors(M2, 5000) == 1
    # memory is bounded
    small = SocialTracker(window=10 ** 9, max_mints=10)
    for i in range(50):
        small.record(f"m{i}", "x", "g", 1)
    assert len(small.mints) == 10
    print("  SocialTracker ok")


def msg(content, author="u1", guild="g1", channel="c1", bot=False, embeds=None):
    return dict(id="999", content=content, channel_id=channel, guild_id=guild,
                author=dict(id=author, username=author, bot=bot), embeds=embeds or [])


def test_listener(tmp_log):
    t = SocialTracker()
    lis = DiscordListener("tok", t, log_path=tmp_log)
    lis.guilds["g1"] = "Memecoins"
    out = lis.handle_message(msg(f"ape {M1}"), 100)
    assert len(out) == 1 and out[0][0] == M1
    assert out[0][1]["guild"] == "Memecoins" and out[0][1]["first"] and out[0][1]["authors"] == 1
    # embeds (most call channels are bots posting embeds)
    out = lis.handle_message(msg("", author="sniperbot", bot=True, embeds=[
        dict(title="New call", description=f"CA: {M2}", fields=[dict(name="mcap", value="40 SOL")])]), 101)
    assert out and out[0][0] == M2, "embed addresses must be read"
    # our own messages are ignored (the bot posts its own trade alerts)
    lis.me = "self"
    assert lis.handle_message(msg(f"BUY {M1}", author="self"), 102) == []
    # server filter
    lis.allow = {"g2"}
    assert lis.handle_message(msg(f"{M1}", guild="g1"), 103) == []
    assert lis.handle_message(msg(f"{M1}", guild="g2"), 104)
    # channel ID also allowed
    lis.allow = {"c9"}
    assert lis.handle_message(msg(f"{M1}", guild="g1", channel="c9"), 105)
    # DMs (no guild_id) are ignored
    lis.allow = set()
    assert lis.handle_message(dict(id="1", content=M1, channel_id="c", author=dict(id="x")), 106) == []
    rows = open(tmp_log, encoding="utf-8").read().strip().split("\n")
    assert rows[0].startswith("time,mint") and len(rows) >= 4
    print("  DiscordListener ok")


def test_outcomes():
    t = SocialTracker()
    t.record(M1, "a", "g1", 0)
    t.record(M1, "b", "g1", 10)
    oc = OutcomeTracker(authors_fn=lambda m, n: t.authors(m, n, 300))
    assert oc.start(M1, 0, dict(guild="Memecoins", first_author="a"))
    assert oc.following(M1) and oc.start(M1, 0, {})            # starting twice is fine
    ev = lambda v, **kw: dict(mint=M1, vSolInBondingCurve=v, vTokensInBondingCurve=V, **kw)
    oc.on_event(ev(30), 1)                                     # p0
    oc.on_event(ev(45), 200)                                   # +50%
    rows, done = oc.tick(400)                                  # 5-min checkpoint passed
    assert not rows and not done
    oc.on_event(ev(60), 800)                                   # +100%
    oc.on_event(ev(36), 1000)                                  # back to +20%
    oc.tick(1000)
    oc.on_event(ev(33), 3000)
    rows, done = oc.tick(3700)
    assert done == [M1] and len(rows) == 1
    r = rows[0]
    assert r["ret5m"] == 50.0 and r["ret15m"] == 20.0 and r["ret60m"] == 10.0
    assert r["peak_x"] == 2.0 and r["authors_5m"] == 2 and r["guild"] == "Memecoins"
    assert not oc.following(M1)
    # graduation to a DEX is flagged
    oc2 = OutcomeTracker()
    oc2.start(M2, 0, {})
    oc2.on_event(dict(mint=M2, vSolInBondingCurve=30, vTokensInBondingCurve=V), 1)
    oc2.on_event(dict(mint=M2, vSolInBondingCurve=90, vTokensInBondingCurve=V, pool="raydium"), 2)
    assert oc2.tick(3700)[0][0]["graduated"] == 1
    # the follow list is bounded
    oc3 = OutcomeTracker(max_follow=2)
    assert oc3.start("a", 0, {}) and oc3.start("b", 0, {}) and not oc3.start("c", 0, {})
    print("  OutcomeTracker ok")


def test_engine_gate():
    """min_social_authors blocks launches nobody called, and lets through the ones they did."""
    t = SocialTracker()

    def launch(e, mint, t0, buyers=10):
        e.on_event(dict(txType="create", mint=mint, symbol="X", traderPublicKey="dev",
                        initialBuy=0.02 * V, vSolInBondingCurve=30, vTokensInBondingCurve=V,
                        marketCapSol=60), t0)
        for i in range(buyers):
            e.on_event(dict(txType="buy", mint=mint, traderPublicKey=f"b{i}", solAmount=0.5,
                            tokenAmount=1e6, vSolInBondingCurve=30, vTokensInBondingCurve=V,
                            marketCapSol=60), t0 + 1)
        return e.tick(t0 + 21)

    e = Engine(Config(min_social_authors=2, social_window_min=30))
    e.social_authors = t.authors
    r = launch(e, M1, 0)
    assert r[0][0] == "skip" and "0 Discord callers" in r[0][3], r
    t.record(M2, "alice", "g1", 100)
    r = launch(e, M2, 100)
    assert r[0][0] == "skip" and "1 Discord caller (need 2)" in r[0][3], r
    t.record(M2, "bob", "g1", 110)
    e2 = Engine(Config(min_social_authors=2, social_window_min=30))
    e2.social_authors = t.authors
    r = launch(e2, M2, 200)
    assert r[0][0] == "buy" and "2 Discord callers" in r[0][3], r
    # calls outside the window don't count
    e3 = Engine(Config(min_social_authors=2, social_window_min=1))
    e3.social_authors = t.authors
    r = launch(e3, M2, 1000)
    assert r[0][0] == "skip" and "Discord caller" in r[0][3], r
    # filter off (default) = unchanged behaviour, and no tracker needed at all
    e4 = Engine(Config())
    assert launch(e4, M1, 2000)[0][0] == "buy"
    print("  engine social gate ok")


def test_gateway_protocol():
    """READY/GUILD_CREATE/MESSAGE_CREATE handling, without touching the network."""
    import asyncio
    t = SocialTracker()
    seen = []
    lis = DiscordListener("tok", t, on_call=lambda m, i: seen.append(m), log_path="/dev/null")

    async def go():
        await lis._dispatch({"op": 0, "t": "READY", "s": 1,
                             "d": {"user": {"id": "me", "username": "memebot"}, "session_id": "s1",
                                   "resume_gateway_url": "wss://x"}})
        assert lis.connected and lis.me == "me" and lis._seq == 1 and lis._session == "s1"
        await lis._dispatch({"op": 0, "t": "GUILD_CREATE", "s": 2,
                             "d": {"id": "g1", "name": "Memecoins",
                                   "channels": [{"id": "c1", "name": "calls", "type": 0},
                                                {"id": "c2", "name": "voice", "type": 2}]}})
        assert lis.guilds["g1"] == "Memecoins" and lis.channels == {"c1": "calls"}
        await lis._dispatch({"op": 0, "t": "MESSAGE_CREATE", "s": 3, "d": msg(f"ca: {M1}")})
        assert seen == [M1]
        for op in (7, 9):
            try:
                await lis._dispatch({"op": op, "d": {}})
                raise AssertionError("should ask for a reconnect")
            except ConnectionError:
                pass
    asyncio.run(go())
    print("  gateway protocol ok")


def test_servers_file():
    import os
    import tempfile
    p = os.path.join(tempfile.mkdtemp(), "s.txt")
    open(p, "w").write("# a comment\n1234567890   # Memecoins\nnot-an-id\n\n9876543210\n")
    assert load_filter(p) == {"1234567890", "9876543210"}
    assert load_filter("/nope/missing.txt") == set()
    print("  discord_servers.txt ok")


def run():
    import os
    import tempfile
    tmp = os.path.join(tempfile.mkdtemp(), "calls.csv")
    test_extract()
    test_tracker()
    test_listener(tmp)
    test_outcomes()
    test_engine_gate()
    test_gateway_protocol()
    test_servers_file()
    print("all social tests passed")


run()
