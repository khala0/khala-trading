"""
KHALA TRADING Signal Engine
----------------------
Core ICT/SMC-style logic: swing high/low detection, ATR-based dynamic
stop-loss sizing, take-profit targets, and a confluence-based setup score.

This replaces a fixed "beyond full HTF swing" stop with a tighter,
volatility-scaled buffer (swing point +/- multiplier * ATR), tuned
per asset class.
"""

import statistics


ASSET_ATR_MULTIPLIERS = {
    'XAUUSD': 0.5,
    'XAGUSD': 0.5,
    'EURUSD': 0.6,
    'GBPUSD': 0.6,
    'GBPJPY': 0.6,
    'AUDUSD': 0.6,
    'USDJPY': 0.6,
    'US30': 0.4,
    'NAS100': 0.4,
    'BTCUSD': 0.75,
}
DEFAULT_ATR_MULTIPLIER = 0.5


def get_atr_multiplier(symbol):
    return ASSET_ATR_MULTIPLIERS.get(symbol.upper(), DEFAULT_ATR_MULTIPLIER)


def calculate_atr(candles, period=14):
    """candles: list of dicts with 'high','low','close', oldest -> newest."""
    if len(candles) < period + 1:
        period = max(2, len(candles) - 1)  # degrade gracefully on thin data

    true_ranges = []
    for i in range(1, len(candles)):
        high, low = candles[i]['high'], candles[i]['low']
        prev_close = candles[i - 1]['close']
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        true_ranges.append(tr)

    recent = true_ranges[-period:]
    return statistics.fmean(recent) if recent else 0.0


def find_swing_points(candles, lookback=8):
    """
    Fractal-style swing detection, returning the most recent *unmitigated*
    swing high and swing low relative to the current close -- i.e. a swing
    high that price hasn't already closed above, and a swing low price
    hasn't already closed below. Using a stale/already-broken swing point
    as an invalidation reference produces a nonsensical SL (on the wrong
    side of entry), so this is required, not optional.
    """
    highs = [c['high'] for c in candles]
    lows = [c['low'] for c in candles]
    last_close = candles[-1]['close']

    candidate_highs = []
    candidate_lows = []

    for i in range(lookback, len(candles) - lookback):
        window_highs = highs[i - lookback:i + lookback + 1]
        window_lows = lows[i - lookback:i + lookback + 1]
        if highs[i] == max(window_highs):
            candidate_highs.append({'price': highs[i], 'index': i})
        if lows[i] == min(window_lows):
            candidate_lows.append({'price': lows[i], 'index': i})

    # Most recent swing high that price hasn't closed above yet
    swing_high = next(
        (h for h in reversed(candidate_highs) if h['price'] >= last_close),
        None,
    )
    # Most recent swing low that price hasn't closed below yet
    swing_low = next(
        (l for l in reversed(candidate_lows) if l['price'] <= last_close),
        None,
    )

    # Fallback: no unmitigated swing found in the lookback window --
    # widen to the full window's extreme on that side.
    if swing_high is None:
        swing_high = {'price': max(highs), 'index': highs.index(max(highs))}
    if swing_low is None:
        swing_low = {'price': min(lows), 'index': lows.index(min(lows))}

    return swing_high, swing_low


def calculate_dynamic_sl(direction, swing_price, candles, symbol=None,
                          atr_period=14, atr_multiplier=None):
    """Tighter SL: swing point +/- (multiplier * ATR) instead of full swing distance."""
    if direction not in ('bullish', 'bearish'):
        raise ValueError("direction must be 'bullish' or 'bearish'")

    if atr_multiplier is None:
        atr_multiplier = get_atr_multiplier(symbol) if symbol else DEFAULT_ATR_MULTIPLIER

    atr_value = calculate_atr(candles, period=atr_period)
    buffer_size = atr_value * atr_multiplier

    sl_price = swing_price + buffer_size if direction == 'bearish' else swing_price - buffer_size

    return {
        'sl_price': round(sl_price, 5),
        'atr_value': round(atr_value, 5),
        'buffer_size': round(buffer_size, 5),
        'atr_multiplier_used': atr_multiplier,
        'symbol': symbol,
    }


def calculate_targets(direction, entry_price, sl_price):
    """TP1/TP2/TP3 at 1R, 2R, 3R (risk-reward multiples of the SL distance)."""
    risk = abs(entry_price - sl_price)
    sign = -1 if direction == 'bearish' else 1
    return {
        'tp1': round(entry_price + sign * risk * 1, 5),
        'tp2': round(entry_price + sign * risk * 2, 5),
        'tp3': round(entry_price + sign * risk * 3, 5),
    }


