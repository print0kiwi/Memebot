# Solana Meme Coin Bot

A trading bot for Solana meme coins, plus tools to research whether a strategy actually makes money before you risk real SOL.

| File | What it does |
|---|---|
| `bot.py` | The trading bot. Snipes new pump.fun launches and copies chosen wallets. Paper mode by default. |
| `analyze_wallets.py` | Scores your 494 tracked wallets: who's profitable, who's copyable, and which signals work. |
| `x_monitor.py` | Watches 11 X accounts for contract addresses and tickers, and measures how their calls perform. |
| `backtest/` | Downloads 5-minute candles and backtests the strategy with fees and slippage. |

**Hard limits built into the bot:** max **0.5 SOL per trade**, max **5 trades open at once**. Higher values in `.env` are ignored, and any buy over 0.5 SOL is blocked before it's signed.

## Quick start: the launcher

Double-click `memebot`, or run `./memebot` in a terminal, to get a menu: paper mode, dashboard demo, live mode, X monitor, wallet scoring, tests and settings. The first run creates the Python environment, installs packages and makes your `.env`, all automatically. Run `./memebot install-shortcut` once to add **Memebot** to your app menu and desktop.

Commands: `./memebot paper` · `demo` · `live` · `test` · `wallets` · `x` · `xreport` · `discord` · `dservers` · `dreport` · `remote` · `review` · `research` · `export` · `portable`. Live mode asks you to type `LIVE` to confirm, and paper mode is always paper, whatever `.env` says.

## Live dashboard

While the bot runs, it opens **http://localhost:8787** in your browser, updating every second:
- **Top row:** today's realized P&L, open (unrealized) P&L, open slots (x/5), win rate, all-time P&L, tokens seen
- **Watching now:** every coin the bot is evaluating right now, with its age, buyers, net SOL inflow and market cap, a ✓/✗ for each entry rule (dev, buyers, inflow, market cap, …), the reason it hasn't bought yet, and a **Just decided** list of what it bought or passed on in the last minutes
- **Open positions:** live % and SOL P&L for each, a bar showing where the price sits between stop loss and take profit (gold tick = peak), and a time-left bar
- **Sell** button on each position, and **Pause buys / Resume buys** in the header (open positions are still managed while paused)
- **Discord buzz:** which coins your servers are calling right now, how many different people called each one, and whether the bot is already holding or watching it
- **Realized P&L chart**, **closed trades** table (🚀 on +100% trades), and an **activity feed** you can filter: trades, Discord calls, wallet moves, copy skips, errors
- **Why tokens were skipped**, so you can see which filter is doing the work

### Editing strategy settings in the dashboard

Click **Edit** on the *Strategy settings* panel. Changes apply to the running bot instantly, are saved to `settings.json`, and override `.env` from then on.

| Group | Settings |
|---|---|
| Position size | Buy size (max 0.5 SOL), copy-trade buy size, max open trades (max 5) |
| Exits | Take profit %, stop loss %, trailing stop % (0 = off), max hold time. These apply to trades already open. |
| Dev | Max dev holding % of supply; allow or skip coins whose dev sells |
| Holders | Min distinct buyers, max share of buys from one wallet (whale or fake-volume filter), min net SOL inflow |
| Coin age | Min and max age in seconds. The bot buys at the first moment in that window when every rule passes. |
| Market cap | Min and max market cap in SOL for snipes; max market cap for copy trades |
| Bundles | Max % of supply bought by other wallets within the first moments of launch, and the length of that window |
| Discord | Min number of different people who must have called the coin in your servers (0 = off), and how far back to count |

**Profiles.** Inside the editor, **Load a profile** fills in the fields for you (nothing changes until you press Save):
- **Balanced** is the built-in default.
- **Aggressive** trades more often: looser entry rules (5+ buyers instead of 8, 0.5 SOL net inflow instead of 1, dev up to 30%, market cap up to 1000 SOL, up to 20% bundled), full 0.5 SOL copy trades, a +100% take-profit with a 25% trailing stop so winners can run, and a 15-minute max hold so slots turn over. It stays inside the hard limits (0.5 SOL per trade, 5 open trades). It is a judgment call, **not** a backtested result: more trades also means more fees and more rugs, so run it in paper mode first.
- **Before aggressive** restores what you had before the aggressive profile was applied.
- **Research result** appears after `./memebot research` finds settings that held up (see below).

