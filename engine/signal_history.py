"""
Signal history tracker (admin-only view), database-backed.
-------------------------------------------------------------
Logs every A+ signal (score >= signal_engine.MIN_SIGNAL_SCORE) the engine
generates, then resolves each one's outcome by checking whether price
touched the stop-loss or TP1 first in the candles that followed.

Resolution approach: walks forward through candles after the signal was
generated, in chronological order, and checks each candle's high/low
against SL and TP1. Whichever level a candle touches first (by candle
order) determines the outcome. If a single candle's range touches BOTH
levels, this is treated as a LOSS -- we can't know which was hit first
within that candle from OHLC data alone, so we assume the worse outcome
rather than overstate performance.

Same function signatures as before -- only the storage underneath changed
from JSON files (wiped on every deploy/restart) to a real persistent
database (see db.py).
"""

import time
import db

P = db.placeholder()


def log_signal(setup):
    """
    Logs a setup IF it's an actionable signal (is_signal True) AND there
    isn't already a PENDING entry for the same symbol+direction+entry+SL
    combo -- this dedupes repeated polling of the same still-forming
    setup so history doesn't fill with duplicates every 60 seconds.

    Returns the logged entry, or None if not logged (not a signal, or
    a duplicate of an existing pending entry).
    """
    if not setup.get('is_signal') or setup.get('direction') is None:
        return None

    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT id FROM signal_history WHERE status = 'PENDING' AND symbol = {P} "
            f"AND direction = {P} AND ABS(entry_price - {P}) < 1e-9 AND ABS(sl_price - {P}) < 1e-9",
            (setup['symbol'], setup['direction'], setup['entry_price'], setup['sl_price']),
        )
        if cur.fetchone():
            return None  # already logged, still pending

        entry = {
            'id': f"{setup['symbol']}-{int(setup.get('generated_at', time.time()) * 1000)}",
            'symbol': setup['symbol'],
            'direction': setup['direction'],
            'entry_price': setup['entry_price'],
            'sl_price': setup['sl_price'],
            'tp1': setup['targets']['tp1'],
            'tp2': setup['targets']['tp2'],
            'tp3': setup['targets']['tp3'],
            'score': setup['score'],
            'lot_size': setup.get('lot_size'),
            'atr_value': setup.get('atr_value'),
            'atr_multiplier_used': setup.get('atr_multiplier_used'),
            'risk_amount_usd': setup.get('risk_amount_usd'),
            'stop_distance_pips': setup.get('stop_distance_pips'),
            'trend_4h': setup.get('trend_4h'),
            'trend_1h': setup.get('trend_1h'),
            'htf_agreement': 1 if setup.get('htf_agreement') else 0,
            'narrative': setup.get('narrative'),
            'generated_at': setup.get('generated_at', time.time()),
            'anchor_candle_time': setup.get('anchor_candle_time'),
            'status': 'PENDING',
            'resolved_at': None,
            'resolved_price': None,
        }
        cols = list(entry.keys())
        placeholders = ','.join([P] * len(cols))
        cur.execute(
            f"INSERT INTO signal_history ({','.join(cols)}) VALUES ({placeholders})",
            tuple(entry[c] for c in cols),
        )
        conn.commit()
        entry['htf_agreement'] = bool(entry['htf_agreement'])
        return entry
    finally:
        conn.close()


def resolve_pending(symbol, candles):
    """
    Checks all PENDING entries for this symbol against candles that came
    after each entry's anchor_candle_time, in order, and marks WIN/LOSS
    the first time SL or TP1 is touched. Entries with no resolution yet
    stay PENDING. Returns the number of entries resolved in this call.
    """
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM signal_history WHERE status = 'PENDING' AND symbol = {P}", (symbol,))
        pending_entries = [db.row_to_dict(r) for r in cur.fetchall()]

        resolved_count = 0
        for e in pending_entries:
            anchor = e.get('anchor_candle_time')
            relevant = [c for c in candles if anchor is None or c.get('time', 0) > anchor]
            relevant.sort(key=lambda c: c.get('time', 0))

            for c in relevant:
                if e['direction'] == 'bearish':
                    hit_sl = c['high'] >= e['sl_price']
                    hit_tp = c['low'] <= e['tp1']
                else:
                    hit_sl = c['low'] <= e['sl_price']
                    hit_tp = c['high'] >= e['tp1']

                if hit_sl:
                    cur.execute(
                        f"UPDATE signal_history SET status = 'LOSS', resolved_at = {P}, "
                        f"resolved_price = {P} WHERE id = {P}",
                        (c.get('time'), e['sl_price'], e['id']),
                    )
                    resolved_count += 1
                    break
                elif hit_tp:
                    cur.execute(
                        f"UPDATE signal_history SET status = 'WIN', resolved_at = {P}, "
                        f"resolved_price = {P} WHERE id = {P}",
                        (c.get('time'), e['tp1'], e['id']),
                    )
                    resolved_count += 1
                    break

        conn.commit()
        return resolved_count
    finally:
        conn.close()


def has_pending_signal(symbol):
    """
    True if this symbol currently has an unresolved (PENDING) signal --
    used to enforce 'wait for TP or SL before issuing a new signal on the
    same symbol' rather than stacking multiple concurrent signals.
    """
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT id FROM signal_history WHERE symbol = {P} AND status = 'PENDING' LIMIT 1", (symbol,))
        return cur.fetchone() is not None
    finally:
        conn.close()


def get_history(symbol=None, limit=200):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        if symbol:
            cur.execute(f"SELECT * FROM signal_history WHERE symbol = {P} ORDER BY generated_at DESC LIMIT {P}", (symbol, limit))
        else:
            cur.execute(f"SELECT * FROM signal_history ORDER BY generated_at DESC LIMIT {P}", (limit,))
        rows = [db.row_to_dict(r) for r in cur.fetchall()]
        for r in rows:
            r['htf_agreement'] = bool(r.get('htf_agreement'))
        return rows
    finally:
        conn.close()


def count_signals_today(symbol):
    """
    Counts how many signals were logged for this symbol so far today
    (server-local calendar day, based on generated_at timestamps).
    """
    today_str = time.strftime('%Y-%m-%d')
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT generated_at FROM signal_history WHERE symbol = {P}", (symbol,))
        count = 0
        for row in cur.fetchall():
            generated_at = db.row_to_dict(row)['generated_at']
            if time.strftime('%Y-%m-%d', time.localtime(generated_at)) == today_str:
                count += 1
        return count
    finally:
        conn.close()


def compute_stats(symbol=None):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        if symbol:
            cur.execute(f"SELECT status FROM signal_history WHERE symbol = {P}", (symbol,))
        else:
            cur.execute("SELECT status FROM signal_history")
        statuses = [db.row_to_dict(r)['status'] for r in cur.fetchall()]

        wins = statuses.count('WIN')
        losses = statuses.count('LOSS')
        pending = statuses.count('PENDING')
        resolved = wins + losses

        return {
            'total_signals': len(statuses),
            'wins': wins,
            'losses': losses,
            'pending': pending,
            'win_rate': round(wins / resolved * 100, 1) if resolved > 0 else None,
            'loss_rate': round(losses / resolved * 100, 1) if resolved > 0 else None,
        }
    finally:
        conn.close()
