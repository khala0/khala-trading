"""
Khala Trading -- MT5 Auto-Executor
------------------------------------
Runs on your Windows PC/VPS, next to a running MetaTrader 5 terminal that is
already logged into your Exness account. Polls your deployed Khala Trading
signal API, and places real trades on Exness when a setup clears every
safety check in executor_core.py.

FIXED 2026-07-17 -- five execution-layer issues found during review:

1. Entry price drift. setup['entry_price'] is a Yahoo-sourced candle close
   from whenever the server last computed the signal -- potentially minutes
   old by the time this poll loop gets to it. sl_price and all three
   targets were absolute prices computed against THAT entry, but
   place_order() then submitted at the LIVE MT5 tick price while keeping
   the old sl/tp unchanged -- silently changing the real stop distance (and
   therefore the real % risked) from what was scored and sized. Fixed by
   re-deriving sl/tp as DISTANCES from the signal's entry_price, then
   re-applying those distances to the live tick at execution time -- plus a
   drift guard that aborts the trade if price has already moved too far
   from the signal's entry_price to trust it (MAX_DRIFT_FRACTION below).

2. Only TP2 was ever used -- no partial close, no breakeven move, so TP1
   and TP3 had zero effect on managed trades despite the scoring model
   assuming a scaled 1R/2R/3R exit. Added manage_open_positions(), which
   runs every loop iteration: once price reaches TP1, closes
   PARTIAL_CLOSE_FRACTION of the position and moves the stop to breakeven;
   the remainder keeps running to the broker-side TP2 (or the new breakeven
   stop). TP3 still isn't mechanically wired in -- it's persisted for
   reference, but treat it as a manual/future-enhancement target for now.

3. The max-daily-loss circuit breaker in executor_core.py was fully wired
   into should_execute() but never actually fed a number: nothing called
   daily_state.record_closed_pnl() anywhere, so it could never trip.
   manage_open_positions() now detects when a tracked position fully
   closes, sums its real P&L from MT5's own deal history, and records it.

4. No resilience: a single transient MT5/account-info hiccup raised past
   the only `except KeyboardInterrupt` and killed the whole 24/7 process,
   with nothing logged to a file and no way to know it had happened. Now
   logs to both console and a file next to this script, wraps each loop
   iteration so a transient error is logged and the loop continues instead
   of dying, and pings Telegram on startup/shutdown/crash and when the
   daily loss limit trips.

5. Kill switch path was relative to the process's working directory, not
   this script -- fixed in executor_core.py (see the notice there), which
   this file already imports is_kill_switch_active() from via should_execute().

Also added a single retry on TRADE_RETCODE_REQUOTE / TRADE_RETCODE_PRICE_CHANGED.

TESTING NOTE: none of the position-management code below (partial close,
breakeven move, deal-history P&L summation) has run against a live MT5
terminal -- there's no Windows/MT5 environment available in the sandbox
this was written in (MetaTrader5's Python package is Windows-only). The
logic is written directly against the documented MetaTrader5 API, but test
it on your DEMO account (which you should already be doing per the setup
instructions below) before trusting it with real capital, and watch the
first few TP1 partial-closes closely.

REQUIREMENTS (install on the Windows machine, not in the cloud):
  pip install MetaTrader5 requests

SETUP:
  1. Open MetaTrader 5, log into your Exness account, leave it running.
  2. Edit the CONFIG section below (API URL, symbols, risk limits).
  3. Run: python mt5_executor.py
  4. To stop trading instantly at any time: create a file named KILL_SWITCH
     next to this script (e.g. `type nul > KILL_SWITCH` on Windows). Delete
     it to resume.

IMPORTANT: Start on your Exness DEMO account first. Change to your live
account only once you've watched it place sensible trades for a while.
"""

