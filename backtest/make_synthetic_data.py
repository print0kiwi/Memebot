"""
Generate FAKE meme coin candles in the same format as download_data.py, for testing
the backtester without internet. Results on this data mean nothing about real markets.

  python make_synthetic_data.py --out synthetic --pools 800
"""
import argparse
import csv
import math
import os
import random
from datetime import datetime, timezone

BAR = 300


def make_pool(rng, launch_ts, end_ts):
    """pump.fun-style life: launch near $5k mcap, most die, a few run."""
    fate = rng.random()
    if fate < 0.70:
        life, drift, vol = rng.randint(3, 60), -0.04, 0.25        # quick death
    elif fate < 0.93:
        life, drift, vol = rng.randint(60, 2000), -0.002, 0.10    # slow bleed / chop
    else:
        life, drift, vol = rng.randint(500, 8000), 0.004, 0.12    # runner
    price = rng.uniform(4000, 9000) / 1e9
    base_vol = rng.uniform(800, 6000)
    rows = []
    ts = launch_ts
    for i in range(life):
        if ts >= end_ts:
            break
        hype = math.exp(-i / rng.uniform(5, 40))
        r = rng.gauss(drift + 0.08 * hype * (1 if fate >= 0.7 else 0.3), vol * (0.5 + hype))
        if fate < 0.70 and rng.random() < 0.06:
            r = -rng.uniform(0.6, 0.95) if r > -0.6 else r   # rug candle
        o = price
        c = max(o * math.exp(r), 1e-12)
        h = max(o, c) * math.exp(abs(rng.gauss(0, vol / 2)))
        l = min(o, c) * math.exp(-abs(rng.gauss(0, vol / 2)))
        v = base_vol * (1 + 8 * hype) * math.exp(rng.gauss(0, 0.5))
        if rng.random() > 0.15 or i < 3:  # thin tokens skip candles with no trades
            rows.append((ts, o, h, l, c, round(v, 2)))
        price = c
        ts += BAR
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="synthetic")
    ap.add_argument("--pools", type=int, default=800)
    ap.add_argument("--start", default="2025-01-01")
    ap.add_argument("--end", default="2026-10-27")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    rng = random.Random(a.seed)
    lo = int(datetime.fromisoformat(a.start).replace(tzinfo=timezone.utc).timestamp())
    hi = int(datetime.fromisoformat(a.end).replace(tzinfo=timezone.utc).timestamp())
    os.makedirs(os.path.join(a.out, "ohlcv"), exist_ok=True)
    meta = []
    for n in range(a.pools):
        launch = lo + (rng.randrange(0, hi - lo) // BAR) * BAR
        pool = f"SYN{n:05d}"
        rows = make_pool(rng, launch, hi)
        with open(os.path.join(a.out, "ohlcv", f"{pool}.csv"), "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ts", "open", "high", "low", "close", "volume_usd"])
            w.writerows(rows)
        meta.append(dict(pool=pool, name=f"FAKE{n}/SOL", symbol=f"FAKE{n}", base_token="", dex="pump-fun",
                         created_at=datetime.fromtimestamp(launch, timezone.utc).isoformat(), reserve_usd=""))
    with open(os.path.join(a.out, "pools.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(meta[0].keys()))
        w.writeheader()
        w.writerows(meta)
    print(f"wrote {a.pools} fake pools to {a.out}/")


if __name__ == "__main__":
    main()
