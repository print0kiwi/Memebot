"""
Download Solana meme coin 5-minute candles (OHLCV) for backtesting.

Pure Python standard library, no pip installs needed.

  FREE  (GeckoTerminal public API): about the last 6 months of history, ~30 requests/min
      python download_data.py

  PAID  (CoinGecko Analyst plan or higher): history back to Sept 2021, when a pool existed
      python download_data.py --api-key YOUR_KEY --start 2021-09-01

Re-running resumes where it left off. Output:
  data/pools.csv          one row per pool (name, token, dex, created, liquidity)
  data/ohlcv/<pool>.csv   ts,open,high,low,close,volume_usd   (oldest first)

The token universe = top Solana pools by volume + newest pools (+ any you list in a file).
"""
import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

FREE_BASE = "https://api.geckoterminal.com/api/v2"
PAID_BASE = "https://pro-api.coingecko.com/api/v3/onchain"
NETWORK = "solana"
BARS_PER_REQUEST = 1000  # 1000 x 5m = ~3.5 days

# Not meme coins: majors, stables, LSTs, infra tokens
NOT_MEMES = {
    "SOL", "WSOL", "USDC", "USDT", "PYUSD", "USDS", "USDE", "EURC", "MSOL", "JITOSOL",
    "BSOL", "JUPSOL", "INF", "STSOL", "HSOL", "JUP", "RAY", "JTO", "PYTH", "ORCA",
    "W", "TNSR", "DRIFT", "KMNO", "MNDE", "HNT", "RENDER", "RNDR", "WBTC", "CBBTC",
    "ETH", "WETH", "ZBTC", "TBTC",
}


class Api:
    def __init__(self, api_key: str | None, per_minute: float):
        self.key = api_key
        self.base = PAID_BASE if api_key else FREE_BASE
        self.min_gap = 60.0 / per_minute
        self.last = 0.0

    def get(self, path: str, **params) -> dict:
        url = f"{self.base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        headers = {"Accept": "application/json", "User-Agent": "memecoin-backtester/1.0"}
        if self.key:
            headers["x-cg-pro-api-key"] = self.key
        for attempt in range(6):
            wait = self.min_gap - (time.time() - self.last)
            if wait > 0:
                time.sleep(wait)
            self.last = time.time()
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
                    return json.loads(r.read())
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return {}
                if e.code in (401, 403):
                    sys.exit(f"API refused the request ({e.code}). Check your API key / plan.")
                backoff = 15 * (attempt + 1) if e.code == 429 else 5 * (attempt + 1)
                print(f"   HTTP {e.code}, retrying in {backoff}s")
                time.sleep(backoff)
            except (urllib.error.URLError, TimeoutError) as e:
                print(f"   network error ({e}), retrying")
                time.sleep(5 * (attempt + 1))
        print(f"   giving up on {url}")
        return {}


def parse_pool(item: dict) -> dict:
    a = item.get("attributes", {})
    rel = item.get("relationships", {})
    base_id = rel.get("base_token", {}).get("data", {}).get("id", "")
    name = a.get("name", "")
    return {
        "pool": a.get("address", ""),
        "name": name,
        "symbol": name.split("/")[0].strip(),
        "base_token": base_id.replace(f"{NETWORK}_", ""),
        "dex": rel.get("dex", {}).get("data", {}).get("id", ""),
        "created_at": a.get("pool_created_at") or "",
        "reserve_usd": a.get("reserve_in_usd") or "",
    }


def is_meme(p: dict) -> bool:
    quote = p["name"].split("/")[-1].strip().upper() if "/" in p["name"] else ""
    return (p["symbol"].upper() not in NOT_MEMES
            and quote in ("SOL", "WSOL", "USDC", "USDT", "")
            and p["pool"])


def build_universe(api: Api, top_pages: int, new_pages: int, extra: list[str]) -> list[dict]:
    pools: dict[str, dict] = {}

    def add(items):
        for it in items or []:
            p = parse_pool(it)
            if is_meme(p) and p["pool"] not in pools:
                pools[p["pool"]] = p

    for page in range(1, top_pages + 1):
        print(f"Universe: top pools page {page}/{top_pages}")
        add(api.get(f"/networks/{NETWORK}/pools", page=page, sort="h24_volume_usd_desc").get("data"))
    for page in range(1, new_pages + 1):
        print(f"Universe: new pools page {page}/{new_pages}")
        add(api.get(f"/networks/{NETWORK}/new_pools", page=page).get("data"))
    for addr in extra:
        # accept either a pool address or a token mint (-> its top pool)
        d = api.get(f"/networks/{NETWORK}/pools/{addr}").get("data")
        if d:
            add([d])
        else:
            add((api.get(f"/networks/{NETWORK}/tokens/{addr}/pools", page=1).get("data") or [])[:1])
    return list(pools.values())


