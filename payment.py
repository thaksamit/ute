"""
payment.py — Razorpay Integration for UTE
==========================================
Handles query top-ups and Pro subscriptions via Razorpay.

Flow:
  1. User hits daily limit → sees paywall
  2. Clicks "Buy queries" or "Upgrade to Pro"
  3. Server creates a Razorpay order  → returns order_id + key_id
  4. Browser opens Razorpay Checkout modal
  5. User pays → Razorpay calls our /api/payment/verify
  6. Server verifies signature → upgrades user / adds queries

Plans:
  topup_10   ₹99   →  10 extra queries today
  topup_50   ₹299  →  50 extra queries today
  pro_month  ₹1499 →  Pro tier (unlimited, 30 days)
  pro_year   ₹9999 →  Pro tier (unlimited, 365 days)
"""

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import datetime
from typing import Optional


# ── Plans ─────────────────────────────────────────────────────

PLANS = {
    'topup_10': {
        'name':        '10 Extra Queries',
        'description': '10 additional queries today',
        'amount':      9900,       # in paise (₹99)
        'currency':    'INR',
        'type':        'topup',
        'queries':     10,
        'days':        0,
    },
    'topup_50': {
        'name':        '50 Extra Queries',
        'description': '50 additional queries today',
        'amount':      29900,      # ₹299
        'currency':    'INR',
        'type':        'topup',
        'queries':     50,
        'days':        0,
    },
    'pro_month': {
        'name':        'Pro — Monthly',
        'description': 'Unlimited queries + full history for 30 days',
        'amount':      149900,     # ₹1,499
        'currency':    'INR',
        'type':        'subscription',
        'tier':        'pro',
        'days':        30,
        'queries':     0,
    },
    'pro_year': {
        'name':        'Pro — Annual',
        'description': 'Unlimited queries + full history for 365 days',
        'amount':      999900,     # ₹9,999  (~2 months free vs monthly)
        'currency':    'INR',
        'type':        'subscription',
        'tier':        'pro',
        'days':        365,
        'queries':     0,
    },
}


# ── DB setup ──────────────────────────────────────────────────

