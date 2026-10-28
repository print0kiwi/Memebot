"""
Remote control: use the dashboard from your phone (or any other device).

  python remote.py              show the address(es) and your access key
  python remote.py new          make a new key (signs every phone out)
  python remote.py on | off     turn REMOTE_CONTROL on/off in .env

How it works: with REMOTE_CONTROL=true the bot opens a SECOND dashboard port (8788) that
requires an access key. Your normal localhost dashboard (8787) is unchanged and stays private.

Safety rules baked in:
  * nothing is reachable without the key: not the page, not the data, not the buttons
  * the key is 24 random characters (120 bits) and never travels in a URL
  * 5 wrong keys from one address locks that address out for 15 minutes
  * a login lasts 30 days, can be signed out, and a new key signs every device out
  * it is plain HTTP, so use it on your home Wi-Fi or (better) over Tailscale, which encrypts
    everything end to end. Never forward port 8788 on your router to the internet.

This module is pure standard library with no bot imports, so test_remote.py can run it offline.
"""
import base64
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = ".remote_key"
ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"      # Crockford base32: no I, L, O, U
MIN_KEY_LEN = 20
COOKIE = "mb_session"
SESSION_TTL = 30 * 86400
MAX_FAILS, FAIL_WINDOW, LOCKOUT = 5, 600, 900
DEFAULT_PORT = 8788


# ------------------------------------------------------------------ keys
def normalize_key(s) -> str:
    """Forgiving about how you type it: case, dashes, spaces, and O/I/L look-alikes."""
    s = re.sub(r"[\s\-]+", "", str(s or "")).upper()
    return s.translate(str.maketrans({"O": "0", "I": "1", "L": "1"}))


def generate_key() -> str:
    k = "".join(secrets.choice(ALPHABET) for _ in range(24))
    return "-".join(k[i:i + 4] for i in range(0, 24, 4))


def check_strength(key: str):
    k = normalize_key(key)
    if len(k) < MIN_KEY_LEN or len(set(k)) < 6:
        raise ValueError(f"access key too weak: use at least {MIN_KEY_LEN} varied characters "
                         "(or delete REMOTE_KEY from .env and let the bot make one)")


def load_key(base_dir: str = HERE) -> str:
    """REMOTE_KEY from the environment if set, else the key file (created on first use, mode 600)."""
    env = os.getenv("REMOTE_KEY", "").strip()
    if env:
        check_strength(env)
        return env
    path = os.path.join(base_dir, KEY_FILE)
    try:
        with open(path, encoding="utf-8") as f:
            key = f.read().strip()
        check_strength(key)
        return key
    except FileNotFoundError:
        return rotate_key(base_dir)


def rotate_key(base_dir: str = HERE) -> str:
    key = generate_key()
    path = os.path.join(base_dir, KEY_FILE)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(key + "\n")
    os.replace(tmp, path)
    return key


# ------------------------------------------------------------------ sessions
class Auth:
    """Checks the access key, hands out signed session cookies, locks out guessers.
    Sessions are signed with a key derived from the access key, so they survive a bot restart
    and a new key invalidates all of them. `clock` is injectable for tests."""

    def __init__(self, key: str, clock=time.time):
        check_strength(key)
        self._k = hashlib.sha256(normalize_key(key).encode()).digest()
        self._mac = hashlib.sha256(b"memebot-session:" + self._k).digest()
        self.clock = clock
        self._fails: dict[str, list[float]] = {}
        self._locked: dict[str, float] = {}
        self._revoked: dict[str, float] = {}

    # --- lockout
    def locked_for(self, ip: str) -> int:
        """Seconds this address is still locked out (0 = not locked)."""
        until = self._locked.get(ip, 0)
        left = until - self.clock()
        if left <= 0:
            self._locked.pop(ip, None)
            return 0
        return int(left) + 1

    def _prune(self, now: float):
        for ip in [i for i, u in self._locked.items() if u <= now]:
            del self._locked[ip]
        for ip in [i for i, ts in self._fails.items() if not ts or now - ts[-1] > FAIL_WINDOW]:
            del self._fails[ip]
        if len(self._fails) > 2000:                    # a flood of addresses can't grow memory
            self._fails.clear()
        for sid in [s for s, e in self._revoked.items() if e <= now]:
            del self._revoked[sid]

    # --- login
    def login(self, supplied, ip: str):
        """-> (cookie_value | None, seconds_locked, wrong_attempts_so_far)"""
        now = self.clock()
        self._prune(now)
        wait = self.locked_for(ip)
        if wait:
            return None, wait, 0
        got = hashlib.sha256(normalize_key(supplied).encode()).digest()
        if hmac.compare_digest(got, self._k):
            self._fails.pop(ip, None)
            return self._issue(now), 0, 0
        ts = [t for t in self._fails.get(ip, []) if now - t <= FAIL_WINDOW] + [now]
        self._fails[ip] = ts
        if len(ts) >= MAX_FAILS:
            self._locked[ip] = now + LOCKOUT
            del self._fails[ip]
            return None, LOCKOUT, len(ts)
        return None, 0, len(ts)

    # --- cookies
    def _sign(self, payload: str) -> str:
        d = hmac.new(self._mac, payload.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(d).decode().rstrip("=")

    def _issue(self, now: float) -> str:
        payload = f"{secrets.token_hex(8)}.{int(now + SESSION_TTL)}"
        return f"{payload}.{self._sign(payload)}"

    def verify(self, cookie_header: str):
        """-> session id if the Cookie header holds a valid, unexpired, unrevoked session, else None."""
        tok = ""
        for part in (cookie_header or "")[:4096].split(";"):
            name, _, val = part.strip().partition("=")
            if name == COOKIE:
                tok = val
        try:
            sid, exp, sig = tok.split(".")
            payload = f"{sid}.{exp}"
            if not hmac.compare_digest(sig, self._sign(payload)):
                return None
            if int(exp) <= self.clock() or sid in self._revoked:
                return None
            return sid
        except ValueError:
            return None

    def revoke(self, sid: str):
        self._revoked[sid] = self.clock() + SESSION_TTL


# ------------------------------------------------------------------ addresses
def is_loopback(host: str) -> bool:
    return host in ("localhost", "127.0.0.1", "::1", "[::1]")


def tailscale_ip():
    exe = shutil.which("tailscale")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=4).stdout.split()
        return out[0] if out else None
    except (OSError, subprocess.SubprocessError):
        return None


