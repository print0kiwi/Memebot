"""
Analyze tracked wallets: who actually makes money, who is copyable, and what signals work.

Data sources (use either or both):
  1. On-chain history via Helius (free key at helius.dev):
        python analyze_wallets.py --helius-key YOUR_KEY --days 30
  2. The bot's own live log (no key needed, grows while the bot runs):
        python analyze_wallets.py --bot-log wallet_trades.csv

Outputs (in wallet_analysis/):
  wallet_scorecard.csv   one row per wallet: P&L, win rate, hold time, copyability verdict
  consensus.csv          does it pay when several tracked wallets buy the same token?
  similar_wallets.csv    wallet pairs that trade the same tokens (same person / leader-follower)
  wallets/best_wallets.txt  copy candidates -> set COPY_WALLETS=wallets/best_wallets.txt

Pure standard library. Downloaded history is cached in cache/helius/, so re-runs are fast.
"""
import argparse
import csv
import json
import os
import statistics as st
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

from wallets import load_wallets

WSOL = "So11111111111111111111111111111111111111112"
STABLES = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",   # USDC
           "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}   # USDT
NOT_MEMES = {WSOL, *STABLES,
             "JUPyiwrYJFskUPiHa7hkeR8VUtAeFoSYbKedZNsDvCN",  # JUP
             "mSoLzYCxHdYgdzU16g5QSh3i5K3z3KZK7ytfqcJm7So",  # mSOL
             "J1toso1uCk3RLmjorhTtrVwY9HJ7X8V9yYac6Y7kGCPn",  # jitoSOL
             "bSo13r4TkiE4KumL71LsHTPpL2euBYLFx6h9HP3piy1"}   # bSOL
PUMP_SOURCES = {"PUMP_FUN", "PUMP_AMM", "PUMP_SWAP"}


# ---------------------------------------------------------------- fetching
class Helius:
    def __init__(self, key, host, per_sec):
        self.key, self.host, self.gap, self.last = key, host.rstrip("/"), 1.0 / per_sec, 0.0

    def history(self, wallet, before=None):
        q = {"api-key": self.key, "type": "SWAP", "limit": 100}
        if before:
            q["before"] = before
        url = f"{self.host}/v0/addresses/{wallet}/transactions?{urllib.parse.urlencode(q)}"
        for attempt in range(6):
            wait = self.gap - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            try:
                with urllib.request.urlopen(urllib.request.Request(
                        url, headers={"User-Agent": "wallet-analyzer/1.0"}), timeout=40) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                body = e.read()[:200].decode(errors="ignore")
                if e.code in (401, 403):
                    sys.exit(f"Helius refused the key ({e.code}): {body}")
                if e.code == 404:
                    return []
                time.sleep(3 * (attempt + 1) if e.code != 429 else 10 * (attempt + 1))
            except (urllib.error.URLError, TimeoutError):
                time.sleep(3 * (attempt + 1))
        print(f"   giving up on a page for {wallet[:6]}")
        return None


def fetch_wallet(api: Helius, wallet, cutoff, max_pages, cache_dir) -> list[dict]:
    path = os.path.join(cache_dir, f"{wallet}.json")
    if os.path.exists(path):
        cached = json.load(open(path))
        if cached.get("cutoff", 1e18) <= cutoff and time.time() - cached.get("fetched", 0) < 6 * 3600:
            return cached["txs"]
    txs, before = [], None
    for _ in range(max_pages):
        page = api.history(wallet, before)
        if not page:
            break
        txs += page
        before = page[-1].get("signature")
        if page[-1].get("timestamp", 0) < cutoff or len(page) < 100:
            break
    json.dump({"fetched": time.time(), "cutoff": cutoff, "txs": txs}, open(path, "w"))
    return txs


# ---------------------------------------------------------------- parsing
def parse_swap(tx: dict, w: str, sol_price: float):
    """One Helius-parsed transaction -> (ts, mint, side, sol, tokens, source) or None."""
    if tx.get("transactionError"):
        return None
    sol = None
    for ad in tx.get("accountData") or []:
        if ad.get("account") == w:
            sol = ad.get("nativeBalanceChange", 0) / 1e9
    if sol is None:
        sol = sum((n.get("amount", 0) if n.get("toUserAccount") == w else 0)
                  - (n.get("amount", 0) if n.get("fromUserAccount") == w else 0)
                  for n in tx.get("nativeTransfers") or []) / 1e9
    deltas = defaultdict(float)
    for t in tx.get("tokenTransfers") or []:
        amt = float(t.get("tokenAmount") or 0)
        if t.get("toUserAccount") == w:
            deltas[t.get("mint")] += amt
        if t.get("fromUserAccount") == w:
            deltas[t.get("mint")] -= amt
    wsol = deltas.pop(WSOL, 0.0)
    usd = sum(deltas.pop(m, 0.0) for m in STABLES)
    if abs(wsol) > abs(sol):              # traded via wrapped SOL
        sol = wsol
    if abs(sol) < 0.002 and usd:          # stablecoin-quoted swap
        sol = usd / sol_price
    memes = {m: d for m, d in deltas.items() if m and m not in NOT_MEMES and abs(d) > 0}
    if len(memes) != 1 or abs(sol) < 1e-6:
        return None                        # multi-token route, transfer, or token-for-token
    (mint, d), = memes.items()
    if d > 0 and sol < 0:
        side = "buy"
    elif d < 0 and sol > 0:
        side = "sell"
    else:
        return None
    return dict(ts=int(tx.get("timestamp", 0)), mint=mint, side=side, sol=abs(sol),
                tokens=abs(d), source=tx.get("source", ""))


