# Auto-Trading Setup: Khala Trading -> Exness (MT5)

This connects your deployed signal engine to your Exness account so it can
place trades automatically. It runs on your **Windows PC or VPS**, separate
from your Render deployment (Render only hosts the dashboard + signal API).

## Before you start

- **Test on a demo account first.** Exness lets you create a free demo MT5
  account -- use that until you've watched the bot place sensible trades
  for at least a couple weeks. Only switch to your live account once you
  trust it.
- Read through `mt5_executor.py`'s CONFIG section before running it. The
  risk limits (`max_daily_loss_usd`, `min_score`, etc.) are yours to tune.

## 1. Install MetaTrader 5 and log into Exness

If not already installed, download MT5 from Exness's site and log in with
your account credentials (demo or live). Leave it running in the background
-- the executor script talks to this running terminal, it doesn't log in
itself.

## 2. Install Python on Windows (skip if already installed)

Download from https://python.org -- during install, check "Add Python to PATH".

## 3. Install the required packages

Open Command Prompt or PowerShell:
```
pip install -r requirements-mt5.txt
```

## 4. Find your exact Exness symbol names

Open MT5, look at the "Market Watch" panel (View -> Market Watch if hidden).
Exness often appends suffixes to symbol names depending on account type --
for example `XAUUSDm` instead of `XAUUSD`. Note the exact spelling for each
symbol you want to trade.

## 5. Edit the config in `mt5_executor.py`

Open the file and update the `CONFIG` section near the top:

- `API_BASE_URL` -- your live Render URL, e.g. `https://khala-trading.onrender.com`
- `SYMBOL_MAP` -- match each key to the exact Exness symbol name from step 4
- `EXECUTION_CONFIG` -- your risk limits:
  - `min_score`: only auto-execute setups scoring this or higher (out of 10)
  - `max_open_positions`: cap on total open trades at once
  - `max_positions_per_symbol`: usually 1, to avoid doubling up
  - `max_daily_loss_usd`: trading halts for the day once hit
  - `max_daily_trades`: cap on number of trades per day
- `ACCOUNT_RISK_PERCENT`: % of account balance risked per trade

## 6. Run it

```
python mt5_executor.py
```

You'll see it connect to MT5, then start polling your signal API every 60
seconds (configurable via `POLL_INTERVAL_SECONDS`), printing what it decides
for each symbol and why.

## 7. The kill switch

At any point, to stop all auto-trading instantly without closing the
program:
```
type nul > KILL_SWITCH
```
Delete that file to resume. This check happens before anything else, every
single loop -- it can't be bypassed by a logic bug elsewhere in the script.

## 8. Keeping it running 24/7

If you want this running around the clock without your PC staying on:
- Rent a cheap Windows VPS (many providers offer these for $5-15/month)
- Install MT5 + Python + this script on the VPS the same way
- Leave both running continuously

## What this does NOT do

- It does not manage your emotions or override your own judgment -- you can
  stop it anytime with the kill switch.
- It does not guarantee profit. The signal engine's scoring is a starting
  point, not a proven strategy -- back-test and demo-test before trusting
  it with real money.
- It does not currently handle partial take-profits, breakeven-stop moves,
  or trailing stops after entry -- it places the order with SL/TP2 and then
  leaves it to run. Let me know if you want that added.
