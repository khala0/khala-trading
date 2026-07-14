"""
Simple JSON-file backed user store: signup, login verification, and
subscription status tracking. Same lightweight pattern as ledger.py --
swap for a real database if you outgrow this.
"""

import json
import os
import threading
import time
import secrets
from werkzeug.security import generate_password_hash, check_password_hash

USERS_PATH = os.environ.get('USERS_PATH', '/tmp/khala_users.json')
_lock = threading.Lock()


def _load():
    if not os.path.exists(USERS_PATH):
        return {'users': {}}
    with open(USERS_PATH, 'r') as f:
        return json.load(f)


def _save(data):
    with open(USERS_PATH, 'w') as f:
        json.dump(data, f, indent=2)


def create_user(email, password):
    email = email.strip().lower()
    with _lock:
        data = _load()
        if email in data['users']:
            return None, 'An account with this email already exists'
        data['users'][email] = {
            'email': email,
            'password_hash': generate_password_hash(password),
            'created_at': time.time(),
            'is_subscribed': False,
            'stripe_customer_id': None,
            'stripe_subscription_id': None,
            'subscription_status': None,  # active, past_due, canceled, etc.
            'active_session_token': None,
            'last_login_at': None,
            'last_login_ip': None,
        }
        _save(data)
        return data['users'][email], None


def get_user(email):
    email = email.strip().lower()
    data = _load()
    return data['users'].get(email)


def verify_password(email, password):
    user = get_user(email)
    if user is None:
        return False
    return check_password_hash(user['password_hash'], password)


def set_stripe_customer(email, customer_id):
    email = email.strip().lower()
    with _lock:
        data = _load()
        if email in data['users']:
            data['users'][email]['stripe_customer_id'] = customer_id
            _save(data)


def update_subscription_status(email=None, customer_id=None, subscription_id=None, status=None):
    """
    Update a user's subscription status, looked up by email or stripe_customer_id
    (webhooks identify users by customer_id, not email).
    """
    with _lock:
        data = _load()
        target = None
        if email:
            target = data['users'].get(email.strip().lower())
        elif customer_id:
            target = next((u for u in data['users'].values() if u.get('stripe_customer_id') == customer_id), None)

        if target is None:
            return False

        target['subscription_status'] = status
        target['is_subscribed'] = status == 'active'
        if subscription_id:
            target['stripe_subscription_id'] = subscription_id
        _save(data)
        return True


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
    with _lock:
        data = _load()
        if email not in data['users']:
            return None
        data['users'][email]['active_session_token'] = token
        data['users'][email]['last_login_at'] = time.time()
        data['users'][email]['last_login_ip'] = ip
        _save(data)
    return token


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
    with _lock:
        data = _load()
        if email not in data['users']:
            return False, 'No account with this email exists'
        data['users'][email]['is_subscribed'] = granted
        data['users'][email]['subscription_status'] = 'comp' if granted else 'comp_revoked'
        _save(data)
        return True, None


def list_users():
    """Returns all users with password hashes and session tokens stripped out, for admin display."""
    data = _load()
    return [
        {
            'email': u['email'],
            'is_subscribed': u.get('is_subscribed', False),
            'subscription_status': u.get('subscription_status'),
            'created_at': u.get('created_at'),
            'last_login_at': u.get('last_login_at'),
            'last_login_ip': u.get('last_login_ip'),
        }
        for u in data['users'].values()
    ]
