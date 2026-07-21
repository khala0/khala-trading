"""
Khala Trading -- Database Layer
-----------------------------------
Fixes the "data disappears after every deploy" problem: previously, users,
ledger positions, and signal history were stored in JSON files under /tmp,
which Render (and most hosts) wipe on every restart/redeploy/sleep cycle.

This module connects to a REAL persistent database instead:
  - If DATABASE_URL is set (a Postgres connection string, e.g. from Neon's
    free permanent tier), uses that -- this is what production should use.
  - If DATABASE_URL is NOT set, falls back to a local SQLite file -- this
    keeps local development and testing working without needing a real
    Postgres instance.

Both paths use the exact same SQL (kept deliberately simple/portable) and
the same table schema, so the rest of the app never needs to know which
one is active.
"""

import os
import sqlite3

DATABASE_URL = os.environ.get('DATABASE_URL', '').strip()
USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras

SQLITE_PATH = os.environ.get('SQLITE_PATH', '/tmp/khala_trading.db')


def get_connection():
    """Returns a live DB connection. Caller is responsible for closing it."""
    if USE_POSTGRES:
        return psycopg2.connect(DATABASE_URL, sslmode='require')
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def placeholder():
    """Parameter placeholder style differs between the two drivers."""
    return '%s' if USE_POSTGRES else '?'


def row_to_dict(row):
    """Normalizes a result row to a plain dict regardless of driver."""
    if row is None:
        return None
    if USE_POSTGRES:
        return dict(row)
    return dict(row)  # sqlite3.Row also supports dict()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    email TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    created_at REAL,
    is_subscribed INTEGER DEFAULT 0,
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    subscription_status TEXT,
    active_session_token TEXT,
    last_login_at REAL,
    last_login_ip TEXT
);

CREATE TABLE IF NOT EXISTS ledger_positions (
    id TEXT PRIMARY KEY,
    symbol TEXT,
    direction TEXT,
    entry_price REAL,
    sl_price REAL,
    tp1 REAL,
    tp2 REAL,
    tp3 REAL,
    lot_size REAL,
    status TEXT,
    opened_at REAL,
    closed_at REAL,
    close_price REAL,
    realized_pnl REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS signal_history (
    id TEXT PRIMARY KEY,
    symbol TEXT,
    direction TEXT,
    entry_price REAL,
    sl_price REAL,
    tp1 REAL,
    tp2 REAL,
    tp3 REAL,
    score REAL,
    lot_size REAL,
    atr_value REAL,
    atr_multiplier_used REAL,
    risk_amount_usd REAL,
    stop_distance_pips REAL,
    trend_4h TEXT,
    trend_1h TEXT,
    htf_agreement INTEGER,
    narrative TEXT,
    generated_at REAL,
    anchor_candle_time REAL,
    status TEXT,
    resolved_at REAL,
    resolved_price REAL
);
"""


def init_schema():
    """Creates all tables if they don't already exist. Safe to call every
    app startup -- CREATE TABLE IF NOT EXISTS is a no-op once tables exist."""
    conn = get_connection()
    try:
        cur = conn.cursor()
        for statement in SCHEMA.strip().split(';'):
            statement = statement.strip()
            if statement:
                cur.execute(statement)
        conn.commit()
    finally:
        conn.close()
