"""
Khala Trading -- Premium / Discount (Equilibrium)
----------------------------------------------------
Splits a structural range (the 4H swing high to swing low) into two halves
around its 50% midpoint ("equilibrium"):
    - Above the midpoint = PREMIUM  (favors selling -- price is "expensive"
      relative to the recent range)
    - Below the midpoint = DISCOUNT (favors buying -- price is "cheap")

A setup that enters FROM the side that agrees with its own direction
(bullish entries from discount, bearish entries from premium) is
structurally stronger than one chasing price into the wrong half of the
range. This becomes a real scoring factor, not just narrative flavor.
"""


def calculate_equilibrium(swing_high, swing_low):
    """Returns the 50% midpoint of a range."""
    return (swing_high + swing_low) / 2.0


def get_zone(current_price, swing_high, swing_low):
    """
    Returns 'premium' if current_price is above the equilibrium midpoint,
    'discount' if below. If the range is degenerate (high == low), returns
    'equilibrium' (neither favors a side).
    """
    if swing_high <= swing_low:
        return 'equilibrium'

    eq = calculate_equilibrium(swing_high, swing_low)
    if current_price > eq:
        return 'premium'
    elif current_price < eq:
        return 'discount'
    return 'equilibrium'


def is_favorable_zone(direction, current_price, swing_high, swing_low):
    """
    Returns True if the current price sits in the zone that favors this
    trade direction: discount for bullish setups, premium for bearish ones.
    """
    zone = get_zone(current_price, swing_high, swing_low)
    if direction == 'bullish':
        return zone == 'discount'
    elif direction == 'bearish':
        return zone == 'premium'
    return False


def analyze(direction, current_price, swing_high, swing_low):
    """
    Full analysis dict for use in scoring and narrative generation.
    """
    eq = calculate_equilibrium(swing_high, swing_low)
    zone = get_zone(current_price, swing_high, swing_low)
    favorable = is_favorable_zone(direction, current_price, swing_high, swing_low)
    range_size = swing_high - swing_low
    # How deep into the favorable half is price, as a 0-1 ratio (only
    # meaningful when favorable=True) -- deeper into discount/premium is a
    # stronger entry than one sitting right at equilibrium.
    if range_size > 0:
        if direction == 'bullish':
            depth = max(0.0, min(1.0, (eq - current_price) / (eq - swing_low))) if eq > swing_low else 0.0
        else:
            depth = max(0.0, min(1.0, (current_price - eq) / (swing_high - eq))) if swing_high > eq else 0.0
    else:
        depth = 0.0

    return {
        'equilibrium': round(eq, 5),
        'zone': zone,
        'favorable': favorable,
        'depth': round(depth, 3),
        'swing_high': swing_high,
        'swing_low': swing_low,
    }
