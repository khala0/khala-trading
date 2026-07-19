"""
Khala Trading -- Malaysian SNR Levels
----------------------------------------
Detects the two SNR level types from the framework:
  - Classic SNR (A-shape / V-shape): formed by two OPPOSITE-direction
    candles meeting at a shared price
  - Open|Close SNR: formed by two SAME-direction candles sharing an
    open/close price

Tracks freshness per the framework's rule:
  - A level is FRESH until price wicks through it (first touch) -> UNFRESH
  - If a later candle CLOSES a body beyond it, the level becomes FRESH
    again (available for one more touch)
  - A level can be used (touched while fresh) a maximum of 2 times, after
    which it's EXHAUSTED and no longer a valid confluence
"""


def _is_bullish(candle):
    return candle['close'] > candle['open']


def detect_level_formations(candles, tolerance=0.15):
    """
    Scans consecutive candle pairs for SNR level formations. Returns a list
    of {price, type, formed_at_index, side} dicts, where side is 'support'
    (level sits below/at the pair, i.e. formed by a low) or 'resistance'
    (formed by a high). `tolerance` is how close two prices must be
    (as an absolute value) to count as "shared."

    Classic A-shape (resistance): bullish candle followed by bearish candle,
      sharing a similar HIGH (the peak between them).
    Classic V-shape (support): bearish candle followed by bullish candle,
      sharing a similar LOW (the trough between them).
    Open|Close (either side): two same-direction candles sharing an
      open/close price.
    """
    levels = []
    for i in range(1, len(candles)):
        prev, cur = candles[i - 1], candles[i]
        prev_bull, cur_bull = _is_bullish(prev), _is_bullish(cur)

        # Classic A-shape: bullish then bearish, sharing a high (resistance)
        if prev_bull and not cur_bull and abs(prev['high'] - cur['high']) <= tolerance:
            levels.append({
                'price': (prev['high'] + cur['high']) / 2,
                'type': 'classic_a', 'side': 'resistance', 'formed_at_index': i,
            })

        # Classic V-shape: bearish then bullish, sharing a low (support)
        if not prev_bull and cur_bull and abs(prev['low'] - cur['low']) <= tolerance:
            levels.append({
                'price': (prev['low'] + cur['low']) / 2,
                'type': 'classic_v', 'side': 'support', 'formed_at_index': i,
            })

        # Open|Close: two same-direction candles sharing an open/close price
        if prev_bull and cur_bull and abs(prev['close'] - cur['open']) <= tolerance:
            levels.append({
                'price': (prev['close'] + cur['open']) / 2,
                'type': 'open_close', 'side': 'support', 'formed_at_index': i,
            })
        if not prev_bull and not cur_bull and abs(prev['close'] - cur['open']) <= tolerance:
            levels.append({
                'price': (prev['close'] + cur['open']) / 2,
                'type': 'open_close', 'side': 'resistance', 'formed_at_index': i,
            })

    return levels


def track_freshness(candles, levels):
    """
    Walks forward through candles (starting after each level's formation
    index) to determine its current state: 'fresh', 'unfresh', or
    'exhausted' (used twice already).

    Returns the same level dicts with 'state' and 'touch_count' added.
    """
    results = []
    for level in levels:
        price = level['price']
        state = 'fresh'
        touch_count = 0

        for i in range(level['formed_at_index'] + 1, len(candles)):
            c = candles[i]
            wicked_through = c['low'] <= price <= c['high']
            closed_through = (c['close'] > price and c['open'] < price) or (c['close'] < price and c['open'] > price)

            if state == 'fresh' and wicked_through:
                touch_count += 1
                state = 'unfresh' if touch_count < 2 else 'exhausted'
            elif state == 'unfresh' and closed_through:
                state = 'fresh'  # broken by a body close -- fresh again

            if state == 'exhausted':
                break

        results.append({**level, 'state': state, 'touch_count': touch_count})
    return results


def find_nearest_fresh_level(levels, current_price, side, max_distance):
    """
    Returns the nearest FRESH level of the given side ('support' or
    'resistance') within max_distance of current_price, or None.
    """
    candidates = [
        lvl for lvl in levels
        if lvl['side'] == side and lvl['state'] == 'fresh'
        and abs(lvl['price'] - current_price) <= max_distance
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda lvl: abs(lvl['price'] - current_price))


def analyze(candles, direction, current_price, max_distance, level_tolerance=0.15):
    """
    Full analysis for scoring/narrative use: finds the nearest relevant
    fresh SNR level supporting `direction` (support for bullish, resistance
    for bearish) within max_distance of current_price.

    `level_tolerance` is the absolute price difference that counts two
    candles' highs/lows/opens/closes as "the same level" when detecting
    formations in the first place -- this is a DIFFERENT concept from
    `max_distance` (how close a level must be to current_price to count as
    confluence). Callers should scale `level_tolerance` to the instrument
    (e.g. a fraction of ATR), the same way `max_distance` already is --
    the previous fixed 0.15 default is only sane for an instrument priced
    like gold; it's enormous relative to EURUSD (over 1000 pips, so nearly
    every candle pair "shares" a level) and negligible relative to BTCUSD
    (so it almost never registers one at all).

    Returns {'supporting_level': dict or None, 'has_confluence': bool}
    """
    raw_levels = detect_level_formations(candles, tolerance=level_tolerance)
    tracked = track_freshness(candles, raw_levels)

    side = 'support' if direction == 'bullish' else 'resistance'
    nearest = find_nearest_fresh_level(tracked, current_price, side, max_distance)

    return {
        'supporting_level': nearest,
        'has_confluence': nearest is not None,
    }
