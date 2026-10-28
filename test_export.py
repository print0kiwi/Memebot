"""Offline test for export_zip.py. Run: python test_export.py"""
import contextlib
import io
import os
import random
import tempfile
import zipfile

import export_zip

rng = random.Random(5)
B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
FAKE_KEY = "".join(rng.choice(B58) for _ in range(88))              # looks like a wallet private key
FAKE_TOKEN_VALUE = "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(40))


PUBLIC_ADDR = "".join(rng.choice(B58) for _ in range(44))              # a public wallet address (not a secret)


def write(root, rel, text="x\n"):
    p = os.path.join(root, rel)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write(text)


def run(root, out):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = export_zip.main(["--root", root, "--out", out])
    return rc, buf.getvalue()


def run_args(args):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = export_zip.main(args)
    return rc, buf.getvalue()


def make_tree(root):
    write(root, ".env", f"BOT_WALLET_PRIVATE_KEY={FAKE_KEY}\nTWITTERAPI_IO_KEY={FAKE_TOKEN_VALUE}\nLIVE_TRADING=false\n"
                        f"COPY_WALLETS=wallets/best_wallets.txt\nTRACKED_WALLETS=wallets/axiom_wallets.json,{PUBLIC_ADDR}\n")
    write(root, ".env.example", "BOT_WALLET_PRIVATE_KEY=\nTWITTERAPI_IO_KEY=\nLIVE_TRADING=false\n")
    write(root, ".remote_key", "-".join(["K7QD", "3MWX", "9PTC", "BZ4N", "HR2V", "8FAB"]) + "\n")
    write(root, "settings.json", "{}")
    write(root, "settings.before-aggressive.json", "{}")
    write(root, "trades.csv", "a,b\n")
    write(root, "bot.py", "print('hi')\n")
    write(root, "README.md", "# hi\n")
    write(root, "memebot", "#!/bin/bash\n")
    os.chmod(os.path.join(root, "memebot"), 0o755)
    write(root, "wallets/best_wallets.txt", "W1\n")
    write(root, ".env.example", f"BOT_WALLET_PRIVATE_KEY=\nTWITTERAPI_IO_KEY=\nLIVE_TRADING=false\nCOPY_WALLETS=wallets/best_wallets.txt\nTRACKED_WALLETS=wallets/axiom_wallets.json,{PUBLIC_ADDR}\n")
    write(root, "__pycache__/bot.cpython-313.pyc", "x")
    write(root, ".venv/lib/x.py", "x")
    write(root, ".claude/settings.local.json", "{}")
    write(root, "backtest/data/pools.csv", "x")
    write(root, "old.zip", "x")
    write(root, "android/build/out.apk", "x")
    write(root, "android/app/src/Main.java", "class Main {}\n")


def main():
    with tempfile.TemporaryDirectory() as d:
        root, out = os.path.join(d, "proj"), os.path.join(d, "proj-github.zip")
        make_tree(root)
        rc, text = run(root, out)
        assert rc == 0 and os.path.exists(out), text
        z = zipfile.ZipFile(out)
        names = set(z.namelist())
        assert names == {"solana-memebot/" + n for n in (".env.example", "bot.py", "README.md", "memebot",
                                                          "wallets/best_wallets.txt", "android/app/src/Main.java")}, names
        assert FAKE_KEY.encode() not in b"".join(z.read(n) for n in names)
        assert (z.getinfo("solana-memebot/memebot").external_attr >> 16) & 0o111          # launcher stays executable
        assert "Secret scan: clean" in text and "PRIVATE" in text
        assert FAKE_KEY not in text and FAKE_TOKEN_VALUE not in text                        # never printed

        # each kind of planted secret stops the export and writes nothing
        plants = {
            "a value copied from .env": f"KEY = '{FAKE_TOKEN_VALUE}'\n",
            "a wallet key": f"w = '{FAKE_KEY}'\n",
            "a keypair file": "[" + ",".join(str(rng.randrange(256)) for _ in range(64)) + "]\n",
            "a Discord token": "t = '" + ".".join(["MTIzNDU2Nzg5MDEyMzQ1Njc4OQ", "GabcDE", "abcdefghijklmnopqrstuvwxyz1"]) + "'\n",
            "a webhook": "u = 'https://discord.com/api/" + "web" + "hooks/123456789012345678/abcdefghijklmnopqrstuvwxyz_ABCDEF'\n",
            "a key assignment": "SOME_API_KEY=abcdefghijklmnop1234567890\n",
            "the remote key": "k = '" + "-".join(["K7QD", "3MWX", "9PTC", "BZ4N", "HR2V", "8FAB"]) + "'\n",
        }
        os.remove(out)
        for what, text_in in plants.items():
            write(root, "bot.py", text_in)
            rc, text = run(root, out)
            assert rc == 1 and not os.path.exists(out), what
            assert "bot.py" in text and FAKE_KEY not in text and FAKE_TOKEN_VALUE not in text, (what, text)
        # a secret-named setting is protected even when its value is shaped like a path; a path setting is not
        write(root, ".env", "SOME_TOKEN=abc/def/ghijklmnop\nCOPY_WALLETS=wallets/best_wallets.txt\n")
        write(root, "bot.py", "x = 'abc/def/ghijklmnop'\n")
        rc, text = run(root, out)
        assert rc == 1 and "bot.py" in text, text
        write(root, "bot.py", "x = 'wallets/best_wallets.txt'\n")
        rc, text = run(root, out)
        assert rc == 0, text
        # placeholders and the public test key do not trigger it
        write(root, "bot.py", "print('" + "-".join(["XXXX"] * 6) + "', '" + "-".join(["K7QD", "3MWX", "9PTC", "BZ4N", "HR2V", "8FGA"]) + "')\n")
        rc, text = run(root, out)
        assert rc == 0, text
        # a leftover zip in the project is never packed into itself
        rc, _ = run(root, os.path.join(root, "inside.zip"))
        assert rc == 0 and "solana-memebot/inside.zip" not in zipfile.ZipFile(os.path.join(root, "inside.zip")).namelist()

    # --portable: adds settings and trade history, still never .env or the remote key, still scanned
    with tempfile.TemporaryDirectory() as d:
        root, out = os.path.join(d, "proj"), os.path.join(d, "p.zip")
        make_tree(root)
        rc, text = run_args(["--root", root, "--out", out, "--portable"])
        assert rc == 0, text
        names = set(zipfile.ZipFile(out).namelist())
        for n in ("settings.json", "settings.before-aggressive.json", "trades.csv", "README-FLASH-DRIVE.txt", "bot.py"):
            assert "solana-memebot/" + n in names, (n, names)
        assert not {"solana-memebot/.env", "solana-memebot/.remote_key", "solana-memebot/android/app/src/Main.java"} & names
        assert (zipfile.ZipFile(out).getinfo("solana-memebot/memebot").external_attr >> 16) & 0o111
        write(root, "trades.csv", f"time,note\n2026,{FAKE_KEY}\n")                    # a key in the history: stop
        os.remove(out)
        rc, text = run_args(["--root", root, "--out", out, "--portable"])
        assert rc == 1 and not os.path.exists(out) and "trades.csv" in text and FAKE_KEY not in text

    # the real project must export cleanly (tested on a copy of the tree list only, nothing is written)
    here = os.path.dirname(os.path.abspath(__file__))
    files = export_zip.collect(here)
    assert not any(f == ".env" or f.startswith(".venv/") or f.endswith(".csv") or f == "settings.json" for f in files)
    assert ".env.example" in files and "bot.py" in files and export_zip.scan(here, files) == []
    print("all export tests passed")


main()
