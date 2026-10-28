"""
Test aggressive variants of the bot's settings on downloaded market data and say which (if any) held up.

  python sweep.py --data data --start 2026-01-01          # after download_data.py
  python sweep.py --data synthetic --start 2025-01-01     # plumbing check only (fake data!)

How it avoids fooling you:
  * the data is split by time: variants are CHOSEN on the first 70% and JUDGED on the last 30%
  * "aggressive" is defined as trading at least as often as your current settings
  * a suggestion is only written if it also beats your current settings on the unseen 30% and is profitable there
  * everything uses the same cost model as backtest.py (fees, slippage, AMM impact, 1-candle latency)
Honest limits: 5-minute candles can't see the first seconds of a pump.fun launch, and the data only
contains tokens the data provider listed (survivors), so treat the result as a hint, not proof.

Writes research_result.json next to the bot's settings; the dashboard then offers it under "Load a profile".
Pure standard library.
"""
import argparse
import json
import os
import sys
import time
from dataclasses import fields, replace

import backtest as bt

HERE = os.path.dirname(os.path.abspath(__file__))
MIN_TRADES = 30                  # a variant needs at least this many trades in the training part to count
TRAIN_FRACTION = 0.7

# knobs tried, in the order they are tuned (exits first, then entries, then exits again)
GRID = {
    "take_profit_pct": [40, 60, 100, 150, 250],
    "stop_loss_pct": [15, 25, 35],
    "trailing_stop_pct": [15, 25, 35],
    "max_hold_bars": [1, 2, 3, 6, 12],
    "min_first_bar_volume_usd": [500, 1500, 3000],
    "require_green_bar": [True, False],
    "min_mcap_sol": [20, 30, 45],
    "max_mcap_sol": [120, 400, 1000],
}
ORDER = ["take_profit_pct", "stop_loss_pct", "trailing_stop_pct", "max_hold_bars",
         "min_first_bar_volume_usd", "require_green_bar", "min_mcap_sol", "max_mcap_sol",
         "take_profit_pct", "stop_loss_pct", "trailing_stop_pct", "max_hold_bars"]
# backtest settings that exist in the live bot (same name, or converted)
TO_BOT = {"take_profit_pct": ("take_profit_pct", 1), "stop_loss_pct": ("stop_loss_pct", 1),
          "trailing_stop_pct": ("trailing_stop_pct", 1), "min_mcap_sol": ("min_mcap_sol", 1),
          "max_mcap_sol": ("max_mcap_sol", 1), "max_hold_bars": ("max_hold_seconds", bt.BAR)}


def load(data: str, c: bt.Cfg):
    lo = bt.ts_of(c.start)
    hi = bt.ts_of(c.end, end=True) if c.end else int(time.time())
    pools = bt.load_pools(data)
    d = os.path.join(data, "ohlcv")
    files = sorted(f for f in os.listdir(d) if f.endswith(".csv")) if os.path.isdir(d) else []
    if not files:
        raise SystemExit(f"No candle files in {d}. Run download_data.py first.")
    rows, data_end = [], 0
    for f in files:
        bars = bt.read_bars(os.path.join(d, f), lo, hi)
        if len(bars) >= 2:
            rows.append((f[:-4], pools.get(f[:-4]), bars))
            data_end = max(data_end, bars[-1][0])
    return rows, lo, min(data_end, hi)


def candidates(rows, c: bt.Cfg, lo, data_end):
    out = []
    for pool, meta, bars in rows:
        out += bt.scan_pool(pool, meta, bars, c, bt.bot_entry, lo, data_end)
    return out


def metrics(cands, c: bt.Cfg) -> dict:
    taken, _, equity = bt.portfolio(cands, c)
    n = len(taken)
    pnl = sum(t["pnl"] for t in taken)
    wins = sum(t["pnl"] > 0 for t in taken)
    return dict(trades=n, pnl_usd=round(pnl, 2), pnl_pct=round(pnl / c.starting_capital_usd * 100, 2),
                win_rate=round(wins / n * 100, 1) if n else 0.0,
                expectancy_pct=round(sum(t["pnl"] / t["size"] for t in taken) / n * 100, 2) if n else 0.0,
                max_drawdown_pct=round(bt.max_drawdown(equity), 1) if n else 0.0)


def evaluate(rows, c, lo, data_end, split):
    cands = candidates(rows, c, lo, data_end)
    train = [t for t in cands if t["entry_ts"] < split]
    test = [t for t in cands if t["entry_ts"] >= split]
    return metrics(train, c), metrics(test, c)


