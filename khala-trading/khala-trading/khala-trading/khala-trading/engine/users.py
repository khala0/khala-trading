"""
Database-backed user store: signup, login verification, and subscription
status tracking. Same function signatures as before -- only the storage
underneath changed, from JSON files (wiped on every deploy/restart) to a
real persistent database (see db.py).
"""

import time
import secrets
from werkzeug.security import generate_password_hash, check_password_hash
import db

P = db.placeholder()


def create_user(email, password):
    email = email.strip().lower()
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT email FROM users WHERE email = {P}", (email,))
        if cur.fetchone():
            return None, 'An account with this email already exists'

        user = {
            'email': email,
            'password_hash': generate_password_hash(password),
            'created_at': time.time(),
            'is_subscribed': 0,
            'stripe_customer_id': None,
            'stripe_subscription_id': None,
            'subscription_status': None,
            'active_session_token': None,
            'last_login_at': None,
            'last_login_ip': None,
        }
        cur.execute(
            f"INSERT INTO users (email, password_hash, created_at, is_subscribed, "
            f"stripe_customer_id, stripe_subscription_id, subscription_status, "
            f"active_session_token, last_login_at, last_login_ip) "
            f"VALUES ({P},{P},{P},{P},{P},{P},{P},{P},{P},{P})",
            (user['email'], user['password_hash'], user['created_at'], user['is_subscribed'],
             user['stripe_customer_id'], user['stripe_subscription_id'], user['subscription_status'],
             user['active_session_token'], user['last_login_at'], user['last_login_ip']),
        )
        conn.commit()
        user['is_subscribed'] = False
        return user, None
    finally:
        conn.close()


def get_user(email):
    email = email.strip().lower()
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM users WHERE email = {P}", (email,))
        row = cur.fetchone()
        if row is None:
            return None
        user = db.row_to_dict(row)
        user['is_subscribed'] = bool(user['is_subscribed'])
        return user
    finally:
        conn.close()


def verify_password(email, password):
    user = get_user(email)
    if user is None:
        return False
    return check_password_hash(user['password_hash'], password)


def set_stripe_customer(email, customer_id):
    email = email.strip().lower()
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"UPDATE users SET stripe_customer_id = {P} WHERE email = {P}", (customer_id, email))
        conn.commit()
    finally:
        conn.close()


def update_subscription_status(email=None, customer_id=None, subscription_id=None, status=None):
    """
    Update a user's subscription status, looked up by email or stripe_customer_id
    (webhooks identify users by customer_id, not email).
    """
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        is_subscribed_val = 1 if status == 'active' else 0

        if email:
            target_email = email.strip().lower()
        elif customer_id:
            cur.execute(f"SELECT email FROM users WHERE stripe_customer_id = {P}", (customer_id,))
            row = cur.fetchone()
            if row is None:
                return False
            target_email = db.row_to_dict(row)['email']
        else:
            return False

        if subscription_id:
            cur.execute(
                f"UPDATE users SET subscription_status = {P}, is_subscribed = {P}, "
                f"stripe_subscription_id = {P} WHERE email = {P}",
                (status, is_subscribed_val, subscription_id, target_email),
            )
        else:
            cur.execute(
                f"UPDATE users SET subscription_status = {P}, is_subscribed = {P} WHERE email = {P}",
                (status, is_subscribed_val, target_email),
            )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def is_subscribed(email):
    user = get_user(email)
    return bool(user and user.get('is_subscribed'))


def start_new_session(email, ip=None):
    """
    Call this on every successful login/signup. Generates a fresh session
    token and stores it as the ONLY valid one for this account -- any
    browser holding an older token will fail is_session_valid() on its
    next request, effectively logging it out. This is what prevents two
    people from being logged into the same account at once.
    """
    email = email.strip().lower()
    token = secrets.token_hex(32)
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT email FROM users WHERE email = {P}", (email,))
        if cur.fetchone() is None:
            return None
        cur.execute(
            f"UPDATE users SET active_session_token = {P}, last_login_at = {P}, last_login_ip = {P} "
            f"WHERE email = {P}",
            (token, time.time(), ip, email),
        )
        conn.commit()
        return token
    finally:
        conn.close()


def is_session_valid(email, token):
    """True only if `token` matches the single currently-active session
    token for this account (i.e. this is the most recent login)."""
    if not email or not token:
        return False
    user = get_user(email)
    if user is None:
        return False
    return user.get('active_session_token') == token


def set_comp_access(email, granted=True):
    """
    Admin-granted free access, independent of Stripe. Sets is_subscribed
    directly rather than deriving it from a Stripe subscription status,
    so a later Stripe webhook update won't silently overwrite a comp grant
    unless the admin explicitly revokes it.
    """
    email = email.strip().lower()
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT email FROM users WHERE email = {P}", (email,))
        if cur.fetchone() is None:
            return False, 'No account with this email exists'
        status = 'comp' if granted else 'comp_revoked'
        cur.execute(
            f"UPDATE users SET is_subscribed = {P}, subscription_status = {P} WHERE email = {P}",
            (1 if granted else 0, status, email),
        )
        conn.commit()
        return True, None
    finally:
        conn.close()


def list_users():
    """Returns all users with password hashes and session tokens stripped out, for admin display."""
    conn = db.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT email, is_subscribed, subscription_status, created_at, last_login_at, last_login_ip FROM users")
        rows = [db.row_to_dict(r) for r in cur.fetchall()]
        for r in rows:
            r['is_subscribed'] = bool(r['is_subscribed'])
        return rows
    finally:
        conn.close()
