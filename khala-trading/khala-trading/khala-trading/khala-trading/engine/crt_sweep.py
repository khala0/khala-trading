"""
Khala Trading -- CRT (Candle Range Theory) Sweep Detection
---------------------------------------------------------------
A CRT sweep: price wicks beyond a prior reference range's high or low
(sweeping resting liquidity) and then CLOSES back inside that range on
the same or a subsequent candle -- signaling the breakout was a liquidity
grab, not genuine continuation, and the move is likely to reverse.

This is the same wick-then-reverse pattern our BOS/CHoCH engine already
tracks (see market_structure.py), applied here specifically to a defined
reference range (typically the prior session/period's high-low) rather
than to swing structure.
"""


def detect_sweep(candles, reference_high, reference_low, lookback_candles=3):
    """
    Checks the last `lookback_candles` for a CRT sweep against a reference
    range (not just the single most recent candle -- a sweep that happened
    1-2 candles ago is still a valid, recent liquidity event, and checking
    only the latest candle made this trigger far too rare in practice).

    Returns a dict:
        swept:      True if a sweep occurred within the lookback window
        direction:  'bullish' or 'bearish' or None
        swept_level: the reference level that was swept

    A sweep requires the wick to have gone beyond the reference level
    AND the candle's close to have come back inside the range -- a candle
    that closes beyond the level is a genuine breakout, not a sweep.
    """
    if not candles:
        return {'swept': False, 'direction': None, 'swept_level': None}

    for last in reversed(candles[-lookback_candles:]):
        swept_high = last['high'] > reference_high and last['close'] < reference_high
        swept_low = last['low'] < reference_low and last['close'] > reference_low

        if swept_high and swept_low:
            continue  # ambiguous candle -- skip, keep checking earlier ones in the window

        if swept_high:
            return {'swept': True, 'direction': 'bearish', 'swept_level': reference_high}
        if swept_low:
            return {'swept': True, 'direction': 'bullish', 'swept_level': reference_low}

    return {'swept': False, 'direction': None, 'swept_level': None}


def get_reference_range(candles, lookback_bars=24):
    """
    Builds a reference high/low from the prior `lookback_bars` candles,
    EXCLUDING the most recent one (which is the candle being checked for
    a sweep against this range). Typically used with hourly candles and
    lookback_bars=24 to represent "the prior session."
    """
    if len(candles) < lookback_bars + 1:
        window = candles[:-1]
    else:
        window = candles[-(lookback_bars + 1):-1]

    if not window:
        return None, None

    return max(c['high'] for c in window), min(c['low'] for c in window)
