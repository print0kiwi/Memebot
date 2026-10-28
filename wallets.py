"""
Load wallet lists. Accepts any mix, comma-separated:
  - plain addresses:           5CEb...SPAG,Bi4r...YdLt
  - an Axiom wallet-tracker export (.json with trackedWalletAddress / name)
  - a text file: one address per line, optional name after a space or comma
    (best_wallets.txt written by analyze_wallets.py uses this format)
Returns {address: name}.
"""
import json
import os
import re

ADDR = re.compile(r"[1-9A-HJ-NP-Za-km-z]{32,44}")


def _from_file(path: str) -> dict[str, str]:
    text = open(path, encoding="utf-8").read()
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    out = {}
    if isinstance(data, list):
        for x in data:
            if isinstance(x, dict):
                a = x.get("trackedWalletAddress") or x.get("address") or x.get("wallet")
                if a:
                    out[a.strip()] = (x.get("name") or "").strip()
            elif isinstance(x, str):
                out[x.strip()] = ""
        return out
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = re.split(r"[,\s]+", line, maxsplit=1)
        out[parts[0]] = parts[1].strip() if len(parts) > 1 else ""
    return out


def load_wallets(spec: str, base_dir: str = ".") -> dict[str, str]:
    out: dict[str, str] = {}
    for item in (s.strip() for s in (spec or "").split(",")):
        if not item:
            continue
        path = item if os.path.isabs(item) else os.path.join(base_dir, item)
        if os.path.isfile(path):
            out.update(_from_file(path))
        else:
            out.setdefault(item, "")
    return out


def valid(addr: str) -> bool:
    return bool(ADDR.fullmatch(addr))