def fills_from_bot_log(path: str) -> dict[str, list]:
    out = defaultdict(list)
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                ts = int(datetime.strptime(r["time"], "%Y-%m-%d %H:%M:%S").timestamp())
                tokens = float(r.get("tokens") or 0)
                if not tokens and r.get("price_sol"):
                    tokens = float(r["sol"]) / float(r["price_sol"])
                out[r["wallet"]].append(dict(ts=ts, mint=r["mint"], side=r["side"], sol=float(r["sol"]),
                                             tokens=tokens, source=(r.get("pool") or "").upper()))
            except (KeyError, ValueError, ZeroDivisionError):
                continue
    return out


# ---------------------------------------------------------------- analysis
def build_positions(fills: list[dict]) -> dict[str, dict]:
    pos = {}
    for f in sorted(fills, key=lambda x: x["ts"]):
        p = pos.get(f["mint"])
        if p is None:
            if f["side"] != "buy":
                continue                  # bought before our window: incomplete, skip
            p = pos[f["mint"]] = dict(mint=f["mint"], bought=0.0, sold=0.0, sol_in=0.0, sol_out=0.0,
                                      first_buy=f["ts"], last_sell=None, last_ts=f["ts"], pump=False)
        if f["side"] == "buy":
            p["bought"] += f["tokens"]
            p["sol_in"] += f["sol"]
        else:
            p["sold"] += f["tokens"]
            p["sol_out"] += f["sol"]
            p["last_sell"] = f["ts"]
        p["last_ts"] = f["ts"]
        p["pump"] |= f["source"].upper() in PUMP_SOURCES or "PUMP" in f["source"].upper()
    for p in pos.values():
        frac = min(p["sold"] / p["bought"], 1.0) if p["bought"] else 0
        p["frac_sold"] = frac
        p["status"] = "closed" if frac >= 0.9 else ("holding" if frac < 0.1 else "partial")
        p["realized"] = p["sol_out"] - p["sol_in"] * frac
        p["roi"] = p["sol_out"] / p["sol_in"] - 1 if p["status"] == "closed" and p["sol_in"] else None
        p["hold_s"] = (p["last_sell"] - p["first_buy"]) if p["last_sell"] else None
    return pos


def score_wallet(addr, name, fills, pos, a, now) -> dict:
    closed = [p for p in pos.values() if p["status"] == "closed"]
    rois = [p["roi"] for p in closed]
    holds = [p["hold_s"] for p in closed if p["hold_s"] is not None]
    buys = [f["sol"] for f in fills if f["side"] == "buy"]
    realized = sum(p["realized"] for p in pos.values())
    days = {datetime.fromtimestamp(f["ts"], timezone.utc).date() for f in fills}
    last = max((f["ts"] for f in fills), default=0)
    copy_rois = [r - a.copy_penalty for r in rois]
    r = dict(
        name=name, wallet=addr, swaps=len(fills), tokens=len(pos), closed=len(closed),
        holding=sum(p["status"] == "holding" for p in pos.values()),
        win_rate=round(sum(x > 0 for x in rois) / len(rois) * 100, 1) if rois else "",
        realized_pnl_sol=round(realized, 3),
        avg_roi_pct=round(st.mean(rois) * 100, 1) if rois else "",
        median_roi_pct=round(st.median(rois) * 100, 1) if rois else "",
        est_copy_roi_pct=round(st.mean(copy_rois) * 100, 1) if copy_rois else "",
        big_wins_2x=sum(x >= 1 for x in rois),
        median_buy_sol=round(st.median(buys), 3) if buys else "",
        median_hold_min=round(st.median(holds) / 60, 1) if holds else "",
        fast_flip_pct=round(sum(h < 60 for h in holds) / len(holds) * 100, 1) if holds else "",
        pump_fun_pct=round(sum(p["pump"] for p in pos.values()) / len(pos) * 100) if pos else "",
        trades_per_day=round(len(fills) / max(len(days), 1), 1),
        active_days=len(days),
        last_active=datetime.fromtimestamp(last, timezone.utc).strftime("%Y-%m-%d") if last else "",
    )
    if not fills:
        v = "no data"
    elif now - last > a.inactive_days * 86400:
        v = "inactive"
    elif len(closed) < a.min_closed:
        v = "not enough trades yet"
    elif realized <= 0:
        v = "losing money"
    elif (holds and st.median(holds) < a.min_hold_s) or (r["fast_flip_pct"] or 0) > 40:
        v = "profitable but too fast to copy"
    elif st.mean(copy_rois) <= 0:
        v = "profitable but not after copy lag"
    else:
        v = "COPY CANDIDATE"
    r["verdict"] = v
    r["score"] = round(realized * min(1.0, len(closed) / 30), 3) if v == "COPY CANDIDATE" else ""
    return r


