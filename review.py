"""
Self-review: after each bot session and every 24 hours, look at the trades the bot
made in that window and nudge the strategy if the numbers clearly say so.

    python review.py                 # review the last 24 hours, print it, change nothing
    python review.py --hours 72      # a longer window
    python review.py --apply         # also apply the changes to settings.json

Pure standard library, no network. The bot calls `run_review()` itself
(REVIEW_MODE=apply|suggest|off in .env, default apply).

Guard rails (do not weaken):
- It only touches exits and entry filters (TUNABLE below). Never trade size, max
  open trades, the daily loss cap, or live/paper mode.
- Every new value goes through settings.validate, so the dashboard's limits apply.
- Needs at least REVIEW_MIN_TRADES closed trades (default 20) in the window,
  makes at most 3 changes, and moves each one at most 25% at a time.
- After a change it waits for REVIEW_MIN_TRADES new trades before changing anything
  else, then judges it: if trades since then did clearly worse, the change is undone.
- Every review is appended to reviews.jsonl; "Undo last review" restores the
  settings from before it.
"""
import argparse
import csv
import io
import json
import os
import time

import settings as ui_settings

HERE = os.path.dirname(os.path.abspath(__file__))
TRADES = "trades.csv"
CONTEXT = "trade_context.csv"
LOG = "reviews.jsonl"
STATE = "review_state.json"

TUNABLE = ("take_profit_pct", "stop_loss_pct", "trailing_stop_pct", "max_hold_seconds",
           "min_mcap_sol", "max_mcap_sol", "min_unique_buyers", "min_net_inflow_sol")
MAX_STEP = 0.25          # at most 25% per review
MAX_CHANGES = 3
MIN_BUCKET = 8           # trades needed in a market-cap bucket before judging it


# ---------- data ----------
def parse_time(s: str) -> float | None:
    try:
        return time.mktime(time.strptime(s.strip(), "%Y-%m-%d %H:%M:%S"))
    except (ValueError, AttributeError):
        return None


def load_csv(path: str) -> list[dict]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            text = f.read().replace("\x00", "")                       # a crash mid-write can leave NUL bytes
        return list(csv.DictReader(io.StringIO(text)))
    except (OSError, csv.Error):
        return []


def num(row, key, default=0.0):
    try:
        return float(row.get(key) or default)
    except (TypeError, ValueError):
        return default


def exit_kind(reason: str) -> str:
    r = (reason or "").lower()
    for word, kind in (("take profit", "tp"), ("stop loss", "sl"), ("trailing", "trail"),
                       ("max hold", "timeout"), ("manual", "manual")):
        if word in r:
            return kind
    return "copy" if "sold" in r or "wallet" in r else "other"


def load_trades(root: str, start: float, end: float, mode: str, files: tuple = ()) -> list[dict]:
    """Closed trades in [start, end] for this mode, joined with their entry context if logged.
    files = (trades_csv, context_csv) to override the default names in root."""
    trades_path, ctx_path = files or (os.path.join(root, TRADES), os.path.join(root, CONTEXT))
    ctx = {}
    for c in load_csv(ctx_path):
        t = parse_time(c.get("time", ""))
        if t is not None:
            ctx.setdefault(c.get("mint", ""), []).append((t, c))
    out = []
    for r in load_csv(trades_path):
        t = parse_time(r.get("time", ""))
        if t is None or not (start <= t <= end) or (mode and r.get("mode") != mode):
            continue
        tr = dict(time=t, symbol=r.get("symbol", ""), mint=r.get("mint", ""), source=r.get("source", ""),
                  sol_in=num(r, "sol_in"), pnl=num(r, "pnl"), pct=num(r, "pct"), held_s=num(r, "held_s"),
                  kind=exit_kind(r.get("reason", "")))
        opened = t - tr["held_s"]
        best = None
        for ct, c in ctx.get(tr["mint"], []):
            if ct <= t + 1 and (best is None or abs(ct - opened) < abs(best[0] - opened)):
                best = (ct, c)
        if best:
            c = best[1]
            tr.update(mcap=num(c, "mcap_sol", None), buyers=num(c, "buyers", None), inflow=num(c, "net_inflow_sol", None))
        out.append(tr)
    return out