def lan_ip():
    """This computer's address on the local network (no packet is actually sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
        return None if ipaddress.ip_address(ip).is_loopback else ip
    except OSError:
        return None


def default_bind() -> str:
    """Prefer Tailscale only (encrypted, nobody else on the Wi-Fi can reach it); else the local network."""
    return tailscale_ip() or "0.0.0.0"


def urls(bind: str, port: int) -> list[str]:
    if bind not in ("0.0.0.0", "::"):
        return [f"http://{bind}:{port}"]
    found = [ip for ip in (tailscale_ip(), lan_ip()) if ip]
    return [f"http://{ip}:{port}" for ip in dict.fromkeys(found)]


# ------------------------------------------------------------------ .env helper
def set_env(path: str, name: str, value: str):
    """Set NAME=value in a .env file, keeping every other line exactly as it is."""
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    new, done = [], False
    for ln in lines:
        if re.match(rf"\s*{re.escape(name)}\s*=", ln) and not done:
            new.append(f"{name}={value}")
            done = True
        else:
            new.append(ln)
    if not done:
        new.append(f"{name}={value}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(new) + "\n")
    try:
        os.chmod(path, 0o600)              # an older, looser .env gets tightened too
    except OSError:
        pass


def env_value(path: str, name: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                m = re.match(rf"\s*{re.escape(name)}\s*=\s*(.*)", ln)
                if m:
                    return m.group(1).strip().strip("\"'")
    except OSError:
        pass
    return ""


# ------------------------------------------------------------------ CLI
def main(argv):
    cmd = argv[0] if argv else "show"
    env_path = os.path.join(HERE, ".env")
    if cmd in ("on", "off"):
        set_env(env_path, "REMOTE_CONTROL", "true" if cmd == "on" else "false")
        print(f"Remote control {'ON' if cmd == 'on' else 'OFF'}. "
              f"{'Restart the bot for it to take effect.' if cmd == 'on' else 'Restart the bot to close the port.'}")
        if cmd == "off":
            return 0
    elif cmd == "new":
        rotate_key(HERE)
        print("New access key made. Every phone is signed out; enter the new key on each one.")
    elif cmd != "show":
        print(__doc__)
        return 1
    port = int(env_value(env_path, "REMOTE_PORT") or DEFAULT_PORT)
    bind = env_value(env_path, "REMOTE_BIND") or default_bind()
    on = env_value(env_path, "REMOTE_CONTROL").lower() == "true"
    print()
    print(f"  Remote control: {'ON' if on else 'OFF (run: ./memebot remote on)'}")
    print("  Open this on your phone (same Wi-Fi or Tailscale):")
    for u in urls(bind, port) or [f"http://<this computer's address>:{port}"]:
        print(f"      {u}")
    print(f"  Access key:   {load_key(HERE)}")
    print("  Keep the key private. It controls the bot. `python remote.py new` replaces it.")
    if bind in ("0.0.0.0", "::") and not tailscale_ip():
        print("\n  Heads up: on plain Wi-Fi the key is sent unencrypted, so only use it on a network you trust.")
        print("  Away from home, install Tailscale on both devices (free) and it's encrypted automatically.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