def _db_path() -> str:
    home = os.environ.get('HOME', os.path.expanduser('~'))
    path = os.path.join(home, 'ute_knowledge', 'users.db')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _conn(path: str = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or _db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_payments_table(conn)
    return conn


def _ensure_payments_table(conn: sqlite3.Connection):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS payments (
            id              TEXT PRIMARY KEY,
            user_id         TEXT NOT NULL,
            razorpay_order_id  TEXT NOT NULL,
            razorpay_payment_id TEXT DEFAULT '',
            plan_id         TEXT NOT NULL,
            amount          INTEGER NOT NULL,
            currency        TEXT DEFAULT 'INR',
            status          TEXT DEFAULT 'pending',  -- pending|paid|failed
            created_at      TEXT NOT NULL,
            verified_at     TEXT DEFAULT ''
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS query_topups (
            id          TEXT PRIMARY KEY,
            user_id     TEXT NOT NULL,
            date        TEXT NOT NULL,
            extra_count INTEGER DEFAULT 0
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_topups_user_date ON query_topups(user_id, date)")
    conn.commit()


# ── Razorpay client ───────────────────────────────────────────

def _rzp_client():
    """Return an authenticated Razorpay client."""
    import razorpay
    key_id     = os.environ.get('RAZORPAY_KEY_ID', '')
    key_secret = os.environ.get('RAZORPAY_KEY_SECRET', '')
    if not key_id or not key_secret:
        raise ValueError(
            "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET environment variables not set."
        )
    client = razorpay.Client(auth=(key_id, key_secret))
    client.set_app_details({'title': 'UTE', 'version': '4.0'})
    return client


# ── Create order ──────────────────────────────────────────────

def create_order(user_id: str, plan_id: str, db: str = None) -> dict:
    """
    Create a Razorpay order for the given plan.
    Returns { ok, order_id, amount, currency, key_id, plan } or { ok, error }
    """
    plan = PLANS.get(plan_id)
    if not plan:
        return {'ok': False, 'error': f'Unknown plan: {plan_id}'}

    try:
        client = _rzp_client()
        receipt = f"ute_{user_id[:8]}_{secrets.token_hex(4)}"

        order = client.order.create({
            'amount':   plan['amount'],
            'currency': plan['currency'],
            'receipt':  receipt,
            'notes': {
                'user_id': user_id,
                'plan_id': plan_id,
                'product': 'UTE Unified Theory Engine',
            },
        })

        # Store pending payment record
        pay_id = secrets.token_hex(8)
        now    = datetime.datetime.now().isoformat()
        conn   = _conn(db)
        conn.execute("""
            INSERT INTO payments
              (id, user_id, razorpay_order_id, plan_id, amount, currency, status, created_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, (pay_id, user_id, order['id'], plan_id,
              plan['amount'], plan['currency'], 'pending', now))
        conn.commit()
        conn.close()

        return {
            'ok':       True,
            'order_id': order['id'],
            'amount':   plan['amount'],
            'currency': plan['currency'],
            'key_id':   os.environ.get('RAZORPAY_KEY_ID', ''),
            'plan':     plan,
            'plan_id':  plan_id,
        }

    except ValueError as e:
        return {'ok': False, 'error': str(e)}
    except Exception as e:
        return {'ok': False, 'error': f'Could not create payment order: {e}'}


# ── Verify payment ────────────────────────────────────────────

def verify_payment(
    razorpay_order_id:   str,
    razorpay_payment_id: str,
    razorpay_signature:  str,
    db: str = None,
) -> dict:
    """
    Verify payment signature and activate the plan.
    Returns { ok, message, user_id, plan_id } or { ok, error }
    """
    # 1. Verify signature
    key_secret = os.environ.get('RAZORPAY_KEY_SECRET', '')
    if not key_secret:
        return {'ok': False, 'error': 'Payment verification not configured'}

    payload       = f"{razorpay_order_id}|{razorpay_payment_id}"
    expected_sig  = hmac.new(
        key_secret.encode('utf-8'),
        payload.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(expected_sig, razorpay_signature):
        return {'ok': False, 'error': 'Payment signature verification failed'}

    # 2. Find the pending payment record
    conn = _conn(db)
    row  = conn.execute("""
        SELECT * FROM payments
        WHERE razorpay_order_id = ? AND status = 'pending'
        LIMIT 1
    """, (razorpay_order_id,)).fetchone()

    if not row:
        conn.close()
        return {'ok': False, 'error': 'Payment record not found'}

    pay = dict(row)
    plan = PLANS.get(pay['plan_id'])
    if not plan:
        conn.close()
        return {'ok': False, 'error': 'Unknown plan in payment record'}

    # 3. Mark payment as paid
    now = datetime.datetime.now().isoformat()
    conn.execute("""
        UPDATE payments
        SET status='paid', razorpay_payment_id=?, verified_at=?
        WHERE id=?
    """, (razorpay_payment_id, now, pay['id']))

    # 4. Activate the plan
    user_id = pay['user_id']
    if plan['type'] == 'topup':
        _add_topup_queries(conn, user_id, plan['queries'])
        msg = f"Added {plan['queries']} queries to your account."
    else:
        _upgrade_to_pro(conn, user_id, plan['days'])
        msg = f"Upgraded to Pro for {plan['days']} days. Enjoy unlimited queries!"

    conn.commit()
    conn.close()

    return {
        'ok':      True,
        'message': msg,
        'user_id': user_id,
        'plan_id': pay['plan_id'],
        'type':    plan['type'],
    }


# ── Plan activation ───────────────────────────────────────────

def _add_topup_queries(conn: sqlite3.Connection, user_id: str, count: int):
    """Add extra queries to the user's daily count."""
    today = datetime.date.today().isoformat()
    uid   = secrets.token_hex(8)
    # Check if record exists
    row = conn.execute(
        "SELECT id FROM query_topups WHERE user_id=? AND date=?",
        (user_id, today)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE query_topups SET extra_count = extra_count + ? WHERE user_id=? AND date=?",
            (count, user_id, today)
        )
    else:
        conn.execute(
            "INSERT INTO query_topups (id, user_id, date, extra_count) VALUES (?,?,?,?)",
            (uid, user_id, today, count)
        )


def _upgrade_to_pro(conn: sqlite3.Connection, user_id: str, days: int):
    """Upgrade user tier to pro and set expiry."""
    expiry = (datetime.datetime.now() + datetime.timedelta(days=days)).isoformat()
    conn.execute("""
        UPDATE users SET tier='pro', pro_expires_at=?
        WHERE id=?
    """, (expiry, user_id))
    # Add pro_expires_at column if it doesn't exist
    try:
        conn.execute("ALTER TABLE users ADD COLUMN pro_expires_at TEXT DEFAULT ''")
    except Exception:
        pass  # Already exists


def get_extra_queries(user_id: str, db: str = None) -> int:
    """Return how many extra (paid) queries the user has today."""
    today = datetime.date.today().isoformat()
    conn  = _conn(db)
    row   = conn.execute("""
        SELECT extra_count FROM query_topups
        WHERE user_id=? AND date=?
    """, (user_id, today)).fetchone()
    conn.close()
    return row['extra_count'] if row else 0


def get_payment_history(user_id: str, db: str = None) -> list:
    """Return payment history for a user."""
    conn = _conn(db)
    rows = conn.execute("""
        SELECT id, plan_id, amount, currency, status, created_at, verified_at
        FROM payments WHERE user_id=?
        ORDER BY created_at DESC LIMIT 20
    """, (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
