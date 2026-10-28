"""Offline tests for review.py (the bot's self-review). Run: python test_review.py"""
import csv
import json
import os
import random
import tempfile
import time

import review
import settings as ui_settings
from strategy import Config

NOW = time.mktime(time.strptime("2026-10-27 12:00:00", "%Y-%m-%d %H:%M:%S"))
HARD = ("buy_sol", "copy_buy_sol", "max_open_positions", "max_daily_loss_sol")


def ts(t):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(t))


def write_trades(root, rows, ctx=()):
    with open(os.path.join(root, review.TRADES), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["time", "mode", "symbol", "mint", "source", "sol_in", "sol_out",
                                          "pnl", "pct", "held_s", "reason"])
        w.writeheader()
        for r in rows:
            w.writerow(r)
    if ctx:
        with open(os.path.join(root, review.CONTEXT), "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["time", "mode", "mint", "symbol", "source", "age_s", "mcap_sol",
                                              "buyers", "net_inflow_sol", "dev_share"])
            w.writeheader()
            for r in ctx:
                w.writerow(r)


def trade(i, t, pct, reason, mode="paper", source="launch", held=200):
    return dict(time=ts(t), mode=mode, symbol=f"T{i}", mint=f"M{i}", source=source, sol_in=0.5,
                sol_out=round(0.5 * (1 + pct / 100), 4), pnl=round(0.5 * pct / 100, 4), pct=pct, held_s=held,
                reason=reason)


def mixed(n, start, kinds, rng):
    """kinds: list of (reason, pct_lo, pct_hi, weight)."""
    out, total = [], sum(k[3] for k in kinds)
    for i in range(n):
        x, acc = rng.random() * total, 0
        for reason, lo, hi, wgt in kinds:
            acc += wgt
            if x <= acc:
                break
        out.append(trade(i, start + i * 60, round(rng.uniform(lo, hi), 1), reason))
    return out


def fresh_cfg():
    c = Config()
    c.max_hold_seconds, c.min_unique_buyers, c.min_net_inflow_sol = 900, 5, 0.5
    c.take_profit_pct, c.stop_loss_pct, c.trailing_stop_pct = 100, 25, 25
    c.min_mcap_sol, c.max_mcap_sol = 30, 1000
    return c


def check_bounds(cfg, before_hard):
    for k in HARD:
        assert getattr(cfg, k) == before_hard[k], k                     # never touched
    disp = {f["key"]: getattr(cfg, f["key"]) * f.get("scale", 1) for f in ui_settings.SCHEMA if f["type"] != "bool"}
    clean, errors = ui_settings.validate(cfg, disp)
    assert not errors, errors


