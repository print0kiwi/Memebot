"""
Make a zip of the bot that is safe to put on GitHub, or to carry on a flash drive.

  python export_zip.py                 -> ../solana-memebot-github.zip
  python export_zip.py --portable      -> ../solana-memebot-portable.zip  (adds your settings + trade history)
  python export_zip.py --out my.zip

What it leaves OUT: your .env (keys, tokens), the remote-control key, saved settings, trade/wallet/Discord logs,
downloaded market data, virtual environments, caches, build output, signing keys and old zips.
Before it writes anything it scans every file it WOULD include for secrets (wallet keys, Discord tokens and
webhooks, anything that matches a value in your own .env) and refuses to continue if it finds one.
Pure standard library. Use a PRIVATE repository: the zip still holds your tracked-wallet list and strategy code.
"""
import argparse
import fnmatch
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOP = "solana-memebot"            # folder name inside the zip

# paths (relative, forward slashes) that never go in. Patterns are matched against the path and each folder name.
DENY_DIRS = {".git", ".venv", "venv", "__pycache__", ".claude", "cache", "wallet_analysis", ".gradle", ".idea",
             "build", "node_modules", "synthetic", "results", "results_synth", ".pytest_cache"}
DENY_FILES = [".env", ".env.*", "!.env.example", ".remote_key*", "settings.json", "settings.*.json", "demo_settings.json",
              "*.csv", "x_state.json", "reviews.jsonl", "review_state.json", "*.zip", "*.bak", "*.save", "*.tmp", "*.log", "local.properties", "*.jks",
              "*.keystore", "*.pyc", ".DS_Store", "Thumbs.db", "nohup.out"]
DENY_PATHS = ["backtest/data/*"]
BINARY_EXT = {".webp", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".jar", ".zip", ".apk", ".ttf", ".woff", ".woff2"}

SECRET_RULES = [
    ("wallet private key assignment", re.compile(r"BOT_WALLET_PRIVATE_KEY\s*=\s*[\"']?[1-9A-HJ-NP-Za-km-z]{40,}")),
    ("long base58 string (looks like a wallet key)", re.compile(r"(?<![1-9A-HJ-NP-Za-km-z])[1-9A-HJ-NP-Za-km-z]{85,90}(?![1-9A-HJ-NP-Za-km-z])")),
    ("keypair file (64 numbers)", re.compile(r"\[\s*(?:\d{1,3}\s*,\s*){63}\d{1,3}\s*\]")),
    ("Discord bot token", re.compile(r"[MNO][A-Za-z\d_-]{23,27}\.[\w-]{6,7}\.[\w-]{27,}")),
    ("Discord webhook URL", re.compile(r"discord(?:app)?\.com/api/webhooks/\d{6,}/[\w-]{20,}")),
    ("API key/token assignment with a value", re.compile(
        r"(?im)^\s*(?:export\s+)?[A-Z_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|PRIVATE_KEY)[A-Z_]*\s*=\s*[\"']?[A-Za-z0-9_\-\.]{16,}")),
    ("remote access key", re.compile(r"(?<![0-9A-Z])[0-9A-HJKMNP-TV-Z]{4}(?:-[0-9A-HJKMNP-TV-Z]{4}){5}(?![0-9A-Z-])")),
]


# strings that look like secrets but are public: placeholders and the throwaway key used by test_remote.py
SAFE_LOOKALIKES = {"K7QD-3MWX-9PTC-BZ4N-HR2V-8FGA"}


# --portable adds these (your strategy settings and the history the self-review learns from). Never .env or keys.
PORTABLE_EXTRA = ["settings.json", "settings.before-aggressive.json", "research_result.json",
                  "trades.csv", "trade_context.csv", "reviews.jsonl", "review_state.json"]
