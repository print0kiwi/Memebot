"""
Backtest the meme coin bot's strategy on 5-minute candles, with realistic costs.

  python backtest.py                                  # 2025-01-01 -> today, bot strategy
  python backtest.py --start 2026-05-01               # different window
  python backtest.py --strategy momentum              # breakout strategy for older tokens
  python backtest.py --set take_profit_pct=100 --set position_usd=25

Costs modeled on every buy AND sell:
  - DEX swap fee + trading-platform/bot fee (% of trade)
  - fixed transaction cost (priority fee + Jito tip), in USD
  - base slippage + volatility slippage (fraction of the candle's high-low range)
  - price impact from pool liquidity (exact constant-product AMM formula)
  - 1-candle latency: signal on a candle's close, fill at the NEXT candle's open
  - stops that gap down fill at the open, not at the stop price
  - if both stop and target are inside one candle, the stop is assumed to hit first
  - if a token's candles stop (no more trades) while you hold it, exit with a big haircut

Pure standard library. Reads data/pools.csv and data/ohlcv/*.csv from download_data.py.
"""
import argparse
import csv
import html
import math
import os
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone

BAR = 300  # 5 minutes


@dataclass
class Cfg:
    # ---- window & portfolio ----
    start: str = "2025-01-01"
    end: str = ""                       # "" = today
    starting_capital_usd: float = 1000.0
    position_usd: float = 75.0          # per trade (0.5 SOL at $150, the bot's limit)
    max_positions: int = 5
    sol_price_usd: float = 150.0        # to convert the bot's SOL settings
    # ---- bot strategy (candle version of the live bot's launch filter) ----
    max_entry_bar: int = 0              # decide on launch candle #0 (first 5 min)
    min_first_bar_volume_usd: float = 1500.0
    require_green_bar: bool = True
    min_mcap_sol: float = 30.0
    max_mcap_sol: float = 120.0
    token_supply: float = 1_000_000_000  # pump.fun standard supply
    launch_tolerance_s: int = 900       # first candle must be within 15 min of pool creation
    # ---- momentum strategy ----
    lookback_bars: int = 12
    vol_mult: float = 3.0
    min_bar_volume_usd: float = 5000.0
    max_bar_gain_pct: float = 40.0
    # ---- exits (same as live bot) ----
    take_profit_pct: float = 60.0
    stop_loss_pct: float = 25.0
    trailing_stop_pct: float = 20.0
    trailing_arm_pct: float = 20.0      # trailing stop activates once up this much
    max_hold_bars: int = 1              # bot: 300 s = 1 candle
    # ---- execution & costs ----
    entry_delay_bars: int = 1
    swap_fee_pct: float = 1.0           # pump.fun curve 1%; AMMs 0.25–1%
    platform_fee_pct: float = 1.0       # Axiom/Photon/bot fee ~0.75–1%
    tx_cost_usd: float = 0.15           # priority fee + Jito tip, per transaction
    base_slippage_pct: float = 1.0
    range_slippage_frac: float = 0.25   # + this fraction of the candle's high-low range
    liq_volume_mult: float = 3.0        # liquidity ≈ this × avg 5-min volume
    liq_min_usd: float = 5000.0
    liq_max_usd: float = 2_000_000.0
    max_participation: float = 0.25     # skip if trade > 25% of the signal candle's volume
    stale_after_s: int = 86400          # candles end > 1 day before data end = token died
    dead_haircut_pct: float = 50.0      # exit loss applied when a token dies while held


def ts_of(s: str, end=False) -> int:
    if not s:
        return int(time.time())
    d = datetime.fromisoformat(s).replace(tzinfo=timezone.utc)
    return int(d.timestamp()) + (86400 if end else 0)


def load_pools(data: str) -> dict:
    path = os.path.join(data, "pools.csv")
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return {r["pool"]: r for r in csv.DictReader(f)}


def read_bars(path: str, lo: int, hi: int) -> list[tuple]:
    out = []
    with open(path) as f:
        next(f)
        for line in f:
            p = line.rstrip().split(",")
            ts = int(float(p[0]))
            if lo <= ts < hi:
                o, h, l, c, v = map(float, p[1:6])
                if o > 0 and h > 0 and l > 0 and c > 0:
                    out.append((ts, o, h, l, c, v))
    out.sort()
    return out


