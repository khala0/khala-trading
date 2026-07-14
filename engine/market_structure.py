"""
Khala Trading -- Market Structure Engine (SMC Framework, Phase 1)
--------------------------------------------------------------------
Implements the structural foundation of the proprietary SMC framework:

  - Swing point detection (fractal method)
  - HH / HL / LH / LL labeling (each swing compared to the previous
    swing of the same type)
  - BOS (Break of Structure) and CHoCH (Change of Character) detection

CRITICAL RULE (per the framework): structural confirmations require a
CANDLE CLOSE beyond the level. A wick alone does NOT confirm a break --
instead, it shifts the watched level forward to the new wick extreme,
so the next candle must close beyond that (more extreme) point to
confirm. This file enforces that rule everywhere; there is no code path
that confirms a break on a wick/high/low alone.

This is the foundation that Order Blocks, IDM, and IFC (Phase 2) will be
built on top of -- they are NOT implemented in this file yet.
"""


def find_raw_swings(candles, lookback=5):
    """
    Fractal swing detection. A swing high at index i requires candles[i]['high']
    to be the maximum within the window [i-lookback, i+lookback]; mirrored for
    swing lows. A swing is only knowable once `lookback` candles have passed
    after it -- this function reports the swing's own index (when it occurred),
    not when it becomes confirmable; callers needing the "known as of" index
    should add `lookback` to it (see detect_structure below).

    Returns a chronologically sorted list of {index, price, type} dicts,
    where type is 'high' or 'low'. If a single candle qualifies as both
    (rare, only in very choppy/thin data), both are included.
    """
    swings = []
    n = len(candles)
    for i in range(lookback, n - lookback):
        window_highs = [candles[j]['high'] for j in range(i - lookback, i + lookback + 1)]
        window_lows = [candles[j]['low'] for j in range(i - lookback, i + lookback + 1)]
        if candles[i]['high'] == max(window_highs):
            swings.append({'index': i, 'price': candles[i]['high'], 'type': 'high'})
        if candles[i]['low'] == min(window_lows):
            swings.append({'index': i, 'price': candles[i]['low'], 'type': 'low'})
    swings.sort(key=lambda s: s['index'])
    return swings


def clean_alternating_swings(swings):
    """
    Real market structure alternates high/low/high/low. The fractal method can
    occasionally report two of the same type back to back (e.g. two highs with
    no low detected between them) on choppy data. When that happens, keep only
    the more extreme of the two (the higher high, or the lower low) and drop
    the other -- it wasn't the real turning point.
    """
    if not swings:
        return []
    cleaned = [dict(swings[0])]
    for s in swings[1:]:
        if s['type'] == cleaned[-1]['type']:
            if s['type'] == 'high' and s['price'] > cleaned[-1]['price']:
                cleaned[-1] = dict(s)
            elif s['type'] == 'low' and s['price'] < cleaned[-1]['price']:
                cleaned[-1] = dict(s)
            # else: previous swing was more extreme, discard this one
        else:
            cleaned.append(dict(s))
    return cleaned


def label_swings(swings):
    """
    Labels each swing HH/LH (highs, vs. the previous swing high) or HL/LL
    (lows, vs. the previous swing low). The very first high and first low
    have no prior reference, so their label is None.
    """
    labeled = []
    last_high = None
    last_low = None
    for s in swings:
        s = dict(s)
        if s['type'] == 'high':
            s['label'] = None if last_high is None else ('HH' if s['price'] > last_high['price'] else 'LH')
            last_high = s
        else:
            s['label'] = None if last_low is None else ('HL' if s['price'] > last_low['price'] else 'LL')
            last_low = s
        labeled.append(s)
    return labeled


def detect_structure(candles, lookback=5):
    """
    Full pipeline: find swings, clean alternation, label HH/HL/LH/LL, then
    walk forward through candles detecting BOS/CHoCH with strict candle-close
    confirmation (wicks shift the watched level forward, never confirm).

    Returns a dict:
        'swings':  labeled swing list (chronological)
        'events':  list of {index, type: 'BOS'|'CHoCH', direction, level}
                   in chronological order
        'trend':   'bullish' | 'bearish' | None -- the trend as of the last candle
        'watch_high_level': the level currently being watched for an upside
                             break (None if no swing high known yet)
        'watch_low_level':  mirror, for downside breaks
    """
    raw = find_raw_swings(candles, lookback=lookback)
    cleaned = clean_alternating_swings(raw)
    labeled = label_swings(cleaned)

    highs = [s for s in labeled if s['type'] == 'high']
    lows = [s for s in labeled if s['type'] == 'low']

    trend = None
    events = []
    watch_high_level = None
    watch_low_level = None
    known_high_idx = -1
    known_low_idx = -1

    for i in range(len(candles)):
        c = candles[i]

        # Incorporate any swing highs/lows that have become knowable as of
        # this candle (i.e. `lookback` candles have passed since they formed).
        while known_high_idx + 1 < len(highs) and highs[known_high_idx + 1]['index'] + lookback <= i:
            known_high_idx += 1
            watch_high_level = highs[known_high_idx]['price']
        while known_low_idx + 1 < len(lows) and lows[known_low_idx + 1]['index'] + lookback <= i:
            known_low_idx += 1
            watch_low_level = lows[known_low_idx]['price']

        # Upside level: watch for a confirmed close above it. This is BOS if
        # we're already bullish OR if no trend is established yet (nothing to
        # "change" from -- this is establishing initial structure, not
        # reversing it). CHoCH only applies when genuinely reversing an
        # established bearish trend.
        if watch_high_level is not None:
            if c['close'] > watch_high_level:
                event_type = 'CHoCH' if trend == 'bearish' else 'BOS'
                events.append({
                    'index': i, 'type': event_type, 'direction': 'bullish',
                    'level': watch_high_level,
                })
                trend = 'bullish'
                watch_high_level = None  # re-armed once the next swing high becomes known
            elif c['high'] > watch_high_level:
                watch_high_level = c['high']  # wick poke -- shift the level forward, do NOT confirm

        # Downside level: mirror logic.
        if watch_low_level is not None:
            if c['close'] < watch_low_level:
                event_type = 'CHoCH' if trend == 'bullish' else 'BOS'
                events.append({
                    'index': i, 'type': event_type, 'direction': 'bearish',
                    'level': watch_low_level,
                })
                trend = 'bearish'
                watch_low_level = None
            elif c['low'] < watch_low_level:
                watch_low_level = c['low']  # wick poke -- shift forward, do NOT confirm

    return {
        'swings': labeled,
        'events': events,
        'trend': trend,
        'watch_high_level': watch_high_level,
        'watch_low_level': watch_low_level,
    }