# ---------- analysis ----------
def stats(trades: list[dict]) -> dict:
    n = len(trades)
    if not n:
        return dict(n=0)
    wins = [t for t in trades if t["pnl"] > 0]
    by = {}
    for t in trades:
        b = by.setdefault(t["kind"], dict(n=0, pnl=0.0, pct=0.0))
        b["n"] += 1
        b["pnl"] += t["pnl"]
        b["pct"] += t["pct"]
    for b in by.values():
        b["avg_pct"] = round(b["pct"] / b["n"], 1)
        b["share"] = round(b["n"] / n, 2)
        b["pnl"] = round(b["pnl"], 4)
        del b["pct"]
    launch = [t for t in trades if t["source"] == "launch"]
    copy_ = [t for t in trades if t["source"] != "launch"]
    return dict(n=n, win_rate=round(len(wins) / n, 3), pnl=round(sum(t["pnl"] for t in trades), 4),
                avg_pct=round(sum(t["pct"] for t in trades) / n, 2),
                avg_win=round(sum(t["pct"] for t in wins) / len(wins), 1) if wins else 0,
                avg_loss=round(sum(t["pct"] for t in trades if t["pnl"] <= 0) / max(1, n - len(wins)), 1),
                exits=by, launch=dict(n=len(launch), pnl=round(sum(t["pnl"] for t in launch), 4)),
                copy=dict(n=len(copy_), pnl=round(sum(t["pnl"] for t in copy_), 4)))


def mcap_buckets(trades: list[dict]) -> list[dict]:
    """Launch trades with a known entry market cap, split into thirds by market cap."""
    xs = sorted((t for t in trades if t["source"] == "launch" and t.get("mcap")), key=lambda t: t["mcap"])
    if len(xs) < 3 * MIN_BUCKET:
        return []
    k = len(xs) // 3
    parts = [xs[:k], xs[k:2 * k], xs[2 * k:]]
    return [dict(lo=p[0]["mcap"], hi=p[-1]["mcap"], n=len(p), avg_pct=sum(t["pct"] for t in p) / len(p)) for p in parts]


def _step(old: float, factor: float) -> float:
    factor = max(1 - MAX_STEP, min(1 + MAX_STEP, factor))
    return old * factor


def propose(cfg, trades: list[dict]) -> list[dict]:
    """Rule-based changes, each with the numbers behind it. Empty list = leave it alone."""
    s = stats(trades)
    if not s.get("n"):
        return []
    ex = s["exits"]
    share = lambda k: ex.get(k, {}).get("share", 0)
    avg = lambda k: ex.get(k, {}).get("avg_pct", 0)
    out = []

    def add(key, new, why):
        old = getattr(cfg, key)
        if isinstance(old, int) and not isinstance(old, bool):
            new = int(round(new))
        else:
            new = round(new, 2)
        if new != old and key in TUNABLE and all(c["key"] != key for c in out):
            out.append(dict(key=key, old=old, new=new, why=why))

    # trades that run out the clock while losing: get out sooner, free the slot
    if share("timeout") >= 0.40 and avg("timeout") < 0:
        add("max_hold_seconds", max(60, _step(cfg.max_hold_seconds, 0.8)),
            f"{share('timeout'):.0%} of trades hit the max hold time, averaging {avg('timeout'):+.1f}%")
    # trades that are still winning when the clock runs out: give them longer
    elif share("timeout") >= 0.30 and avg("timeout") >= 5:
        add("max_hold_seconds", _step(cfg.max_hold_seconds, 1.2),
            f"{share('timeout'):.0%} of trades hit the max hold time while up {avg('timeout'):+.1f}% on average")
    # mostly stopped out: entries are too loose
    if share("sl") >= 0.50:
        add("min_unique_buyers", cfg.min_unique_buyers + max(1, round(cfg.min_unique_buyers * 0.15)),
            f"{share('sl'):.0%} of trades were stopped out: ask for more buyers before entering")
        add("min_net_inflow_sol", max(0.1, _step(max(cfg.min_net_inflow_sol, 0.1), 1.2)),
            f"{share('sl'):.0%} of trades were stopped out: ask for more net buying before entering")
    # winners often reach the target: let them run further
    if share("tp") >= 0.25 and s["pnl"] > 0:
        add("take_profit_pct", _step(cfg.take_profit_pct, 1.2),
            f"{share('tp'):.0%} of trades reached the take-profit; raising it lets winners run")
    # trailing stop shakes out tiny gains: loosen it a bit
    if cfg.trailing_stop_pct > 0 and share("trail") >= 0.30 and avg("trail") < 10:
        add("trailing_stop_pct", min(50, cfg.trailing_stop_pct + 5),
            f"{share('trail'):.0%} of trades left on the trailing stop with only {avg('trail'):+.1f}% on average")
    # a market-cap third that clearly loses while the rest make money
    b = mcap_buckets(trades)
    if b:
        rest_lo = (b[1]["avg_pct"] + b[2]["avg_pct"]) / 2
        rest_hi = (b[0]["avg_pct"] + b[1]["avg_pct"]) / 2
        if b[0]["avg_pct"] < -5 and rest_lo > 0:
            add("min_mcap_sol", min(_step(max(cfg.min_mcap_sol, 1), 1 + MAX_STEP), b[0]["hi"]),
                f"entries under {b[0]['hi']:.0f} SOL market cap averaged {b[0]['avg_pct']:+.1f}% ({b[0]['n']} trades)")
        elif b[2]["avg_pct"] < -5 and rest_hi > 0:
            add("max_mcap_sol", max(_step(cfg.max_mcap_sol, 1 - MAX_STEP), b[2]["lo"]),
                f"entries over {b[2]['lo']:.0f} SOL market cap averaged {b[2]['avg_pct']:+.1f}% ({b[2]['n']} trades)")
    return out[:MAX_CHANGES]