import os
import sys
import json
import time
import logging
import requests

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, 'engine'))
from executor_core import ExecutionConfig, DailyState, should_execute
import telegram_client

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
MAGIC_NUMBER = 20260709     # arbitrary ID to identify this bot's trades/partials in MT5 history

EXECUTION_CONFIG = ExecutionConfig(
    min_score=8,                 # only auto-execute A+ setups (8+/10)
    max_open_positions=3,        # across the whole account
    max_positions_per_symbol=1,  # no doubling up on the same pair
    max_daily_loss_usd=100.0,    # stop trading for the day past this loss
    max_daily_trades=6,          # cap on trades per day
    allowed_symbols=list(SYMBOL_MAP.keys()),
)

ACCOUNT_RISK_PERCENT = 1.0  # % of account balance risked per trade

# How far live price may have drifted from the signal's own entry_price
# (as a fraction of the intended stop distance) before we abort rather than
# execute on a stale signal. 0.3 = abort once price has moved 30%+ of the
# stop distance away from where the setup was actually scored.
MAX_DRIFT_FRACTION = 0.3

# Fraction of the position closed once price reaches TP1; the remainder
# keeps running to TP2 (broker-side) with its stop moved to breakeven.
PARTIAL_CLOSE_FRACTION = 0.5

MANAGED_POSITIONS_PATH = os.environ.get('MANAGED_POSITIONS_PATH', os.path.join(_HERE, 'managed_positions.json'))
LOG_PATH = os.environ.get('EXECUTOR_LOG_PATH', os.path.join(_HERE, 'executor.log'))

# ====================================================================


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.FileHandler(LOG_PATH), logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger('khala_executor')

_last_crash_alert_at = 0
CRASH_ALERT_COOLDOWN_SECONDS = 900  # avoid flooding Telegram if an error repeats every loop


def alert(message):
    """Best-effort Telegram ping -- a notification failure should never affect trading."""
    try:
        telegram_client.send_text(message)
    except Exception as e:
        log.warning(f"Could not send Telegram alert: {e}")


def alert_crash(message):
    """Same as alert(), rate-limited so a repeating error doesn't spam Telegram."""
    global _last_crash_alert_at
    now = time.time()
    if now - _last_crash_alert_at >= CRASH_ALERT_COOLDOWN_SECONDS:
        alert(message)
        _last_crash_alert_at = now


def _load_managed_positions():
    if not os.path.exists(MANAGED_POSITIONS_PATH):
        return {}
    try:
        with open(MANAGED_POSITIONS_PATH, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"Could not read {MANAGED_POSITIONS_PATH} ({e}) -- starting with an empty store")
        return {}


def _save_managed_positions(store):
    with open(MANAGED_POSITIONS_PATH, 'w') as f:
        json.dump(store, f, indent=2)


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


def _send_market_order(mt5_symbol, order_type, volume, price, sl, tp, comment):
    """One order_send call, with a single retry against a fresh tick on
    requote / price-changed (common when the market's moving fast)."""
    request = {
        'action': mt5.TRADE_ACTION_DEAL,
        'symbol': mt5_symbol,
        'volume': volume,
        'type': order_type,
        'price': price,
        'sl': sl,
        'tp': tp,
        'deviation': 20,
        'magic': MAGIC_NUMBER,
        'comment': comment,
        'type_time': mt5.ORDER_TIME_GTC,
        'type_filling': mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)

    retryable = {mt5.TRADE_RETCODE_REQUOTE, mt5.TRADE_RETCODE_PRICE_CHANGED}
    if result.retcode in retryable:
        log.warning(f"  Order got retcode={result.retcode} ({result.comment}), retrying once against a fresh tick...")
        tick = mt5.symbol_info_tick(mt5_symbol)
        fresh_price = tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask
        request['price'] = fresh_price
        result = mt5.order_send(request)

    return result


