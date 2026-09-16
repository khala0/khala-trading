"""
Telegram alert client. Sends a formatted message to your bot/chat whenever
a new A+ setup is generated. No-ops quietly if not configured.
"""

import os
import json
import urllib.request
import urllib.error

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')


def send_text(text: str) -> dict:
    """
    Sends a plain Telegram message. Returns a dict with 'sent': bool and
    either 'error' or the raw Telegram response.

    ADDED 2026-07-17: factored out of send_alert() so anything that needs to
    ping you -- not just a new setup -- can reuse it (mt5_executor.py uses
    this for crash/restart/daily-loss-limit alerts, so a 24/7 unattended bot
    can actually tell you when something's wrong instead of failing silently).
    """
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return {'sent': False, 'error': 'Telegram not configured (missing bot token or chat id)'}

    url = f'https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage'
    body = json.dumps({
        'chat_id': TELEGRAM_CHAT_ID,
        'text': text,
        'parse_mode': 'Markdown',
    }).encode('utf-8')

    req = urllib.request.Request(
        url, data=body, headers={'Content-Type': 'application/json'}, method='POST'
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return {'sent': True, 'response': json.loads(resp.read().decode('utf-8'))}
    except urllib.error.URLError as e:
        return {'sent': False, 'error': str(e)}


def send_alert(setup: dict, narrative: str = '') -> dict:
    """
    Sends a Telegram message summarizing the setup. Returns a dict with
    'sent': bool and either 'error' or the raw Telegram response.
    """
    if setup.get('direction') is None:
        return {'sent': False, 'error': 'No valid setup to alert on'}

    text = (
        f"*{setup.get('symbol')} — {setup.get('status')}*\n"
        f"Direction: {setup.get('direction').upper()}\n"
        f"Entry: {setup.get('entry_price')}\n"
        f"SL: {setup.get('sl_price')}  (ATR x{setup.get('atr_multiplier_used')})\n"
        f"TP1/TP2/TP3: {setup['targets']['tp1']} / {setup['targets']['tp2']} / {setup['targets']['tp3']}\n"
        f"Lot size: {setup.get('lot_size')}  |  Score: {setup.get('score')}/10\n\n"
        f"{narrative}"
    )
    return send_text(text)
