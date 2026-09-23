"""
Khala Trading -- Backtest Diagnostic Tool
----------------------------------------------
Digs into a saved backtest JSON file to find WHY a symbol is
underperforming, instead of guessing. Same evidence-based approach that
found the Wednesday pattern for XAUUSD -- applied more thoroughly here:
breaks down win/loss by score, stop distance, hour of day, and streak
patterns.

Usage:
    python3 diagnose.py backtest_XAGUSD_2y.json
"""

import sys
import json
import time


def load(path):
    with open(path) as f:
        return json.load(f)


def analyze(data):
    symbol = data['symbol']
    trades = [t for t in data['trades'] if t['outcome'] in ('WIN', 'LOSS')]

    if not trades:
        print(f"No resolved trades found for {symbol}")
        return

    print(f"\n{'='*60}\n  DIAGNOSTIC -- {symbol} ({len(trades)} resolved trades)\n{'='*60}")

    # --- Breakdown by score ---
    print("\nWin rate by score:")
    by_score = {}
    for t in trades:
        by_score.setdefault(t['score'], {'wins': 0, 'losses': 0})
        by_score[t['score']]['wins' if t['outcome'] == 'WIN' else 'losses'] += 1
    for score in sorted(by_score.keys()):
        w, l = by_score[score]['wins'], by_score[score]['losses']
        total = w + l
        wr = round(w / total * 100, 1) if total else 0
        print(f"  Score {score}: {w}W/{l}L ({wr}% win rate, {total} trades)")

    # --- Breakdown by hour of day (UTC) -- session proxy ---
    print("\nWin rate by entry hour (UTC):")
    by_hour = {}
    for t in trades:
        if t.get('entry_time') is None:
            continue
        hour = time.gmtime(t['entry_time']).tm_hour
        by_hour.setdefault(hour, {'wins': 0, 'losses': 0})
        by_hour[hour]['wins' if t['outcome'] == 'WIN' else 'losses'] += 1
    for hour in sorted(by_hour.keys()):
        w, l = by_hour[hour]['wins'], by_hour[hour]['losses']
        total = w + l
        wr = round(w / total * 100, 1) if total else 0
        session = _session_label(hour)
        print(f"  {hour:02d}:00 UTC ({session}): {w}W/{l}L ({wr}%, {total} trades)")

    # --- Breakdown by direction ---
    print("\nWin rate by direction:")
    for direction in ('bullish', 'bearish'):
        dir_trades = [t for t in trades if t['direction'] == direction]
        if not dir_trades:
            continue
        w = len([t for t in dir_trades if t['outcome'] == 'WIN'])
        l = len(dir_trades) - w
        wr = round(w / len(dir_trades) * 100, 1)
        print(f"  {direction.upper()}: {w}W/{l}L ({wr}%, {len(dir_trades)} trades)")

    # --- Stop distance analysis: are losses systematically tighter/wider? ---
    win_distances = [abs(t['entry_price'] - t['sl_price']) for t in trades if t['outcome'] == 'WIN']
    loss_distances = [abs(t['entry_price'] - t['sl_price']) for t in trades if t['outcome'] == 'LOSS']
    if win_distances and loss_distances:
        avg_win_dist = sum(win_distances) / len(win_distances)
        avg_loss_dist = sum(loss_distances) / len(loss_distances)
        print(f"\nAverage stop distance -- Winners: {avg_win_dist:.4f} | Losers: {avg_loss_dist:.4f}")
        if avg_loss_dist < avg_win_dist * 0.7:
            print("  -> Losers have MEANINGFULLY TIGHTER stops than winners -- SL may be getting")
            print("     placed too close to entry for this symbol's typical volatility, causing")
            print("     premature stop-outs on trades that would have otherwise worked.")

    # --- Longest losing streak ---
    max_streak = 0
    current_streak = 0
    for t in sorted(trades, key=lambda x: x.get('entry_time') or 0):
        if t['outcome'] == 'LOSS':
            current_streak += 1
            max_streak = max(max_streak, current_streak)
        else:
            current_streak = 0
    print(f"\nLongest losing streak: {max_streak} consecutive losses")


def _session_label(hour):
    if 0 <= hour < 7:
        return 'Asian'
    if 7 <= hour < 12:
        return 'London'
    if 12 <= hour < 17:
        return 'London/NY overlap'
    if 17 <= hour < 21:
        return 'New York'
    return 'Late NY/Asian handoff'


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python3 diagnose.py backtest_SYMBOL_RANGE.json")
        sys.exit(1)
    analyze(load(sys.argv[1]))