def search(rows, base: bt.Cfg, lo, data_end, split, log=print):
    """Coordinate search on the training part. Returns (best_cfg, base_train, base_test, tried)."""
    base_tr, base_te = evaluate(rows, base, lo, data_end, split)
    log(f"  current settings: train {base_tr['trades']} trades {base_tr['pnl_pct']:+.1f}% | test {base_te['trades']} trades {base_te['pnl_pct']:+.1f}%")
    best, best_tr = base, base_tr
    tried = 0
    for knob in ORDER:
        for v in GRID[knob]:
            if getattr(best, knob) == v:
                continue
            cand = replace(best, **{knob: v})
            tr, _ = evaluate(rows, cand, lo, data_end, split)
            tried += 1
            # aggressive = trades at least as often as today, and enough trades to mean something
            if tr["trades"] >= max(MIN_TRADES, base_tr["trades"]) and tr["pnl_usd"] > best_tr["pnl_usd"]:
                best, best_tr = cand, tr
                log(f"  {knob} = {v}: train {tr['pnl_pct']:+.1f}% on {tr['trades']} trades")
    return best, base_tr, base_te, tried


def suggest(base: bt.Cfg, best: bt.Cfg) -> dict:
    out = {}
    for k, (bot_key, mult) in TO_BOT.items():
        if getattr(best, k) != getattr(base, k):
            v = getattr(best, k) * mult
            out[bot_key] = int(v) if bot_key == "max_hold_seconds" else v
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data")
    ap.add_argument("--start", default="2026-01-01")
    ap.add_argument("--end", default="")
    ap.add_argument("--out", default=os.path.join(HERE, "..", "research_result.json"))
    a = ap.parse_args(argv)

    base = bt.Cfg(start=a.start, end=a.end)
    rows, lo, data_end = load(a.data, base)
    if len(rows) < 20:
        raise SystemExit(f"Only {len(rows)} pools have candles in this window; too few to learn anything from.")
    split = lo + int((data_end - lo) * TRAIN_FRACTION)
    print(f"{len(rows):,} pools, {bt.fmt_ts(lo)[:10]} to {bt.fmt_ts(data_end)[:10]}; "
          f"choosing on data before {bt.fmt_ts(split)[:10]}, judging on what comes after")
    synthetic = "synth" in os.path.basename(os.path.normpath(a.data)).lower()
    if synthetic:
        print("  NOTE: this is SYNTHETIC data. Only the plumbing is being checked; ignore the numbers.")

    best, base_tr, base_te, tried = search(rows, base, lo, data_end, split)
    best_tr, best_te = evaluate(rows, best, lo, data_end, split)
    changes = suggest(base, best)
    holds = (best_te["pnl_usd"] > 0 and best_te["pnl_usd"] > base_te["pnl_usd"]
             and best_te["trades"] >= base_te["trades"] and bool(changes))
    result = dict(
        generated=time.strftime("%Y-%m-%d %H:%M:%S"), data_dir=os.path.abspath(a.data), synthetic=synthetic,
        window=dict(start=a.start, end=a.end or "today", train_until=bt.fmt_ts(split)[:10]),
        pools=len(rows), variants_tried=tried,
        current=dict(train=base_tr, test=base_te), chosen=dict(train=best_tr, test=best_te),
        changes=changes, held_up_on_unseen_data=holds and not synthetic,
        suggested_settings=changes if holds and not synthetic else {},
        not_in_live_bot={k: getattr(best, k) for k in ("min_first_bar_volume_usd", "require_green_bar")
                         if getattr(best, k) != getattr(base, k)},
        caveats=["5-minute candles cannot see the first seconds of a launch, which is when the live bot decides",
                 "only tokens the data provider listed are included, so dead-on-arrival coins are under-represented",
                 "costs are modelled, not measured; real fills in a fast pump are usually worse",
                 "a profitable test period can still be luck: paper-trade the settings before trusting them"])
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print()
    print(f"Current settings : test {base_te['trades']} trades, {base_te['pnl_pct']:+.1f}%, win rate {base_te['win_rate']}%")
    print(f"Chosen variant   : test {best_te['trades']} trades, {best_te['pnl_pct']:+.1f}%, win rate {best_te['win_rate']}%")
    print("Changes          :", ", ".join(f"{k}={v}" for k, v in changes.items()) or "none (nothing beat the current settings)")
    if result["suggested_settings"]:
        print("\nIt held up on the unseen data. In the dashboard: Strategy settings -> Edit -> Load a profile -> Research result.")
    elif synthetic:
        print("\nSynthetic data: nothing is suggested.")
    else:
        print("\nNo variant beat your current settings on the unseen data, so nothing is suggested. That is a real answer:"
              "\nit means loosening these knobs did not help in this data.")
    print(f"Saved {os.path.abspath(a.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