FLASH_README = "README-FLASH-DRIVE.txt"
FLASH_TEXT = """MEMEBOT ON A FLASH DRIVE
========================

Works on Linux and macOS (anything with bash and Python 3.10 or newer). Needs internet the first time
on each computer to install its packages, and to trade.

1. Copy solana-memebot-portable.zip to the drive and unzip it there (or unzip, then copy the folder).
2. On the computer, open the solana-memebot folder on the drive and run the launcher:
     - double-click "memebot" and choose "Run" / "Run in Terminal", or
     - open a terminal in that folder and type:   bash memebot
   (Flash drives formatted FAT32/exFAT drop the "executable" flag, so "bash memebot" always works.)
3. First run on a computer: it makes a Python environment and installs the packages. When the drive
   is FAT32/exFAT/NTFS that environment goes in ~/.local/share/memebot/ on the computer instead of the
   drive, so the drive stays small and nothing breaks.
4. Your keys are NOT in this zip. The first run creates .env from the template on the drive. Put your
   keys in it, or on the dashboard (on that computer) use Customize -> Wallet key.
   A flash drive can be lost: if you keep the wallet key on it, only keep a small amount of SOL in the
   bot wallet. FAT32/exFAT can't protect .env with file permissions.
5. ./memebot install-shortcut adds a "Memebot" menu entry that points at the drive (only works while
   the drive is plugged in at the same place).

Included: the bot, dashboard, tools, your strategy settings and trade history (so the self-review keeps
learning). Left out: .env, the remote-control key, logs of tracked wallets/Discord, downloaded data.
Paper mode is the default; live trading still asks you to type LIVE.
"""


def real_hit(label: str, rx, text: str) -> bool:
    for m in rx.finditer(text):
        found = m.group(0)
        if found in SAFE_LOOKALIKES or len(set(re.sub(r"[^A-Za-z0-9]", "", found))) <= 3:   # XXXX-XXXX... placeholders
            continue
        return True
    return False


def denied(rel: str) -> bool:
    parts = rel.split("/")
    if any(p in DENY_DIRS for p in parts[:-1]):
        return True
    name = parts[-1]
    for pat in DENY_FILES:
        if pat.startswith("!"):
            continue
        if fnmatch.fnmatch(name, pat) and not any(fnmatch.fnmatch(name, q[1:]) for q in DENY_FILES if q.startswith("!")):
            return True
    return any(fnmatch.fnmatch(rel, pat) for pat in DENY_PATHS)


