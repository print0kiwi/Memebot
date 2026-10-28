"""Offline test for sweep.py's decision logic (no market data needed). Run: python test_sweep.py (from backtest/)"""
import contextlib
import io
import json
import os
import sys
import tempfile
from dataclasses import replace

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))
import backtest as bt          # noqa: E402
import sweep                   # noqa: E402


def fake_eval_factory(table):
    """Metrics as a function of the config, so we know exactly what the search should pick."""
    def ev(rows, c, lo, end, split):
        tr = table(c, "train")
        te = table(c, "test")
        return tr, te
    return ev


def m(trades, pnl):
    return dict(trades=trades, pnl_usd=pnl, pnl_pct=pnl / 10, win_rate=40.0, expectancy_pct=1.0, max_drawdown_pct=5.0)


def main():
    base = bt.Cfg()
    # --- the search only accepts variants that trade at least as often and make more money on TRAIN data
    def table(c, part):
        trades = 100 + (0 if c.take_profit_pct >= 100 else 0)
        pnl = 50 + (40 if c.take_profit_pct == 100 else 0) + (200 if c.stop_loss_pct == 15 else 0)
        if c.stop_loss_pct == 15:
            trades = 60                                 # tighter stop but far fewer trades: not "aggressive", rejected
        if c.trailing_stop_pct == 35:
            pnl += 10
        return m(trades, pnl)
    sweep.evaluate = fake_eval_factory(table)
    best, b_tr, b_te, tried = sweep.search([], base, 0, 1, 0.5, log=lambda *_: None)
    assert best.take_profit_pct == 100 and best.stop_loss_pct == base.stop_loss_pct, best
    assert best.trailing_stop_pct == 35 and tried > 10
    # --- a variant with too few trades never wins, even if it makes the most money
    sweep.evaluate = fake_eval_factory(lambda c, part: m(5 if c.take_profit_pct != base.take_profit_pct else 100,
                                                          1000 if c.take_profit_pct != base.take_profit_pct else 10))
    best, *_ = sweep.search([], base, 0, 1, 0.5, log=lambda *_: None)
    assert best == base
    # --- conversion to the live bot's settings (5-minute bars -> seconds)
    assert sweep.suggest(base, replace(base, take_profit_pct=100, max_hold_bars=3, require_green_bar=False)) == \
        {"take_profit_pct": 100, "max_hold_seconds": 900}

    # --- end to end decisions with a stub dataset
    rows = [("p%d" % i, None, [(0, 1, 1, 1, 1, 1), (300, 1, 1, 1, 1, 1)]) for i in range(30)]
    sweep.load = lambda data, c: (rows, 0, 1000)
    def run(train_gain, test_chosen, test_base, data_name="data"):
        def table(c, part):
            chosen = c.take_profit_pct == 100
            if part == "train":
                return m(100, 100 + (train_gain if chosen else 0))
            return m(100, test_chosen if chosen else test_base)
        sweep.evaluate = fake_eval_factory(table)
        out = os.path.join(tempfile.mkdtemp(), "r.json")
        with contextlib.redirect_stdout(io.StringIO()):
            sweep.main(["--data", os.path.join(tempfile.mkdtemp(), data_name), "--out", out])
        return json.load(open(out))
    r = run(50, 80, 10)                                   # better on train AND on unseen data
    assert r["held_up_on_unseen_data"] and r["suggested_settings"] == {"take_profit_pct": 100}, r
    r = run(50, -20, -40)                                 # better than before on test but still losing: not suggested
    assert not r["suggested_settings"] and not r["held_up_on_unseen_data"]
    r = run(50, 5, 10)                                    # looked good on train, worse on unseen data: overfit
    assert not r["suggested_settings"]
    r = run(50, 80, 10, data_name="synthetic")            # synthetic data never produces a suggestion
    assert r["synthetic"] and not r["suggested_settings"]

    # --- the dashboard offers only sane research settings
    import settings as ui_settings
    d = tempfile.mkdtemp()
    path = os.path.join(d, ui_settings.RESEARCH_FILE)
    good = {"held_up_on_unseen_data": True, "synthetic": False,
            "suggested_settings": {"take_profit_pct": 100, "buy_sol": 5, "max_open_positions": 50,
                                   "stop_loss_pct": 9999, "max_hold_seconds": 900, "bogus": 1, "min_mcap_sol": True}}
    json.dump(good, open(path, "w"))
    assert ui_settings.research_suggestion(path) == {"take_profit_pct": 100.0, "max_hold_seconds": 900}
    assert ui_settings.presets_for(os.path.join(d, "settings.json"))["research"] == {"take_profit_pct": 100.0, "max_hold_seconds": 900}
    json.dump({**good, "held_up_on_unseen_data": False}, open(path, "w"))
    assert ui_settings.research_suggestion(path) == {}
    open(path, "w").write("not json")
    assert ui_settings.research_suggestion(path) == {} and "research" not in ui_settings.presets_for(os.path.join(d, "s.json"))
    print("all sweep tests passed")


main()
