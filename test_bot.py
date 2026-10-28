"""Offline tests for the bot runtime (Bot.handle). Run: python test_bot.py
Needs the packages from requirements.txt installed (bot.py imports them) but makes no network calls
and never touches your .env mode: it forces paper mode before bot.py is imported."""
import asyncio
import os
import tempfile

os.environ.update(LIVE_TRADING="false", TRACKED_WALLETS="W1,W2", COPY_WALLETS="W1", DISCORD_WEBHOOK_URL="",
                  DISCORD_BOT_TOKEN="", DISCORD_USER_ID="", REMOTE_CONTROL="false")

import bot as botmod                       # noqa: E402  (must come after the environment is set)

M = "M" * 44
V = 1_000_000_000.0


class FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)


def make_bot():
    tmp = tempfile.mkdtemp()
    botmod.TRADE_LOG, botmod.WALLET_LOG = os.path.join(tmp, "trades.csv"), os.path.join(tmp, "w.csv")
    botmod.CONTEXT_LOG = os.path.join(tmp, botmod.review.CONTEXT)
    botmod.SETTINGS_FILE = os.path.join(tmp, "s.json")
    b = botmod.Bot()
    b.tel = botmod.Telemetry(False, botmod.TRADE_LOG)
    b.ws = FakeWS()
    return b


def trade_ev(tx, trader, sol, **kw):
    # a PumpPortal-shaped trade event: note there is NO "symbol" key, as on real trade events
    return {"txType": tx, "mint": M, "traderPublicKey": trader, "solAmount": sol, "tokenAmount": 5,
            "vSolInBondingCurve": 30, "vTokensInBondingCurve": V, "marketCapSol": 40,
            "signature": f"sig-{tx}-{trader}-{sol}", "pool": "pump", "newTokenBalance": 1,
            "bondingCurveKey": "x", "name": "n", **kw}


async def run():
    # 1. a tracked wallet's trade with no "symbol" must not crash the bot (it used to raise inside handle(),
    #    which the websocket loop treated as a lost connection)
    b = make_bot()
    await b.handle(b.engine.on_event(trade_ev("buy", "W2", 0.1), 1000.0))      # tracked, not copied
    await b.handle(b.engine.on_event(trade_ev("sell", "W2", 0.1), 1001.0))
    texts = [e["text"] for e in b.tel.events if e["kind"] == "wallet"]
    assert len(texts) == 2 and all("MMMM..MMMM" in t for t in texts), texts
    assert b.tel.wallet_trades == 2 and os.path.exists(botmod.WALLET_LOG)

    # 2. copy trade end to end in paper mode: W1 buys -> we buy; W1 sells -> we sell; P&L is booked
    b = make_bot()
    await b.handle(b.engine.on_event(trade_ev("buy", "W1", 1.0), 2000.0))
    assert M in b.engine.positions and b.engine.positions[M].sol_in == b.cfg.copy_buy_sol <= 0.5
    assert any("subscribeTokenTrade" in m for m in b.ws.sent)
    await b.handle(b.engine.on_event(trade_ev("sell", "W1", 1.0, signature="s2"), 2010.0))
    assert M not in b.engine.positions and len(b.engine.closed) == 1
    assert b.engine.closed[0]["reason"].startswith("copied sell") and b.tel.history
    assert any("unsubscribeTokenTrade" in m for m in b.ws.sent)
    assert os.path.exists(botmod.TRADE_LOG)

    # 3. the Watching panel: what the bot is evaluating right now, and why it hasn't bought yet
    from dashboard import Dashboard
    import json
    b = make_bot()
    c = b.cfg
    t0 = 5000.0
    b.engine.on_event({"txType": "create", "mint": M, "symbol": "WATCHME", "traderPublicKey": "dev",
                       "initialBuy": 0.02 * 1e9, "vSolInBondingCurve": 30, "vTokensInBondingCurve": V,
                       "marketCapSol": 50, "signature": "c1"}, t0)
    for i in range(3):
        b.engine.on_event(trade_ev("buy", f"buyer{i}", 0.5, signature=f"b{i}"), t0 + 1)
    w = Dashboard(b).watch_state(t0 + 2)
    assert w["total"] == 1 and w["launch"] and json.dumps(w), w
    row = w["rows"][0]
    assert row["symbol"] == "WATCHME" and row["mint"] == M and row["buyers"] == 3 and row["state"] == "young", row
    assert {x["k"] for x in row["checks"]} >= {"dev", "buyers", "inflow", "mcap", "price"}
    row = Dashboard(b).watch_state(t0 + c.watch_seconds + 1)["rows"][0]       # old enough, but only 3 buyers
    assert row["state"] == "no" and "only 3 buyers" in row["status"], row
    for i in range(3, c.min_unique_buyers + 2):                                # now enough buyers
        b.engine.on_event(trade_ev("buy", f"buyer{i}", 0.5, signature=f"b{i}"), t0 + 3)
    row = Dashboard(b).watch_state(t0 + c.watch_seconds + 1)["rows"][0]
    assert row["state"] in ("go", "no") and row["passed"] >= 4, row
    # decisions are remembered for the panel
    await b.handle([("skip", M, b.engine.watching[M], "only 3 buyers")])
    assert Dashboard(b).watch_state(t0 + 30)["recent"][0]["kind"] == "skip"
    assert json.dumps(Dashboard(b).state())

    # 5. self-review: entry context is logged with each buy, and a review can run and be undone
    b = make_bot()
    await b.handle(b.engine.on_event(trade_ev("buy", "W1", 1.0), 2000.0))
    await b.handle(b.engine.on_event(trade_ev("sell", "W1", 1.0, signature="s2"), 2010.0))
    assert os.path.exists(botmod.CONTEXT_LOG) and "mcap_sol" in open(botmod.CONTEXT_LOG).readline()
    b.cfg.review_min_trades = 1
    rec = b.do_review("session", start=0)
    assert rec and rec["stats"]["n"] == 1 and b.tel.review is rec
    st = Dashboard(b).review_state()
    assert st["last"]["n"] == 1 and json.dumps(st)
    b.cfg.review_mode = "off"
    assert b.do_review("24h") is None                                          # off means off
    res = await Dashboard(b)._control("/api/review", b"{}")                    # "Review now" still works (suggest)
    assert res["ok"] and not res["review"]["last"]["applied"], res
    assert (await Dashboard(b)._control("/api/review/undo", b"{}"))["ok"] is False   # nothing applied yet
    assert b.cfg.buy_sol <= 0.5 and b.cfg.max_open_positions <= 5

    # 4. the hard limits survive a hostile config
    os.environ["BUY_SOL"], os.environ["MAX_OPEN_POSITIONS"] = "5", "50"
    cfg = botmod.load_config()
    assert cfg.buy_sol == 0.5 and cfg.max_open_positions == 5
    del os.environ["BUY_SOL"], os.environ["MAX_OPEN_POSITIONS"]
    print("all bot tests passed")


asyncio.run(run())
