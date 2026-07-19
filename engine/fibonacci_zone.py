"""
Khala Trading -- SK System Fibonacci Zone
---------------------------------------------
Checks whether current price sits within the valid 0-A-B entry zone
(38.2%-88.6% retracement of the 0->A impulse leg), and projects the
Point C extension target (1.618x by default).
"""

ENTRY_ZONE_MIN = 0.382
ENTRY_ZONE_MAX = 0.886


def retracement_ratio(current_price, point_0, point_a, direction):
    """
    0 = impulse origin, A = impulse extreme (already reached), current
    price is the pullback (B). Returns the retracement ratio (0 = at A,
    1 = fully retraced back to 0), or None if the leg has zero range.
    """
    leg_range = abs(point_a - point_0)
    if leg_range == 0:
        return None
    if direction == 'bullish':
        return (point_a - current_price) / leg_range
    else:
        return (current_price - point_a) / leg_range


def is_in_entry_zone(current_price, point_0, point_a, direction):
    ratio = retracement_ratio(current_price, point_0, point_a, direction)
    if ratio is None:
        return False
    return ENTRY_ZONE_MIN <= ratio <= ENTRY_ZONE_MAX


def calculate_extension_target(point_0, point_a, direction, ratio=1.618):
    leg = point_a - point_0
    return point_0 + leg * ratio


def analyze(current_price, point_0, point_a, direction):
    ratio = retracement_ratio(current_price, point_0, point_a, direction)
    in_zone = is_in_entry_zone(current_price, point_0, point_a, direction)
    target_c = calculate_extension_target(point_0, point_a, direction)
    return {
        'retracement_ratio': round(ratio, 3) if ratio is not None else None,
        'in_entry_zone': in_zone,
        'target_c': round(target_c, 5),
    }
