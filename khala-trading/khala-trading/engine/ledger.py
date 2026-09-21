"""
Database-backed ledger. Tracks positions you (or auto-execute) open, and
computes live P&L against current price. Same function signatures as
before -- only the storage underneath changed from JSON files (wiped on
every deploy/restart) to a real persistent database (see db.py).
"""

import time
import db

P = db.placeholder()


def open_position(symbol, direction, entry_price, sl_price, tp1, tp2, tp3, lot_size):
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
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO ledger_positions (id, symbol, direction, entry_price, sl_price, "
            f"tp1, tp2, tp3, lot_size, status, opened_at, closed_at, close_price, realized_pnl) "
            f"VALUES ({P},{P},{P},{P},{P},{P},{P},{P},{P},{P},{P},{P},{P},{P})",
            (position['id'], position['symbol'], position['direction'], position['entry_price'],
             position['sl_price'], position['tp1'], position['tp2'], position['tp3'],
             position['lot_size'], position['status'], position['opened_at'],
             position['closed_at'], position['close_price'], position['realized_pnl']),
        )
        conn.commit()
        return position
    finally:
        conn.close()


def close_position(position_id, close_price, pip_value_per_lot=10.0, pip_size=0.0001):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM ledger_positions WHERE id = {P} AND status = 'RUNNING'", (position_id,))
        row = cur.fetchone()
        if row is None:
            return None
        p = db.row_to_dict(row)

        direction_sign = 1 if p['direction'] == 'bullish' else -1
        pips = ((close_price - p['entry_price']) / pip_size) * direction_sign
        realized_pnl = round(pips * pip_value_per_lot * p['lot_size'], 2)
        closed_at = time.time()

        cur.execute(
            f"UPDATE ledger_positions SET status = 'CLOSED', closed_at = {P}, "
            f"close_price = {P}, realized_pnl = {P} WHERE id = {P}",
            (closed_at, close_price, realized_pnl, position_id),
        )
        conn.commit()

        p.update({'status': 'CLOSED', 'closed_at': closed_at, 'close_price': close_price, 'realized_pnl': realized_pnl})
        return p
    finally:
        conn.close()


def get_positions(status=None):
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        if status:
            cur.execute(f"SELECT * FROM ledger_positions WHERE status = {P}", (status,))
        else:
            cur.execute("SELECT * FROM ledger_positions")
        return [db.row_to_dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


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
