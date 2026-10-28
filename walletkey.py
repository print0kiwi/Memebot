"""
Importing the bot wallet's private key from the dashboard (this computer only).

The key is written to .env as BOT_WALLET_PRIVATE_KEY (file mode 600) and nothing else: it is never returned
by any API, never logged, and never sent anywhere. Only the public address is ever shown.

Accepted formats (what Phantom and the Solana CLI give you):
  * base58 text, 64 bytes  (Phantom: Settings > Manage Accounts > the account > Show Private Key)
  * a JSON array of 64 numbers (the Solana CLI keypair file, e.g. [12,34,...])
Pure standard library, so test_bot.py can run it offline. If `solders` is installed (it is in the bot's
venv) the key pair is also checked for consistency: the public half must really belong to the secret half.
"""
import json
import os
import re

import remote

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
ENV_NAME = "BOT_WALLET_PRIVATE_KEY"
HERE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(HERE, ".env")           # tests point this somewhere else


def b58decode(s: str) -> bytes:
    n = 0
    for ch in s:
        i = ALPHABET.find(ch)
        if i < 0:
            raise ValueError("not base58")
        n = n * 58 + i
    body = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\x00" * (len(s) - len(s.lstrip("1"))) + body


def b58encode(b: bytes) -> str:
    n = int.from_bytes(b, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = ALPHABET[r] + out
    return "1" * (len(b) - len(b.lstrip(b"\x00"))) + out


def parse_key(text) -> bytes:
    """Any accepted format -> the 64 secret-key bytes. Raises ValueError with a message safe to show."""
    t = str(text or "").strip().strip("\"'").strip()
    if not t:
        raise ValueError("Paste the private key first.")
    if len(t) > 1000:
        raise ValueError("That is too long to be a wallet key.")
    if t.startswith("["):
        try:
            arr = json.loads(t)
            raw = bytes(arr)
        except (ValueError, TypeError):
            raise ValueError("That looks like a key file, but it isn't 64 numbers between 0 and 255.") from None
    else:
        if re.search(r"\s", t):
            raise ValueError("A private key has no spaces. If that is a seed phrase, don't paste it here: "
                             "export the account's private key instead.")
        try:
            raw = b58decode(t)
        except ValueError:
            raise ValueError("That isn't a valid private key (it should be letters and numbers, "
                             "about 87-88 characters).") from None
    if len(raw) != 64:
        raise ValueError(f"A wallet private key is 64 bytes; this one is {len(raw)}. "
                         "Copy the full key from Phantom's 'Show Private Key'.")
    try:                                    # when solders is installed, make sure the two halves belong together
        from solders.keypair import Keypair
        Keypair.from_bytes(raw)
    except ImportError:
        pass
    except Exception:
        raise ValueError("That key is damaged: its public half doesn't match its secret half.") from None
    return raw


def address_of(secret: bytes) -> str:
    """A Solana keypair is 32 secret bytes + the 32-byte public key, and the address is the base58 public key."""
    return b58encode(secret[32:])


def to_env_value(secret: bytes) -> str:
    return b58encode(secret)


def status(env_path: str | None = None) -> dict:
    """{configured, address} for the key in .env (or the process environment). Never the key itself."""
    env_path = env_path or ENV_PATH
    val = remote.env_value(env_path, ENV_NAME) or os.getenv(ENV_NAME, "")
    if not val:
        return dict(configured=False, address="")
    try:
        return dict(configured=True, address=address_of(parse_key(val)))
    except ValueError:
        return dict(configured=True, address="", invalid=True)


def import_key(text, env_path: str | None = None) -> dict:
    """Validate and save. Returns {ok, address, replaced} or raises ValueError (message is safe to show)."""
    env_path = env_path or ENV_PATH
    secret = parse_key(text)
    before = status(env_path)
    addr = address_of(secret)
    remote.set_env(env_path, ENV_NAME, to_env_value(secret))
    return dict(ok=True, address=addr, replaced=bool(before["configured"] and before.get("address") != addr),
                previous=before.get("address", ""))