def place_order(setup):
    """Places a real market order on Exness via MT5 based on the setup.

    Re-derives SL/TP1/TP2/TP3 as distances from the setup's own
    entry_price and re-applies those distances to the live tick price at
    the moment of execution (fix #1 in the module docstring) instead of
    blindly reusing the absolute prices the server computed against a
    possibly stale/different-sourced entry -- this is also why lot sizing
    doesn't need to change here: the STOP DISTANCE is preserved exactly,
    only its reference point shifts, so the % risked stays what was
    intended. Aborts (returns None) if live price has already drifted too
    far from the signal's entry_price to trust the setup.
    """
    symbol = setup['symbol']
    mt5_symbol = SYMBOL_MAP[symbol]

    symbol_info = mt5.symbol_info(mt5_symbol)
    if symbol_info is None:
        log.error(f"  Symbol {mt5_symbol} not found in MT5 -- check SYMBOL_MAP spelling against Market Watch.")
        return None
    if not symbol_info.visible:
        mt5.symbol_select(mt5_symbol, True)

    tick = mt5.symbol_info_tick(mt5_symbol)
    direction = setup['direction']
    order_type = mt5.ORDER_TYPE_SELL if direction == 'bearish' else mt5.ORDER_TYPE_BUY
    price = tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask

    signal_entry = setup['entry_price']
    sign = -1 if direction == 'bearish' else 1
    sl_distance = abs(signal_entry - setup['sl_price'])
    tp1_distance = abs(setup['targets']['tp1'] - signal_entry)
    tp2_distance = abs(setup['targets']['tp2'] - signal_entry)
    tp3_distance = abs(setup['targets']['tp3'] - signal_entry)

    drift = abs(price - signal_entry)
    if sl_distance > 0 and drift > MAX_DRIFT_FRACTION * sl_distance:
        log.warning(
            f"  [{symbol}] Skipping: live price {price} has drifted {drift:.5f} from the "
            f"signal's entry_price {signal_entry} -- more than {MAX_DRIFT_FRACTION * 100:.0f}% "
            f"of the {sl_distance:.5f} stop distance. Signal is stale, not executing."
        )
        return None

    digits = symbol_info.digits or 5
    sl = round(price - sign * sl_distance, digits)
    tp1 = round(price + sign * tp1_distance, digits)
    tp2 = round(price + sign * tp2_distance, digits)
    tp3 = round(price + sign * tp3_distance, digits)

    # Round lot size to the broker's allowed step (usually 0.01)
    lot_step = symbol_info.volume_step or 0.01
    lot = max(symbol_info.volume_min, round(setup['lot_size'] / lot_step) * lot_step)

    result = _send_market_order(
        mt5_symbol, order_type, lot, price, sl, tp2,
        comment=f"KhalaTrading A+ score{setup['score']}",
    )
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        log.error(f"  ORDER FAILED: retcode={result.retcode}, comment={result.comment}")
        return None

    log.info(f"  ORDER PLACED: {mt5_symbol} {direction.upper()} {lot} lots @ {price}, "
             f"SL {sl}, TP1(managed) {tp1}, TP2(broker) {tp2}, ticket #{result.order}")

    # Persist enough to manage this position later: partial-close at TP1 +
    # breakeven move, and P&L recording once it fully closes.
    store = _load_managed_positions()
    store[str(result.order)] = {
        'symbol': symbol,
        'mt5_symbol': mt5_symbol,
        'direction': direction,
        'entry_fill_price': price,
        'sl_original': sl,
        'tp1': tp1,
        'tp2': tp2,
        'tp3': tp3,
        'original_volume': lot,
        'partial1_done': False,
        'opened_at': time.time(),
    }
    _save_managed_positions(store)

    return result


def _sum_realized_pnl(ticket):
    """Sums profit + swap + commission across every deal MT5 recorded
    against this position ticket (the opening deal, any partial closes,
    and the final close) -- the true total realized P&L for the position's
    whole lifecycle. Safe to call exactly once, right when the ticket
    disappears from positions_get(), which is how manage_open_positions()
    uses it (avoids double-counting a partial close's P&L)."""
    deals = mt5.history_deals_get(position=ticket)
    if not deals:
        return None
    return round(sum(d.profit + d.swap + d.commission for d in deals), 2)


