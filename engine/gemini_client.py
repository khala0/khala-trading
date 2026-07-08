"""
Google Gemini client for generating plain-English trade narrative.
Uses the free-tier gemini-2.0-flash model, matching your existing KHALA TRADING setup.
"""

import os
import json
import urllib.request
import urllib.error

GEMINI_API_KEY = os.environ.get('GEMINI_API_KEY', '')
GEMINI_MODEL = os.environ.get('GEMINI_MODEL', 'gemini-2.0-flash')
GEMINI_URL = (
    f'https://generativelanguage.googleapis.com/v1beta/models/'
    f'{GEMINI_MODEL}:generateContent?key={{key}}'
)


def generate_narrative(setup: dict) -> str:
    """
    Given a setup dict from signal_engine.score_setup(), ask Gemini for a
    short plain-English explanation of the bias and setup. Falls back to a
    templated explanation if no API key is configured or the call fails,
    so the app still works before you've added your key.
    """
    if not GEMINI_API_KEY:
        return _fallback_narrative(setup)

    prompt = (
        f"You are a trading assistant. In 3-4 concise sentences, explain the "
        f"reasoning behind this {setup.get('direction', 'neutral')} setup on "
        f"{setup.get('symbol')}. Entry: {setup.get('entry_price')}, "
        f"Stop-loss: {setup.get('sl_price')}, "
        f"Swing high: {setup.get('swing_high')}, Swing low: {setup.get('swing_low')}, "
        f"Confluence score: {setup.get('score')}/10. "
        f"Write it like a professional market analyst. No headers, no markdown."
    )

    body = json.dumps({
        'contents': [{'parts': [{'text': prompt}]}]
    }).encode('utf-8')

    req = urllib.request.Request(
        GEMINI_URL.format(key=GEMINI_API_KEY),
        data=body,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return data['candidates'][0]['content']['parts'][0]['text'].strip()
    except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError) as e:
        return _fallback_narrative(setup, error=str(e))


def _fallback_narrative(setup: dict, error: str = None) -> str:
    """Templated narrative used when Gemini isn't configured or the call fails."""
    direction = setup.get('direction')
    if direction is None:
        return "No clean structural reference is available right now -- price has moved through both nearby swing points, so no valid setup is being generated."

    bias_word = 'bearish' if direction == 'bearish' else 'bullish'
    note = f" (AI narrative unavailable: {error})" if error else " (using fallback narrative -- add GEMINI_API_KEY for full AI reasoning)"
    return (
        f"{setup.get('symbol')} is showing a {bias_word} bias with a confluence score of "
        f"{setup.get('score')}/10. Entry is referenced at {setup.get('entry_price')}, with the stop "
        f"placed at {setup.get('sl_price')} using an ATR-scaled buffer beyond the nearest unmitigated "
        f"swing point, keeping risk tighter than a full structural stop while still respecting sweep risk."
        f"{note}"
    )
