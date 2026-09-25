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
        'trend_4h': None, 'trend_1h': None, 'trend_15m': None, 'htf_agreement': False, 'execution_ready': False,
        'premium_discount': None, 'crt_sweep': None, 'snr_confluence': None, 'fibonacci_zone': None,
        'score': 0, 'status': status, 'is_signal': False, 'reason': reason,
        'generated_at': time.time(), 'anchor_candle_time': None,
    }


def generate_signal(symbol, candles_4h, candles_1h, candles_5m, candles_15m=None,
                     account_balance=10000, risk_percent=1.0,
                     pip_value_per_lot=10.0, pip_size=0.0001,
                     skip_news_filter=False):
    """
    Returns a dict describing the current signal state for `symbol`. Always
    returns a dict (never raises) -- callers can check 'direction' (None
    means no trade) and 'status' for what's actually going on.

    skip_news_filter: news_filter.py only knows TODAY's news calendar, not
    historical dates -- checking it while backtesting against, say, candles
    from 2023 would incorrectly apply 2026's news schedule. Backtesting
    code should pass skip_news_filter=True; live trading should never set
    this (default False keeps the live safety check active).
    """
    # --- Step 1: news filter -- checked first, since a blackout should
    # override everything else regardless of how good the setup looks ---
    if not skip_news_filter:
        blocked, event = news_filter.is_near_high_impact_news(symbol)
        if blocked:
            return _no_trade_result(
                symbol, 'NEWS BLACKOUT',
                f"High-impact {event['currency']} news ({event['event']}) near this time -- trading paused as a precaution",
            )

    # --- Step 1b: Wednesday filter -- a 1-year XAUUSD backtest showed a
    # 22% win rate on Wednesdays (4W/14L) vs 50-70% on every other weekday.
    # Likely FOMC minutes / mid-week consolidation noise. Evidence-based,
    # not arbitrary -- uses the reference candle's own timestamp so this
    # works correctly in both live trading and historical backtesting. ---
    reference_time = candles_1h[-1].get('time') if candles_1h else None
    if reference_time is not None:
        weekday = time.gmtime(reference_time).tm_wday  # Monday=0 ... Wednesday=2
        if weekday == 2:
            return _no_trade_result(
                symbol, 'NO TRADE',
                'Wednesday filter active -- backtesting showed a significantly worse win rate on Wednesdays for this symbol',
            )

    # --- Step 2: multi-timeframe bias. 4H=direction (required), 1H=refinement,
    # 15M=setup confirmation + SL anchor, 5M=entry trigger only ---
    bias = multi_timeframe_engine.determine_bias(candles_4h, candles_1h, candles_5m, candles_15m=candles_15m)

    if bias['direction'] is None:
        return _no_trade_result(symbol, 'NO TRADE', bias['reason'])

    direction = bias['direction']
    structure_4h = bias['structure_4h']

    # --- Step 3: SL reference. Prefer the 15M swing anchor (tight, right
    # behind the actual setup) -- fall back to the 4H swing only if 15M
    # hasn't formed a usable swing yet. Either way, the ATR cap below still
    # protects against an unreasonably wide stop. ---
    if bias.get('sl_anchor') is not None:
        swing_ref = bias['sl_anchor']
    elif direction == 'bearish':
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

    # Cap the stop distance to a reasonable multiple of ATR. Without this,
    # a stale/distant swing reference (common in a strongly trending market
    # that hasn't pulled back in a while) can anchor a technically-valid
    # but practically useless stop thousands of pips away. If the raw
    # swing-based stop is unreasonably wide, tighten it to a fixed ATR
    # multiple from entry instead -- still respects "beyond structure" in
    # spirit, but never produces an impractical, oversized stop.
    MAX_SL_ATR_MULTIPLE = 4
    raw_stop_distance = abs(entry_price - sl_data['sl_price'])
    max_reasonable_distance = sl_data['atr_value'] * MAX_SL_ATR_MULTIPLE

    if sl_data['atr_value'] > 0 and raw_stop_distance > max_reasonable_distance:
        capped_sl_price = (
            entry_price + max_reasonable_distance if direction == 'bearish'
            else entry_price - max_reasonable_distance
        )
        sl_data['sl_price'] = round(capped_sl_price, 5)
        sl_data['sl_capped'] = True
        sl_data['sl_raw_distance'] = round(raw_stop_distance, 5)
    else:
        sl_data['sl_capped'] = False

    # Minimum SL floor for specific symbols where backtesting showed losing
    # trades had systematically TIGHTER stops than winning trades -- i.e.
    # the 15M anchor was placing stops too close to entry, getting stopped
    # out on normal volatility noise before the real move could develop.
    # Evidence: XAGUSD backtest (78 trades) showed losers averaged $2.19
    # stop distance vs $4.06 for winners. This floor forces a wider stop
    # for symbols with this signature -- add others here if their own
    # diagnostics show the same pattern.
    MIN_SL_ATR_MULTIPLE_OVERRIDES = {
        'XAGUSD': 2.5,
    }
    min_multiple = MIN_SL_ATR_MULTIPLE_OVERRIDES.get(symbol)
    if min_multiple and sl_data['atr_value'] > 0:
        current_distance = abs(entry_price - sl_data['sl_price'])
        min_distance = sl_data['atr_value'] * min_multiple
        if current_distance < min_distance:
            widened_sl_price = (
                entry_price + min_distance if direction == 'bearish'
                else entry_price - min_distance
            )
            sl_data['sl_price'] = round(widened_sl_price, 5)
            sl_data['sl_widened'] = True

    # Bearish XAGUSD is fully disabled. First tried a stricter 8.5 score
    # bar instead of an outright ban -- but a follow-up 2-year backtest
    # showed that made it WORSE, not better (win rate dropped from 23.1%
    # to 16.7% on the trades that still got through), meaning score isn't
    # the right lever here at all. Two separate datasets now consistently
    # show bearish XAGUSD losing regardless of how selective the score bar
    # is, which points to a structural/regime issue (current precious-
    # metals bull market) rather than something a confidence threshold can
    # filter for. Revisit if a later backtest shows the pattern reversing.
    DISABLED_SYMBOL_DIRECTIONS = {
        ('XAGUSD', 'bearish'),
    }
    if (symbol, direction) in DISABLED_SYMBOL_DIRECTIONS:
        return _no_trade_result(
            symbol, 'NO TRADE',
            f'{direction.capitalize()} {symbol} signals are currently disabled -- backtesting showed '
            f'consistently poor performance in this direction regardless of score threshold',
            direction=direction,
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

    # --- Step 4: restructured scoring (max 10 points) ---
    # 4H trend:          2 pts  (required but not sufficient alone)
    # 1H agrees 4H:      2 pts  (structural alignment)
    # 15M agrees:        2 pts  (confirmation layer -- new, key differentiator)
    # 5M trigger:        1 pt   (entry timing)
    # Premium/discount:  1 pt   (ICT equilibrium zone)
    # Fibonacci zone:    1 pt   (SK system retracement)
    # CRT sweep:         1 pt   (liquidity grab confirmation)
    # Threshold = 7.5 means a setup needs at minimum: 4H + 1H + 15M + 5M + one confluence
    # factor -- not just "trend exists + any 5M candle closed the right way."

    trend_pts = 2  # 4H trend established (we wouldn't be here without it)
    htf_pts = 2 if bias['htf_agreement'] else 0  # 1H agrees with 4H
    mtf_pts = 2 if bias.get('trend_15m') == direction else 0  # 15M confirms
    exec_pts = 1 if bias['execution_ready'] else 0  # 5M trigger

    stable_high, stable_low = _stable_range(structure_4h)
    range_high, range_low = stable_high, stable_low

    if range_high is not None and range_low is not None:
        pd_analysis = premium_discount.analyze(direction, entry_price, range_high, range_low)
    else:
        pd_analysis = {'equilibrium': None, 'zone': None, 'favorable': False, 'depth': 0}
    pd_pts = 1 if pd_analysis['favorable'] else 0

    if range_high is not None and range_low is not None:
        point_0 = range_low if direction == 'bullish' else range_high
        point_a = range_high if direction == 'bullish' else range_low
        fib_result = fibonacci_zone.analyze(entry_price, point_0, point_a, direction)
    else:
        fib_result = {'retracement_ratio': None, 'in_entry_zone': False, 'target_c': None}
    fib_pts = 1 if fib_result['in_entry_zone'] else 0

    ref_high, ref_low = crt_sweep.get_reference_range(candles_1h, lookback_bars=24)
    if ref_high is not None:
        sweep_result = crt_sweep.detect_sweep(candles_1h, ref_high, ref_low)
    else:
        sweep_result = {'swept': False, 'direction': None, 'swept_level': None}
    crt_pts = 1 if (sweep_result['swept'] and sweep_result['direction'] == direction) else 0

    atr_value = sl_data.get('atr_value')
    snr_proximity = atr_value * 2 if atr_value else 5
    snr_level_tolerance = atr_value * 0.1 if atr_value else 0.15
    snr_result = snr_levels.analyze(
        candles_1h, direction, entry_price,
        max_distance=snr_proximity, level_tolerance=snr_level_tolerance,
    )

    # Reward:risk kept for narrative/display but removed from score since it
    # depends on the same stable_range reference that's often unavailable --
    # keeping it in the score was producing silent 0s that biased everything down.
    stop_distance = abs(entry_price - sl_data['sl_price'])
    opposite_level = stable_low if direction == 'bearish' else stable_high
    if opposite_level is not None and stop_distance > 0:
        reward_potential = abs(entry_price - opposite_level) / stop_distance
    else:
        reward_potential = 0

    score = min(trend_pts + htf_pts + mtf_pts + exec_pts + pd_pts + fib_pts + crt_pts, 10)
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
        'trend_15m': bias.get('trend_15m'),
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