def created_ts(meta: dict) -> int | None:
    s = (meta or {}).get("created_at") or ""
    try:
        return int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()) if s else None
    except ValueError:
        return None


# ---------------- entries ----------------
def bot_entry(bars, i, c: Cfg, meta, lo) -> bool:
    if i > c.max_entry_bar:
        return False
    first = bars[0][0]
    created = created_ts(meta)
    if created is not None:
        if created < lo or first - created > c.launch_tolerance_s:
            return False       # launched before the window, or candles start late
    elif first <= lo + BAR:
        return False           # can't tell if this is really the launch
    ts, o, h, l, cl, v = bars[i]
    if v < c.min_first_bar_volume_usd:
        return False
    if c.require_green_bar and cl <= o:
        return False
    mcap_sol = cl * c.token_supply / c.sol_price_usd
    return c.min_mcap_sol <= mcap_sol <= c.max_mcap_sol


def momentum_entry(bars, i, c: Cfg, meta, lo) -> bool:
    n = c.lookback_bars
    if i < n:
        return False
    ts, o, h, l, cl, v = bars[i]
    window = bars[i - n:i]
    if v < c.min_bar_volume_usd or (cl / o - 1) * 100 > c.max_bar_gain_pct:
        return False
    return cl > max(b[2] for b in window) and v > c.vol_mult * (sum(b[5] for b in window) / n)


STRATEGIES = {"bot": bot_entry, "momentum": momentum_entry}


# ---------------- exits ----------------
def run_exit(bars, j, entry, c: Cfg, data_end):
    tp = entry * (1 + c.take_profit_pct / 100)
    sl = entry * (1 - c.stop_loss_pct / 100)
    peak = entry
    for k in range(j, len(bars)):
        ts, o, h, l, cl, v = bars[k]
        o = entry if k == j else o
        trail = peak * (1 - c.trailing_stop_pct / 100) if peak >= entry * (1 + c.trailing_arm_pct / 100) else 0
        stop = max(sl, trail)
        why = "trailing stop" if trail > sl else "stop loss"
        if o <= stop:
            return k, o, why + " (gapped)"
        if l <= stop:
            return k, stop, why
        if o >= tp:
            return k, o, "take profit"
        if h >= tp:
            return k, tp, "take profit"
        peak = max(peak, h)
        if k - j + 1 >= c.max_hold_bars:
            return k, cl, "max hold time"
    k = len(bars) - 1
    if bars[k][0] < data_end - c.stale_after_s:
        return k, bars[k][4] * (1 - c.dead_haircut_pct / 100), "token died while held"
    return k, bars[k][4], "end of data"


def rng_pct(bar) -> float:
    return (bar[2] - bar[3]) / bar[1] * 100


def scan_pool(pool, meta, bars, c: Cfg, entry_fn, lo, data_end) -> list[dict]:
    trades, i = [], 0
    while i < len(bars) - c.entry_delay_bars:
        if not entry_fn(bars, i, c, meta, lo):
            i += 1
            continue
        sig = bars[i]
        j = i + c.entry_delay_bars
        entry = bars[j][1]
        recent = bars[max(0, i - c.lookback_bars + 1): i + 1]
        avg_vol = sum(b[5] for b in recent) / len(recent)
        liq = min(max(c.liq_volume_mult * avg_vol, c.liq_min_usd), c.liq_max_usd)
        k, exit_raw, reason = run_exit(bars, j, entry, c, data_end)
        trades.append(dict(
            pool=pool, symbol=(meta or {}).get("symbol") or pool[:6],
            entry_ts=bars[j][0], exit_ts=bars[k][0] + BAR, entry_raw=entry, exit_raw=exit_raw,
            reason=reason, liq=liq, sig_vol=sig[5],
            slip_in=c.base_slippage_pct + c.range_slippage_frac * rng_pct(sig),
            slip_out=c.base_slippage_pct + c.range_slippage_frac * rng_pct(bars[k]),
            bars_held=k - j + 1))
        i = k + 1
    return trades