def ohlcv_path(out: str, pool: str) -> str:
    return os.path.join(out, "ohlcv", f"{pool}.csv")


def download_pool(api: Api, p: dict, out: str, start_ts: int) -> int:
    path = ohlcv_path(out, p["pool"])
    done_marker = path + ".done"
    if os.path.exists(done_marker):
        return 0
    rows: dict[int, list] = {}
    if os.path.exists(path):
        with open(path) as f:
            for r in csv.DictReader(f):
                rows[int(r["ts"])] = [float(r[k]) for k in ("open", "high", "low", "close", "volume_usd")]
    created = 0
    if p["created_at"]:
        try:
            created = int(datetime.fromisoformat(p["created_at"].replace("Z", "+00:00")).timestamp())
        except ValueError:
            pass
    floor = max(start_ts, created)
    before = min(rows) if rows else int(time.time())
    while before > floor:
        res = api.get(f"/networks/{NETWORK}/pools/{p['pool']}/ohlcv/minute",
                      aggregate=5, before_timestamp=before, limit=BARS_PER_REQUEST,
                      currency="usd", token="base")
        bars = (res.get("data") or {}).get("attributes", {}).get("ohlcv_list") or []
        if not bars:
            break
        for ts, o, h, l, c, v in bars:
            if ts >= floor:
                rows[int(ts)] = [o, h, l, c, v]
        oldest = min(int(b[0]) for b in bars)
        if oldest >= before:
            break
        before = oldest
        _save(path, rows)  # checkpoint so a crash can resume
        print(f"   {p['symbol']:<12} {len(rows):>7} bars, back to "
              f"{datetime.fromtimestamp(oldest, timezone.utc):%Y-%m-%d}")
    _save(path, rows)
    open(done_marker, "w").close()
    return len(rows)


def _save(path: str, rows: dict):
    tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["ts", "open", "high", "low", "close", "volume_usd"])
        for ts in sorted(rows):
            w.writerow([ts, *rows[ts]])
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api-key", default=os.getenv("COINGECKO_API_KEY"),
                    help="CoinGecko paid API key (Analyst+) for history older than ~6 months")
    ap.add_argument("--start", default=None, help="earliest date, e.g. 2021-09-01 (default: 5 years ago)")
    ap.add_argument("--top-pages", type=int, default=10, help="pages of top pools by volume (20 per page)")
    ap.add_argument("--new-pages", type=int, default=10, help="pages of newest pools (20 per page)")
    ap.add_argument("--tokens-file", help="text file with extra pool or token addresses, one per line")
    ap.add_argument("--max-pools", type=int, default=0, help="stop after this many pools (0 = all)")
    ap.add_argument("--rate", type=float, default=None, help="requests per minute (default 25 free / 400 paid)")
    ap.add_argument("--out", default="data")
    a = ap.parse_args()

    api = Api(a.api_key, a.rate or (400 if a.api_key else 25))
    start = a.start or f"{datetime.now(timezone.utc).year - 5}-{datetime.now(timezone.utc):%m-%d}"
    start_ts = int(datetime.fromisoformat(start).replace(tzinfo=timezone.utc).timestamp())
    os.makedirs(os.path.join(a.out, "ohlcv"), exist_ok=True)

    print(f"Mode: {'PAID (CoinGecko)' if a.api_key else 'FREE (GeckoTerminal, ~6 months max)'} | start {start}")
    extra = []
    if a.tokens_file:
        extra = [l.strip() for l in open(a.tokens_file) if l.strip() and not l.startswith("#")]

    pools_csv = os.path.join(a.out, "pools.csv")
    known = {}
    if os.path.exists(pools_csv):
        with open(pools_csv) as f:
            known = {r["pool"]: r for r in csv.DictReader(f)}
    for p in build_universe(api, a.top_pages, a.new_pages, extra):
        known.setdefault(p["pool"], p)
    pools = list(known.values())
    with open(pools_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(pools[0].keys()) if pools else ["pool"])
        w.writeheader()
        w.writerows(pools)
    print(f"{len(pools)} meme coin pools in universe -> {pools_csv}")

    if a.max_pools:
        pools = pools[: a.max_pools]
    total = 0
    for i, p in enumerate(pools, 1):
        print(f"[{i}/{len(pools)}] {p['name']}  ({p['pool']})")
        total += download_pool(api, p, a.out, start_ts)
    print(f"Done. {total:,} new bars written under {a.out}/ohlcv/")


if __name__ == "__main__":
    main()
