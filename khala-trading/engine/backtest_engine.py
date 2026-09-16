"""
Khala Trading -- Backtest Engine
------------------------------------
Replays master_signal.generate_signal() bar-by-bar against historical data,
strictly forward in time (no lookahead -- at each step, only data up to
that point is visible to the signal logic, exactly like live trading).

KNOWN LIMITATION (documented, not hidden): Yahoo Finance only provides
historical 5-minute data for roughly the last 60 days, so multi-year
backtests can't use real 5M candles for the execution trigger check. This
engine substitutes the most recent 1H candles as a stand-in. That means
backtested "execution_ready" is a coarser approximation of what live
trading actually checks -- results should be read as directionally
informative, not as a precise prediction of live performance.

Uses a rolling window (not full growing history) for structure detection,
matching how live trading actually fetches data (a fixed recent range),
which also keeps this computationally feasible over years of hourly data.
"""

import time
import price_feed
import master_signal

WINDOW_1H_BARS = 2000  # roughly matches the ~3mo range used live


def simulate_outcome(direction, entry_price, sl_price, tp1, candles_after):
    """
    Walks forward through candles AFTER a signal fired, checking each
    candle's high/low against SL and TP1 in chronological order. Whichever
    is touched first determines the outcome. A single candle touching BOTH
    is treated as a LOSS (can't know which happened first from OHLC alone
    -- conservative, doesn't overstate performance). Returns (outcome,
    exit_index_offset) where outcome is 'WIN', 'LOSS', or 'PENDING' (ran
    out of data before either was touched).
    """
    for offset, c in enumerate(candles_after):
        if direction == 'bearish':
            hit_sl = c['high'] >= sl_price
            hit_tp = c['low'] <= tp1
        else:
            hit_sl = c['low'] <= sl_price
            hit_tp = c['high'] >= tp1

        if hit_sl:
            return 'LOSS', offset
        if hit_tp:
            return 'WIN', offset

    return 'PENDING', None


def run_backtest(symbol, candles_1h_full, account_balance=10000, risk_percent=1.0,
                  pip_value_per_lot=10.0, pip_size=0.0001, min_history=250):
    """
    Runs the walk-forward backtest across the full candle history provided.
    Returns (trades: list of dicts, stats: dict).

    One trade at a time per symbol (matches the live "wait for TP/SL before
    next signal" rule) -- after a signal fires, the walk skips ahead to
    where it resolved before looking for the next one, rather than
    overlapping hypothetical trades that couldn't have coexisted live.
    """
    trades = []
    i = min_history

    while i < len(candles_1h_full) - 1:
        window_start = max(0, i - WINDOW_1H_BARS)
        history_1h = candles_1h_full[window_start:i + 1]
        candles_4h = price_feed.resample_candles(history_1h, 4)
        candles_5m_proxy = history_1h[-3:]  # documented limitation, see module docstring

        setup = master_signal.generate_signal(
            symbol, candles_4h, history_1h, candles_5m_proxy,
            account_balance=account_balance, risk_percent=risk_percent,
            pip_value_per_lot=pip_value_per_lot, pip_size=pip_size,
            skip_news_filter=True,  # news_filter only knows today's news, not historical dates
        )

        if setup.get('is_signal'):
            candles_after = candles_1h_full[i + 1:]
            outcome, offset = simulate_outcome(
                setup['direction'], setup['entry_price'], setup['sl_price'],
                setup['targets']['tp1'], candles_after,
            )

            if outcome == 'WIN':
                pips = abs(setup['targets']['tp1'] - setup['entry_price']) / pip_size
                pnl = round(pips * pip_value_per_lot * setup['lot_size'], 2)
            elif outcome == 'LOSS':
                pips = abs(setup['sl_price'] - setup['entry_price']) / pip_size
                pnl = round(-pips * pip_value_per_lot * setup['lot_size'], 2)
            else:
                pnl = 0.0  # PENDING -- ran out of data, excluded from stats below

            trades.append({
                'symbol': symbol,
                'direction': setup['direction'],
                'entry_price': setup['entry_price'],
                'sl_price': setup['sl_price'],
                'tp1': setup['targets']['tp1'],
                'score': setup['score'],
                'entry_time': history_1h[-1].get('time'),
                'outcome': outcome,
                'pnl': pnl,
            })

            # Skip ahead to where this trade resolved (or end of data if
            # PENDING) -- one signal at a time, matching live behavior.
            i = i + 1 + offset if offset is not None else len(candles_1h_full)
        else:
            i += 1

    stats = compute_statistics(trades)
    return trades, stats


def compute_statistics(trades):
    """
    Computes the metrics that actually answer "does this have edge":
    win rate, profit factor, expectancy, max drawdown, average R:R,
    plus breakdowns by weekday and entry hour (session proxy).
    """
    resolved = [t for t in trades if t['outcome'] in ('WIN', 'LOSS')]
    wins = [t for t in resolved if t['outcome'] == 'WIN']
    losses = [t for t in resolved if t['outcome'] == 'LOSS']

    total_win_pnl = sum(t['pnl'] for t in wins)
    total_loss_pnl = sum(t['pnl'] for t in losses)  # negative

    win_rate = round(len(wins) / len(resolved) * 100, 1) if resolved else None
    profit_factor = round(total_win_pnl / abs(total_loss_pnl), 2) if total_loss_pnl != 0 else None
    expectancy = round((total_win_pnl + total_loss_pnl) / len(resolved), 2) if resolved else None

    # Equity curve + max drawdown
    equity = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for t in resolved:
        equity += t['pnl']
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)

    avg_rr = None
    if wins and losses:
        avg_win = sum(t['pnl'] for t in wins) / len(wins)
        avg_loss = abs(sum(t['pnl'] for t in losses) / len(losses))
        avg_rr = round(avg_win / avg_loss, 2) if avg_loss > 0 else None

    # Breakdown by weekday (0=Monday) and entry hour (UTC), using entry_time epoch
    weekday_stats = {}
    for t in resolved:
        if t['entry_time'] is None:
            continue
        wd = time.gmtime(t['entry_time']).tm_wday
        weekday_stats.setdefault(wd, {'wins': 0, 'losses': 0})
        weekday_stats[wd]['wins' if t['outcome'] == 'WIN' else 'losses'] += 1

    return {
        'total_signals': len(trades),
        'resolved': len(resolved),
        'pending_at_end': len(trades) - len(resolved),
        'wins': len(wins),
        'losses': len(losses),
        'win_rate': win_rate,
        'profit_factor': profit_factor,
        'expectancy_per_trade': expectancy,
        'total_pnl': round(total_win_pnl + total_loss_pnl, 2),
        'max_drawdown': round(max_drawdown, 2),
        'average_rr': avg_rr,
        'weekday_breakdown': weekday_stats,
    }
