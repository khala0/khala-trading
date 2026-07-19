"""
KHALA TRADING -- Flask backend
-----------------------------------
Serves the dashboard frontend and a small JSON API:

  GET  /api/price/<symbol>          recent candles + latest price (public)
  GET  /api/signal/<symbol>         current setup (subscription or admin required)
  GET  /api/ledger                  positions + P&L summary (subscription or admin required)
  POST /api/ledger/open             open a position (admin only)
  POST /api/ledger/close            close a position (admin only)
  POST /api/admin/login             simple password-gated admin session
  GET  /api/admin/check             check if current session is authenticated

  POST /api/auth/signup             create a user account
  POST /api/auth/login              log in
  POST /api/auth/logout             log out
  GET  /api/auth/status             current login + subscription status

  POST /api/billing/create-checkout-session   start a Stripe subscription checkout
  POST /api/billing/webhook                   Stripe webhook (payment/cancellation events)

Configuration is via environment variables (see .env.example):
  GEMINI_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
  ADMIN_PASSWORD, FLASK_SECRET_KEY,
  STRIPE_SECRET_KEY, STRIPE_PRICE_ID, STRIPE_WEBHOOK_SECRET
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
import users
import stripe_client
import signal_history
import multi_timeframe_engine
import news_filter
import master_signal

app = Flask(__name__, static_folder='static', static_url_path='')
app.secret_key = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-change-me')

ADMIN_PASSWORD = os.environ.get('ADMIN_PASSWORD', 'change-me')

SUPPORTED_SYMBOLS = list(signal_engine.ASSET_ATR_MULTIPLIERS.keys())

# Rough pip sizing per symbol for P&L/lot math -- adjust to your broker's quoting.
#
# FIXED 2026-07-17: this table (and, before this table existed, ledger.py's
# own hardcoded defaults) used a single 4-decimal-FX convention -- pip_size
# 0.0001, $10/lot -- for EVERY symbol. That's right for EURUSD/GBPUSD/AUDUSD,
# but for gold, JPY pairs, silver, crypto, or indices it made the Ledger
# tab's P&L figures wrong by orders of magnitude (e.g. a $0.0001-per-"pip"
# convention applied to gold, which moves in whole dollars, would have
# registered a normal few-dollar move as tens of thousands of "pips").
#
# The values below assume standard lot sizes (100,000 units for FX, 100oz
# for gold, 5000oz for silver, 1 BTC for crypto, $1/point for the indices) --
# verify these against Exness's actual contract specification for your
# account type, since exact contract sizes do vary by broker.
#
# USDJPY/GBPJPY pip value is a static approximation (~USDJPY near 110-115).
# Unlike the direct-USD-quoted pairs above, JPY-quoted pairs' true pip value
# moves with the live USDJPY rate -- a full fix would convert dynamically at
# close time, which this simple ledger doesn't do. Fine for a rough dashboard
# P&L, not precise enough to rely on for anything that matters financially.
PIP_CONFIG = {
    'XAUUSD': {'pip_size': 0.01, 'pip_value_per_lot': 1.0},     # 100oz/lot
    'XAGUSD': {'pip_size': 0.001, 'pip_value_per_lot': 5.0},    # 5000oz/lot
    'EURUSD': {'pip_size': 0.0001, 'pip_value_per_lot': 10.0},
    'GBPUSD': {'pip_size': 0.0001, 'pip_value_per_lot': 10.0},
    'GBPJPY': {'pip_size': 0.01, 'pip_value_per_lot': 9.0},     # approx, see note above
    'AUDUSD': {'pip_size': 0.0001, 'pip_value_per_lot': 10.0},
    'USDJPY': {'pip_size': 0.01, 'pip_value_per_lot': 9.0},     # approx, see note above
    'US30': {'pip_size': 1.0, 'pip_value_per_lot': 1.0},
    'NAS100': {'pip_size': 1.0, 'pip_value_per_lot': 1.0},
    'BTCUSD': {'pip_size': 1.0, 'pip_value_per_lot': 1.0},      # 1 BTC/lot
}


def current_user_valid():
    """
    Returns the logged-in user's email if their session token is still the
    active one for that account, else None (and clears the stale session).
    A session goes stale the moment the same account logs in elsewhere --
    this is what stops two people sharing one login at the same time.
    """
    email = session.get('user_email')
    token = session.get('session_token')
    if not email or not token:
        return None
    if not users.is_session_valid(email, token):
        session.pop('user_email', None)
        session.pop('session_token', None)
        return None
    return email


def require_admin(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get('is_admin'):
            return jsonify({'error': 'Not authenticated'}), 401
        return f(*args, **kwargs)
    return wrapper


def require_subscription(f):
    """Allows access if the user is an admin OR a logged-in (single-session,
    non-shared), active subscriber."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if session.get('is_admin'):
            return f(*args, **kwargs)
        email = current_user_valid()
        if not email:
            return jsonify({'error': 'Login required', 'code': 'LOGIN_REQUIRED'}), 401
        if not users.is_subscribed(email):
            return jsonify({'error': 'Active subscription required', 'code': 'SUBSCRIPTION_REQUIRED'}), 402
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
@require_subscription
def signal(symbol):
    symbol = symbol.upper()
    if symbol not in signal_engine.ASSET_ATR_MULTIPLIERS:
        return jsonify({'error': f'Unsupported symbol {symbol}'}), 400

    account_balance = float(request.args.get('balance', 10000))
    risk_percent = float(request.args.get('risk', 1.0))
    pip_cfg = PIP_CONFIG.get(symbol, {'pip_size': 0.0001, 'pip_value_per_lot': 10.0})

    try:
        mtf_data = price_feed.fetch_multi_timeframe(symbol)

        # Resolve any pending signal for this symbol against fresh price data
        # BEFORE deciding whether a new one is allowed to fire.
        signal_history.resolve_pending(symbol, mtf_data['candles_5m'])

        setup = master_signal.generate_signal(
            symbol, mtf_data['candles_4h'], mtf_data['candles_1h'], mtf_data['candles_5m'],
            account_balance=account_balance, risk_percent=risk_percent,
            pip_value_per_lot=pip_cfg['pip_value_per_lot'], pip_size=pip_cfg['pip_size'],
        )

        # One active signal per symbol at a time: if the previous signal on
        # this symbol hasn't hit TP or SL yet, don't issue a new one even if
        # this setup would otherwise qualify.
        if setup.get('is_signal') and signal_history.has_pending_signal(symbol):
            setup['is_signal'] = False
            setup['status'] = 'MONITORING - AWAITING PREVIOUS SIGNAL RESULT'
            setup['reason'] = (
                f'A previous {symbol} signal is still open (has not hit TP or SL yet) -- '
                f'waiting for it to resolve before issuing a new one'
            )

        narrative = gemini_client.generate_narrative(setup)
        setup['narrative'] = narrative

        # Auto-alert and log to history on high-confidence setups only
        if setup.get('is_signal'):
            telegram_client.send_alert(setup, narrative)
            signal_history.log_signal(setup)

        return jsonify(setup)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.route('/api/ledger')
