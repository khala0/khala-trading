"""
Khala Trading -- MT5 Auto-Executor
------------------------------------
Runs on your Windows PC/VPS, next to a running MetaTrader 5 terminal that is
already logged into your Exness account. Polls your deployed Khala Trading
signal API, and places real trades on Exness when a setup clears every
safety check in executor_core.py.

REQUIREMENTS (install on the Windows machine, not in the cloud):
  pip install MetaTrader5 requests

SETUP:
  1. Open MetaTrader 5, log into your Exness account, leave it running.
  2. Edit the CONFIG section below (API URL, symbols, risk limits).
  3. Run: python mt5_executor.py
  4. To stop trading instantly at any time: create a file named KILL_SWITCH
     in this same folder (e.g. `type nul > KILL_SWITCH` on Windows). Delete
     it to resume.

IMPORTANT: Start on your Exness DEMO account first. Change to your live
account only once you've watched it place sensible trades for a while.
"""

import os
import sys
import time
import requests

sys.path.insert(0, os.path.dirname(__file__))
from executor_core import ExecutionConfig, DailyState, should_execute

try:
    import MetaTrader5 as mt5
except ImportError:
    print("ERROR: MetaTrader5 package not installed. Run: pip install MetaTrader5")
    print("(This only works on Windows, alongside a running MT5 terminal.)")
    sys.exit(1)


# ============================= CONFIG =============================

API_BASE_URL = os.environ.get('KHALA_API_URL', 'https://khala-trading.onrender.com')

# Map your internal symbol names to the exact symbol name Exness uses in
# YOUR MT5 terminal. Exness often appends suffixes (e.g. "XAUUSDm" or
# "EURUSDpro") depending on account type -- check your MT5 "Market Watch"
# panel for the EXACT spelling and copy it here.
SYMBOL_MAP = {
    'XAUUSD': 'XAUUSDm',
    'EURUSD': 'EURUSDm',
    'GBPUSD': 'GBPUSDm',
    'USDJPY': 'USDJPYm',
    'BTCUSD': 'BTCUSDm',
}

POLL_INTERVAL_SECONDS = 60  # how often to check for a new setup, per symbol

EXECUTION_CONFIG = ExecutionConfig(
    min_score=8,                 # only auto-execute A+ setups (8+/10)
    max_open_positions=3,        # across the whole account
    max_positions_per_symbol=1,  # no doubling up on the same pair
    max_daily_loss_usd=100.0,    # stop trading for the day past this loss
    max_daily_trades=6,          # cap on trades per day
    allowed_symbols=list(SYMBOL_MAP.keys()),
)

ACCOUNT_RISK_PERCENT = 1.0  # % of account balance risked per trade

# ====================================================================


def get_account_balance():
    info = mt5.account_info()
    if info is None:
        raise RuntimeError("Could not read MT5 account info -- is the terminal running and logged in?")
    return info.balance


def get_open_positions():
    positions = mt5.positions_get()
    if positions is None:
        return []
    # Map back from MT5 symbol names to our internal names for the safety checks
    reverse_map = {v: k for k, v in SYMBOL_MAP.items()}
    return [
        {'symbol': reverse_map.get(p.symbol, p.symbol), 'mt5_symbol': p.symbol, 'ticket': p.ticket}
        for p in positions
    ]


def fetch_signal(symbol, balance):
    resp = requests.get(
        f"{API_BASE_URL}/api/signal/{symbol}",
        params={'balance': balance, 'risk': ACCOUNT_RISK_PERCENT},
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def place_order(setup):
    """Places a real market order on Exness via MT5 based on the setup."""
    mt5_symbol = SYMBOL_MAP[setup['symbol']]

    symbol_info = mt5.symbol_info(mt5_symbol)
    if symbol_info is None:
        print(f"  Symbol {mt5_symbol} not found in MT5 -- check SYMBOL_MAP spelling against Market Watch.")
        return None
    if not symbol_info.visible:
        mt5.symbol_select(mt5_symbol, True)

    tick = mt5.symbol_info_tick(mt5_symbol)
    order_type = mt5.ORDER_TYPE_SELL if setup['direction'] == 'bearish' else mt5.ORDER_TYPE_BUY
    price = tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask

    # Round lot size to the broker's allowed step (usually 0.01)
    lot_step = symbol_info.volume_step or 0.01
    lot = max(symbol_info.volume_min, round(setup['lot_size'] / lot_step) * lot_step)

    request = {
        'action': mt5.TRADE_ACTION_DEAL,
        'symbol': mt5_symbol,
        'volume': lot,
        'type': order_type,
        'price': price,
        'sl': setup['sl_price'],
        'tp': setup['targets']['tp2'],  # using TP2 as the main broker-side target
        'deviation': 20,
        'magic': 20260709,  # arbitrary ID to identify this bot's trades in MT5 history
        'comment': f"KhalaTrading A+ score{setup['score']}",
        'type_time': mt5.ORDER_TIME_GTC,
        'type_filling': mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        print(f"  ORDER FAILED: retcode={result.retcode}, comment={result.comment}")
        return None

    print(f"  ORDER PLACED: {mt5_symbol} {setup['direction'].upper()} {lot} lots @ {price}, "
          f"SL {setup['sl_price']}, TP {setup['targets']['tp2']}, ticket #{result.order}")
    return result


def main():
    if not mt5.initialize():
        print(f"Failed to initialize MT5: {mt5.last_error()}")
        print("Make sure MetaTrader 5 is open and logged into your Exness account.")
        sys.exit(1)

    print(f"Connected to MT5. Account: {mt5.account_info().login}, "
          f"Balance: {mt5.account_info().balance} {mt5.account_info().currency}")
    print(f"Polling {API_BASE_URL} every {POLL_INTERVAL_SECONDS}s for symbols: {list(SYMBOL_MAP.keys())}")
    print("Create a file named KILL_SWITCH in this folder anytime to halt trading instantly.\n")

    daily_state = DailyState(persist_path='daily_state.json')

    try:
        while True:
            balance = get_account_balance()
            open_positions = get_open_positions()

            for symbol in SYMBOL_MAP:
                try:
                    setup = fetch_signal(symbol, balance)
                except requests.RequestException as e:
                    print(f"[{symbol}] Failed to fetch signal: {e}")
                    continue

                ok, reason = should_execute(setup, EXECUTION_CONFIG, daily_state, open_positions)
                status = setup.get('status', 'N/A')
                print(f"[{symbol}] score={setup.get('score', '-')} status={status} -> "
                      f"{'EXECUTE' if ok else 'skip'} ({reason})")

                if ok:
                    result = place_order(setup)
                    if result is not None:
                        daily_state.record_trade()
                        open_positions.append({'symbol': symbol})  # avoid double-firing this loop

            time.sleep(POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        mt5.shutdown()


if __name__ == '__main__':
    main()