def check(cfg, changes: list[dict]) -> tuple[list[dict], list[str]]:
    """Run the proposals through the dashboard's own validation (bounds + cross-field rules)."""
    ok = []
    for c in changes:
        f = ui_settings.BY_KEY[c["key"]]
        clean, errors = ui_settings.validate(cfg, {c["key"]: c["new"] * f.get("scale", 1)})
        if not errors:
            ok.append(c)
    disp = {c["key"]: c["new"] * ui_settings.BY_KEY[c["key"]].get("scale", 1) for c in ok}
    clean, errors = ui_settings.validate(cfg, disp)
    return (ok, []) if not errors else ([], errors)


# ---------- state ----------
def load_state(root: str) -> dict:
    try:
        return json.load(open(os.path.join(root, STATE), encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_state(root: str, st: dict):
    p = os.path.join(root, STATE)
    with open(p + ".tmp", "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)
    os.replace(p + ".tmp", p)


def append_log(root: str, rec: dict):
    with open(os.path.join(root, LOG), "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def last_review(root: str) -> dict | None:
    try:
        lines = open(os.path.join(root, LOG), encoding="utf-8").read().splitlines()
    except OSError:
        return None
    for ln in reversed(lines):
        try:
            return json.loads(ln)
        except ValueError:
            continue
    return None


def judge_last_change(root: str, st: dict, mode: str, now: float, min_trades: int, files: tuple = ()) -> dict | None:
    """Undo the previous change if trades made after it did clearly worse than the ones before it."""
    lc = st.get("last_change")
    if not lc or lc.get("judged"):
        return None
    after = load_trades(root, lc["at"], now, mode, files)
    if len(after) < min_trades:
        return None
    lc["judged"] = True
    a, b = stats(after)["avg_pct"], lc.get("baseline_avg_pct", 0)
    lc["after_avg_pct"] = a
    if a < b - 5 and a < 0:
        return dict(revert=lc["before"], why=f"since the last change trades averaged {a:+.1f}% vs {b:+.1f}% before it")
    return None


# ---------- the review ----------
def run_review(cfg, root: str = HERE, settings_path: str = "", hours: float | None = None,
               start: float | None = None, now: float | None = None, mode: str = "paper",
               apply: bool = False, min_trades: int = 20, label: str = "24h", files: tuple = ()) -> dict:
    """Review a window, optionally change cfg (and save settings.json). Returns the review record."""
    now = now or time.time()
    start = start if start is not None else now - (hours or 24) * 3600
    trades = load_trades(root, start, now, mode, files)
    s = stats(trades)
    st = load_state(root)
    rec = dict(ts=int(now), window=label, start=int(start), mode=mode, stats=s, changes=[], reverted=None,
               applied=False, notes=[])
    before = {k: getattr(cfg, k) for k in TUNABLE}

    verdict = judge_last_change(root, st, mode, now, min_trades, files)
    if verdict:
        rec["reverted"] = verdict["why"]
        rec["changes"] = [dict(key=k, old=getattr(cfg, k), new=v, why="undo: " + verdict["why"])
                          for k, v in verdict["revert"].items() if getattr(cfg, k) != v]
    elif st.get("last_change") and not st["last_change"].get("judged"):
        n_after = len(load_trades(root, st["last_change"]["at"], now, mode, files))
        rec["notes"].append(f"Waiting for {min_trades - n_after} more trades to judge the last change "
                            "before changing anything else.")
    elif s.get("n", 0) < min_trades:
        rec["notes"].append(f"{s.get('n', 0)} closed trades in this window; need {min_trades} before changing anything.")
    else:
        changes, errors = check(cfg, propose(cfg, trades))
        rec["changes"] = changes
        if errors:
            rec["notes"].append("Proposed changes failed validation: " + "; ".join(errors))
        if not changes:
            rec["notes"].append("Nothing clearly needs changing.")

    if apply and rec["changes"]:
        for c in rec["changes"]:
            setattr(cfg, c["key"], c["new"])
        if settings_path:
            ui_settings.save(settings_path, cfg)
        rec["applied"] = True
        rec["before"] = before
        if rec["reverted"]:
            st.pop("last_change", None)
        else:
            st["last_change"] = dict(at=now, before={c["key"]: c["old"] for c in rec["changes"]},
                                     baseline_avg_pct=s.get("avg_pct", 0), judged=False)
    elif st.get("last_change") and verdict is None and st["last_change"].get("judged"):
        st.pop("last_change", None)
    if label == "24h" or rec["applied"]:
        st["last_review_ts"] = now
    save_state(root, st)
    append_log(root, rec)
    return rec


def undo_last(cfg, root: str = HERE, settings_path: str = "") -> list[str]:
    """Restore the settings from before the last applied review. Returns change notes."""
    try:
        lines = open(os.path.join(root, LOG), encoding="utf-8").read().splitlines()
    except OSError:
        return []
    for ln in reversed(lines):
        try:
            rec = json.loads(ln)
        except ValueError:
            continue
        if rec.get("applied") and rec.get("before") and not rec.get("undone"):
            disp = {k: v * ui_settings.BY_KEY[k].get("scale", 1) for k, v in rec["before"].items() if k in TUNABLE}
            clean, errors = ui_settings.validate(cfg, disp)
            if errors:
                return []
            notes = ui_settings.apply(cfg, clean)
            if settings_path:
                ui_settings.save(settings_path, cfg)
            st = load_state(root)
            st.pop("last_change", None)
            save_state(root, st)
            append_log(root, dict(ts=int(time.time()), window="undo", undone=True, of=rec["ts"], applied=True,
                                  changes=[], notes=["Undid the review from " + time.strftime(
                                      "%Y-%m-%d %H:%M", time.localtime(rec["ts"]))], stats={}))
            return notes
    return []


def summary(rec: dict) -> str:
    s = rec.get("stats") or {}
    head = (f"Review ({rec.get('window')}): {s.get('n', 0)} trades" +
            (f", win rate {s['win_rate']:.0%}, {s['pnl']:+.4f} SOL, avg {s['avg_pct']:+.1f}%" if s.get("n") else ""))
    lines = [head]
    for c in rec.get("changes", []):
        f = ui_settings.BY_KEY.get(c["key"], {})
        sc = f.get("scale", 1)
        lines.append(f"  {f.get('label', c['key'])}: {c['old'] * sc:g} → {c['new'] * sc:g}{f.get('unit') and ' ' + f.get('unit')}"
                     f"  ({c['why']})")
    lines += ["  " + n for n in rec.get("notes", [])]
    if rec.get("changes"):
        lines.append("  applied" if rec.get("applied") else "  (suggestion only, not applied)")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--mode", default="paper", choices=["paper", "live"])
    ap.add_argument("--apply", action="store_true", help="write the changes to settings.json")
    ap.add_argument("--min-trades", type=int, default=int(os.getenv("REVIEW_MIN_TRADES", "20")))
    a = ap.parse_args(argv)
    from strategy import Config
    cfg = Config()
    path = os.path.join(HERE, "settings.json")
    for k, v in ui_settings.load(path).items():
        setattr(cfg, k, v)
    rec = run_review(cfg, HERE, path if a.apply else "", hours=a.hours, mode=a.mode, apply=a.apply,
                     min_trades=a.min_trades, label=f"{a.hours:g}h" if a.hours != 24 else "24h")
    print(summary(rec))
    if a.apply and rec["applied"]:
        print("Saved to settings.json. A running bot picks this up on its next restart "
              "(or let the bot run its own reviews: REVIEW_MODE=apply).")


if __name__ == "__main__":
    main()