def consensus(all_pos: dict, window_s: int):
    """Group each token's tracked-wallet buys; does crowd size predict their ROI?"""
    by_mint = defaultdict(list)
    for w, pos in all_pos.items():
        for p in pos.values():
            by_mint[p["mint"]].append((p["first_buy"], w, p))
    rows, buckets = [], defaultdict(list)
    for mint, entries in by_mint.items():
        entries.sort()
        t0 = entries[0][0]
        crowd = [e for e in entries if e[0] - t0 <= window_s]
        rois = [e[2]["roi"] for e in crowd if e[2]["roi"] is not None]
        k = len(crowd)
        bucket = "1" if k == 1 else "2" if k == 2 else "3-4" if k <= 4 else "5+"
        buckets[bucket] += rois
        if k >= 3:
            rows.append(dict(mint=mint, wallets_in_window=k, total_tracked_buyers=len(entries),
                             first_buy=datetime.fromtimestamp(t0, timezone.utc).strftime("%Y-%m-%d %H:%M"),
                             first_buyer=entries[0][1], avg_roi_pct=round(st.mean(rois) * 100, 1) if rois else "",
                             closed_positions=len(rois)))
    rows.sort(key=lambda r: -r["wallets_in_window"])
    summary = {b: (len(v), st.mean(v) * 100 if v else 0, sum(x > 0 for x in v) / len(v) * 100 if v else 0)
               for b, v in buckets.items()}
    return rows, summary


def similar(all_pos: dict, names: dict, min_shared=5, min_jaccard=0.3):
    sets = {w: {m: p["first_buy"] for m, p in pos.items()} for w, pos in all_pos.items() if len(pos) >= 10}
    ws, rows = list(sets), []
    for i in range(len(ws)):
        for j in range(i + 1, len(ws)):
            a, b = sets[ws[i]], sets[ws[j]]
            shared = a.keys() & b.keys()
            if len(shared) < min_shared:
                continue
            jac = len(shared) / len(a.keys() | b.keys())
            if jac < min_jaccard:
                continue
            lead = st.median(b[m] - a[m] for m in shared)  # >0: A buys first
            leader, follower = (ws[i], ws[j]) if lead >= 0 else (ws[j], ws[i])
            rows.append(dict(wallet_a=names.get(ws[i]) or ws[i], wallet_b=names.get(ws[j]) or ws[j],
                             shared_tokens=len(shared), overlap_pct=round(jac * 100),
                             usually_first=names.get(leader) or leader,
                             median_lead_s=int(abs(lead)),
                             note="likely same trader" if abs(lead) < 10 and jac > 0.6 else
                                  f"{names.get(follower) or follower[:6]} tends to follow"))
    rows.sort(key=lambda r: -r["overlap_pct"])
    return rows