Out-of-range values are rejected. Bundle detection is approximate, because it's based on when the bot receives each trade.

To try it without a wallet or internet, run `./memebot demo`. That's the real bot logic fed with simulated launches.

The dashboard is only reachable from your own computer, and its buttons can't be triggered by other websites. Change the port with `DASHBOARD_PORT`.

### Customize: background image and wallet key

The **Customize** panel has two buttons:
- **Background image…** pick any picture from your device. It is shrunk to fit and stored in that browser only (nothing is uploaded) and replaces the banner art; use **Darken** if the text gets hard to read. It is per device, so set it again on your phone. **Use the default art** goes back.
- **Wallet key…** paste the private key of the dedicated bot wallet (Phantom: Settings → Manage Accounts → the bot account → Show Private Key; the 64-byte base58 text or the `[12,34,…]` key file both work). It is written to `.env` (file mode 600) as `BOT_WALLET_PRIVATE_KEY`, never sent anywhere, never shown again, and only the public address is displayed. It only works on the dashboard on your own computer, never from the phone, and the bot must be restarted to use a new key. Importing a key does not turn on live trading.

### Self-review: the bot tunes itself

When the bot stops (Ctrl+C or closing the window) it reviews that session, and while it runs it reviews the last 24 hours once a day. It looks at its closed trades (how each one exited, and the market cap, buyers and inflow when it bought, from `trade_context.csv`) and changes settings when the numbers clearly call for it. For example:
- many trades run out the clock while losing → shorter max hold time
- most trades get stopped out → ask for more buyers and more net inflow before buying
- winners often hit the take-profit → raise it so they can run further
- the trailing stop keeps cutting tiny gains → loosen it a little
- the lowest (or highest) third of entry market caps clearly loses while the rest make money → raise the min (or lower the max) market cap

The limits on what it may change:
- It only touches exits and entry rules, never trade size, max open trades or the daily loss cap.
- It needs at least 20 closed trades (`REVIEW_MIN_TRADES`).
- It makes at most 3 changes, each one at most 25%.
- After a change it waits for 20 new trades, then undoes the change if those trades did clearly worse.

The **Self-review** card on the dashboard shows the last review, what changed and why, and has **Review now** and **Undo last review**; changes are also posted to Discord. `./memebot review` prints what it sees without changing anything.

In `.env`, `REVIEW_MODE=apply` (the default) lets it change settings itself, `suggest` only shows a suggestion with an **Apply suggestion** button, and `off` turns it off. These are simple rules, not a guarantee: a day of meme-coin trades is a small, noisy sample, so the limits above are there to keep a bad day from steering it far. It runs in live mode too; use `suggest` there if you want to approve each change.

## Remote control from your phone

Watch and control the bot from your phone, with the same dashboard (sized for phones). Your normal
`localhost:8787` dashboard stays private; remote control is a **second port that needs an access key**.

1. On your computer: `./memebot remote on`, then (re)start the bot. `./memebot remote` prints the address and your access key.
2. On your phone (same Wi-Fi), open the address, e.g. `http://192.168.1.20:8788`, and enter the key.
3. In Chrome, menu → **Add to Home screen** (or **Install app**) and it opens like an app.
4. Pause or resume buying, sell a position, **Sell all** (pauses buying and sells every open position, with a confirmation), and edit strategy settings, all with the same limits as on the computer (0.5 SOL per trade, 5 open).

