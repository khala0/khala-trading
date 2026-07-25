"""
Khala Trading -- Backtest Runner
------------------------------------
Run this on your own machine (needs internet access to fetch historical
data from Yahoo Finance -- no MT5/Wine needed, this is separate from the
live executor).

Usage:
    python3 backtest.py XAUUSD
    python3 backtest.py XAUUSD --range 2y --balance 10000 --risk 1.0

Fetches historical hourly candles for the symbol, replays the actual
signal logic bar-by-bar with zero lookahead (see engine/backtest_engine.py
for how that's verified), and prints a full statistics report.

IMPORTANT LIMITATION: Yahoo Finance only provides real 5-minute candle
history for about the last 60 days. Beyond that, this substitutes recent
1-hour candles as a stand-in for the 5-minute execution trigger check --
meaning results for older history are a coarser approximation of live
behavior, not an exact replica. Treat this as directionally informative
(does this symbol/setup show a real edge at all?), not as a precise
prediction of live performance.
"""

import sys
import os
import argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'engine'))

import price_feed
import backtest_engine


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


def print_report(symbol, stats, trades):
    print()
    print('=' * 60)
    print(f'  BACKTEST REPORT -- {symbol}')
    print('=' * 60)
    print(f"  Total signals generated:   {stats['total_signals']}")
    print(f"  Resolved (WIN/LOSS):       {stats['resolved']}")
    print(f"  Still pending at end:      {stats['pending_at_end']}")
    print(f"  Wins / Losses:             {stats['wins']} / {stats['losses']}")
    print(f"  Win rate:                  {stats['win_rate']}%" if stats['win_rate'] is not None else "  Win rate:                  N/A (no resolved trades)")
    print(f"  Profit factor:             {stats['profit_factor']}" if stats['profit_factor'] is not None else "  Profit factor:             N/A")
    print(f"  Expectancy per trade:      ${stats['expectancy_per_trade']}" if stats['expectancy_per_trade'] is not None else "  Expectancy per trade:      N/A")
    print(f"  Average R:R realized:      {stats['average_rr']}" if stats['average_rr'] is not None else "  Average R:R realized:      N/A")
    print(f"  Total P&L:                 ${stats['total_pnl']}")
    print(f"  Max drawdown:              ${stats['max_drawdown']}")
    print()
    print('  Win/Loss by weekday (0=Mon .. 6=Sun, UTC):')
    weekday_names = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
    for wd in sorted(stats['weekday_breakdown'].keys()):
        w = stats['weekday_breakdown'][wd]
        print(f"    {weekday_names[wd]}: {w['wins']}W / {w['losses']}L")
    print('=' * 60)

    if stats['resolved'] < 30:
        print()
        print(f"  NOTE: only {stats['resolved']} resolved trades -- this is too few to draw")
        print(f"  reliable conclusions from. Treat these numbers as very rough until")
        print(f"  you have at least 50-100+ resolved trades, ideally across a longer")
        print(f"  date range or multiple symbols.")
    print()


def main():
    parser = argparse.ArgumentParser(description='Backtest the Khala Trading signal engine against historical data.')
    parser.add_argument('symbol', help='Symbol to backtest, e.g. XAUUSD')
    parser.add_argument('--range', default='2y', help='Historical range to fetch (e.g. 6mo, 1y, 2y). Yahoo max for 1h data is roughly 2y.')
    parser.add_argument('--balance', type=float, default=10000, help='Simulated account balance')
    parser.add_argument('--risk', type=float, default=1.0, help='Risk percent per trade')
    args = parser.parse_args()

    symbol = args.symbol.upper()
    if symbol not in PIP_CONFIG:
        print(f"Unknown symbol '{symbol}'. Supported: {list(PIP_CONFIG.keys())}")
        sys.exit(1)

    pip_cfg = PIP_CONFIG[symbol]

    print(f"Fetching {args.range} of historical 1H data for {symbol}...")
    candles_1h = price_feed.fetch_candles(symbol, interval='60m', range_=args.range)
    print(f"Got {len(candles_1h)} candles. Running walk-forward backtest (this may take a few minutes)...")

    trades, stats = backtest_engine.run_backtest(
        symbol, candles_1h,
        account_balance=args.balance, risk_percent=args.risk,
        pip_value_per_lot=pip_cfg['pip_value_per_lot'], pip_size=pip_cfg['pip_size'],
    )

    print_report(symbol, stats, trades)

    # Save full trade list to a file for further analysis
    import json
    out_path = f'backtest_{symbol}_{args.range}.json'
    with open(out_path, 'w') as f:
        json.dump({'symbol': symbol, 'stats': stats, 'trades': trades}, f, indent=2, default=str)
    print(f"Full trade-by-trade results saved to {out_path}")


if __name__ == '__main__':
    main()
