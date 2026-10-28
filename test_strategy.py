"""Offline test: feeds fake PumpPortal events through the strategy. Run: python test_strategy.py"""
from strategy import Config, Engine

V_TOK = 1_000_000_000.0


def ev(tx, mint, v_sol, trader="t", sol=0.5, mcap=60, **kw):
    return {"txType": tx, "mint": mint, "vSolInBondingCurve": v_sol,
            "vTokensInBondingCurve": V_TOK, "traderPublicKey": trader,
            "solAmount": sol, "marketCapSol": mcap, **kw}


def launch(e, mint, t0, buyers=10, dev_share=0.05, dev_sells=False, mcap=60):
    e.on_event(ev("create", mint, 30, trader="dev", symbol=mint.upper(),
                  initialBuy=dev_share * V_TOK, mcap=mcap), t0)
    for i in range(buyers):
        e.on_event(ev("buy", mint, 30, trader=f"b{i}", mcap=mcap), t0 + 1)
    if dev_sells:
        e.on_event(ev("sell", mint, 30, trader="dev", mcap=mcap), t0 + 2)
    return e.tick(t0 + 21)


def run():
    cfg = Config(tracked_wallets="W1,W2")
    e = Engine(cfg)

    def act(actions):
        out = []
        for a in actions:
            if a[0] == "buy":
                e.record_buy(a[2], 0, cfg.buy_sol)
            if a[0] == "copy_buy":
                e.record_buy(a[2], 0, cfg.copy_buy_sol, source=a[3])
            if a[0] == "sell":
                e.record_sell(a[1], 0, a[2])
            out.append(a[0] if a[0] != "skip" else f"skip:{a[3]}")
        return out

    assert "buy" in act(launch(e, "good", 0))
    r = act(launch(e, "few", 0, buyers=3)); assert any("buyers" in x for x in r), r
    r = act(launch(e, "rug", 0, dev_sells=True)); assert any("dev sold" in x for x in r), r
    r = act(launch(e, "whale", 0, dev_share=0.3)); assert any("dev holds" in x for x in r), r
    r = act(launch(e, "big", 0, mcap=400)); assert any("mcap" in x for x in r), r

    # take profit: price +70%
    act(e.on_event(ev("buy", "good", 51), 30))
    assert "good" not in e.positions and e.closed[-1]["reason"].startswith("take profit")

    # stop loss
    act(launch(e, "dump", 100)); act(e.on_event(ev("sell", "dump", 21), 130))
    assert e.closed[-1]["reason"].startswith("stop loss")

    # trailing stop: up 40%, then falls 25% off peak while still green
    act(launch(e, "trail", 200))
    act(e.on_event(ev("buy", "trail", 42), 230)); act(e.on_event(ev("sell", "trail", 31.5), 240))
    assert e.closed[-1]["reason"].startswith("trailing"), e.closed[-1]

    # max hold time
    act(launch(e, "flat", 300)); act(e.tick(300 + 21 + cfg.max_hold_seconds))
    assert e.closed[-1]["reason"].startswith("max hold")

    # --- copy trading ---
    # tracked wallet buys -> bot copies; an untracked wallet buying does nothing
    r = act(e.on_event(ev("buy", "cp", 40, trader="W1", sol=1.0, signature="s1"), 400-390))
    assert r == ["wallet_trade", "copy_buy"], r
    assert e.positions["cp"].source == "W1"
    assert act(e.on_event(ev("buy", "x", 40, trader="nobody", sol=5), 400-390)) == []
    # same event delivered twice (account + token subscription) -> ignored
    assert act(e.on_event(ev("buy", "cp", 40, trader="W1", sol=1.0, signature="s1"), 401-390)) == []
    # second tracked wallet buying the same token doesn't double-buy
    assert act(e.on_event(ev("buy", "cp", 41, trader="W2", sol=1.0), 402-390)) == ["wallet_trade"]
    # W2 selling doesn't trigger a copy sell (we copied W1), W1 selling does
    assert "sell" not in act(e.on_event(ev("sell", "cp", 42, trader="W2"), 403-390))
    r = act(e.on_event(ev("sell", "cp", 44, trader="W1"), 404-390))
    assert "sell" in r and e.closed[-1]["reason"].startswith("copied sell"), r
    assert e.closed[-1]["source"] == "copy:W1"
    # filters: tiny buys and tokens that already ran are not copied
    r = e.on_event(ev("buy", "tiny", 40, trader="W1", sol=0.01), 405-390)
    assert r[-1][0] == "copy_skip" and "SOL <" in r[-1][3], r
    r = e.on_event(ev("buy", "moon", 40, trader="W1", sol=2, mcap=900), 406-390)
    assert r[-1][0] == "copy_skip" and "too high" in r[-1][3], r
    # log-only mode: records the trade, never copies
    e2 = Engine(Config(tracked_wallets="W1", copy_trading=False))
    assert [a[0] for a in e2.on_event(ev("buy", "z", 40, trader="W1", sol=1), 0)] == ["wallet_trade"]

    # track many, copy few: W2 is logged but not copied
    e3 = Engine(Config(tracked_wallets="W1,W2", copy_wallets="W1"))
    assert [a[0] for a in e3.on_event(ev("buy", "q", 40, trader="W2", sol=1), 0)] == ["wallet_trade"]
    assert [a[0] for a in e3.on_event(ev("buy", "q", 40, trader="W1", sol=1), 0)] == ["wallet_trade", "copy_buy"]
    # Axiom export loads with names
    e4 = Engine(Config(tracked_wallets="wallets/axiom_wallets.json", copy_wallets="W9"))
    assert len(e4.wallets) == 495 and e4.copy_from == {"W9"}
    assert e4.names["suqh5sHtr8HyJ7q8scBimULPkPpA557prMG47xCHQfK"] == "Cupsey"

    # daily loss cap blocks new buys (launch and copy)
    e.realized_pnl_today = -cfg.max_daily_loss_sol
    r = act(launch(e, "blocked", 1000)); assert "skip:risk limits" in r, r
    r = e.on_event(ev("buy", "blk2", 40, trader="W1", sol=1), 1000)
    assert r[-1][0] == "copy_skip" and r[-1][3] == "risk limits", r

    # --- new entry criteria ---
    def watch(e, mint, t0, buys, dev_share=0.02):
        e.on_event(ev("create", mint, 30, trader="dev", symbol=mint, initialBuy=dev_share * V_TOK), t0)
        for (t, trader, sol, tok) in buys:
            e.on_event(ev("buy", mint, 30, trader=trader, sol=sol, tokenAmount=tok), t0 + t)

    # age window: not enough buyers at 20 s, enough at 30 s -> buys at 30 s when max age is 60
    e5 = Engine(Config(max_age_seconds=60))
    watch(e5, "late", 0, [(1, f"a{i}", 0.5, 1e6) for i in range(4)])
    assert [a[0] for a in e5.tick(21)] == []            # still young enough to wait
    watch_more = [(25, f"b{i}", 0.5, 1e6) for i in range(6)]
    for (t, tr, sol, tok) in watch_more:
        e5.on_event(ev("buy", "late", 30, trader=tr, sol=sol, tokenAmount=tok), t)
    r = e5.tick(30); assert r[0][0] == "buy" and "age 30s" in r[0][3], r
    # ...and gives up at max age
    watch(e5, "dead", 100, [(1, "z", 0.5, 1e6)])
    r = e5.tick(161); assert r[0][0] == "skip" and "buyers" in r[0][3], r

    # holder concentration: one wallet = 80% of buys
    e6 = Engine(Config(max_top_buyer_pct=50))
    watch(e6, "whale", 0, [(1, "big", 8.0, 1e6)] + [(1, f"s{i}", 0.25, 1e5) for i in range(8)])
    r = e6.tick(21); assert r[0][0] == "skip" and "top buyer" in r[0][3], r

    # bundles: 3 wallets grab 12% of supply within 1.5 s of launch
    e7 = Engine(Config(max_bundle_pct=10))
    watch(e7, "bund", 0, [(0.5, f"x{i}", 1.0, 4e7) for i in range(3)] + [(5, f"y{i}", 0.3, 1e6) for i in range(8)])
    r = e7.tick(21); assert r[0][0] == "skip" and "bundled 12.0%" in r[0][3] and "3 wallets" in r[0][3], r
    e7b = Engine(Config(max_bundle_pct=15))     # same launch passes with a looser limit
    watch(e7b, "bund", 0, [(0.5, f"x{i}", 1.0, 4e7) for i in range(3)] + [(5, f"y{i}", 0.3, 1e6) for i in range(8)])
    assert e7b.tick(21)[0][0] == "buy"

    # dev selling allowed/not allowed
    for allow, want in ((False, "skip"), (True, "buy")):
        e8 = Engine(Config(allow_dev_selling=allow))
        watch(e8, "ds", 0, [(1, f"c{i}", 0.5, 1e6) for i in range(10)])
        e8.on_event(ev("sell", "ds", 30, trader="dev", sol=0.1), 2)
        assert e8.tick(21)[0][0] == want, allow

    # trailing stop 0 = off (no instant sell once in profit)
    e9 = Engine(Config(trailing_stop_pct=0))
    act9 = e9.tick
    watch(e9, "tr0", 0, [(1, f"d{i}", 0.5, 1e6) for i in range(10)])
    b = e9.tick(21); e9.record_buy(b[0][2], 21, 0.5)
    e9.on_event(ev("buy", "tr0", 40), 25); r = e9.on_event(ev("buy", "tr0", 39), 26)
    assert not any(a[0] == "sell" for a in r), r

    # the dashboard checklist must agree with the real filter, whatever the settings and the token
    import random
    from strategy import Watch
    rng = random.Random(7)
    for _ in range(3000):
        cfg_r = Config(min_unique_buyers=rng.choice([0, 3, 8]), max_top_buyer_pct=rng.choice([100, 40, 70]),
                       max_bundle_pct=rng.choice([100, 5, 20]), min_social_authors=rng.choice([0, 0, 2]),
                       min_net_inflow_sol=rng.choice([0, 1, 3]), max_dev_share=rng.choice([0.05, 0.1, 0.3]),
                       allow_dev_selling=rng.random() < 0.4, min_mcap_sol=rng.choice([0, 30]),
                       max_mcap_sol=rng.choice([80, 120]))
        er = Engine(cfg_r)
        er.social_authors = lambda m, n, win: rng.choice([0, 1, 2, 3])
        wr = Watch(mint="m", symbol="S", dev="d", created_at=0, dev_share=rng.choice([0, 0.04, 0.2]),
                   buyers={f"b{i}" for i in range(rng.randint(0, 12))}, buy_sol=rng.uniform(0, 8),
                   sell_sol=rng.uniform(0, 3), dev_sold=rng.random() < 0.3, price=rng.choice([0, 1e-7]),
                   mcap_sol=rng.uniform(0, 200), bundle_tokens=rng.choice([0, 3e7, 2e8]))
        wr.buyer_sol = {b: rng.uniform(0.1, 3) for b in wr.buyers}
        er.social_authors = None if cfg_r.min_social_authors == 0 else (lambda m, n, win: 1)
        passed, _ = er.passes_filters(wr, 10)
        assert passed == all(ok for _, ok, _ in er.checklist(wr, 10)), (cfg_r, wr)

    # the dashboard profiles are valid, inside the hard limits, and "balanced" is the shipped default
    import settings as ui_settings
    for name, vals in ui_settings.PRESETS.items():
        clean, errs = ui_settings.validate(Config(), ui_settings.preset_display(vals))
        assert not errs, (name, errs)
        assert vals["buy_sol"] <= 0.5 and vals["copy_buy_sol"] <= 0.5 and vals["max_open_positions"] <= 5, name
    base = Config()
    for k, v in ui_settings.PRESETS["balanced"].items():
        assert getattr(base, k) == v, ("balanced preset differs from the shipped default", k)
    assert ui_settings.PRESETS["aggressive"]["min_unique_buyers"] < base.min_unique_buyers

    for t in e.closed:
        print(f"{t['symbol']:6} {t['pct']:+6.1f}%  {t['reason']}")
    print("all strategy tests passed")


run()
