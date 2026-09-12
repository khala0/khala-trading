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
    Given a setup dict from master_signal.generate_signal() (the function
    app.py actually calls -- NOT signal_engine.score_setup(), which this
    docstring used to reference but is unused dead code, see the LEGACY
    notice at the top of signal_engine.py), ask Gemini for a short
    plain-English explanation of the bias and setup. Falls back to a
    templated explanation if no API key is configured or the call fails,
    so the app still works before you've added your key.
    """
    if not GEMINI_API_KEY:
        return _fallback_narrative(setup)

    pd_info = setup.get('premium_discount') or {}
    crt_info = setup.get('crt_sweep') or {}
    prompt = (
        f"You are a trading assistant. In 3-4 concise sentences, explain the "
        f"reasoning behind this {setup.get('direction', 'neutral')} setup on "
        f"{setup.get('symbol')}. Entry: {setup.get('entry_price')}, "
        f"Stop-loss: {setup.get('sl_price')}, "
        f"4H trend: {setup.get('trend_4h')}, 1H trend: {setup.get('trend_1h')}, "
        f"1H/4H agreement: {setup.get('htf_agreement')}, "
        f"5M execution trigger present: {setup.get('execution_ready')}, "
        f"Premium/Discount zone: {pd_info.get('zone')} (favorable: {pd_info.get('favorable')}), "
        f"CRT sweep detected: {crt_info.get('swept')} (direction: {crt_info.get('direction')}), "
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
        return setup.get('reason') or "No valid setup is being generated right now."

    bias_word = 'bearish' if direction == 'bearish' else 'bullish'
    agreement_note = "with 1H structure confirming the same direction" if setup.get('htf_agreement') else "while 1H is currently in a pullback against it"

    pd_info = setup.get('premium_discount') or {}
    crt_info = setup.get('crt_sweep') or {}
    extra_notes = []
    if pd_info.get('zone'):
        favorable_word = "a favorable" if pd_info.get('favorable') else "an unfavorable"
        extra_notes.append(f"Price sits in {favorable_word} {pd_info['zone']} zone relative to the current range.")
    if crt_info.get('swept'):
        extra_notes.append(f"A CRT liquidity sweep was detected in the {crt_info.get('direction')} direction.")
    extra_text = " " + " ".join(extra_notes) if extra_notes else ""

    note = f" (AI narrative unavailable: {error})" if error else " (using fallback narrative -- add GEMINI_API_KEY for full AI reasoning)"
    return (
        f"{setup.get('symbol')} shows a {bias_word} 4H trend {agreement_note}, with a confluence score of "
        f"{setup.get('score')}/10. Entry is referenced at {setup.get('entry_price')}, with the stop "
        f"placed at {setup.get('sl_price')} using an ATR-scaled buffer beyond the 4H structural level, "
        f"keeping the direction locked to the higher timeframe trend rather than reacting to lower-timeframe noise."
        f"{extra_text}{note}"
    )