def calculate_position_size(account_balance, risk_percent, entry_price, sl_price,
                             pip_value_per_lot=10.0, pip_size=0.0001):
    """Recompute lot size so tightening the SL doesn't silently change $ risk per trade."""
    risk_amount_usd = account_balance * (risk_percent / 100.0)
    stop_distance = abs(entry_price - sl_price)
    stop_distance_pips = stop_distance / pip_size
    if stop_distance_pips == 0:
        return {'lot_size': 0.0, 'risk_amount_usd': round(risk_amount_usd, 2), 'stop_distance_pips': 0.0}
    lot_size = risk_amount_usd / (stop_distance_pips * pip_value_per_lot)
    return {
        'lot_size': round(lot_size, 2),
        'risk_amount_usd': round(risk_amount_usd, 2),
        'stop_distance_pips': round(stop_distance_pips, 1),
    }


def score_setup(candles, symbol='XAUUSD', account_balance=10000, risk_percent=1.0,
                 pip_value_per_lot=10.0, pip_size=0.0001):
    """
    Build a full setup from raw candles: direction bias, entry, SL, TP1-3,
    lot size, and a 0-10 confluence score.

    candles: list of dicts with 'open','high','low','close', oldest -> newest.
             Needs at least ~30 candles for meaningful swing detection.
    Returns a dict describing the current setup.
    """
    if len(candles) < 20:
        raise ValueError("Need at least 20 candles for a reliable setup read")

    swing_high, swing_low = find_swing_points(candles, lookback=8)
    last_close = candles[-1]['close']
    prior_close = candles[-2]['close'] if len(candles) > 1 else last_close

    # A swing high is only a valid bearish reference if it sits ABOVE current
    # price (short stop must sit above entry). Same logic mirrored for lows.
    high_valid = swing_high['price'] >= last_close
    low_valid = swing_low['price'] <= last_close
    momentum_down = last_close < prior_close

    if high_valid and low_valid:
        # Both structurally valid -- use momentum + proximity to pick a side
        dist_to_high = abs(swing_high['price'] - last_close)
        dist_to_low = abs(last_close - swing_low['price'])
        if momentum_down and dist_to_high <= dist_to_low:
            direction, swing_ref = 'bearish', swing_high['price']
        elif not momentum_down and dist_to_low <= dist_to_high:
            direction, swing_ref = 'bullish', swing_low['price']
        else:
            direction = 'bearish' if momentum_down else 'bullish'
            swing_ref = swing_high['price'] if direction == 'bearish' else swing_low['price']
    elif high_valid:
        direction, swing_ref = 'bearish', swing_high['price']
    elif low_valid:
        direction, swing_ref = 'bullish', swing_low['price']
    else:
        # Neither reference is structurally valid (price broke both) --
        # no clean setup available right now.
        return {
            'symbol': symbol,
            'direction': None,
            'status': 'NO TRADE',
            'reason': 'No unmitigated swing structure available for a valid SL reference',
            'entry_price': round(last_close, 5),
        }

    sl_data = calculate_dynamic_sl(direction, swing_ref, candles, symbol=symbol)
    entry_price = last_close

    # Sanity check: SL must sit on the correct side of entry for the direction.
    if direction == 'bearish' and sl_data['sl_price'] <= entry_price:
        raise AssertionError('Invalid SL: bearish stop must be above entry')
    if direction == 'bullish' and sl_data['sl_price'] >= entry_price:
        raise AssertionError('Invalid SL: bullish stop must be below entry')

    targets = calculate_targets(direction, entry_price, sl_data['sl_price'])
    sizing = calculate_position_size(
        account_balance, risk_percent, entry_price, sl_data['sl_price'],
        pip_value_per_lot=pip_value_per_lot, pip_size=pip_size,
    )

    # Confluence score out of 10 -- placeholder weighting across a few checks.
    # Extend this with real FVG/order-block/liquidity-sweep confirmations.
    score = 5
    score += 1 if abs(last_close - prior_close) > sl_data['atr_value'] * 0.1 else 0
    score += 1 if (dist_to_high < sl_data['atr_value'] * 3 or dist_to_low < sl_data['atr_value'] * 3) else 0
    score += 2 if sl_data['buffer_size'] < abs(swing_high['price'] - swing_low['price']) * 0.5 else 0
    score = min(score, 10)

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
        'swing_high': round(swing_high['price'], 5),
        'swing_low': round(swing_low['price'], 5),
        'score': score,
        'status': 'A+ SETUP' if score >= 8 else ('WATCH' if score >= 6 else 'NO TRADE'),
    }