@require_subscription
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


# ---------------------------------------------------------------------
# User auth (separate from admin login above)
# ---------------------------------------------------------------------

@app.route('/api/auth/signup', methods=['POST'])
def signup():
    data = request.get_json(force=True)
    email = (data.get('email') or '').strip()
    password = data.get('password') or ''
    if not email or '@' not in email:
        return jsonify({'error': 'Valid email required'}), 400
    if len(password) < 8:
        return jsonify({'error': 'Password must be at least 8 characters'}), 400

    user, err = users.create_user(email, password)
    if err:
        return jsonify({'error': err}), 400

    token = users.start_new_session(user['email'], ip=request.remote_addr)
    session['user_email'] = user['email']
    session['session_token'] = token
    return jsonify({'success': True, 'email': user['email'], 'is_subscribed': user['is_subscribed']})


@app.route('/api/auth/login', methods=['POST'])
def user_login():
    data = request.get_json(force=True)
    email = (data.get('email') or '').strip()
    password = data.get('password') or ''

    if not users.verify_password(email, password):
        return jsonify({'error': 'Invalid email or password'}), 401

    email = email.lower()
    token = users.start_new_session(email, ip=request.remote_addr)
    session['user_email'] = email
    session['session_token'] = token
    return jsonify({'success': True, 'email': email, 'is_subscribed': users.is_subscribed(email)})


@app.route('/api/auth/logout', methods=['POST'])
def user_logout():
    session.pop('user_email', None)
    session.pop('session_token', None)
    return jsonify({'success': True})


