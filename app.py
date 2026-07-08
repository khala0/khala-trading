"""
KHALA TRADING -- Flask backend
-----------------------------------
Serves the dashboard frontend and a small JSON API:

  GET  /api/price/<symbol>          recent candles + latest price
  GET  /api/signal/<symbol>         current setup (direction, SL/TP, score, AI narrative)
  GET  /api/ledger                  positions + P&L summary
  POST /api/ledger/open             open a position from a confirmed setup
  POST /api/ledger/close            close a position
  POST /api/admin/login             simple password-gated admin session
  GET  /api/admin/check             check if current session is authenticated

Configuration is via environment variables (see .env.example):
  GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
  ADMIN_PASSWORD, FLASK_SECRET_KEY
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'engine'))

from flask import Flask, jsonify, request, session, send_from_directory
from functools import wraps

import price_feed
import signal_engine
import gemini_client
import telegram_client
import ledger

app = Flask(__name__, static_folder='static', static_url_path='')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-change-me')

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'change-me')

SUPPORTED_SYMBOLS = list(signal_engine.ASSET_ATR_MULTIPLIERS.keys())

# Rough pip sizing per symbol for P&L/lot math -- adjust to your broker's quoting
PIP_CONFIG = {
    'XAUUSD': {'pip_size': 0.1, 'pip_value_per_lot': 1.0},
    'XAGUSD': {'pip_size': 0.01, 'pip_value_per_lot': 5.0},
    'EURUSD': {'pip_size': 0.0001, 'pip_value_per_lot': 10.0},
    'GBPUSD': {'pip_size': 0.0001, 'pip_value_per_lot': 10.0},
    'GBPJPY': {'pip_size': 0.01, 'pip_value_per_lot': 9.0},
    'AUDUSD': {'pip_size': 0.0001, 'pip_value_per_lot': 10.0},
    'USDJPY': {'pip_size': 0.01, 'pip_value_per_lot': 9.0},
    'US30': {'pip_size': 1.0, 'pip_value_per_lot': 1.0},
    'NAS100': {'pip_size': 1.0, 'pip_value_per_lot': 1.0},
    'BTCUSD': {'pip_size': 1.0, 'pip_value_per_lot': 1.0},
}


def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('is_admin'):
            return jsonify({'error': 'Not authenticated'}), 401
        return f(*args, **kwargs)
    return wrapper


@app.route('/')
def index():
    return send_from_directory(app.static_folder, 'index.html')


@app.route('/api/symbols')
def symbols():
    return jsonify({'symbols': SUPPORTED_SYMBOLS})


@app.route('/api/price/<symbol>')
def price(symbol):
    symbol = symbol.upper()
    if symbol not in signal_engine.ASSET_ATR_MULTIPLIERS:
        return jsonify({'error': f'Unsupported symbol {symbol}'}), 400
    try:
        candles = price_feed.fetch_candles(symbol, interval='5m', range_='5d')
        if not candles:
            return jsonify({'error': 'No price data returned'}), 502
        latest = candles[-1]
        return jsonify({
            'symbol': symbol,
            'latest_close': latest['close'],
            'candles': candles[-200:],  # cap payload size
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.route('/api/signal/<symbol>')
def signal(symbol):
    symbol = symbol.upper()
    if symbol not in signal_engine.ASSET_ATR_MULTIPLIERS:
        return jsonify({'error': f'Unsupported symbol {symbol}'}), 400

    account_balance = float(request.args.get('balance', 10000))
    risk_percent = float(request.args.get('risk', 1.0))
    pip_cfg = PIP_CONFIG.get(symbol, {'pip_size': 0.0001, 'pip_value_per_lot': 10.0})

    try:
        candles = price_feed.fetch_candles(symbol, interval='5m', range_='5d')
        setup = signal_engine.score_setup(
            candles, symbol=symbol,
            account_balance=account_balance, risk_percent=risk_percent,
            pip_value_per_lot=pip_cfg['pip_value_per_lot'], pip_size=pip_cfg['pip_size'],
        )
        narrative = gemini_client.generate_narrative(setup)
        setup['narrative'] = narrative

        # Auto-alert on high-confidence setups only
        if setup.get('status') == 'A+ SETUP':
            telegram_client.send_alert(setup, narrative)

        return jsonify(setup)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.route('/api/ledger')
def get_ledger():
    positions = ledger.get_positions()
    pip_cfg_default = {'pip_size': 0.0001, 'pip_value_per_lot': 10.0}

    for p in positions:
        if p['status'] == 'RUNNING':
            try:
                candles = price_feed.fetch_candles(p['symbol'], interval='5m', range_='1d')
                current_price = candles[-1]['close'] if candles else p['entry_price']
            except Exception:
                current_price = p['entry_price']
            pip_cfg = PIP_CONFIG.get(p['symbol'], pip_cfg_default)
            p['current_price'] = current_price
            p['floating_pnl'] = ledger.compute_floating_pnl(
                p, current_price,
                pip_value_per_lot=pip_cfg['pip_value_per_lot'],
                pip_size=pip_cfg['pip_size'],
            )

    return jsonify({'positions': positions, 'summary': ledger.summary()})


@app.route('/api/ledger/open', methods=['POST'])
@require_admin
def open_position():
    data = request.get_json(force=True)
    required = ['symbol', 'direction', 'entry_price', 'sl_price', 'tp1', 'tp2', 'tp3', 'lot_size']
    if not all(k in data for k in required):
        return jsonify({'error': f'Missing fields, need: {required}'}), 400
    position = ledger.open_position(**{k: data[k] for k in required})
    return jsonify(position)


@app.route('/api/ledger/close', methods=['POST'])
@require_admin
def close_position():
    data = request.get_json(force=True)
    if 'position_id' not in data or 'close_price' not in data:
        return jsonify({'error': 'Need position_id and close_price'}), 400
    pip_cfg = PIP_CONFIG.get(data.get('symbol', ''), {'pip_size': 0.0001, 'pip_value_per_lot': 10.0})
    result = ledger.close_position(
        data['position_id'], data['close_price'],
        pip_value_per_lot=pip_cfg['pip_value_per_lot'], pip_size=pip_cfg['pip_size'],
    )
    if result is None:
        return jsonify({'error': 'Position not found or already closed'}), 404
    return jsonify(result)


@app.route('/api/admin/login', methods=['POST'])
def admin_login():
    data = request.get_json(force=True)
    if data.get('password') == ADMIN_PASSWORD:
        session['is_admin'] = True
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Invalid password'}), 401


@app.route('/api/admin/logout', methods=['POST'])
def admin_logout():
    session.pop('is_admin', None)
    return jsonify({'success': True})


@app.route('/api/admin/check')
def admin_check():
    return jsonify({'is_admin': bool(session.get('is_admin'))})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=os.environ.get('FLASK_DEBUG', '') == '1')