def main():
    rng = random.Random(11)
    with tempfile.TemporaryDirectory() as root:
        sp = os.path.join(root, "settings.json")

        # 1) too few trades: report only, change nothing
        write_trades(root, mixed(8, NOW - 3600 * 5, [("max hold time (-10%)", -30, -1, 1)], rng))
        cfg = fresh_cfg()
        rec = review.run_review(cfg, root, sp, now=NOW, apply=True, min_trades=20)
        assert rec["stats"]["n"] == 8 and not rec["changes"] and not rec["applied"] and not os.path.exists(sp)
        assert "need 20" in rec["notes"][0]

        # 2) mostly losing timeouts -> shorter max hold, by at most 25%
        os.remove(os.path.join(root, review.LOG)); os.remove(os.path.join(root, review.STATE))
        write_trades(root, mixed(40, NOW - 3600 * 20, [("max hold time (-8%)", -25, -1, 3),
                                                        ("take profit +100%", 100, 130, 1)], rng))
        cfg = fresh_cfg()
        hard = {k: getattr(cfg, k) for k in HARD}
        rec = review.run_review(cfg, root, sp, now=NOW, apply=True, min_trades=20)
        keys = {c["key"]: c for c in rec["changes"]}
        assert "max_hold_seconds" in keys and 675 <= cfg.max_hold_seconds < 900, rec
        assert rec["applied"] and json.load(open(sp))["max_hold_seconds"] == cfg.max_hold_seconds
        check_bounds(cfg, hard)
        assert review.load_state(root)["last_change"]["before"]["max_hold_seconds"] == 900

        # 2b) the same evidence never compounds: nothing else changes until the last change can be judged
        again = review.run_review(cfg, root, sp, now=NOW + 60, apply=True, min_trades=20)
        assert not again["changes"] and "Waiting for 20 more trades" in again["notes"][0], again
        assert 675 <= cfg.max_hold_seconds < 900

        # 3) trades outside the window, or in the other mode, are ignored
        cfg2 = fresh_cfg()
        rec2 = review.run_review(cfg2, root, "", start=NOW - 60, now=NOW, apply=False, min_trades=1, label="session")
        assert rec2["stats"]["n"] == 0
        rec3 = review.run_review(cfg2, root, "", now=NOW, mode="live", apply=False, min_trades=1, label="session")
        assert rec3["stats"]["n"] == 0

        # 4) undo puts the old values back
        notes = review.undo_last(cfg, root, sp)
        assert notes and cfg.max_hold_seconds == 900 and json.load(open(sp))["max_hold_seconds"] == 900
        assert review.undo_last(cfg, root, sp) == []                     # nothing left to undo

        # 5) mostly stopped out -> stricter entries; suggest mode changes nothing
        write_trades(root, mixed(30, NOW - 3600 * 10, [("stop loss -26%", -30, -25, 3),
                                                        ("take profit +100%", 100, 120, 1)], rng))
        cfg = fresh_cfg()
        rec = review.run_review(cfg, root, sp, now=NOW, apply=False, min_trades=20)
        keys = {c["key"]: c for c in rec["changes"]}
        assert keys["min_unique_buyers"]["new"] > 5 and keys["min_net_inflow_sol"]["new"] > 0.5, rec
        assert cfg.min_unique_buyers == 5 and not rec["applied"]
        assert len(rec["changes"]) <= review.MAX_CHANGES
        for c in rec["changes"]:
            assert c["key"] in review.TUNABLE
            if c["old"]:
                assert abs(c["new"] / c["old"] - 1) <= review.MAX_STEP + 0.21, c   # +1 buyer on small counts

        # 6) a losing low-market-cap third raises the min market cap (needs entry context)
        rows, ctx = [], []
        for i in range(30):
            t = NOW - 3600 * 12 + i * 300
            mcap = [40, 200, 600][i % 3] + rng.uniform(-5, 5)
            pct = rng.uniform(-30, -10) if i % 3 == 0 else rng.uniform(5, 40)
            reason = "trailing stop (30% off peak, +20%)" if pct > 0 else "stop loss -25%"
            rows.append(trade(i, t, round(pct, 1), reason, held=120))
            ctx.append(dict(time=ts(t - 120), mode="paper", mint=f"M{i}", symbol=f"T{i}", source="launch", age_s=5,
                            mcap_sol=round(mcap, 1), buyers=8, net_inflow_sol=1.2, dev_share=0.1))
        write_trades(root, rows, ctx)
        cfg = fresh_cfg()
        rec = review.run_review(cfg, root, sp, now=NOW, apply=True, min_trades=20)
        keys = {c["key"]: c for c in rec["changes"]}
        assert "min_mcap_sol" in keys and 30 < cfg.min_mcap_sol <= 46, rec
        assert cfg.min_mcap_sol < cfg.max_mcap_sol
        check_bounds(cfg, {k: getattr(fresh_cfg(), k) for k in HARD})

        # 7) the next review undoes a change that made things clearly worse
        st = review.load_state(root)
        st["last_change"]["baseline_avg_pct"] = 10.0
        review.save_state(root, st)
        bad = [trade(100 + i, NOW + 600 + i * 60, -20.0, "stop loss -20%") for i in range(25)]
        write_trades(root, rows + bad, ctx)
        rec = review.run_review(cfg, root, sp, now=NOW + 600 + 26 * 60, apply=True, min_trades=20)
        assert rec["reverted"] and cfg.min_mcap_sol == 30, rec
        assert "last_change" not in review.load_state(root)

        # 8) a broken/missing log never crashes it
        open(os.path.join(root, review.TRADES), "w").write("garbage\n\x00,,\n")
        rec = review.run_review(fresh_cfg(), root, "", now=NOW, apply=True, min_trades=1)
        assert rec["stats"]["n"] == 0
        assert review.summary(rec).startswith("Review")

    # the rule set as a whole: random histories never break limits or validation
    for seed in range(300):
        r = random.Random(seed)
        kinds = [("take profit +x", 50, 300, r.random()), ("stop loss -x", -40, -15, r.random()),
                 ("trailing stop (x)", -5, 40, r.random()), ("max hold time (x)", -30, 30, r.random()),
                 ("manual sell (dashboard)", -20, 20, r.random() * 0.2)]
        trades = []
        for i in range(r.randrange(20, 120)):
            x = r.random() * sum(k[3] for k in kinds)
            for reason, lo, hi, w in kinds:
                x -= w
                if x <= 0:
                    break
            pct = r.uniform(lo, hi)
            trades.append(dict(time=NOW - i, symbol="t", mint=f"m{i}", source="launch", sol_in=0.5,
                               pnl=0.5 * pct / 100, pct=pct, held_s=100, kind=review.exit_kind(reason),
                               mcap=r.uniform(10, 1500), buyers=5, inflow=1))
        cfg = fresh_cfg()
        for k in ("max_hold_seconds", "min_unique_buyers", "take_profit_pct", "trailing_stop_pct"):
            setattr(cfg, k, type(getattr(cfg, k))(getattr(cfg, k) * r.uniform(0.3, 2)))
        hard = {k: getattr(cfg, k) for k in HARD}
        ok, errors = review.check(cfg, review.propose(cfg, trades))
        for c in ok:
            setattr(cfg, c["key"], c["new"])
        check_bounds(cfg, hard)
    print("all review tests passed")


main()
