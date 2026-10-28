"""
Which Discord servers and callers are actually worth following?

Reads what discord_signals.py recorded while the bot was running:
  discord_calls.csv      every pump.fun address posted, who posted it, where
  discord_outcomes.csv   what each called token's price did at +5m / +15m / +1h

    python analyze_discord.py                 # full report
    python analyze_discord.py --days 7
    python analyze_discord.py --min-calls 5   # ignore callers with fewer calls

Standard library only. "Median" is used instead of average because one 40x distorts an average
badly; the hit rate (% of calls up more than 50% within an hour) is usually the more honest number.
"""
import argparse
import csv
import os
import statistics
import time
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))


def read(path, days):
    p = path if os.path.isabs(path) else os.path.join(HERE, path)
    if not os.path.exists(p):
        return []
    cutoff = time.time() - days * 86400 if days else 0
    out = []
    for r in csv.DictReader(open(p, encoding="utf-8")):
        try:
            ts = time.mktime(time.strptime(r["time"], "%Y-%m-%d %H:%M:%S"))
        except (ValueError, KeyError):
            continue
        if ts >= cutoff:
            r["_ts"] = ts
            out.append(r)
    return out


def num(r, k):
    try:
        v = r.get(k, "")
        return float(v) if v not in ("", None) else None
    except ValueError:
        return None


def score(rows: list[dict]) -> dict:
    """Summarise a group of outcome rows."""
    r15 = [num(r, "ret15m") for r in rows]
    r15 = [v for v in r15 if v is not None]
    r60 = [num(r, "ret60m") for r in rows]
    r60 = [v for v in r60 if v is not None]
    peaks = [num(r, "peak_x") or 1 for r in rows]
    hits = sum(1 for p in peaks if p >= 1.5)
    rugs = sum(1 for v in r60 if v <= -60)
    return dict(n=len(rows),
                med15=statistics.median(r15) if r15 else None,
                med60=statistics.median(r60) if r60 else None,
                best=max(peaks) if peaks else None,
                hit=hits / len(peaks) * 100 if peaks else None,
                rug=rugs / len(r60) * 100 if r60 else None,
                grad=sum(int(r.get("graduated") or 0) for r in rows))


def fmt(s: dict) -> str:
    def pc(v, signed=True):
        if v is None:
            return "–"
        return f"{v:+.0f}%" if signed else f"{v:.0f}%"
    best = f"{s['best']:.1f}x" if s["best"] else "–"
    return (f"{s['n']:5}  {pc(s['med15']):>7}  {pc(s['med60']):>7}  "
            f"{pc(s['hit'], False):>6}  {pc(s['rug'], False):>5}  {best:>7}")


HEAD = f"{'calls':>5}  {'med15m':>7}  {'med1h':>7}  {'>+50%':>6}  {'rug':>5}  {'best':>7}"


def table(title, groups: dict, min_calls: int, limit=20):
    rows = [(k, score(v)) for k, v in groups.items() if len(v) >= min_calls]
    if not rows:
        print(f"\n{title}\n  (nothing with at least {min_calls} calls yet)")
        return
    rows.sort(key=lambda x: (-(x[1]["hit"] or 0), -(x[1]["med60"] or -999)))
    width = max(len(str(k)) for k, _ in rows[:limit])
    print(f"\n{title}\n  {'':{width}}  {HEAD}")
    for k, s in rows[:limit]:
        print(f"  {str(k):{width}}  {fmt(s)}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=float, default=0, help="only the last N days (0 = everything)")
    ap.add_argument("--min-calls", type=int, default=3, help="ignore groups with fewer calls than this")
    ap.add_argument("--calls", default="discord_calls.csv")
    ap.add_argument("--outcomes", default="discord_outcomes.csv")
    a = ap.parse_args()

    calls = read(a.calls, a.days)
    out = read(a.outcomes, a.days)
    if not calls and not out:
        print("No Discord data yet.\n"
              "Run the bot (./memebot paper) or `python discord_signals.py --watch` with "
              "DISCORD_BOT_TOKEN set and your servers listed in discord_servers.txt.")
        return

    span = ""
    if calls:
        span = (f"{time.strftime('%Y-%m-%d', time.localtime(min(r['_ts'] for r in calls)))}"
                f" → {time.strftime('%Y-%m-%d', time.localtime(max(r['_ts'] for r in calls)))}")
    print(f"\nDISCORD SIGNAL REPORT   {span}")
    print(f"{len(calls)} calls logged · {len(out)} followed to the 1-hour mark · "
          f"{len({r['mint'] for r in calls})} different tokens")
    print("\nmed15m/med1h = median % move after the call. >+50% = share that at least 1.5x'd. "
          "rug = share down 60%+ at 1h.")

    by_guild, by_author, by_hour = defaultdict(list), defaultdict(list), defaultdict(list)
    for r in out:
        by_guild[r.get("guild") or r.get("guild_id") or "?"].append(r)
        by_author[(r.get("first_author") or r.get("first_author_id") or "?")].append(r)
        by_hour[time.strftime("%H", time.localtime(r["_ts"]))].append(r)
    table("BY SERVER (who called it first)", by_guild, 1)
    table("BY CALLER (first to post the address)", by_author, a.min_calls)

    # how much consensus is worth: 1 caller vs several
    buckets = defaultdict(list)
    for r in out:
        n = num(r, "authors_5m") or 1
        buckets["1 caller" if n <= 1 else ("2-3 callers" if n <= 3 else "4+ callers")].append(r)
    table("BY HOW MANY PEOPLE CALLED IT (within 5 min)", buckets, 1)
    table("BY HOUR OF DAY (local)", by_hour, max(3, a.min_calls))

    if out:
        best = sorted(out, key=lambda r: -(num(r, "peak_x") or 0))[:10]
        print("\nBIGGEST MOVERS")
        print(f"  {'token':<12} {'peak':>6}  {'1h':>7}  server / caller")
        for r in best:
            h = num(r, "ret60m")
            print(f"  {r['mint'][:10]:<12} {num(r, 'peak_x') or 0:>5.1f}x  "
                  f"{(f'{h:+.0f}%' if h is not None else '–'):>7}  "
                  f"{r.get('guild', '?')} / {r.get('first_author', '?')}")

    # consensus right now (calls not yet followed to the end)
    recent = [r for r in calls if time.time() - r["_ts"] < 1800]
    if recent:
        hot = defaultdict(set)
        for r in recent:
            hot[r["mint"]].add(r.get("author_id") or r.get("author"))
        hot = sorted(hot.items(), key=lambda x: -len(x[1]))[:5]
        if hot and len(hot[0][1]) > 1:
            print("\nBEING CALLED RIGHT NOW (last 30 min)")
            for mint, who in hot:
                print(f"  {mint}  {len(who)} caller(s)")

    graded = sum(int(r.get("graduated") or 0) for r in out)
    if graded:
        print(f"\nNote: {graded} of these tokens left the pump.fun bonding curve during the hour. "
              "Their later price isn't captured here, so their real peak was probably higher.")
    print("\nTo act on this: set 'Min Discord callers' in the dashboard (Social group), and mute "
          "servers that score badly by removing their ID from discord_servers.txt.\n")


if __name__ == "__main__":
    main()