# ---------------- costs ----------------
def apply_costs(t: dict, size: float, c: Cfg) -> dict:
    fee = (c.swap_fee_pct + c.platform_fee_pct) / 100
    rq = t["liq"] / 2                                    # quote-side reserve
    fees_in = size * fee
    swap_in = max(size - fees_in - c.tx_cost_usd, 0)
    px_in = t["entry_raw"] * (1 + t["slip_in"] / 100) * (1 + swap_in / rq)
    tokens = swap_in / px_in
    value_out = tokens * t["exit_raw"]
    px_out = t["exit_raw"] * max(1 - t["slip_out"] / 100, 0) / (1 + value_out / rq)
    proceeds = tokens * px_out
    fees_out = proceeds * fee
    net = proceeds - fees_out - c.tx_cost_usd
    gross = size * (t["exit_raw"] / t["entry_raw"] - 1)
    pnl = net - size
    return dict(size=size, net_out=net, pnl=pnl, gross_pnl=gross, fees=fees_in + fees_out,
                tx=2 * c.tx_cost_usd, slippage=gross - pnl - (fees_in + fees_out) - 2 * c.tx_cost_usd)


# ---------------- portfolio ----------------
def portfolio(cands: list[dict], c: Cfg):
    cash = c.starting_capital_usd
    open_pos: list[dict] = []
    taken, skipped = [], {"max positions": 0, "not enough cash": 0, "too big for volume": 0}
    equity = [(cands[0]["entry_ts"] if cands else 0, cash)]
    for t in sorted(cands, key=lambda x: x["entry_ts"]):
        for p in sorted([p for p in open_pos if p["exit_ts"] <= t["entry_ts"]], key=lambda p: p["exit_ts"]):
            cash += p["net_out"]
            open_pos.remove(p)
            equity.append((p["exit_ts"], cash + sum(q["size"] for q in open_pos)))
        if len(open_pos) >= c.max_positions:
            skipped["max positions"] += 1
            continue
        size = min(c.position_usd, cash)
        if size < c.position_usd * 0.5:
            skipped["not enough cash"] += 1
            continue
        if size > c.max_participation * t["sig_vol"]:
            skipped["too big for volume"] += 1
            continue
        r = {**t, **apply_costs(t, size, c)}
        cash -= size
        open_pos.append(r)
        taken.append(r)
    for p in sorted(open_pos, key=lambda p: p["exit_ts"]):
        cash += p["net_out"]
        open_pos.remove(p)
        equity.append((p["exit_ts"], cash + sum(q["size"] for q in open_pos)))
    return taken, skipped, equity


# ---------------- reporting ----------------
def max_drawdown(equity):
    peak, mdd = -math.inf, 0.0
    for _, e in equity:
        peak = max(peak, e)
        mdd = max(mdd, (peak - e) / peak if peak > 0 else 0)
    return mdd * 100


