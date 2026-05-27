"""
auth.py — UTE Authentication, Sessions & Pricing
==================================================
Users, sessions, query limits, and pricing tiers.
All stored in SQLite alongside the microsite DB.

Tiers:
  free        10 queries/day, no persistence beyond session
  pro         Unlimited queries, full chat history, priority
  enterprise  Unlimited + custom branding + API access
"""

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import datetime
from typing import Optional


# ── DB ────────────────────────────────────────────────────────

def _db_path() -> str:
    home = os.environ.get('HOME', os.path.expanduser('~'))
    path = os.path.join(home, 'ute_knowledge', 'users.db')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


def _conn(path: str = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or _db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        id          TEXT PRIMARY KEY,
        email       TEXT UNIQUE NOT NULL,
        name        TEXT NOT NULL,
        password    TEXT NOT NULL,     -- bcrypt-style: sha256(salt+pw)
        salt        TEXT NOT NULL,
        tier        TEXT DEFAULT 'free',   -- free | pro | enterprise
        status      TEXT DEFAULT 'active', -- active | suspended
        created_at  TEXT NOT NULL,
        last_login  TEXT DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS sessions (
        token       TEXT PRIMARY KEY,
        user_id     TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        expires_at  TEXT NOT NULL,
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS usage (
        id          TEXT PRIMARY KEY,
        user_id     TEXT NOT NULL,
        date        TEXT NOT NULL,      -- YYYY-MM-DD
        query_count INTEGER DEFAULT 0,
        UNIQUE(user_id, date)
    );

    CREATE TABLE IF NOT EXISTS chats (
        id          TEXT PRIMARY KEY,
        user_id     TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        title       TEXT DEFAULT '',
        FOREIGN KEY(user_id) REFERENCES users(id)
    );

    CREATE TABLE IF NOT EXISTS messages (
        id          TEXT PRIMARY KEY,
        chat_id     TEXT NOT NULL,
        user_id     TEXT NOT NULL,
        role        TEXT NOT NULL,      -- user | assistant
        content     TEXT NOT NULL,
        query       TEXT DEFAULT '',
        answer      TEXT DEFAULT '',
        signal      TEXT DEFAULT '',
        noise       TEXT DEFAULT '',
        reality     TEXT DEFAULT '',
        delusion    TEXT DEFAULT '',
        connect     TEXT DEFAULT '',
        disconnect  TEXT DEFAULT '',
        meaning     TEXT DEFAULT '',
        awareness   TEXT DEFAULT '',
        output_file TEXT DEFAULT '',
        created_at  TEXT NOT NULL,
        FOREIGN KEY(chat_id) REFERENCES chats(id)
    );

    CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
    CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages(chat_id);
    CREATE INDEX IF NOT EXISTS idx_chats_user    ON chats(user_id);
    CREATE INDEX IF NOT EXISTS idx_usage_user    ON usage(user_id, date);
    """)
    conn.commit()


# ── PRICING TIERS ─────────────────────────────────────────────

TIERS = {
    'free': {
        'name':         'Free',
        'price_month':  0,
        'price_year':   0,
        'query_limit':  10,           # per day
        'history':      False,
        'attachments':  True,
        'downloads':    True,
        'refine':       True,
        'description':  '10 queries per day',
    },
    'pro': {
        'name':         'Pro',
        'price_month':  19,
        'price_year':   190,          # ~2 months free
        'query_limit':  0,            # unlimited
        'history':      True,
        'attachments':  True,
        'downloads':    True,
        'refine':       True,
        'description':  'Unlimited queries + full chat history',
    },
    'enterprise': {
        'name':         'Enterprise',
        'price_month':  0,            # custom
        'price_year':   0,
        'query_limit':  0,
        'history':      True,
        'attachments':  True,
        'downloads':    True,
        'refine':       True,
        'description':  'Custom pricing — contact us',
    },
}


# ── PASSWORD ──────────────────────────────────────────────────

def _hash_password(password: str, salt: str) -> str:
    return hmac.new(
        salt.encode(), password.encode(), hashlib.sha256
    ).hexdigest()


def _verify_password(password: str, salt: str, stored_hash: str) -> bool:
    return hmac.compare_digest(
        _hash_password(password, salt), stored_hash
    )


# ── USER MANAGEMENT ───────────────────────────────────────────

def signup(email: str, name: str, password: str,
           tier: str = 'free', db: str = None) -> dict:
    """
    Create a new user account.
    Returns {'ok': True, 'user_id': ..., 'token': ...}
    or {'ok': False, 'error': ...}
    """
    email = email.strip().lower()
    name  = name.strip()

    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email):
        return {'ok': False, 'error': 'Invalid email address'}
    if len(password) < 8:
        return {'ok': False, 'error': 'Password must be at least 8 characters'}
    if not name:
        return {'ok': False, 'error': 'Name is required'}
    if tier not in TIERS:
        tier = 'free'

    conn = _conn(db)
    existing = conn.execute(
        "SELECT id FROM users WHERE email = ?", (email,)
    ).fetchone()
    if existing:
        conn.close()
        return {'ok': False, 'error': 'Email already registered'}

    uid    = secrets.token_hex(8)
    salt   = secrets.token_hex(16)
    pw     = _hash_password(password, salt)
    now    = datetime.datetime.now().isoformat()

    conn.execute(
        "INSERT INTO users (id,email,name,password,salt,tier,status,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (uid, email, name, pw, salt, tier, 'active', now)
    )
    conn.commit()
    conn.close()

    token = _create_session(uid, db)
    return {'ok': True, 'user_id': uid, 'token': token,
            'name': name, 'tier': tier}


def login(email: str, password: str, db: str = None) -> dict:
    """
    Authenticate a user.
    Returns {'ok': True, 'token': ..., 'user': {...}}
    or {'ok': False, 'error': ...}
    """
    email = email.strip().lower()
    conn  = _conn(db)
    row   = conn.execute(
        "SELECT * FROM users WHERE email = ? AND status = 'active'", (email,)
    ).fetchone()

    if not row or not _verify_password(password, row['salt'], row['password']):
        conn.close()
        return {'ok': False, 'error': 'Invalid email or password'}

    now = datetime.datetime.now().isoformat()
    conn.execute("UPDATE users SET last_login=? WHERE id=?", (now, row['id']))
    conn.commit()
    conn.close()

    token = _create_session(row['id'], db)
    return {
        'ok':    True,
        'token': token,
        'user':  {
            'id':    row['id'],
            'email': row['email'],
            'name':  row['name'],
            'tier':  row['tier'],
        }
    }


def logout(token: str, db: str = None):
    conn = _conn(db)
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()
    conn.close()


def get_user(token: str, db: str = None) -> Optional[dict]:
    """
    Validate a session token and return the user dict, or None.
    """
    if not token:
        return None
    conn = _conn(db)
    now  = datetime.datetime.now().isoformat()
    row  = conn.execute("""
        SELECT u.id, u.email, u.name, u.tier
        FROM sessions s
        JOIN users u ON u.id = s.user_id
        WHERE s.token = ? AND s.expires_at > ? AND u.status = 'active'
    """, (token, now)).fetchone()
    conn.close()
    return dict(row) if row else None


def _create_session(user_id: str, db: str = None) -> str:
    token  = secrets.token_hex(32)
    now    = datetime.datetime.now()
    exp    = (now + datetime.timedelta(days=30)).isoformat()
    conn   = _conn(db)
    conn.execute(
        "INSERT INTO sessions (token,user_id,created_at,expires_at) VALUES (?,?,?,?)",
        (token, user_id, now.isoformat(), exp)
    )
    conn.commit()
    conn.close()
    return token


# ── QUERY LIMITS ──────────────────────────────────────────────

def check_limit(user: dict, db: str = None) -> dict:
    """
    Check if the user can make a query.
    Returns {'allowed': bool, 'used': int, 'limit': int, 'tier': str, 'extra': int}
    """
    tier_info = TIERS.get(user.get('tier', 'free'), TIERS['free'])
    limit     = tier_info['query_limit']

    if limit == 0:  # unlimited (pro/enterprise)
        return {'allowed': True, 'used': 0, 'limit': 0,
                'tier': user['tier'], 'extra': 0}

    today = datetime.date.today().isoformat()
    conn  = _conn(db)
    row   = conn.execute(
        "SELECT query_count FROM usage WHERE user_id=? AND date=?",
        (user['id'], today)
    ).fetchone()
    conn.close()

    used = row['query_count'] if row else 0

    # Add any paid topup queries
    try:
        from payment import get_extra_queries
        extra = get_extra_queries(user['id'], db)
    except Exception:
        extra = 0

    effective_limit = limit + extra
    return {
        'allowed': used < effective_limit,
        'used':    used,
        'limit':   effective_limit,
        'base_limit': limit,
        'extra':   extra,
        'tier':    user['tier'],
    }


def increment_usage(user_id: str, db: str = None):
    """Increment today's query count for a user."""
    today = datetime.date.today().isoformat()
    uid   = secrets.token_hex(8)
    conn  = _conn(db)
    conn.execute("""
        INSERT INTO usage (id, user_id, date, query_count)
        VALUES (?, ?, ?, 1)
        ON CONFLICT(user_id, date)
        DO UPDATE SET query_count = query_count + 1
    """, (uid, user_id, today))
    conn.commit()
    conn.close()


# ── CHAT PERSISTENCE ──────────────────────────────────────────

def create_chat(user_id: str, title: str = '', db: str = None) -> str:
    """Create a new chat session. Returns chat_id."""
    chat_id = secrets.token_hex(8)
    now     = datetime.datetime.now().isoformat()
    conn    = _conn(db)
    conn.execute(
        "INSERT INTO chats (id,user_id,created_at,title) VALUES (?,?,?,?)",
        (chat_id, user_id, now, title or f"Chat {now[:10]}")
    )
    conn.commit()
    conn.close()
    return chat_id


def save_message(chat_id: str, user_id: str, role: str,
                 content: str, ute_data: dict = None,
                 db: str = None) -> str:
    """Save a message to a chat. Returns message_id."""
    msg_id = secrets.token_hex(8)
    now    = datetime.datetime.now().isoformat()
    d      = ute_data or {}
    of     = d.get('output_file')
    of_str = f"{of.get('id','')},{of.get('name','')}" if of else ''

    conn = _conn(db)
    conn.execute("""
        INSERT INTO messages
          (id,chat_id,user_id,role,content,query,answer,signal,noise,
           reality,delusion,connect,disconnect,meaning,awareness,
           output_file,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (
        msg_id, chat_id, user_id, role, content,
        d.get('signal', content) if role == 'user' else '',
        d.get('answer', content) if role == 'assistant' else '',
        d.get('signal', ''), d.get('noise', ''),
        d.get('reality', ''), d.get('delusion', ''),
        d.get('connect', ''), d.get('disconnect', ''),
        d.get('meaning', ''), d.get('awareness', ''),
        of_str, now,
    ))
    # Update chat title from first user message
    if role == 'user':
        title = content[:60].strip()
        conn.execute(
            "UPDATE chats SET title=? WHERE id=? AND title LIKE 'Chat %'",
            (title, chat_id)
        )
    conn.commit()
    conn.close()
    return msg_id


def get_chats(user_id: str, db: str = None) -> list:
    """Return all chats for a user, newest first."""
    conn = _conn(db)
    rows = conn.execute("""
        SELECT c.id, c.title, c.created_at,
               COUNT(m.id) as message_count
        FROM chats c
        LEFT JOIN messages m ON m.chat_id = c.id
        WHERE c.user_id = ?
        GROUP BY c.id
        ORDER BY c.created_at DESC
        LIMIT 50
    """, (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_messages(chat_id: str, user_id: str, db: str = None) -> list:
    """Return all messages in a chat."""
    conn = _conn(db)
    rows = conn.execute("""
        SELECT * FROM messages
        WHERE chat_id = ? AND user_id = ?
        ORDER BY created_at ASC
    """, (chat_id, user_id)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_chat(chat_id: str, user_id: str, db: str = None):
    conn = _conn(db)
    conn.execute(
        "DELETE FROM messages WHERE chat_id=? AND user_id=?",
        (chat_id, user_id)
    )
    conn.execute(
        "DELETE FROM chats WHERE id=? AND user_id=?",
        (chat_id, user_id)
    )
    conn.commit()
    conn.close()