def manage_open_positions(daily_state):
    """
    Runs every loop iteration. Two jobs:

    1. Partial-close at TP1: for each position this bot opened (tracked in
       MANAGED_POSITIONS_PATH) that hasn't hit TP1 yet, checks the live
       tick -- once price reaches TP1 in the trade's favor, closes
       PARTIAL_CLOSE_FRACTION of the position and moves the stop to
       breakeven (the actual fill price) for the remainder, which keeps
       running to the broker-side TP2 (unchanged) or the new breakeven
       stop, whichever comes first.

    2. P&L recording: once a tracked ticket no longer appears in MT5's open
       positions (closed by SL, TP2, breakeven stop, or manually), sums its
       real realized P&L from MT5's deal history and feeds it to
       daily_state.record_closed_pnl() -- this is what makes the max-daily-
       loss circuit breaker in executor_core.py actually work (previously
       nothing ever called this, so it could never trip).
    """
    store = _load_managed_positions()
    if not store:
        return

    open_by_ticket = {}
    positions = mt5.positions_get()
    if positions:
        open_by_ticket = {p.ticket: p for p in positions}

    changed = False
    for ticket_str, record in list(store.items()):
        ticket = int(ticket_str)
        pos = open_by_ticket.get(ticket)

        if pos is None:
            # No longer open -- closed via SL, TP2, breakeven stop, or
            # manually. Record its real P&L exactly once, then stop tracking it.
            pnl = _sum_realized_pnl(ticket)
            if pnl is not None:
                daily_state.record_closed_pnl(pnl)
                log.info(f"  [{record['symbol']}] Position #{ticket} closed, realized P&L ${pnl:.2f} "
                         f"-- today's total: ${daily_state.realized_pnl:.2f}")
                if daily_state.realized_pnl <= -abs(EXECUTION_CONFIG.max_daily_loss_usd):
                    alert(f"Daily loss limit hit (${daily_state.realized_pnl:.2f}). "
                          f"Auto-execution is halted for the rest of today.")
            else:
                log.warning(f"  [{record['symbol']}] Position #{ticket} no longer open, but no "
                            f"deal history found for it -- could not record its P&L.")
            del store[ticket_str]
            changed = True
            continue

        if record.get('partial1_done'):
            continue  # nothing left to manage here until it closes

        tick = mt5.symbol_info_tick(record['mt5_symbol'])
        if tick is None:
            continue
        is_buy = pos.type == mt5.ORDER_TYPE_BUY
        favorable_price = tick.bid if is_buy else tick.ask
        tp1_reached = favorable_price >= record['tp1'] if is_buy else favorable_price <= record['tp1']
        if not tp1_reached:
            continue

        symbol_info = mt5.symbol_info(record['mt5_symbol'])
        lot_step = symbol_info.volume_step if symbol_info else 0.01
        volume_min = symbol_info.volume_min if symbol_info else 0.01
        close_volume = round(round(pos.volume * PARTIAL_CLOSE_FRACTION / lot_step) * lot_step, 2)
        remainder = round(pos.volume - close_volume, 2)

        if close_volume < volume_min or remainder < volume_min:
            # Too small to split without leaving a sub-minimum remainder --
            # leave it running to TP2/SL untouched rather than send an
            # order the broker would reject.
            log.info(f"  [{record['symbol']}] #{ticket} reached TP1 but is too small to "
                     f"partial-close (vol {pos.volume}) -- leaving it to run to TP2/SL.")
            record['partial1_done'] = True
            changed = True
            continue

        close_type = mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY
        close_price = tick.bid if is_buy else tick.ask
        close_request = {
            'action': mt5.TRADE_ACTION_DEAL,
            'symbol': record['mt5_symbol'],
            'volume': close_volume,
            'type': close_type,
            'position': ticket,
            'price': close_price,
            'deviation': 20,
            'magic': MAGIC_NUMBER,
            'comment': 'KhalaTrading TP1 partial',
            'type_time': mt5.ORDER_TIME_GTC,
            'type_filling': mt5.ORDER_FILLING_IOC,
        }
        close_result = mt5.order_send(close_request)
        if close_result.retcode != mt5.TRADE_RETCODE_DONE:
            log.error(f"  [{record['symbol']}] #{ticket} TP1 partial-close FAILED: "
                      f"retcode={close_result.retcode}, comment={close_result.comment}")
            continue

        breakeven = record['entry_fill_price']
        modify_request = {
            'action': mt5.TRADE_ACTION_SLTP,
            'position': ticket,
            'symbol': record['mt5_symbol'],
            'sl': breakeven,
            'tp': record['tp2'],
        }
        modify_result = mt5.order_send(modify_request)
        if modify_result.retcode != mt5.TRADE_RETCODE_DONE:
            log.warning(f"  [{record['symbol']}] #{ticket} partial-closed {close_volume} lots at TP1, "
                        f"but moving SL to breakeven FAILED (retcode={modify_result.retcode}) -- "
                        f"original SL is still in place, check this position manually.")
        else:
            log.info(f"  [{record['symbol']}] #{ticket} hit TP1: closed {close_volume} lots, "
                     f"moved SL to breakeven ({breakeven}) for the remaining {remainder} lots.")

        record['partial1_done'] = True
        changed = True

    if changed:
        _save_managed_positions(store)