def collect(root: str) -> list[str]:
    out = []
    for dp, dns, fns in os.walk(root):
        dns[:] = sorted(d for d in dns if d not in DENY_DIRS)
        for fn in sorted(fns):
            full = os.path.join(dp, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            if os.path.islink(full) or denied(rel):
                continue
            out.append(rel)
    return out


SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PRIVATE|PASSW|WEBHOOK|COOKIE|AUTH|SEED|MNEMONIC", re.I)
_B58_ADDR = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")
_PATH = re.compile(r"[\w.\-]+(?:/[\w.\-]+)+|[\w\-]+\.(?:txt|json|csv)")


def looks_public(v: str) -> bool:
    """True for values that are only file paths and/or public wallet addresses (never a private key: those are 64+ chars)."""
    parts = [p for p in re.split(r"[,;\s]+", v) if p]
    return bool(parts) and all(_PATH.fullmatch(p) or _B58_ADDR.fullmatch(p) for p in parts)


def env_values(root: str) -> list[str]:
    """Secret-looking values from the real .env, so we can make sure none of them appears in the zip."""
    vals = []
    try:
        with open(os.path.join(root, ".env"), encoding="utf-8") as f:
            for ln in f:
                m = re.match(r"\s*([A-Z0-9_]+)\s*=\s*(.*)", ln)
                if not m:
                    continue
                v = m.group(2).strip().strip("\"'")
                if len(v) >= 12 and v.lower() not in ("true", "false") and not v.startswith(("http", "wss", "/")):
                    if not SECRET_NAME.search(m.group(1)) and looks_public(v):
                        continue                    # a file path or public wallet addresses, not a secret
                    vals.append(v)
    except OSError:
        pass
    try:
        with open(os.path.join(root, ".remote_key"), encoding="utf-8") as f:
            k = f.read().strip()
            if k:
                vals.append(k)
                vals.append(k.replace("-", ""))
    except OSError:
        pass
    return vals


def scan(root: str, files: list[str]) -> list[tuple[str, str]]:
    """-> [(file, what was found)]; never includes the secret itself."""
    hits = []
    secrets = env_values(root)
    for rel in files:
        ext = os.path.splitext(rel)[1].lower()
        full = os.path.join(root, rel)
        try:
            if os.path.getsize(full) > 5_000_000:
                continue
            data = open(full, "rb").read()
        except OSError:
            continue
        for s in secrets:                                      # binary files too: a value copied anywhere counts
            if s.encode() in data:
                hits.append((rel, "contains a value from your .env"))
                break
        if ext in BINARY_EXT or b"\x00" in data[:2048]:
            continue
        text = data.decode("utf-8", "replace")
        for label, rx in SECRET_RULES:
            if label == "API key/token assignment with a value" and rel.endswith(".env.example"):
                continue
            if real_hit(label, rx, text):
                hits.append((rel, label))
    return hits


def build(root: str, out: str, files: list[str], extra_text: dict | None = None) -> int:
    tmp = out + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel, text in (extra_text or {}).items():
            info = zipfile.ZipInfo(f"{TOP}/{rel}", date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = (0o644 | 0o100000) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(info, text)
        for rel in files:
            info = zipfile.ZipInfo(f"{TOP}/{rel}", date_time=(2026, 1, 1, 0, 0, 0))
            mode = 0o755 if os.access(os.path.join(root, rel), os.X_OK) else 0o644
            info.external_attr = (mode | 0o100000) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with open(os.path.join(root, rel), "rb") as f:
                z.writestr(info, f.read())
    os.replace(tmp, out)
    return os.path.getsize(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=HERE)
    ap.add_argument("--out", default=None)
    ap.add_argument("--portable", action="store_true",
                    help="flash-drive copy: also include your settings and trade history (never keys)")
    a = ap.parse_args(argv)
    root = os.path.abspath(a.root)
    name = "solana-memebot-portable.zip" if a.portable else "solana-memebot-github.zip"
    out = os.path.abspath(a.out or os.path.join(os.path.dirname(root), name))
    files = collect(root)
    if a.portable:
        files = [f for f in files if not f.startswith("android/")]      # the phone app source isn't needed on a drive
        files += [f for f in PORTABLE_EXTRA if os.path.isfile(os.path.join(root, f)) and f not in files]
    if os.path.commonpath([out, root]) == root:                # never zip the zip
        files = [f for f in files if os.path.join(root, f) != out]
    hits = scan(root, files)
    if hits:
        print("STOPPED: possible secrets found. Nothing was written.\n")
        for rel, what in hits:
            print(f"  {rel}: {what}")
        print("\nRemove the secret from that file (keys belong only in .env), then run this again.")
        return 1
    size = build(root, out, files, {FLASH_README: FLASH_TEXT} if a.portable else None)
    print(f"Wrote {out}")
    if a.portable:
        print(f"  {len(files) + 1} files, {size / 1024:.0f} KB. Includes your settings and trade history. "
              "Left out: .env, remote key, wallet/Discord logs, data, venv, caches.")
        print("  Secret scan: clean.")
        print(f"  Unzip it onto the drive, then run: bash memebot   (see {FLASH_README} inside).")
        return 0
    print(f"  {len(files)} files, {size / 1024:.0f} KB. Left out: .env, remote key, settings, logs, data, venv, caches.")
    print("  Secret scan: clean.")
    print("  Make the GitHub repository PRIVATE: it still contains your tracked-wallet list and strategy.")
    if len(files) > 100:
        print(f"  Note: GitHub's web uploader accepts at most 100 files at a time; this has {len(files)}. "
              "Use GitHub Desktop or git, or upload in batches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
