"""
KHALA TRADING Signal Engine
----------------------
Core ICT/SMC-style logic: swing high/low detection, ATR-based dynamic
stop-loss sizing, take-profit targets, and a confluence-based setup score.

This replaces a fixed "beyond full HTF swing" stop with a tighter,
volatility-scaled buffer (swing point +/- multiplier * ATR), tuned
per asset class.

LEGACY NOTICE (2026-07-17): find_swing_points() and score_setup() below are
NOT what the live app runs. app.py calls master_signal.generate_signal()
exclusively, which does its own swing/scoring logic on top of
market_structure.py's BOS/CHoCH output. Nothing in this codebase calls
score_setup() or find_swing_points() -- confirmed by grep across every .py
file. Two other modules' docstrings (gemini_client.py, executor_core.py)
used to describe the setup dict as coming "from score_setup()", which was
stale and has been corrected to point at master_signal.generate_signal().

These two functions are kept (not deleted) in case you're using them
elsewhere outside this snapshot, or want them back as a second scoring
opinion later -- but while dead, a real bug had crept in undetected: the
"no valid structure" branch of score_setup() could never actually trigger,
because its old swing-point fallback always included the current candle's
own high/low, which by construction always satisfies the ">= last_close" /
"<= last_close" validity check. That's fixed below along with the same
doji-candle scoring asymmetry that was fixed in multi_timeframe_engine.py.
If you do start calling this again, ASSET_ATR_MULTIPLIERS/get_atr_multiplier/
calculate_atr/calculate_dynamic_sl/calculate_targets/calculate_position_size
are all still live (master_signal.py uses these), only find_swing_points()
and score_setup() were orphaned.
"""

import statistics
import time


# Only setups scoring at or above this are treated as an actionable signal --
# shown as a full dispatch on the dashboard, sent to Telegram, logged to
# signal history, and eligible for auto-execution. Anything below this is
# "monitoring only" (still visible, but not presented as a trade signal).
MIN_SIGNAL_SCORE = 7
WATCH_THRESHOLD = 5


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

    # FIXED 2026-07-17: this used to fall back to `max(highs)`/`min(lows)` --
    # the raw extreme over ALL candles -- whenever no unmitigated swing was
    # found. That fallback always includes the current (last) candle's own
    # high/low, and a candle's high/low is always >= / <= its own close by
    # definition -- so the fallback could NEVER fail the validity check
    # downstream (`price >= last_close` / `price <= last_close`), no matter
    # how broken the actual structure was. Net effect: score_setup()'s
    # "no valid structure, no clean setup" branch was unreachable dead code.
    # Returning None here (meaning "no valid reference on this side, don't
    # invent one") is what makes that branch mean something again -- see
    # score_setup() for how it's now handled.
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
    # find_swing_points() now returns None (rather than a synthetic always-
    # valid fallback) when there's no genuinely unmitigated swing -- so
    # None here really does mean "not valid," and both branches below are
    # reachable again.
    high_valid = swing_high is not None
    low_valid = swing_low is not None
    momentum_down = last_close < prior_close
    dist_to_high = abs(swing_high['price'] - last_close) if high_valid else None
    dist_to_low = abs(last_close - swing_low['price']) if low_valid else None

    # Raw window extremes, used ONLY as a rough distance proxy for the
    # reward-potential score below when one side has no valid structural
    # swing -- never as a stand-in for a real SL/direction reference.
    raw_highest = max(c['high'] for c in candles)
    raw_lowest = min(c['low'] for c in candles)

    if high_valid and low_valid:
        # Both structurally valid -- use momentum + proximity to pick a side
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

    # Confluence score out of 10, built from four independent checks. Unlike
    # a fixed base score, this starts at 0 -- reaching 8+ requires several
    # of these to line up together, which is what actually reduces how often
    # "A+ SETUP" fires (rather than just hiding low scores from the UI while
    # nearly everything still qualified underneath).
    stop_distance = abs(entry_price - sl_data['sl_price'])
    last_candle = candles[-1]
    # FIXED 2026-07-17: was `is_bearish_candle = close < open` with bullish
    # checked as `not is_bearish_candle` -- which silently counted a flat/
    # doji candle (close == open) as confirming bullish but never bearish.
    # Defining both explicitly and requiring the matching one treats doji
    # as confirming neither, symmetrically.
    is_bullish_candle = last_candle['close'] > last_candle['open']
    is_bearish_candle = last_candle['close'] < last_candle['open']

    # 1) Confirmation candle: did the most recent candle actually close in
    #    the setup's direction? (0 or 2 points)
    confirmation_pts = 2 if (
        (direction == 'bearish' and is_bearish_candle) or
        (direction == 'bullish' and is_bullish_candle)
    ) else 0

    # 2) Momentum strength: is the last move meaningful relative to ATR,
    #    or just noise? (0, 1, or 2 points)
    momentum_size = abs(last_close - prior_close)
    if momentum_size >= sl_data['atr_value'] * 0.5:
        momentum_pts = 2
    elif momentum_size >= sl_data['atr_value'] * 0.25:
        momentum_pts = 1
    else:
        momentum_pts = 0

    # 3) Reward potential: how far is the opposite swing point (the room to
    #    run) relative to the stop distance (the risk)? Rewards setups with
    #    real reward:risk, not just any valid structure. (0-4 points)
    # Use the real swing reference where we have one; otherwise fall back to
    # the raw window extreme just for this distance estimate (low-stakes --
    # it only nudges a 0-4 point score, unlike swing_ref above which had to
    # be a real structural level or the SL itself would be invalid).
    if direction == 'bearish':
        opposite_swing = swing_low['price'] if low_valid else raw_lowest
    else:
        opposite_swing = swing_high['price'] if high_valid else raw_highest
    reward_potential = abs(entry_price - opposite_swing) / stop_distance if stop_distance > 0 else 0
    if reward_potential >= 4:
        reward_pts = 4
    elif reward_potential >= 3:
        reward_pts = 3
    elif reward_potential >= 2:
        reward_pts = 2
    elif reward_potential >= 1.5:
        reward_pts = 1
    else:
        reward_pts = 0

    # 4) Retracement positioning (approximate OTE-style check, not exact
    #    Fibonacci): is entry sitting in a sensible pullback zone rather
    #    than chasing price at the extreme? (0 or 2 points)
    swing_high_ref = swing_high['price'] if high_valid else raw_highest
    swing_low_ref = swing_low['price'] if low_valid else raw_lowest
    swing_range = swing_high_ref - swing_low_ref
    position_in_range = (last_close - swing_low_ref) / swing_range if swing_range > 0 else 0.5
    if direction == 'bearish':
        retracement_pts = 2 if 0.5 <= position_in_range <= 0.9 else 0
    else:
        retracement_pts = 2 if 0.1 <= position_in_range <= 0.5 else 0

    score = confirmation_pts + momentum_pts + reward_pts + retracement_pts
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
        'swing_high': round(swing_high_ref, 5),
        'swing_low': round(swing_low_ref, 5),
        'score': score,
        'status': 'A+ SETUP' if score >= MIN_SIGNAL_SCORE else ('WATCH' if score >= WATCH_THRESHOLD else 'NO TRADE'),
        'is_signal': score >= MIN_SIGNAL_SCORE,
        'generated_at': time.time(),
        'anchor_candle_time': candles[-1].get('time'),
    }