def fmt_ts(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M")


def summarize(taken, skipped, equity, c: Cfg, stats):
    n = len(taken)
    pnl = sum(t["pnl"] for t in taken)
    gross = sum(t["gross_pnl"] for t in taken)
    wins = [t["pnl"] for t in taken if t["pnl"] > 0]
    losses = [t["pnl"] for t in taken if t["pnl"] <= 0]
    lines = [
        f"Period            {c.start} -> {c.end or 'today'}",
        f"Pools in window   {stats['pools_with_data']:,} of {stats['pool_files']:,} files"
        f"   ({stats['bars']:,} candles)",
        f"Signals           {stats['signals']:,}   taken {n:,}   skipped "
        + ", ".join(f"{v} {k}" for k, v in skipped.items() if v),
        "",
        f"Start capital     ${c.starting_capital_usd:,.2f}",
        f"End capital       ${c.starting_capital_usd + pnl:,.2f}",
        f"NET P&L           ${pnl:,.2f}   ({pnl / c.starting_capital_usd * 100:+.1f}%)",
        f"P&L before costs  ${gross:,.2f}   (what you'd make with zero fees/slippage)",
        "",
        "Costs paid",
        f"  fees            ${sum(t['fees'] for t in taken):,.2f}",
        f"  slippage+impact ${sum(t['slippage'] for t in taken):,.2f}",
        f"  tx costs        ${sum(t['tx'] for t in taken):,.2f}",
        "",
    ]
    if n:
        pf = sum(wins) / -sum(losses) if losses and sum(losses) < 0 else float("inf")
        lines += [
            f"Win rate          {len(wins) / n * 100:.1f}%   ({len(wins)} W / {len(losses)} L)",
            f"Avg win / loss    ${(sum(wins) / len(wins) if wins else 0):.2f} / "
            f"${(sum(losses) / len(losses) if losses else 0):.2f}",
            f"Profit factor     {pf:.2f}",
            f"Expectancy        ${pnl / n:.2f} per trade   ({sum(t['pnl'] / t['size'] for t in taken) / n * 100:+.2f}%)",
            f"Max drawdown      {max_drawdown(equity):.1f}%",
            f"Best / worst      ${max(t['pnl'] for t in taken):.2f} / ${min(t['pnl'] for t in taken):.2f}",
            "",
            "By exit reason    trades   win%     net P&L",
        ]
        by = {}
        for t in taken:
            by.setdefault(t["reason"].split(" (")[0], []).append(t["pnl"])
        for k, v in sorted(by.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"  {k:<22}{len(v):>5}  {sum(x > 0 for x in v) / len(v) * 100:5.0f}%  ${sum(v):>10,.2f}")
        lines += ["", "Month             trades     net P&L"]
        months = {}
        for t in taken:
            m = datetime.fromtimestamp(t["exit_ts"], timezone.utc).strftime("%Y-%m")
            months.setdefault(m, []).append(t["pnl"])
        for m in sorted(months):
            lines.append(f"  {m:<16}{len(months[m]):>6}  ${sum(months[m]):>10,.2f}")
    else:
        lines.append("No trades. Check the date window, data folder, and filters.")
    return "\n".join(lines), months if n else {}


def write_outputs(out, taken, equity, text, c: Cfg):
    os.makedirs(out, exist_ok=True)
    cols = ["symbol", "pool", "entry_time", "exit_time", "bars_held", "reason", "size",
            "entry_raw", "exit_raw", "gross_pnl", "fees", "slippage", "tx", "pnl", "pct"]
    with open(os.path.join(out, "trades.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for t in taken:
            w.writerow({**{k: t.get(k) for k in cols},
                        "entry_time": fmt_ts(t["entry_ts"]), "exit_time": fmt_ts(t["exit_ts"]),
                        "pct": round(t["pnl"] / t["size"] * 100, 2),
                        **{k: round(t[k], 4) for k in ("size", "gross_pnl", "fees", "slippage", "tx", "pnl")}})
    with open(os.path.join(out, "equity.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "equity_usd"])
        for ts, e in equity:
            w.writerow([fmt_ts(ts), round(e, 2)])
    with open(os.path.join(out, "report.html"), "w") as f:
        f.write(html_report(text, equity, c))


def html_report(text, equity, c: Cfg):
    W, H, P = 900, 280, 40
    svg = ""
    if len(equity) > 1:
        xs = [e[0] for e in equity]
        ys = [e[1] for e in equity]
        x0, x1 = min(xs), max(xs) or 1
        y0, y1 = min(ys + [c.starting_capital_usd]), max(ys + [c.starting_capital_usd])
        sx = lambda x: P + (x - x0) / max(x1 - x0, 1) * (W - 2 * P)
        sy = lambda y: H - P - (y - y0) / max(y1 - y0, 1e-9) * (H - 2 * P)
        pts = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in equity)
        base = sy(c.starting_capital_usd)
        svg = (f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="Equity curve">'
               f'<line x1="{P}" x2="{W - P}" y1="{base:.1f}" y2="{base:.1f}" class="base"/>'
               f'<polyline points="{pts}" class="eq"/>'
               f'<text x="{P}" y="{P - 12}" class="lbl">${y1:,.0f}</text>'
               f'<text x="{P}" y="{H - 12}" class="lbl">${y0:,.0f}</text>'
               f'<text x="{W - P}" y="{H - 12}" class="lbl" text-anchor="end">{fmt_ts(x1)[:10]}</text>'
               f'<text x="{P + 80}" y="{H - 12}" class="lbl">{fmt_ts(x0)[:10]}</text></svg>')
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Backtest Report</title>
<style>
:root{{--bg:#0f1115;--fg:#e6e6e6;--mut:#8a8f98;--line:#2a2e36;--eq:#4fd1a5}}
@media (prefers-color-scheme: light){{:root{{--bg:#fff;--fg:#14161a;--mut:#6b7079;--line:#e3e5e8;--eq:#0f9d6e}}}}
body{{background:var(--bg);color:var(--fg);font:14px/1.5 ui-monospace,Menlo,Consolas,monospace;margin:0;padding:24px 16px;max-width:960px;margin:auto}}
h1{{font:600 20px system-ui,sans-serif;margin:0 0 16px}}
.eq{{fill:none;stroke:var(--eq);stroke-width:2}} .base{{stroke:var(--line);stroke-dasharray:4 4}}
.lbl{{fill:var(--mut);font-size:12px}} pre{{white-space:pre-wrap;border-top:1px solid var(--line);padding-top:16px}}
</style></head><body><h1>Backtest Report</h1>{svg}<pre>{html.escape(text)}</pre></body></html>"""


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="data")
    ap.add_argument("--out", default="results")
    ap.add_argument("--strategy", choices=STRATEGIES, default="bot")
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="override any setting, e.g. --set stop_loss_pct=30")
    a = ap.parse_args()

    c = Cfg()
    types = {f.name: type(getattr(c, f.name)) for f in fields(Cfg)}
    for kv in a.set:
        k, v = kv.split("=", 1)
        if k not in types:
            raise SystemExit(f"unknown setting {k}. Options: {', '.join(types)}")
        setattr(c, k, v.lower() in ("1", "true", "yes") if types[k] is bool else types[k](v))
    c.start = a.start or c.start
    c.end = a.end or c.end
    lo, hi = ts_of(c.start), ts_of(c.end, end=True) if c.end else int(time.time())

    pools = load_pools(a.data)
    ohlcv_dir = os.path.join(a.data, "ohlcv")
    files = sorted(f for f in os.listdir(ohlcv_dir) if f.endswith(".csv")) if os.path.isdir(ohlcv_dir) else []
    if not files:
        raise SystemExit(f"No candle files in {ohlcv_dir}. Run download_data.py first.")

    data_end = 0
    for f in files:  # newest candle anywhere = "now" for the dataset
        with open(os.path.join(ohlcv_dir, f), "rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(fh.tell() - 200, 0))
            last = fh.read().decode().strip().splitlines()[-1].split(",")[0]
            if last.replace(".", "").isdigit():
                data_end = max(data_end, int(float(last)))
    data_end = min(data_end, hi)

    entry_fn = STRATEGIES[a.strategy]
    stats = dict(pool_files=len(files), pools_with_data=0, bars=0, signals=0)
    cands = []
    for n, f in enumerate(files, 1):
        pool = f[:-4]
        bars = read_bars(os.path.join(ohlcv_dir, f), lo, hi)
        if len(bars) < 2:
            continue
        stats["pools_with_data"] += 1
        stats["bars"] += len(bars)
        cands += scan_pool(pool, pools.get(pool), bars, c, entry_fn, lo, data_end)
        if n % 200 == 0:
            print(f"  scanned {n}/{len(files)} pools, {len(cands)} signals")
    stats["signals"] = len(cands)

    taken, skipped, equity = portfolio(cands, c)
    text, _ = summarize(taken, skipped, equity, c, stats)
    text = f"Strategy          {a.strategy}\n" + text
    write_outputs(a.out, taken, equity, text, c)
    print("\n" + text)
    print(f"\nSaved {a.out}/trades.csv, {a.out}/equity.csv, {a.out}/report.html")


if __name__ == "__main__":
    main()
