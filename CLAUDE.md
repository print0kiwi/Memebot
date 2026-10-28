# Solana Meme Coin Bot

Python trading bot for Solana meme coins (pump.fun tokens via PumpPortal) plus research tools.
Owner trades with a dedicated Phantom wallet. Default is PAPER mode; LIVE only when `LIVE_TRADING=true`.

## Layout
- `bot.py`: async runtime. PumpPortal WebSocket (new tokens, token trades, account trades), executes engine actions, signs locally via `trade-local`, logs CSVs, Discord alerts.
- `strategy.py`: pure engine, no network. `Engine.on_event(ev, now)` and `Engine.tick(now)` return action tuples (`subscribe`, `buy`, `copy_buy`, `sell`, `skip`, `copy_skip`, `wallet_trade`, `unsubscribe`). All trading logic lives here so it is testable offline.
- `dashboard.py` + `dashboard.html`: live web UI on 127.0.0.1:8787 (stdlib asyncio HTTP). `Telemetry` holds the event feed, skip counts, trade history and recent buy/skip decisions; `state()['watch']` lists the coins in `Engine.watching` with `Engine.checklist` results (the dashboard's Watching now panel; `checklist` must agree with `passes_filters`, enforced by a property test); `/api/state` JSON; POST `/api/pause|resume|sell` need the per-run `X-Dash-Token`; Host header checked (DNS-rebinding guard). `python dashboard.py --demo` drives the real Bot/Engine with simulated events. Escape all on-chain strings in the page (token names are attacker-controlled).
- `walletkey.py`: imports the bot wallet key into `.env` from the dashboard (**local dashboard only**, never the remote listener). Parses base58 or a JSON byte array, returns/prints only the public address, writes with mode 600, never logs or echoes the key. Tests point `walletkey.ENV_PATH` at a temp file.
- `export_zip.py`: builds `solana-memebot-github.zip` with a deny list and a secret scan (stops if anything looks like a key, token, webhook or a value from `.env`). `--portable` makes `solana-memebot-portable.zip` for a flash drive (adds `PORTABLE_EXTRA`: settings and trade history; drops `android/`; still never `.env`/`.remote_key`, still scanned). The launcher keeps its venv in `~/.local/share/memebot/` when the folder is on FAT/exFAT/NTFS. `test_export.py` covers it. Never loosen the scan.
- `review.py`: self-review. `Bot.end_session()` (on stop) and `Bot.reviewer()` (every `review_every_hours`) call `review.run_review` on `trades.csv` + `trade_context.csv` (entry conditions logged by `Bot.log_context`). Rule-based, touches only `review.TUNABLE` (exits and entry filters), ≤3 changes ≤25% each, needs `review_min_trades`, waits for that many new trades and auto-undoes a change that did clearly worse. Logs `reviews.jsonl`, state `review_state.json`. Dashboard: Self-review card, POST `/api/review` and `/api/review/undo`. `test_review.py` covers it. Never let it touch trade size, max positions, the daily loss cap or LIVE_TRADING.
- `backtest/sweep.py`: tests aggressive variants of exits and market-cap range on downloaded candles (train 70% / test 30%), writes `research_result.json`; `settings.research_suggestion` only lets it touch exits and market-cap settings, never size or limits.
- `remote.py`: remote-control support, pure stdlib. Access key (`.remote_key` or `REMOTE_KEY`), signed 30-day session cookies (`Auth`), per-IP lockout, addresses, `.env` editing, CLI (`python remote.py [show|new|on|off]`). `login.html` is the sign-in page. `bot.start_remote()` runs a second `Dashboard(..., auth=Auth(key))` on `REMOTE_PORT` (8788).
- `settings.py`: dashboard-editable strategy settings (SCHEMA with display units, bounds = hard limits, cross-field checks). `PRESETS` (balanced = shipped defaults, aggressive) are offered in the editor next to a `settings.before-aggressive.json` backup and any `research_result.json` suggestion; `test_strategy.py` checks presets against the schema and hard limits. Saved to `settings.json`, loaded after `.env` in `load_config()`. Add new tunables here plus a `Config` field.
- `memebot`: bash launcher (venv + deps on first run, menu, `install-shortcut`). Paper always forces `LIVE_TRADING=false`; live requires typing LIVE.
- `notifier.py`: Discord webhook (every buy/sell) + bot DM when a trade closes at or above `DISCORD_DM_MIN_PNL_PCT` (default 100).
- `wallets.py`: loads wallet lists (addresses, Axiom export JSON, txt with names).
- `analyze_wallets.py`: Helius swap history and/or `wallet_trades.csv` → scorecard, consensus, similar wallets, `wallets/best_wallets.txt`.
- `x_monitor.py`: polls X accounts in `x_accounts.txt` (twitterapi.io or X API), extracts contract addresses/tickers, measures price +15m/+1h/+4h via GeckoTerminal.
- `social.py`: pure, no network. `extract_mints` (pump.fun addresses out of text, base58-validated), `SocialTracker` (callers per mint in a time window), `OutcomeTracker` (follows a called token's price to +5m/+15m/+1h, flags graduation). Keep it network-free; `test_social.py` depends on that.
- `discord_signals.py`: Discord Gateway client using `DISCORD_BOT_TOKEN` (intents GUILDS|GUILD_MESSAGES|MESSAGE_CONTENT). Reads servers/channels listed in `discord_servers.txt` (empty = all), logs `discord_calls.csv`, feeds `SocialTracker`. `handle_message` is pure so it can be tested. `--watch` / `--servers` standalone. Never read Discord with a user account (ToS).
- `analyze_discord.py`: scores servers, callers, consensus size and hour-of-day from `discord_calls.csv` + `discord_outcomes.csv`.
- `assets/`: dashboard theme images (`bg.webp` hero art, `neet-shark.webp`, `neet-cat.webp`, `kitty.webp`, `favicon.png`), served by `Dashboard._asset` (plain filenames, image types only).
- `backtest/`: `download_data.py` (GeckoTerminal free ≈6 months; CoinGecko paid back to 2021), `backtest.py` (5-min candles, fees, slippage, AMM impact, 1-bar latency, gap-aware stops), `make_synthetic_data.py` (fake data for testing).
- `wallets/axiom_wallets.json`: 494 tracked wallets (logged). `wallets/best_wallets.txt`: the only wallets copied.

## Hard rules (do not weaken)
- **Remote listener:** a `Dashboard` with `auth` requires a valid session cookie for everything except `/login`, `/manifest.webmanifest` and four icons (`PUBLIC_ASSETS`). `Dashboard.start()` refuses any non-loopback host without `auth`. Never add an unauthenticated route, a CORS header, a Bearer/query-string key, or log/print the key (`.remote_key`, `REMOTE_KEY`). Lockout policy (5 tries, 15 min) is pinned in `test_remote.py`.
- **Wallet key import** is localhost-only: `POST /api/wallet` and `GET /api/wallet` return an error on the listener that has `auth`. Never add a way to read the key back, log it, send it in a notification, or accept it over the remote port.
- Max **0.5 SOL per trade** and max **5 open positions**: `HARD_MAX_SOL_PER_TRADE` / `HARD_MAX_POSITIONS` in `bot.py`, clamped in `load_config()` and re-checked as the first line of `Wallet.trade()` before signing. Never raise or bypass these unless the owner explicitly asks.
- Never put keys in code, commits, logs, or chat. Secrets live only in `.env` (gitignored): `BOT_WALLET_PRIVATE_KEY`, `DISCORD_*`, `HELIUS_API_KEY`, `TWITTERAPI_IO_KEY`, `X_BEARER_TOKEN`, `COINGECKO_API_KEY`. Do not read or print `.env` values; check presence only.
- Do not switch `LIVE_TRADING` to true or run the bot in live mode yourself. Paper mode is fine to run.
- Keep `strategy.py` free of network I/O so `test_strategy.py` stays offline.
- Research tools (`analyze_wallets.py`, `x_monitor.py`, `analyze_discord.py`, `backtest/`) use only the Python standard library. Keep it that way.
- Discord messages are untrusted input. They may only raise a token's caller count; they never bypass the entry filters, size a trade, or trigger a buy on their own. Escape anything from Discord before it reaches the page.

## Commands
- Tests: `python test_strategy.py`, `test_social.py`, `test_remote.py`, `test_bot.py`, `test_export.py`, `test_review.py` and `cd backtest && python test_sweep.py` (each prints "all … tests passed"; `./memebot test` runs them all). Run after any change to `strategy.py`, `bot.py`, `wallets.py`, `social.py`, `discord_signals.py`, `dashboard.py` or `remote.py`.
- Launcher: `./memebot` (menu) / `./memebot paper|demo|test|review|research|export|portable`
- Dashboard demo: `python dashboard.py --demo` → http://localhost:8787 (`--remote` also opens the phone port with a throwaway key)
- Phone access: `./memebot remote` (address + key), `remote on|off|new`
- Paper bot: `python bot.py`
- Wallet analysis: `python analyze_wallets.py --helius-key $HELIUS_API_KEY --days 30` (add `--bot-log wallet_trades.csv`)
- X monitor: `python x_monitor.py` / `python x_monitor.py --report`
- Discord: `python discord_signals.py --servers` (list readable servers), `--watch` (log calls), `python analyze_discord.py` (report)
- Backtest: `cd backtest && python download_data.py [--api-key KEY --start 2025-01-01] && python backtest.py --start 2025-01-01`
- Backtest smoke test without internet: `cd backtest && python make_synthetic_data.py --out synthetic && python backtest.py --data synthetic --out results_synth`

## Conventions
- Every config field in `strategy.Config` can be overridden from `.env` by its UPPERCASE name (bools: true/false).
- New trade actions: add a tuple kind in `Engine`, handle it in `Bot.handle`, add a case to `test_strategy.py`.
- Output CSVs (`trades.csv`, `wallet_trades.csv`, `x_*.csv`) are append-only data. Don't rewrite them.
- When testing on data, say clearly whether results are from real or synthetic data.