def write_csv(path, rows):
    if not rows:
        open(path, "w").close()
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--wallets", default="wallets/axiom_wallets.json",
                    help="addresses and/or files (Axiom export .json, .txt), comma-separated")
    ap.add_argument("--helius-key", default=os.getenv("HELIUS_API_KEY"))
    ap.add_argument("--helius-host", default="https://api.helius.xyz")
    ap.add_argument("--bot-log", default=None, help="wallet_trades.csv from the bot")
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--max-pages", type=int, default=10, help="x100 swaps per wallet")
    ap.add_argument("--rate", type=float, default=5, help="Helius requests per second")
    ap.add_argument("--sol-price", type=float, default=150)
    ap.add_argument("--min-closed", type=int, default=10)
    ap.add_argument("--min-hold-s", type=int, default=120, help="median hold below this = too fast to copy")
    ap.add_argument("--copy-penalty", type=float, default=0.10,
                    help="ROI you lose per trade by buying after them + fees (0.10 = 10%%)")
    ap.add_argument("--inactive-days", type=int, default=14)
    ap.add_argument("--consensus-window-min", type=int, default=30)
    ap.add_argument("--top", type=int, default=25, help="how many copy candidates to write")
    ap.add_argument("--out", default="wallet_analysis")
    a = ap.parse_args()

    names = load_wallets(a.wallets)
    if not names:
        sys.exit("No wallets loaded. Use --wallets")
    if not a.helius_key and not a.bot_log:
        sys.exit("Give --helius-key (on-chain history) and/or --bot-log wallet_trades.csv")
    os.makedirs(a.out, exist_ok=True)
    now = time.time()
    cutoff = int(now - a.days * 86400)
    fills = defaultdict(list)

    if a.helius_key:
        cache = os.path.join("cache", "helius")
        os.makedirs(cache, exist_ok=True)
        api = Helius(a.helius_key, a.helius_host, a.rate)
        for i, w in enumerate(names, 1):
            txs = fetch_wallet(api, w, cutoff, a.max_pages, cache)
            parsed = [s for s in (parse_swap(t, w, a.sol_price) for t in txs) if s and s["ts"] >= cutoff]
            fills[w] += parsed
            print(f"[{i}/{len(names)}] {names[w] or w[:8]:<20} {len(parsed):>5} swaps")
    if a.bot_log:
        for w, fs in fills_from_bot_log(a.bot_log).items():
            if w in names:
                seen = {(f["ts"], f["mint"], f["side"]) for f in fills[w]}
                fills[w] += [f for f in fs if (f["ts"], f["mint"], f["side"]) not in seen and f["ts"] >= cutoff]

    all_pos = {w: build_positions(fills[w]) for w in names}
    cards = [score_wallet(w, names[w], fills[w], all_pos[w], a, now) for w in names]
    order = {"COPY CANDIDATE": 0, "profitable but not after copy lag": 1, "profitable but too fast to copy": 2,
             "not enough trades yet": 3, "losing money": 4, "inactive": 5, "no data": 6}
    cards.sort(key=lambda r: (order[r["verdict"]], -(r["score"] or 0), -r["realized_pnl_sol"]))
    write_csv(os.path.join(a.out, "wallet_scorecard.csv"), cards)

    cons_rows, cons_sum = consensus(all_pos, a.consensus_window_min * 60)
    write_csv(os.path.join(a.out, "consensus.csv"), cons_rows)
    sim = similar(all_pos, names)
    write_csv(os.path.join(a.out, "similar_wallets.csv"), sim)

    best = [c for c in cards if c["verdict"] == "COPY CANDIDATE"][: a.top]
    os.makedirs("wallets", exist_ok=True)
    with open(os.path.join("wallets", "best_wallets.txt"), "w") as f:
        f.write(f"# copy candidates from analyze_wallets.py, {datetime.now():%Y-%m-%d %H:%M}, last {a.days} days\n")
        for c in best:
            f.write(f"{c['wallet']} {c['name']}\n")

    # ---- console summary ----
    verdicts = defaultdict(int)
    for c in cards:
        verdicts[c["verdict"]] += 1
    print(f"\n{len(names)} wallets, last {a.days} days")
    for v in order:
        if verdicts[v]:
            print(f"  {verdicts[v]:>4}  {v}")
    if best:
        print(f"\nTop copy candidates (est. copy ROI assumes you lose {a.copy_penalty:.0%} per trade to lag+fees):")
        print(f"  {'name':<18}{'P&L SOL':>9}{'closed':>8}{'win%':>7}{'med hold':>10}{'copy ROI':>10}")
        for c in best[:15]:
            print(f"  {(c['name'] or c['wallet'][:8])[:17]:<18}{c['realized_pnl_sol']:>9.2f}{c['closed']:>8}"
                  f"{c['win_rate']:>7}{str(c['median_hold_min']) + 'm':>10}{str(c['est_copy_roi_pct']) + '%':>10}")
    print(f"\nDoes a crowd of tracked wallets predict better trades? (buys within {a.consensus_window_min} min)")
    print(f"  {'wallets buying':<16}{'positions':>10}{'avg ROI':>10}{'win%':>8}")
    for b in ("1", "2", "3-4", "5+"):
        if b in cons_sum:
            n, roi, win = cons_sum[b]
            print(f"  {b:<16}{n:>10}{roi:>9.1f}%{win:>7.0f}%")
    if sim:
        print(f"\n{len(sim)} wallet pairs trade the same tokens (see similar_wallets.csv), e.g.:")
        for s in sim[:5]:
            print(f"  {s['wallet_a'][:16]} + {s['wallet_b'][:16]}  {s['overlap_pct']}% overlap, {s['note']}")
    print(f"\nSaved to {a.out}/ and wallets/best_wallets.txt ({len(best)} wallets).")
    print("To copy only these, set in .env:  COPY_WALLETS=wallets/best_wallets.txt")


if __name__ == "__main__":
    main()
