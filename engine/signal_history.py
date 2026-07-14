"""
Signal history tracker (admin-only view).
-------------------------------------------
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

This is an approximation (not tick-level backtesting), but it's an honest,
conservative one.
"""

import json
import os
import threading
import time

HISTORY_PATH = os.environ.get('SIGNAL_HISTORY_PATH', '/tmp/khala_signal_history.json')
_lock = threading.Lock()


def _load():
    if not os.path.exists(HISTORY_PATH):
        return {'entries': []}
    with open(HISTORY_PATH, 'r') as f:
        return json.load(f)


def _save(data):
    with open(HISTORY_PATH, 'w') as f:
        json.dump(data, f, indent=2)


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

    with _lock:
        data = _load()

        for e in data['entries']:
            if (e['status'] == 'PENDING' and e['symbol'] == setup['symbol']
                    and e['direction'] == setup['direction']
                    and abs(e['entry_price'] - setup['entry_price']) < 1e-9
                    and abs(e['sl_price'] - setup['sl_price']) < 1e-9):
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
            'generated_at': setup.get('generated_at', time.time()),
            'anchor_candle_time': setup.get('anchor_candle_time'),
            'status': 'PENDING',
            'resolved_at': None,
            'resolved_price': None,
        }
        data['entries'].append(entry)
        _save(data)
        return entry


def resolve_pending(symbol, candles):
    """
    Checks all PENDING entries for this symbol against candles that came
    after each entry's anchor_candle_time, in order, and marks WIN/LOSS
    the first time SL or TP1 is touched. Entries with no resolution yet
    stay PENDING. Returns the number of entries resolved in this call.
    """
    with _lock:
        data = _load()
        resolved_count = 0

        for e in data['entries']:
            if e['status'] != 'PENDING' or e['symbol'] != symbol:
                continue

            anchor = e.get('anchor_candle_time')
            relevant = [c for c in candles if anchor is None or c.get('time', 0) > anchor]
            relevant.sort(key=lambda c: c.get('time', 0))

            for c in relevant:
                hit_sl = False
                hit_tp = False
                if e['direction'] == 'bearish':
                    hit_sl = c['high'] >= e['sl_price']
                    hit_tp = c['low'] <= e['tp1']
                else:
                    hit_sl = c['low'] <= e['sl_price']
                    hit_tp = c['high'] >= e['tp1']

                if hit_sl:
                    # SL touched -- LOSS, even if TP1 was also touched in the
                    # same candle (can't determine intrabar order, so we
                    # assume the conservative outcome rather than overstate
                    # performance).
                    e['status'] = 'LOSS'
                    e['resolved_at'] = c.get('time')
                    e['resolved_price'] = e['sl_price']
                    resolved_count += 1
                    break
                elif hit_tp:
                    e['status'] = 'WIN'
                    e['resolved_at'] = c.get('time')
                    e['resolved_price'] = e['tp1']
                    resolved_count += 1
                    break

        _save(data)
        return resolved_count


def get_history(symbol=None, limit=200):
    data = _load()
    entries = data['entries']
    if symbol:
        entries = [e for e in entries if e['symbol'] == symbol]
    entries = sorted(entries, key=lambda e: e['generated_at'], reverse=True)
    return entries[:limit]


def compute_stats(symbol=None):
    data = _load()
    entries = data['entries']
    if symbol:
        entries = [e for e in entries if e['symbol'] == symbol]

    wins = sum(1 for e in entries if e['status'] == 'WIN')
    losses = sum(1 for e in entries if e['status'] == 'LOSS')
    pending = sum(1 for e in entries if e['status'] == 'PENDING')
    resolved = wins + losses

    return {
        'total_signals': len(entries),
        'wins': wins,
        'losses': losses,
        'pending': pending,
        'win_rate': round(wins / resolved * 100, 1) if resolved > 0 else None,
        'loss_rate': round(losses / resolved * 100, 1) if resolved > 0 else None,
    }
