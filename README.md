# DeckBot

A hands-off trend-following crypto bot for **Kraken** and **Coinbase** that runs on your Steam Deck,
including while you're in Game Mode.

## Install (5 minutes)
1. Switch the Deck to **Desktop Mode** and download `deckbot.zip`.
2. Extract it to your home folder so you have `/home/deck/deckbot`.
3. Open **Konsole** and run:
   ```
   cd ~/deckbot && ./install.sh
   ```
4. Answer the setup questions. The bot starts in **paper mode**: real prices, pretend money.

That's it. It runs in the background from then on and starts again by itself after a reboot.

## Going live
Let it paper-trade for a few weeks first, then check `./deckbot status`. When you're ready:
```
./deckbot go-live
```
It asks for your API keys, checks they work, and requires you to type `GO LIVE`.

**API key permissions:** allow trading only. Never enable withdrawals or transfers.
- **Kraken:** Settings → API → Create key. Tick Query Funds, Query Open/Closed Orders,
  Create & Modify Orders, Cancel/Close Orders.
- **Coinbase:** coinbase.com/settings/api → Create key with **Trade** permission. Copy the
  downloaded `cdp_api_key.json` to the Deck and give the wizard its path.

## What it does
It trades spot only and never shorts. It works on 4-hour candles for BTC, ETH and SOL by default.

- **Buys** when a coin is in an uptrend (EMA 21 above EMA 55, price above a rising EMA 100) *and*
  closes at a new 20-candle high.
- **Sizes** each trade so a stop-out loses a fixed share of the bot's money. It never puts more
  than 40% into one coin, and holds at most 3 coins at once.
- **Sells** when the trend breaks (a close below EMA 55) or the trailing stop is hit. The stop
  starts 2.5 ATR below the entry and trails 3 ATR below the highest close.
- **Protects positions while the Deck sleeps** by placing real stop orders on the exchange.
  On Kraken these are stop-loss market orders. On Coinbase they are stop-limit orders with a
  5% cushion.
- **Uses only the budget you set.** It never touches other coins in your account.
- **Lets a self-learning AI check each buy** (see below).

| Risk profile | Risk per trade | Daily loss limit | Kill-switch |
|---|---|---|---|
| conservative | 1% | 3% | −15% |
| moderate | 2% | 5% | −20% |
| **aggressive** (yours) | 4% | 10% | −30% |

When the **daily loss limit** is hit, the bot makes no new buys until the next UTC day. When the
**kill-switch** trips, new buys pause until you run `./deckbot resume`. In both cases the bot keeps
managing the stops on positions it already holds.

## Commands
Run these from `~/deckbot`:

| Command | What it does |
|---|---|
| `./deckbot status` | equity, open positions, recent trades, AI test results |
| `./deckbot learn` | retrain the AI now and show how it scored |
| `./deckbot logs` | recent activity |
| `./deckbot pause` / `resume` | stop or allow new buys |
| `./deckbot sell-all` | close everything the bot holds and stop it |
| `./deckbot go-live` / `paper` | switch modes |
| `./deckbot stop` / `start` | stop or start the background bot |
| `./deckbot risk conservative` | change how aggressive it trades (takes effect at the next market check, within 5 minutes) |
| `./deckbot dashboard` | show the link to the web dashboard |
| `./deckbot report` | paper results and whether it's ready to go live |
| `./deckbot budget kraken 750` | change a budget without resetting anything |
| `./deckbot keys kraken` | enter or replace an exchange's API keys (checked before saving) |
| `./deckbot coins BTC,ETH,SOL` | change which coins it trades |
| `./deckbot backup` / `restore` | encrypted backup of keys, settings and history / bring one back |
| `./deckbot alerts my-long-topic` | turn on phone alerts (`alerts test` sends a test, `alerts none` turns them off) |
| `./deckbot setup` | change exchanges, budget, coins or risk |

**Web dashboard and Quick Access panel:** the bot runs a control panel on the Deck. Run
`./deckbot dashboard` to get the link. The dashboard shows equity, profit over time, a **Ready to go
live?** report, positions, trades, the activity log and the AI, with buttons for risk level, pause,
paper/live mode, budgets, phone alerts and emergency sell, plus **Download all trades (CSV)**. Going
live asks you to type `GO LIVE`; emergency sell asks you to type `SELL`. `./deckbot dashboard lan on`
lets your phone on the same Wi-Fi open it too. The separate **Bot Panel** Decky plugin adds the
main controls to the Quick Access menu. The panel only listens on this Deck unless LAN is on, and
anyone with the link can trade your account, so keep it private.

**Phone alerts:** install the free **ntfy** app and subscribe to your topic (set it with
`./deckbot alerts <topic>` or on the dashboard; pick a long, private name). You'll get every buy,
every sell, any problem, a warning if the bot stops checking the market, and a weekly summary. A
separate watchdog checks every 10 minutes, so you hear about a stalled bot even if the bot is stuck.