@app.route('/api/auth/status')
def auth_status():
    had_stale_session = bool(session.get('user_email'))
    email = current_user_valid()
    return jsonify({
        'logged_in': bool(email),
        'email': email,
        'is_subscribed': users.is_subscribed(email) if email else False,
        'is_admin': bool(session.get('is_admin')),
        'session_replaced': had_stale_session and not email,
    })


# ---------------------------------------------------------------------
# Stripe billing
# ---------------------------------------------------------------------

@app.route('/api/billing/create-checkout-session', methods=['POST'])
def create_checkout_session():
    email = session.get('user_email')
    if not email:
        return jsonify({'error': 'Login required before subscribing'}), 401

    base_url = request.host_url.rstrip('/')
    checkout_url, err = stripe_client.create_checkout_session(
        email=email,
        success_url=f'{base_url}/?checkout=success',
        cancel_url=f'{base_url}/?checkout=cancel',
    )
    if err:
        return jsonify({'error': err}), 502
    return jsonify({'checkout_url': checkout_url})


@app.route('/api/billing/webhook', methods=['POST'])
def stripe_webhook():
    payload = request.data
    sig_header = request.headers.get('Stripe-Signature', '')
    try:
        event = stripe_client.construct_webhook_event(payload, sig_header)
    except (ValueError, Exception) as e:
        return jsonify({'error': f'Invalid webhook: {e}'}), 400

    result = stripe_client.handle_webhook_event(event)
    return jsonify({'received': True, 'result': result})


# ---------------------------------------------------------------------
# Admin: comp (free) access management
# ---------------------------------------------------------------------

@app.route('/api/admin/users')
@require_admin
def admin_list_users():
    return jsonify({'users': users.list_users()})


@app.route('/api/admin/grant-access', methods=['POST'])
@require_admin
def admin_grant_access():
    data = request.get_json(force=True)
    email = (data.get('email') or '').strip()
    if not email:
        return jsonify({'error': 'Email required'}), 400
    ok, err = users.set_comp_access(email, granted=True)
    if not ok:
        return jsonify({'error': err}), 404
    return jsonify({'success': True, 'email': email.lower(), 'is_subscribed': True})


@app.route('/api/admin/revoke-access', methods=['POST'])
@require_admin
def admin_revoke_access():
    data = request.get_json(force=True)
    email = (data.get('email') or '').strip()
    if not email:
        return jsonify({'error': 'Email required'}), 400
    ok, err = users.set_comp_access(email, granted=False)
    if not ok:
        return jsonify({'error': err}), 404
    return jsonify({'success': True, 'email': email.lower(), 'is_subscribed': False})


@app.route('/api/admin/news-check')
@require_admin
def admin_news_check():
    """
    Debug route to verify the ForexFactory scrape actually works once
    deployed (it can only be tested against the real site from a live
    server with internet access -- see news_filter.py's docstring for
    what to check if this comes back empty on a day with known news).
    """
    symbol = request.args.get('symbol', 'XAUUSD').upper()
    try:
        events = news_filter.fetch_high_impact_events()
        blocked, matching_event = news_filter.is_near_high_impact_news(symbol)
        return jsonify({
            'symbol': symbol,
            'total_high_impact_events_found': len(events),
            'events': [
                {'datetime': e['datetime'].isoformat(), 'currency': e['currency'], 'event': e['event']}
                for e in events
            ],
            'currently_blocked': blocked,
            'blocking_event': (
                {'datetime': matching_event['datetime'].isoformat(), 'currency': matching_event['currency'], 'event': matching_event['event']}
                if matching_event else None
            ),
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 502


@app.route('/api/admin/signal-history')
@require_admin
def admin_signal_history():
    symbol_filter = request.args.get('symbol')

    # Refresh outcomes for every symbol that has pending entries before
    # returning the list, so the win/loss numbers are current.
    pending_symbols = {e['symbol'] for e in signal_history.get_history(limit=10000) if e['status'] == 'PENDING'}
    for sym in pending_symbols:
        try:
            candles = price_feed.fetch_candles(sym, interval='5m', range_='5d')
            signal_history.resolve_pending(sym, candles)
        except Exception:
            continue  # if a symbol's price fetch fails, leave those entries pending for next refresh

    return jsonify({
        'history': signal_history.get_history(symbol=symbol_filter, limit=200),
        'stats': signal_history.compute_stats(symbol=symbol_filter),
    })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=os.environ.get('FLASK_DEBUG', '') == '1')