**Away from home: use Tailscale.** Install [Tailscale](https://tailscale.com) on the computer and the phone (free for personal use, same login on both). When it's installed, the bot listens **only** on the Tailscale address, which means nobody else on any network can reach it, and everything is encrypted. Use the `http://100.x.y.z:8788` address that `./memebot remote` prints. Without Tailscale it listens on your local network over plain HTTP, so the key could be read by someone on a Wi-Fi you don't trust. **Never forward port 8788 on your router.**

What protects it:
- Nothing is reachable without the key: not the page, the data, or the buttons. The key is 24 random characters and never goes in a URL.
- 5 wrong keys from one address locks that address out for 15 minutes. You get a Discord message when someone signs in or gets locked out, and it shows in the activity feed.
- A login lasts 30 days on that phone. **Sign out** is at the bottom of the page. `./memebot remote new` makes a new key and signs every phone out. If a phone is lost, do that.
- It can only do what the dashboard can: it can't change the wallet, read your keys, or raise the 0.5 SOL / 5 position limits.

It needs the computer on and the bot running; the phone can't start it. Turn off with `./memebot remote off`. To try the phone login with simulated trades and no wallet: `./memebot demo --remote`.

## Setup (once, manual alternative to the launcher)

You need Python 3.10 or newer ([python.org](https://www.python.org/downloads/)).

```bash
cd solana-memebot
python -m venv .venv
# Windows: .venv\Scripts\activate     Mac/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env
python test_strategy.py     # should print "all strategy tests passed"
```

The research tools (`analyze_wallets.py`, `x_monitor.py`, `backtest/`) need no installs beyond Python.

## Recommended order

1. **Analyze the wallets.** Get a free key at [helius.dev](https://helius.dev), then run:
   `python analyze_wallets.py --helius-key YOUR_KEY --days 30`
   This writes `wallets/best_wallets.txt` with the wallets worth copying.
2. **Start the X monitor** in a second window. Put a [twitterapi.io](https://twitterapi.io) key in `.env`, then run:
   `python x_monitor.py`
3. **Run the bot in paper mode:** `python bot.py`
4. **After a week or more,** check `trades.csv`, re-run step 1 with `--bot-log wallet_trades.csv` added, and check `python x_monitor.py --report`.
5. **Go live only if paper results are profitable** (see below).

## The bot

**Launch sniping.** Watches each new pump.fun token for 20 seconds. It buys if there are 8+ real buyers, 1+ SOL net inflow, the dev holds 10% or less and hasn't sold, and the market cap is 30–120 SOL.

**Wallet tracking.** Every trade by every wallet in `TRACKED_WALLETS` (your 4 plus the 494 from your Axiom export) is logged to `wallet_trades.csv`. Only wallets in `COPY_WALLETS` are copied. Copying 494 wallets would trade constantly and burn fees, so copying starts empty until the analyzer fills `best_wallets.txt`.

**Exits.** +60% take profit, −25% stop loss, 20% trailing stop, or 5 minutes, whichever comes first. A copied position is also sold when the wallet you copied sells.

**Risk.** 0.5 SOL per trade, 5 positions at once, and no new trades after losing 1.5 SOL in a day.

Every setting lives in `.env`. Stop the bot anytime with `Ctrl+C`.

## Discord alerts

- **Server channel:** a message for every buy and sell (token, amount, reason, P&L, links). In Discord, open Server Settings → Integrations → Webhooks → New Webhook, pick the channel, then Copy Webhook URL into `DISCORD_WEBHOOK_URL`.
- **DM to you:** sent when a trade closes at **+100% or more** (change `DISCORD_DM_MIN_PNL_PCT` to adjust). Fill in `DISCORD_BOT_TOKEN` with a Discord bot that's in your server, and `DISCORD_USER_ID` with your own ID (Settings → Advanced → Developer Mode, then right-click your name → Copy User ID). Your Discord privacy settings must allow DMs from server members.

Alerts are labeled 📝 paper or 🔴 LIVE and are sent in the background, so Discord being slow or down never delays a trade. P&L in alerts is the bot's estimate from bonding-curve prices; Solscan has the exact on-chain fill.

## Discord signals: reading your servers for coin calls

The bot can watch your Discord servers, pull every pump.fun address people post, and measure what
those coins did afterwards — so you learn which servers and which callers are actually worth following.

**Setup (one time):**

1. Go to [discord.com/developers/applications](https://discord.com/developers/applications) → your app → **Bot**, and turn on the **Message Content Intent**. Without it Discord sends empty messages and nothing is detected.
2. Invite the bot to a server: **OAuth2 → URL Generator**, scopes `bot`, permissions **View Channels** + **Read Message History**. You need *Manage Server* in that server to add a bot to it.
3. `./memebot dservers` prints each server your bot can read, with its ID.
4. Put the IDs (or single channel IDs) in `discord_servers.txt`. Leave it empty to read every server the bot is in.
5. `DISCORD_BOT_TOKEN` in `.env` is the same token used for DM alerts.

**What you get:**

- **Discord buzz** panel in the dashboard: which coins are being called right now and by how many different people
- `discord_calls.csv`: every address posted, who posted it, which server and channel, and a link to the message
- `discord_outcomes.csv`: for each called coin, the price change at +5 min, +15 min and +1 hour, and its peak multiple
- `./memebot dreport` (or `python analyze_discord.py`) scores **servers**, **callers**, **how many people called it**, and **hour of day**, using median moves and hit rate rather than averages, which one 40x would distort

**Using it as a buy filter:** set *Min Discord callers* in the dashboard (Discord group). At 2, the bot only
buys a launch that at least two different people have posted in your servers, **on top of** all the usual
filters. At 0 (the default) nothing is blocked and calls are still logged and scored. A Discord message can
only ever add a reason to skip — it never sizes a trade, bypasses a filter, or triggers a buy by itself.

**Limits worth knowing:**

- A bot can only read servers it has been **added to**. For a public server you don't run (like the invite links you sent), you'd need an admin there to add your bot. Reading Discord with your own account instead is against Discord's terms and gets accounts banned, so this bot doesn't do it.
- Treat call channels as marketing. The report exists precisely because most of them lose money; check the numbers before you let any server influence a trade.

## Wallet analyzer

`python analyze_wallets.py --helius-key KEY` rebuilds every position each wallet opened over the last 30 days and gives each one a verdict:

- **COPY CANDIDATE:** profitable, enough trades, holds long enough to copy, and still profitable after an assumed 10% copy penalty for buying after them
- **profitable but too fast to copy:** snipers or bots that flip in seconds; you'd always be late
- **losing money**, **inactive**, **not enough trades yet**

Output in `wallet_analysis/`:
- `wallet_scorecard.csv`: P&L, win rate, median hold time and more for each wallet
- `consensus.csv`: whether trades turn out better when 3 or more of your wallets buy the same token
- `similar_wallets.csv`: pairs that buy the same tokens, likely the same person or one following the other

The first run downloads a lot (494 wallets × up to 1,000 swaps), so it takes a while and uses Helius credits. Later runs use the cache. `--max-pages 3` makes it lighter.

## X monitor

`python x_monitor.py` checks the accounts in `x_accounts.txt` every 60 seconds and pulls out:
- **contract addresses**, including ones inside pump.fun, dexscreener, axiom, gmgn and birdeye links
- **$tickers**
- **market keywords**

When an account posts a contract address, the monitor records the price at that moment and again at +15 minutes, +1 hour and +4 hours. `python x_monitor.py --report` then shows each account's average move and hit rate after posting.

- **Cost:** twitterapi.io runs about $2–3 per day for 11 accounts polled every minute. The official X API (`X_BEARER_TOKEN`) is pay-per-use, at about $0.005 per post read.
- **Low signal on some accounts:** @realDonaldTrump rarely posts on X, so expect little from it. @elonmusk posts a lot, mostly unrelated to the market.
- **Not wired into trading:** the monitor only logs and alerts; it doesn't trade. Influencers often post coins they already hold, so measure their calls with `--report` before acting on them.

## Backtesting

```bash
cd backtest
python download_data.py                                   # free: about the last 6 months
python download_data.py --api-key CG_KEY --start 2025-01-01   # paid CoinGecko key: back to 2021
python backtest.py --start 2025-01-01                      # bot strategy, 2025 to today
python backtest.py --strategy momentum --start 2022-01-01 --end 2022-12-31
```

Costs are charged on every buy and sell:
- 1% swap fee and 1% platform fee
- a transaction fee
- base slippage plus volatility slippage
- price impact from pool liquidity
- one candle of delay before each fill
- stops that gap down fill at the worse price

The report shows profit before and after costs, which usually explains most of the difference. Results go to `backtest/results/`, including `report.html` with an equity chart.

**Limits of the backtest:**
- 5-minute candles can't see what the live bot sees in its first 20 seconds (unique buyers, dev selling), so the bot strategy is approximated, and it enters about 5 minutes later than the live bot would.
- The token list comes from pools that still exist today, so tokens that died early are underrepresented. That makes results look better than reality.
- **2022:** pump.fun launched in January 2024, so the bot strategy has nothing to trade in 2022. Use `--strategy momentum`, and add 2022 coins yourself with `download_data.py --tokens-file`.

## Testing more aggressive settings on this year's data

```bash
./memebot research              # downloads data since 2026-01-01 (free, slow, resumable), then tests variants
./memebot research 2025-06-01   # a different start date
```

`backtest/sweep.py` tries variants of the exits and market-cap range on the downloaded candles. It **chooses on the first 70% of the period and judges on the last 30%**, only counts variants that trade at least as often as your current settings, and writes a suggestion only if it also made money on the unseen 30% and beat your current settings there. If nothing did, it says so; that is a real answer. A suggestion shows up in the dashboard under **Load a profile → Research result**. The same limits as the backtest apply (5-minute candles can't see the first seconds of a launch, and the data only has tokens that survived long enough to be listed).

## Putting it on GitHub

```bash
./memebot export
```

makes `solana-memebot-github.zip` next to the project folder. Before it writes anything it scans every file it would include for secrets (wallet keys, Discord tokens and webhooks, anything matching a value in your `.env`) and stops if it finds one. It leaves out `.env`, the remote-control key, your saved settings, trade/wallet/Discord logs, downloaded market data, `.venv` and caches. **Create the repository as Private**: the zip still contains your tracked-wallet list and strategy.

Unzip it, then either use [GitHub Desktop](https://desktop.github.com) (File → Add local repository → Publish, tick *Keep this code private*) or:

```bash
cd solana-memebot
git init -b main && git add . && git commit -m "Memebot"
git remote add origin https://github.com/YOUR-NAME/YOUR-REPO.git && git push -u origin main
```

GitHub's web uploader accepts at most 100 files at a time, so use one of these two instead of dragging files into the browser. If you ever paste a key or token into a file by mistake, don't just delete it: rotate it (a new key from the service), because git history keeps old versions.

## Putting it on a flash drive

```bash
./memebot portable
```

makes `solana-memebot-portable.zip` next to the project folder: the bot, dashboard and tools plus your strategy settings and trade history (so the self-review keeps learning), with the same secret scan as the GitHub zip. Your `.env` and remote key are left out. Unzip it onto the drive and on any Linux or Mac computer run `bash memebot` in that folder (FAT32/exFAT drives drop the executable flag, so `bash memebot` always works). The first run on each computer needs internet to install packages; on a FAT32/exFAT/NTFS drive the Python environment goes in `~/.local/share/memebot/` on that computer instead of on the drive. `README-FLASH-DRIVE.txt` in the zip has the steps. A flash drive can be lost, so if you put your wallet key on it, keep only a small amount of SOL in the bot wallet.

## Going live

1. In Phantom, create a **new account** just for the bot. Fund it with only what you can afford to lose; with these limits, about 3 SOL covers 5 trades plus fees.
2. Put that account's private key into `.env` as `BOT_WALLET_PRIVATE_KEY` (or paste it into **Customize → Wallet key…** on the dashboard on your computer). **Never paste it anywhere else, including chat or Discord.**
3. Set `RPC_URL` to a Helius or QuickNode URL, then set `LIVE_TRADING=true`.

**Important:** Most pump.fun tokens go to zero, and paper results are optimistic. Copy trading always gets a worse price than the wallet you copy.
