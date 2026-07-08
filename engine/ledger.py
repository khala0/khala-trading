"""
Simple JSON-file backed ledger. Tracks positions you (or auto-execute)
open, and computes live P&L against current price.

For a production system you'd swap this for a real database (Firestore,
Postgres, etc.) -- this keeps things dependency-free and easy to inspect
for now.
"""

import json
import os
import threading
import time

LEDGER_PATH = os.environ.get('LEDGER_PATH', '/tmp/khala-trading_ledger.json')
_lock = threading.Lock()


def _load():
    if not os.path.exists(LEDGER_PATH):
        return {'positions': []}
    with open(LEDGER_PATH, 'r') as f:
        return json.load(f)


def _save(data):
    with open(LEDGER_PATH, 'w') as f:
        json.dump(data, f, indent=2)


def open_position(symbol, direction, entry_price, sl_price, tp1, tp2, tp3, lot_size):
    with _lock:
        data = _load()
        position = {
            'id': f"{symbol}-{int(time.time() * 1000)}",
            'symbol': symbol,
            'direction': direction,
            'entry_price': entry_price,
            'sl_price': sl_price,
            'tp1': tp1, 'tp2': tp2, 'tp3': tp3,
            'lot_size': lot_size,
            'status': 'RUNNING',
            'opened_at': time.time(),
            'closed_at': None,
            'close_price': None,
            'realized_pnl': 0.0,
        }
        data['positions'].append(position)
        _save(data)
        return position


def close_position(position_id, close_price, pip_value_per_lot=10.0, pip_size=0.0001):
    with _lock:
        data = _load()
        for p in data['positions']:
            if p['id'] == position_id and p['status'] == 'RUNNING':
                direction_sign = 1 if p['direction'] == 'bullish' else -1
                pips = ((close_price - p['entry_price']) / pip_size) * direction_sign
                p['realized_pnl'] = round(pips * pip_value_per_lot * p['lot_size'], 2)
                p['status'] = 'CLOSED'
                p['closed_at'] = time.time()
                p['close_price'] = close_price
                _save(data)
                return p
        return None


def get_positions(status=None):
    data = _load()
    if status:
        return [p for p in data['positions'] if p['status'] == status]
    return data['positions']


def compute_floating_pnl(position, current_price, pip_value_per_lot=10.0, pip_size=0.0001):
    direction_sign = 1 if position['direction'] == 'bullish' else -1
    pips = ((current_price - position['entry_price']) / pip_size) * direction_sign
    return round(pips * pip_value_per_lot * position['lot_size'], 2)


def summary():
    positions = get_positions()
    closed = [p for p in positions if p['status'] == 'CLOSED']
    total_profit = sum(p['realized_pnl'] for p in closed if p['realized_pnl'] > 0)
    total_loss = sum(p['realized_pnl'] for p in closed if p['realized_pnl'] < 0)
    return {
        'total_closed_profit': round(total_profit, 2),
        'total_closed_loss': round(total_loss, 2),
        'net_profit': round(total_profit + total_loss, 2),
        'total_trades': len(positions),
        'running_count': len([p for p in positions if p['status'] == 'RUNNING']),
    }
