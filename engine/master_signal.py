"""
Khala Trading -- Master Signal Function
------------------------------------------
Ties together everything built so far into the one function the API
actually calls:

  1. multi_timeframe_engine.determine_bias()  -- 4H trend (required),
     1H refinement, 5M execution trigger. Direction is LOCKED to the 4H
     trend; nothing below this can override it.
  2. news_filter.is_near_high_impact_news()    -- blocks new signals near
     high-impact economic releases.
  3. signal_engine's ATR-based dynamic SL/TP/position sizing, using the
     4H structure's own swing levels as the invalidation reference (not
     a separate, disconnected swing detection).
  4. A scoring model where the 5M execution trigger is mandatory to reach
     signal-grade (this enforces "5M is execution-only" -- without a real
     trigger candle, the setup cannot score high enough to fire).
"""

import signal_engine
import multi_timeframe_engine
import news_filter
import premium_discount
import crt_sweep
import snr_levels
import fibonacci_zone
import time


def _stable_range(structure):
    """
    Returns (swing_high, swing_low) from the two MOST RECENT swings
    chronologically (whatever their types), not the last swing of each
    type independently. In a strongly trending market, "the last swing low
    ever recorded" can be stale and far away if no pullback has formed in
    a while -- using the two latest swings overall keeps this to the
    current, relevant leg.
    """
    swings = structure['swings']
    if len(swings) < 2:
        return None, None
    last_two_prices = [s['price'] for s in swings[-2:]]
    return max(last_two_prices), min(last_two_prices)


def _no_trade_result(symbol, status, reason, direction=None):
    """
    Every early-return path uses this, so the dict shape is IDENTICAL
    regardless of which branch fired -- callers (the Flask route, the
    frontend, signal_history logging) never need to guard for missing
    keys depending on why a trade wasn't generated.
    """
    return {
        'symbol': symbol, 'direction': direction,
        'entry_price': None, 'sl_price': None, 'atr_value': None,
        'atr_multiplier_used': None, 'targets': None,
        'lot_size': None, 'risk_amount_usd': None, 'stop_distance_pips': None,
        'trend_4h': None, 'trend_1h': None, 'htf_agreement': False, 'execution_ready': False,
        'premium_discount': None, 'crt_sweep': None, 'snr_confluence': None, 'fibonacci_zone': None,
        'score': 0, 'status': status, 'is_signal': False, 'reason': reason,
        'generated_at': time.time(), 'anchor_candle_time': None,
    }


