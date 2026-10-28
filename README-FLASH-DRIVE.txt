MEMEBOT ON A FLASH DRIVE
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
