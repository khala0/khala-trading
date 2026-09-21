"""
Khala Trading -- Multi-Timeframe Bias Engine
4H: WHERE to trade (trend, required)
1H: IS there a valid setup in that 4H area (refinement)
15M: WHERE EXACTLY is the setup valid (confirmation + SL anchor)
5M: Entry trigger only
"""

import market_structure


def determine_bias(candles_4h, candles_1h, candles_5m, candles_15m=None, structure_lookback=5):
    if len(candles_4h) < 15 or len(candles_1h) < 15:
        return _empty('Not enough higher-timeframe data yet')

    structure_4h = market_structure.detect_structure(candles_4h, lookback=structure_lookback)
    structure_1h = market_structure.detect_structure(candles_1h, lookback=structure_lookback)
    trend_4h = structure_4h['trend']
    trend_1h = structure_1h['trend']

    if trend_4h is None:
        return _empty('4H structure has no established trend yet (no confirmed BOS/CHoCH)',
                      structure_4h=structure_4h, structure_1h=structure_1h)

    direction = trend_4h
    htf_agreement = (trend_1h == trend_4h)

    structure_15m = None
    sl_anchor = None
    trend_15m = None

    if candles_15m and len(candles_15m) >= 20:
        structure_15m = market_structure.detect_structure(candles_15m, lookback=structure_lookback)
        trend_15m = structure_15m['trend']
        swings_15m = structure_15m['swings']
        sl_type = 'low' if direction == 'bullish' else 'high'

        # Search within last 40 bars to account for the swing confirmation
        # delay (lookback=5 means a swing needs 5 bars after it forms before
        # it's detectable -- so searching only 20 bars would miss swings that
        # formed 15-20 bars ago but took 5+ bars to confirm).
        recent_sl_swings = [
            s for s in swings_15m
            if s['type'] == sl_type and s.get('index', 0) >= len(candles_15m) - 40
        ]
        if recent_sl_swings:
            sl_anchor = recent_sl_swings[-1]['price']

    # 5M: execution trigger -- any of last 3 closed in trade direction (no dojis)
    execution_ready = False
    if candles_5m:
        for c in candles_5m[-3:]:
            if c['close'] == c['open']:
                continue
            is_bearish = c['close'] < c['open']
            if (direction == 'bearish' and is_bearish) or (direction == 'bullish' and not is_bearish):
                execution_ready = True
                break

    reason = f"4H trend is {direction}"
    reason += " (1H confirms)" if htf_agreement else " (1H in pullback)"
    if trend_15m:
        reason += f", 15M {trend_15m}"
    reason += f", 5M trigger {'present' if execution_ready else 'not yet'}"
    if sl_anchor:
        reason += f", 15M SL anchor {round(sl_anchor, 5)}"

    return {
        'direction': direction, 'reason': reason,
        'trend_4h': trend_4h, 'trend_1h': trend_1h, 'trend_15m': trend_15m,
        'htf_agreement': htf_agreement, 'execution_ready': execution_ready,
        'structure_4h': structure_4h, 'structure_1h': structure_1h, 'structure_15m': structure_15m,
        'sl_anchor': sl_anchor,
    }


def _empty(reason, structure_4h=None, structure_1h=None):
    return {
        'direction': None, 'reason': reason,
        'trend_4h': None, 'trend_1h': None, 'trend_15m': None,
        'htf_agreement': False, 'execution_ready': False,
        'structure_4h': structure_4h, 'structure_1h': structure_1h, 'structure_15m': None,
        'sl_anchor': None,
    }