def generate_signal(symbol, candles_4h, candles_1h, candles_5m,
                     account_balance=10000, risk_percent=1.0,
                     pip_value_per_lot=10.0, pip_size=0.0001):
    """
    Returns a dict describing the current signal state for `symbol`. Always
    returns a dict (never raises) -- callers can check 'direction' (None
    means no trade) and 'status' for what's actually going on.
    """
    # --- Step 1: news filter -- checked first, since a blackout should
    # override everything else regardless of how good the setup looks ---
    blocked, event = news_filter.is_near_high_impact_news(symbol)
    if blocked:
        return _no_trade_result(
            symbol, 'NEWS BLACKOUT',
            f"High-impact {event['currency']} news ({event['event']}) near this time -- trading paused as a precaution",
        )

    # --- Step 2: multi-timeframe bias (4H required, 1H refinement, 5M trigger) ---
    bias = multi_timeframe_engine.determine_bias(candles_4h, candles_1h, candles_5m)

    if bias['direction'] is None:
        return _no_trade_result(symbol, 'NO TRADE', bias['reason'])

    direction = bias['direction']
    structure_4h = bias['structure_4h']

    # --- Step 3: SL reference comes from the 4H structure's own swing levels,
    # not a separate/disconnected swing detection ---
    if direction == 'bearish':
        swing_ref = structure_4h['watch_high_level']
    else:
        swing_ref = structure_4h['watch_low_level']

    if swing_ref is None:
        # No confirmed opposite-side swing to anchor a stop to yet.
        return _no_trade_result(
            symbol, 'NO TRADE',
            f'4H trend is {direction}, but no confirmed swing level exists yet to anchor a stop-loss',
            direction=direction,
        )

    entry_price = candles_5m[-1]['close'] if candles_5m else candles_1h[-1]['close']

    sl_data = signal_engine.calculate_dynamic_sl(
        direction, swing_ref, candles_4h, symbol=symbol,
    )

    # Sanity guard, mirrors the check in the original signal_engine
    if direction == 'bearish' and sl_data['sl_price'] <= entry_price:
        return _no_trade_result(
            symbol, 'NO TRADE',
            'Structural reference invalidated by current price -- no clean stop available',
        )
    if direction == 'bullish' and sl_data['sl_price'] >= entry_price:
        return _no_trade_result(
            symbol, 'NO TRADE',
            'Structural reference invalidated by current price -- no clean stop available',
        )

    targets = signal_engine.calculate_targets(direction, entry_price, sl_data['sl_price'])
    sizing = signal_engine.calculate_position_size(
        account_balance, risk_percent, entry_price, sl_data['sl_price'],
        pip_value_per_lot=pip_value_per_lot, pip_size=pip_size,
    )

    # --- Step 4: scoring. Execution trigger is mandatory to reach signal
    # grade -- this is what enforces "5M is execution-only": without a real
    # trigger candle, the setup simply can't score high enough. ---
    execution_pts = 3 if bias['execution_ready'] else 0
    htf_agreement_pts = 4 if bias['htf_agreement'] else 0

    stable_high, stable_low = _stable_range(structure_4h)

    opposite_level = stable_low if direction == 'bearish' else stable_high
    stop_distance = abs(entry_price - sl_data['sl_price'])
    if opposite_level is not None and stop_distance > 0:
        reward_potential = abs(entry_price - opposite_level) / stop_distance
    else:
        reward_potential = 0
    reward_pts = 2 if reward_potential >= 3 else (1 if reward_potential >= 2 else 0)

    range_high, range_low = stable_high, stable_low
    if range_high is not None and range_low is not None:
        pd_analysis = premium_discount.analyze(direction, entry_price, range_high, range_low)
    else:
        pd_analysis = {'equilibrium': None, 'zone': None, 'favorable': False, 'depth': 0}
    pd_pts = 1 if pd_analysis['favorable'] else 0

    ref_high, ref_low = crt_sweep.get_reference_range(candles_1h, lookback_bars=24)
    if ref_high is not None:
        sweep_result = crt_sweep.detect_sweep(candles_1h, ref_high, ref_low)
    else:
        sweep_result = {'swept': False, 'direction': None, 'swept_level': None}
    crt_pts = 0.5 if (sweep_result['swept'] and sweep_result['direction'] == direction) else 0

    # NOTE: two DIFFERENT tolerances, both scaled off ATR so they behave
    # sanely across every symbol (a fixed dollar amount does not -- e.g.
    # $0.15 is ~1000 pips on EURUSD but a rounding error on BTCUSD):
    #   - snr_proximity: how close a level must be to current price to
    #     count as confluence at all.
    #   - snr_level_tolerance: how close two candles' highs/lows/opens/
    #     closes must be to each other to count as "the same level" when
    #     detecting formations in the first place. This one was previously
    #     left at snr_levels.py's hardcoded 0.15 default regardless of
    #     symbol -- worth re-tuning/backtesting the 0.1 multiplier below,
    #     it's a reasonable starting point rather than a calibrated one.
    atr_value = sl_data.get('atr_value')
    snr_proximity = atr_value * 2 if atr_value else 5
    snr_level_tolerance = atr_value * 0.1 if atr_value else 0.15
    snr_result = snr_levels.analyze(
        candles_1h, direction, entry_price,
        max_distance=snr_proximity, level_tolerance=snr_level_tolerance,
    )
    snr_pts = 0.5 if snr_result['has_confluence'] else 0

    if range_high is not None and range_low is not None:
        point_0 = range_low if direction == 'bullish' else range_high
        point_a = range_high if direction == 'bullish' else range_low
        fib_result = fibonacci_zone.analyze(entry_price, point_0, point_a, direction)
    else:
        fib_result = {'retracement_ratio': None, 'in_entry_zone': False, 'target_c': None}
    fib_pts = 1 if fib_result['in_entry_zone'] else 0

    # Core three (execution + HTF agreement + reward:risk) can reach signal
    # grade on their own -- SNR/CRT/premium-discount/fib are genuine bonus
    # confluence, not additional mandatory simultaneous requirements. This
    # is what keeps signals achievable on real trending data instead of
    # requiring every narrow, momentary condition to align at once.
    score = min(execution_pts + htf_agreement_pts + reward_pts + pd_pts + crt_pts + snr_pts + fib_pts, 10)
    is_signal = score >= signal_engine.MIN_SIGNAL_SCORE

    if is_signal:
        status = 'A+ SETUP'
    elif score >= signal_engine.WATCH_THRESHOLD:
        status = 'WATCH'
    else:
        status = 'NO TRADE'

    return {
        'symbol': symbol,
        'direction': direction,
        'entry_price': round(entry_price, 5),
        'sl_price': sl_data['sl_price'],
        'atr_value': sl_data['atr_value'],
        'atr_multiplier_used': sl_data['atr_multiplier_used'],
        'targets': targets,
        'lot_size': sizing['lot_size'],
        'risk_amount_usd': sizing['risk_amount_usd'],
        'stop_distance_pips': sizing['stop_distance_pips'],
        'trend_4h': bias['trend_4h'],
        'trend_1h': bias['trend_1h'],
        'htf_agreement': bias['htf_agreement'],
        'execution_ready': bias['execution_ready'],
        'premium_discount': pd_analysis,
        'crt_sweep': sweep_result,
        'snr_confluence': snr_result,
        'fibonacci_zone': fib_result,
        'score': score,
        'status': status,
        'is_signal': is_signal,
        'reason': bias['reason'],
        'generated_at': time.time(),
        'anchor_candle_time': candles_4h[-1].get('time') if candles_4h else None,
    }
