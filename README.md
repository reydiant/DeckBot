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
| `./deckbot setup` | change exchanges, budget, coins or risk |

**Phone alerts:** install the free **ntfy** app and subscribe to the topic you chose in setup.
You'll get a notification for every buy, every sell, and any problem.

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
