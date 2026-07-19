"""
Khala Trading -- Multi-Timeframe Bias Engine
-----------------------------------------------
Fixes the bug where a single strong lower-timeframe candle could flip the
trading bias against an established higher-timeframe trend.

Hierarchy (strict -- each level can only do the job listed, nothing else):
    4H structure  -- PRIMARY trend filter. Required. Uses BOS/CHoCH from
                     market_structure.py. If no clear 4H trend exists yet,
                     there is NO trade, full stop -- lower timeframes never
                     get to invent a direction on their own.
    1H structure  -- SECONDARY refinement. Confirms whether price is
                     currently sitting in a sensible pullback/entry zone
                     relative to the 4H trend. Can raise or lower confidence,
                     but can NEVER flip the direction decided by 4H.
    5M candles    -- TERTIARY, execution only. Used solely to check for a
                     confirmation candle closing in the 4H trend's direction,
                     as the actual entry trigger. Never used to decide bias.
"""

import market_structure


def determine_bias(candles_4h, candles_1h, candles_5m, structure_lookback=5):
    """
    Runs the full hierarchy and returns a dict describing the decision:

        direction:        'bullish' | 'bearish' | None (None = no trade)
        reason:           short explanation of why (or why not)
        trend_4h:         the 4H structure's trend
        trend_1h:         the 1H structure's trend (for confluence info only)
        htf_agreement:    True if 1H trend matches 4H trend (stronger setup)
        execution_ready:  True if the most recent 5M candle actually closed
                           in the 4H trend's direction (confirmation trigger)
        structure_4h:      the full detect_structure() output for 4H (for SL/swing reference)
        structure_1h:      the full detect_structure() output for 1H
    """
    if len(candles_4h) < 15 or len(candles_1h) < 15:
        return {
            'direction': None,
            'reason': 'Not enough higher-timeframe data yet to establish structure',
            'trend_4h': None, 'trend_1h': None,
            'htf_agreement': False, 'execution_ready': False,
            'structure_4h': None, 'structure_1h': None,
        }

    structure_4h = market_structure.detect_structure(candles_4h, lookback=structure_lookback)
    structure_1h = market_structure.detect_structure(candles_1h, lookback=structure_lookback)

    trend_4h = structure_4h['trend']
    trend_1h = structure_1h['trend']

    # Rule #1: 4H is required. No 4H trend = no trade, regardless of what
    # anything lower down is doing.
    if trend_4h is None:
        return {
            'direction': None,
            'reason': '4H structure has no established trend yet (no confirmed BOS/CHoCH)',
            'trend_4h': None, 'trend_1h': trend_1h,
            'htf_agreement': False, 'execution_ready': False,
            'structure_4h': structure_4h, 'structure_1h': structure_1h,
        }

    # Direction is LOCKED to the 4H trend. Nothing below this line can change it.
    direction = trend_4h
    htf_agreement = (trend_1h == trend_4h)

    # Execution check: did ANY of the last 3 5M candles close in the 4H
    # trend's direction? Checking only the single most recent candle made
    # this trigger nearly impossible to catch in practice (real-time polling
    # rarely lands on the exact trigger candle) -- a small recent window is
    # still "execution timing", not a bias decision, so it stays in scope.
    execution_ready = False
    if candles_5m:
        for c in candles_5m[-3:]:
            # Defined symmetrically on purpose: a doji (close == open) should
            # confirm NEITHER direction. Deriving "bullish" from merely
            # "not bearish" silently let a flat candle count as a valid
            # bullish confirmation while never counting for bearish.
            is_bullish_candle = c['close'] > c['open']
            is_bearish_candle = c['close'] < c['open']
            if (direction == 'bearish' and is_bearish_candle) or (direction == 'bullish' and is_bullish_candle):
                execution_ready = True
                break

    reason = f"4H trend is {direction}"
    reason += " (1H confirms)" if htf_agreement else " (1H is in a pullback -- still valid, just refining)"
    reason += ", 5M execution trigger " + ("present" if execution_ready else "not yet present")

    return {
        'direction': direction,
        'reason': reason,
        'trend_4h': trend_4h, 'trend_1h': trend_1h,
        'htf_agreement': htf_agreement, 'execution_ready': execution_ready,
        'structure_4h': structure_4h, 'structure_1h': structure_1h,
    }
