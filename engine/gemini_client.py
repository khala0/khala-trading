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


def validate_signal(setup: dict) -> dict:
    """
    Asks Gemini to act as a second-opinion analyst and explicitly APPROVE
    or REJECT the signal before it gets dispatched. This is genuine AI
    analytical work -- not narration of a pre-made decision, but an
    independent critique of the setup's validity.

    Returns a dict:
        approved:   True/False
        confidence: 'high' | 'medium' | 'low'
        reasoning:  short explanation of the decision
        risks:      list of flagged concerns (empty if approved with high confidence)

    Fails SAFE: if Gemini is unavailable or the key isn't set, returns
    approved=True so the existing score-based logic still controls the gate
    rather than blocking all signals silently. The signal display will show
    whether AI validation ran or was skipped.
    """
    if not GEMINI_API_KEY:
        return {
            'approved': True, 'confidence': 'unknown',
            'reasoning': 'Gemini validation skipped -- no API key configured',
            'risks': [], 'validation_ran': False,
        }

    pd_info = setup.get('premium_discount') or {}
    crt_info = setup.get('crt_sweep') or {}
    fib_info = setup.get('fibonacci_zone') or {}

    prompt = f"""You are a professional forex and gold trading analyst with deep expertise in Smart Money Concepts (SMC), ICT methodology, and multi-timeframe analysis.

A trading signal has been generated with the following parameters. Your job is to act as a SECOND-OPINION RISK ANALYST and decide whether this signal should be executed or rejected.

SIGNAL DETAILS:
- Symbol: {setup.get('symbol')}
- Direction: {setup.get('direction', '').upper()}
- Entry price: {setup.get('entry_price')}
- Stop-loss: {setup.get('sl_price')}
- TP1: {setup.get('targets', {}).get('tp1') if setup.get('targets') else 'N/A'}
- TP2: {setup.get('targets', {}).get('tp2') if setup.get('targets') else 'N/A'}
- Confluence score: {setup.get('score')}/10

STRUCTURE & CONFLUENCE:
- 4H trend: {setup.get('trend_4h')}
- 1H trend: {setup.get('trend_1h')}
- 15M trend: {setup.get('trend_15m')}
- 1H agrees with 4H: {setup.get('htf_agreement')}
- 5M execution trigger: {setup.get('execution_ready')}
- Premium/Discount zone: {pd_info.get('zone')} (favorable: {pd_info.get('favorable')})
- CRT sweep: {crt_info.get('swept')} ({crt_info.get('direction')} direction)
- Fibonacci retracement zone: {fib_info.get('in_entry_zone')} (ratio: {fib_info.get('retracement_ratio')})
- Engine reasoning: {setup.get('reason')}

Respond with ONLY a valid JSON object in this exact format (no markdown, no explanation outside the JSON):
{{
  "approved": true or false,
  "confidence": "high" or "medium" or "low",
  "reasoning": "2-3 sentences explaining your decision",
  "risks": ["risk 1", "risk 2"]
}}

APPROVE if: multiple timeframes align, entry is in a logical zone, stop is structurally placed, and reward:risk justifies the trade.
REJECT if: timeframes conflict, entry is chasing price, stop is illogically placed, or the setup lacks meaningful confluence beyond just a trend direction."""

    body = json.dumps({
        'contents': [{'parts': [{'text': prompt}]}],
        'generationConfig': {'temperature': 0.2},  # low temperature = more consistent, less hallucination
    }).encode('utf-8')

    req = urllib.request.Request(
        GEMINI_URL.format(key=GEMINI_API_KEY),
        data=body,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        raw = data['candidates'][0]['content']['parts'][0]['text'].strip()
        # Strip markdown fences if Gemini adds them despite instructions
        raw = raw.replace('```json', '').replace('```', '').strip()
        result = json.loads(raw)
        return {
            'approved': bool(result.get('approved', True)),
            'confidence': result.get('confidence', 'medium'),
            'reasoning': result.get('reasoning', ''),
            'risks': result.get('risks', []),
            'validation_ran': True,
        }
    except Exception as e:
        # Fail safe -- Gemini unavailable doesn't block trading
        return {
            'approved': True, 'confidence': 'unknown',
            'reasoning': f'Gemini validation failed: {str(e)[:100]}',
            'risks': [], 'validation_ran': False,
        }


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