**Ready to go live?** `./deckbot report` (also on the dashboard and panel) shows days running,
closed trades, winners, profit or loss, the worst drop, and how simply holding Bitcoin did over the
same time. It says to keep waiting until there are at least 30 days and 6 closed trades, then
whether paper trading lost money, made less than holding Bitcoin, or did better.

**Keys from the dashboard or Quick Access panel:** the 🔑 Keys card lets you enter Kraken keys or pick
your Coinbase `cdp_api_key.json` file (the panel finds it in Downloads). Keys are checked with the
exchange before they're saved, stored only in `data/keys.json`, and never shown again: the screens
only say "✓ set (…ab12)". For safety the dashboard only accepts keys on the Deck itself, never over
Wi-Fi, and keys can't be swapped while live positions are open. Exchange API keys can only be
created on the exchange's own website; the card shows how.

**Pop-ups while gaming:** the Bot Panel plugin shows a Steam notification for every buy and sell, if a
bot stops checking the market, and if it hits its daily loss limit, even with Quick Access closed.
Turn it off in the panel's ⚙️ section.

**Budget:** `./deckbot budget kraken 750` (or the Budget box on the dashboard) adds or takes back
money without resetting the bot. Money in an open position can't be taken back until it's sold.
In live mode, raising the budget checks that the exchange account has enough free.

## The AI
A trend bot only gets a few buy signals a month. That's far too few to learn from its own trades.
So once a day, DeckBot replays its exact strategy over about four months of 4-hour candles for
20 liquid coins (BTC, ETH, SOL, XRP and others). It records every historical breakout and whether
the bot's own exit rules would have made or lost money on it. Then it trains a model on 12
measurements taken at the moment of each breakout:

- trend strength and slope
- volatility
- how far the price broke out
- breakout volume
- RSI and recent momentum
- how much room is left to the recent high
- what Bitcoin was doing

**The AI has to prove itself before it acts.** It's tested walk-forward: it trains only on
earlier periods and is scored on later ones it has never seen. It starts in **shadow mode**,
scoring signals without acting on them. It only turns itself on when all of these hold:

- at least 80 test signals
- an accuracy score (AUC) of at least 0.57, where 0.50 is a coin flip
- the signals it approves made clearly more money than taking every signal

Once on, it skips breakouts it rates below average. It never adds trades the strategy wouldn't
take. Every daily retrain re-runs the test, so if markets change and it stops helping, it switches
itself back off.

Run `./deckbot learn` once after setup to see right away whether it found anything. A sample
of the output it prints:

```
AI: shadow mode | 231 historical signals, walk-forward test on 139: AUC 0.54
    take every signal: +0.31R avg (38% winners) | only AI-approved: +0.29R avg
```

"R" is the amount risked per trade. In this example the AI isn't adding anything yet, so it
stays out of the way. It's normal for it to stay in shadow mode for long stretches. Real crypto
patterns are weak, and the bot won't pretend otherwise.

## Backups and updates
- **💾 Back up** (dashboard, Quick Access panel, or `./deckbot backup`): saves your keys, settings, AI training and trade history to an
  SD card or USB stick if one is inserted, otherwise to `~/Documents/bot-backups`. The backup is
  encrypted with a password you choose. Write the password down: without it the backup can't be
  opened, and nobody can recover it.
- **♻️ Restore** (same places, or `./deckbot restore`): the bot stops, your current data is moved aside to
  `data.before-restore-…` (never deleted), the backup is restored, and the bot starts again.
- **After reinstalling SteamOS:** extract the zip into your home folder and run `./install.sh`. If it
  finds a backup on your SD card, USB stick or in Documents, it offers to restore it instead of setting
  up from scratch.
- **⬆️ Updates:** download the new `deckbot.zip` on the Deck (into Downloads), then press **Install update**
  on the dashboard or in the Quick Access panel. Only a genuine DeckBot zip is accepted. Your `data/`
  folder is never touched, and the previous version is kept in `.previous/`.
- For safety, backups, restores and updates only work on the Deck itself (or in the Quick Access panel),
  never from another device over Wi-Fi.

## Things to know
- **Expect losses on most trades.** Trend-following usually loses on more trades than it wins
  and relies on a few big trends to come out ahead. On the aggressive profile, three or four
  stop-outs in a row can cost 12–16% of the budget. Sideways markets slowly bleed money. No
  strategy guarantees profit, so only use money you can afford to lose.
- **Sleep:** the bot can't buy or update stops while the Deck is asleep. Exchange-side stops
  cover the downside in the meantime. Leave it plugged in with sleep off if you want it fully
  active.
- **SteamOS updates** can break the Python environment. If `./deckbot` says so, run
  `./install.sh` again. Your settings, keys and positions are kept.
- **Your keys** are stored in `data/keys.json`, readable only by your user.
- **Taxes:** every sell is a taxable event in most countries. Both exchanges let you export
  your trade history.
- All strategy settings live in `data/config.json` if you want to tune them.
