"""
Auto-execution decision core.
-------------------------------
This module contains ONLY the decision logic for whether a new setup should
be auto-executed -- no MT5 calls here. Keeping this separate means it can be
fully unit-tested without a live MT5 terminal, broker connection, or Windows
machine. mt5_executor.py imports this and does the actual order placement.

Safety rails enforced here:
  - Kill switch (a local file's presence disables all trading instantly)
  - Minimum confluence score threshold
  - Max open positions across the whole account
  - Max open positions per symbol (no doubling up on the same pair)
  - Max daily loss limit (stops trading for the day once hit)
  - Max daily trade count (caps overtrading)
  - Symbol whitelist (only trade what you've explicitly approved)
"""

import os
import time
import json

KILL_SWITCH_PATH = os.environ.get('KILL_SWITCH_PATH', 'KILL_SWITCH')


class ExecutionConfig:
    def __init__(
        self,
        min_score=8,
        max_open_positions=5,
        max_positions_per_symbol=1,
        max_daily_loss_usd=200.0,
        max_daily_trades=10,
        allowed_symbols=None,
    ):
        self.min_score = min_score
        self.max_open_positions = max_open_positions
        self.max_positions_per_symbol = max_positions_per_symbol
        self.max_daily_loss_usd = max_daily_loss_usd
        self.max_daily_trades = max_daily_trades
        self.allowed_symbols = allowed_symbols or [
            'XAUUSD', 'EURUSD', 'GBPUSD', 'USDJPY', 'BTCUSD'
        ]


class DailyState:
    """Tracks trades taken and P&L for the current day. Resets automatically."""
    def __init__(self, persist_path='daily_state.json'):
        self.persist_path = persist_path
        self._load()

    def _load(self):
        today = time.strftime('%Y-%m-%d')
        if os.path.exists(self.persist_path):
            with open(self.persist_path, 'r') as f:
                data = json.load(f)
            if data.get('date') == today:
                self.date = today
                self.trade_count = data.get('trade_count', 0)
                self.realized_pnl = data.get('realized_pnl', 0.0)
                return
        # New day (or no file yet) -- reset
        self.date = today
        self.trade_count = 0
        self.realized_pnl = 0.0
        self._save()

    def _save(self):
        with open(self.persist_path, 'w') as f:
            json.dump({
                'date': self.date,
                'trade_count': self.trade_count,
                'realized_pnl': self.realized_pnl,
            }, f)

    def record_trade(self):
        self._load()  # re-check date in case day rolled over
        self.trade_count += 1
        self._save()

    def record_closed_pnl(self, pnl):
        self._load()
        self.realized_pnl += pnl
        self._save()


def is_kill_switch_active():
    """Kill switch: if a file named KILL_SWITCH exists in the working dir,
    all auto-execution stops immediately. Create it with `touch KILL_SWITCH`,
    remove it to resume. Dead simple, can't be bypassed by a code bug."""
    return os.path.exists(KILL_SWITCH_PATH)


def should_execute(setup, config: ExecutionConfig, daily_state: DailyState, open_positions):
    """
    Decide whether to execute a new setup.

    setup:          dict from signal_engine.score_setup()
    config:         ExecutionConfig instance
    daily_state:    DailyState instance (tracks today's trades/pnl)
    open_positions: list of currently open position dicts, each with
                    at least a 'symbol' key

    Returns (should_execute: bool, reason: str)
    """
    if is_kill_switch_active():
        return False, 'Kill switch is active -- all auto-execution halted'

    if setup.get('direction') is None:
        return False, 'No valid setup (no unmitigated swing structure)'

    symbol = setup.get('symbol')
    if symbol not in config.allowed_symbols:
        return False, f'{symbol} is not in the allowed symbol whitelist'

    if setup.get('score', 0) < config.min_score:
        return False, f"Score {setup.get('score')} below minimum threshold {config.min_score}"

    if len(open_positions) >= config.max_open_positions:
        return False, f'Max open positions ({config.max_open_positions}) already reached'

    same_symbol_count = sum(1 for p in open_positions if p.get('symbol') == symbol)
    if same_symbol_count >= config.max_positions_per_symbol:
        return False, f'Already at max positions ({config.max_positions_per_symbol}) for {symbol}'

    daily_state._load()  # ensure fresh day-check
    if daily_state.realized_pnl <= -abs(config.max_daily_loss_usd):
        return False, f'Daily loss limit hit (${daily_state.realized_pnl:.2f}) -- trading halted for today'

    if daily_state.trade_count >= config.max_daily_trades:
        return False, f'Max daily trade count ({config.max_daily_trades}) reached'

    return True, 'All checks passed'