def main():
    if not mt5.initialize():
        log.error(f"Failed to initialize MT5: {mt5.last_error()}")
        log.error("Make sure MetaTrader 5 is open and logged into your Exness account.")
        sys.exit(1)

    acct = mt5.account_info()
    log.info(f"Connected to MT5. Account: {acct.login}, Balance: {acct.balance} {acct.currency}")
    log.info(f"Polling {API_BASE_URL} every {POLL_INTERVAL_SECONDS}s for symbols: {list(SYMBOL_MAP.keys())}")
    log.info(f"Create a file named KILL_SWITCH in {_HERE} anytime to halt trading instantly.")
    alert(f"Khala Trading executor started. Account {acct.login}, balance {acct.balance} {acct.currency}.")

    daily_state = DailyState(persist_path=os.path.join(_HERE, 'daily_state.json'))

    try:
        while True:
            try:
                balance = get_account_balance()
                open_positions = get_open_positions()

                for symbol in SYMBOL_MAP:
                    try:
                        setup = fetch_signal(symbol, balance)
                    except requests.RequestException as e:
                        log.warning(f"[{symbol}] Failed to fetch signal: {e}")
                        continue

                    ok, reason = should_execute(setup, EXECUTION_CONFIG, daily_state, open_positions)
                    status = setup.get('status', 'N/A')
                    log.info(f"[{symbol}] score={setup.get('score', '-')} status={status} -> "
                             f"{'EXECUTE' if ok else 'skip'} ({reason})")

                    if ok:
                        result = place_order(setup)
                        if result is not None:
                            daily_state.record_trade()
                            open_positions.append({'symbol': symbol})  # avoid double-firing this loop

                manage_open_positions(daily_state)

            except Exception as e:
                log.exception(f"Unexpected error in main loop (continuing): {e}")
                alert_crash(f"Khala Trading executor hit an error and is continuing: {e}\n"
                            f"Check {LOG_PATH} for the full traceback.")

            time.sleep(POLL_INTERVAL_SECONDS)

    except KeyboardInterrupt:
        log.info("Stopped by user.")
        alert("Khala Trading executor stopped (manual shutdown).")
    finally:
        mt5.shutdown()


if __name__ == '__main__':
    main()
