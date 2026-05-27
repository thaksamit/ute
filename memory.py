"""
memory.py — UTE Persistent Memory Engine
==========================================
UTE remembers who you are across sessions, connects dots across
conversations, and builds a model of your context over time.

Three layers:
  1. USER PROFILE  — who you are (role, company, domain, goals, preferences)
  2. ENTITY MEMORY — facts UTE has learned about things you care about
  3. CROSS-CHAT RELATIONS — patterns across your conversation history

This is what makes UTE a friend, not a tool.
It holds your context so you don't have to repeat yourself.
It notices patterns you haven't noticed yourself.
It remembers what it told you before and holds itself accountable.
"""

import json
import os
import re
import sqlite3
import secrets
import datetime
from typing import Optional


# ── DB ────────────────────────────────────────────────────────

def _db_path() -> str:
    home = os.environ.get('HOME', os.path.expanduser('~'))
    return os.path.join(home, 'ute_knowledge', 'users.db')


def _conn(path: str = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or _db_path())
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection):
    conn.executescript("""
    -- Who the user is
    CREATE TABLE IF NOT EXISTS user_profiles (
        user_id      TEXT PRIMARY KEY,
        name         TEXT DEFAULT '',
        role         TEXT DEFAULT '',     -- CEO, founder, analyst, student...
        company      TEXT DEFAULT '',
        industry     TEXT DEFAULT '',
        goals        TEXT DEFAULT '',     -- what they're working toward
        preferences  TEXT DEFAULT '{}',  -- JSON: tone, detail level, domains
        updated_at   TEXT NOT NULL
    );

    -- Things UTE has learned about entities the user cares about
    CREATE TABLE IF NOT EXISTS entity_memory (
        id           TEXT PRIMARY KEY,
        user_id      TEXT NOT NULL,
        entity       TEXT NOT NULL,      -- name of person/company/concept/place
        entity_type  TEXT DEFAULT '',    -- company|person|concept|product|market
        known_facts  TEXT DEFAULT '[]',  -- JSON array of fact strings
        last_context TEXT DEFAULT '',    -- last query context this appeared in
        first_seen   TEXT NOT NULL,
        last_seen    TEXT NOT NULL,
        mention_count INTEGER DEFAULT 1
    );
    CREATE INDEX IF NOT EXISTS idx_em_user_entity ON entity_memory(user_id, entity);

    -- Patterns and insights across chat history
    CREATE TABLE IF NOT EXISTS chat_relations (
        id           TEXT PRIMARY KEY,
        user_id      TEXT NOT NULL,
        relation_type TEXT NOT NULL,     -- topic_cluster|decision_thread|contradiction|progression
        summary      TEXT NOT NULL,      -- what the relation says
        chat_ids     TEXT DEFAULT '[]',  -- JSON array of related chat IDs
        topics       TEXT DEFAULT '[]',  -- JSON array of topics
        created_at   TEXT NOT NULL,
        last_updated TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_cr_user ON chat_relations(user_id);

    -- Individual memory facts (short-term → long-term)
    CREATE TABLE IF NOT EXISTS memory_facts (
        id           TEXT PRIMARY KEY,
        user_id      TEXT NOT NULL,
        fact         TEXT NOT NULL,
        fact_type    TEXT DEFAULT 'general',  -- context|preference|decision|goal|concern
        source_query TEXT DEFAULT '',
        confidence   REAL DEFAULT 0.8,
        created_at   TEXT NOT NULL,
        expires_at   TEXT DEFAULT ''          -- empty = permanent
    );
    CREATE INDEX IF NOT EXISTS idx_mf_user ON memory_facts(user_id, fact_type);
    """)
    conn.commit()


# ══════════════════════════════════════════════════════════════════
# USER PROFILE
# ══════════════════════════════════════════════════════════════════

def get_profile(user_id: str, db: str = None) -> dict:
    """Get or create a user profile."""
    conn = _conn(db)
    row  = conn.execute(
        "SELECT * FROM user_profiles WHERE user_id=?", (user_id,)
    ).fetchone()
    conn.close()
    if row:
        d = dict(row)
        try: d['preferences'] = json.loads(d.get('preferences','{}'))
        except: d['preferences'] = {}
        return d
    return {
        'user_id': user_id, 'name': '', 'role': '',
        'company': '', 'industry': '', 'goals': '',
        'preferences': {},
    }


def update_profile(user_id: str, updates: dict, db: str = None):
    """Update user profile fields."""
    conn  = _conn(db)
    now   = datetime.datetime.now().isoformat()
    prefs = updates.get('preferences', {})
    if isinstance(prefs, dict):
        prefs = json.dumps(prefs)
    existing = conn.execute(
        "SELECT user_id FROM user_profiles WHERE user_id=?", (user_id,)
    ).fetchone()
    if existing:
        fields = ['name','role','company','industry','goals','preferences','updated_at']
        vals   = [updates.get('name',''), updates.get('role',''),
                  updates.get('company',''), updates.get('industry',''),
                  updates.get('goals',''), prefs, now]
        set_clause = ', '.join(f"{f}=?" for f in fields)
        conn.execute(f"UPDATE user_profiles SET {set_clause} WHERE user_id=?",
                     vals + [user_id])
    else:
        conn.execute("""
            INSERT INTO user_profiles
              (user_id,name,role,company,industry,goals,preferences,updated_at)
            VALUES (?,?,?,?,?,?,?,?)
        """, (user_id, updates.get('name',''), updates.get('role',''),
              updates.get('company',''), updates.get('industry',''),
              updates.get('goals',''), prefs, now))
    conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════════
# MEMORY EXTRACTION — learn from conversations automatically
# ══════════════════════════════════════════════════════════════════

def extract_and_store(
    user_id: str,
    query:   str,
    answer:  str,
    signal:  str = '',
    connect: str = '',
    db:      str = None,
):
    """
    After every UTE response, extract useful memories automatically.
    Called by the engine — user doesn't need to do anything.
    """
    _extract_profile_facts(user_id, query, db)
    _extract_entity_facts(user_id, query, answer, signal, db)
    _extract_memory_facts(user_id, query, connect, db)


def _extract_profile_facts(user_id: str, query: str, db: str = None):
    """Extract who the user is from their queries."""
    patterns = [
        (r"(?:i'?m|i am|i work as|my role is|i'?m the)\s+(?:a\s+)?([a-z][a-z\s]{2,30}?)(?:\s+at|\s+for|\s+in|[,.]|$)",
         'role'),
        (r"(?:my company|our company|i work at|i work for|at\s+)(?:is\s+called\s+)?([A-Z][a-zA-Z0-9\s&]{1,40}?)(?:\s+and|\s+we|\s+is|[,.]|$)",
         'company'),
        (r"(?:we(?:'re| are) in|our industry is|i(?:'m| am) in the?)\s+([a-z][a-z\s]{2,30}?)(?:\s+industry|\s+sector|[,.]|$)",
         'industry'),
        (r"(?:i(?:'m| am) trying to|my goal is to|i want to|we(?:'re| are) trying to)\s+(.{10,80}?)(?:[,.]|$)",
         'goals'),
    ]
    ql = query.lower()
    updates = {}
    for pattern, field in patterns:
        m = re.search(pattern, ql, re.I)
        if m:
            val = m.group(1).strip().rstrip('.,')
            if len(val) > 2:
                updates[field] = val[:80]
    if updates:
        profile = get_profile(user_id, db)
        for k, v in updates.items():
            if not profile.get(k):  # don't overwrite existing
                profile[k] = v
        update_profile(user_id, profile, db)


def _extract_entity_facts(user_id: str, query: str,
                           answer: str, signal: str, db: str = None):
    """Extract entities and facts about them from the conversation."""
    # Find proper nouns / known terms mentioned
    entities = set(re.findall(r'\b[A-Z][a-zA-Z]{2,}(?:\s+[A-Z][a-zA-Z]{2,})*\b', query))
    # Also known domain terms
    domain_terms = re.findall(
        r'\b(?:gdpr|hipaa|openai|anthropic|google|microsoft|aws|'
        r'bitcoin|ethereum|salesforce|hubspot|stripe|razorpay)\b',
        query.lower()
    )
    entities.update(t.upper() if len(t) <= 4 else t.title() for t in domain_terms)

    # Filter out common words
    stopwords = {'The','This','That','What','How','Why','When','Where',
                 'Will','Can','Should','Would','Could','Have','Been',
                 'Does','Did','Are','Was','Were','Has','Had','For','With'}
    entities = {e for e in entities if e not in stopwords and len(e) > 2}

    if not entities:
        return

    conn = _conn(db)
    now  = datetime.datetime.now().isoformat()

    for entity in list(entities)[:5]:  # max 5 entities per query
        # Extract a brief fact about this entity from the answer
        entity_lower = entity.lower()
        fact = ''
        for sent in re.split(r'[.!?]', answer):
            if entity_lower in sent.lower() and 20 < len(sent.strip()) < 200:
                fact = sent.strip()[:200]
                break

        row = conn.execute(
            "SELECT id, known_facts, mention_count FROM entity_memory "
            "WHERE user_id=? AND LOWER(entity)=?",
            (user_id, entity_lower)
        ).fetchone()

        if row:
            try:
                facts = json.loads(row['known_facts'])
            except Exception:
                facts = []
            if fact and fact not in facts:
                facts.append(fact)
                facts = facts[-10:]  # keep last 10 facts
            conn.execute("""
                UPDATE entity_memory
                SET known_facts=?, last_context=?, last_seen=?,
                    mention_count=mention_count+1
                WHERE id=?
            """, (json.dumps(facts), query[:100], now, row['id']))
        else:
            eid = secrets.token_hex(6)
            conn.execute("""
                INSERT INTO entity_memory
                  (id,user_id,entity,entity_type,known_facts,
                   last_context,first_seen,last_seen,mention_count)
                VALUES (?,?,?,?,?,?,?,?,1)
            """, (eid, user_id, entity, '', json.dumps([fact] if fact else []),
                  query[:100], now, now))

    conn.commit()
    conn.close()


def _extract_memory_facts(user_id: str, query: str,
                           connect: str, db: str = None):
    """Store important facts from the connect layer."""
    if not connect or len(connect) < 20:
        return

    # Detect fact types
    fact_type = 'general'
    ql = query.lower()
    if any(w in ql for w in ['decide','decision','choose','choosing','should i']):
        fact_type = 'decision'
    elif any(w in ql for w in ['goal','want to','trying to','plan to','intend']):
        fact_type = 'goal'
    elif any(w in ql for w in ['worried','concern','risk','problem','issue']):
        fact_type = 'concern'
    elif any(w in ql for w in ['prefer','like','want','better','best']):
        fact_type = 'preference'

    fid  = secrets.token_hex(6)
    now  = datetime.datetime.now().isoformat()
    conn = _conn(db)
    # Avoid duplicate facts
    existing = conn.execute(
        "SELECT id FROM memory_facts WHERE user_id=? AND fact=?",
        (user_id, connect[:500])
    ).fetchone()
    if not existing:
        conn.execute("""
            INSERT INTO memory_facts
              (id,user_id,fact,fact_type,source_query,confidence,created_at)
            VALUES (?,?,?,?,?,?,?)
        """, (fid, user_id, connect[:500], fact_type, query[:100], 0.75, now))
        conn.commit()
    conn.close()


# ══════════════════════════════════════════════════════════════════
# RECALL — build context for the current query
# ══════════════════════════════════════════════════════════════════

def build_memory_context(user_id: str, query: str, db: str = None) -> str:
    """
    Build a memory context string to inject into the UTE prompt.
    Called before every query so UTE knows who it's talking to.
    """
    profile  = get_profile(user_id, db)
    entities = _recall_relevant_entities(user_id, query, db)
    facts    = _recall_relevant_facts(user_id, query, db)
    patterns = _recall_cross_chat_patterns(user_id, db)

    parts = []

    # Profile context
    profile_parts = []
    if profile.get('name'):      profile_parts.append(f"name: {profile['name']}")
    if profile.get('role'):      profile_parts.append(f"role: {profile['role']}")
    if profile.get('company'):   profile_parts.append(f"company: {profile['company']}")
    if profile.get('industry'):  profile_parts.append(f"industry: {profile['industry']}")
    if profile.get('goals'):     profile_parts.append(f"working toward: {profile['goals']}")
    if profile_parts:
        parts.append("USER PROFILE: " + " | ".join(profile_parts))

    # Relevant entities
    if entities:
        ent_strs = []
        for e in entities[:3]:
            facts_list = json.loads(e.get('known_facts','[]'))
            if facts_list:
                ent_strs.append(f"{e['entity']}: {facts_list[-1][:100]}")
        if ent_strs:
            parts.append("KNOWN ENTITIES: " + " | ".join(ent_strs))

    # Relevant memory facts
    if facts:
        fact_strs = [f['fact'][:100] for f in facts[:3]]
        parts.append("PRIOR CONTEXT: " + " | ".join(fact_strs))

    # Cross-chat patterns
    if patterns:
        parts.append("PATTERN ACROSS CHATS: " + patterns[0]['summary'][:150])

    return '\n'.join(parts) if parts else ''


def _recall_relevant_entities(user_id: str, query: str,
                               db: str = None) -> list:
    """Find entities in memory relevant to this query."""
    words = set(re.findall(r'\b\w{3,}\b', query.lower()))
    conn  = _conn(db)
    rows  = conn.execute("""
        SELECT * FROM entity_memory WHERE user_id=?
        ORDER BY mention_count DESC, last_seen DESC LIMIT 20
    """, (user_id,)).fetchall()
    conn.close()
    scored = []
    for row in rows:
        d = dict(row)
        entity_words = set(re.findall(r'\b\w{3,}\b', d['entity'].lower()))
        context_words = set(re.findall(r'\b\w{3,}\b', d.get('last_context','').lower()))
        score = len(words & entity_words) * 3 + len(words & context_words)
        if score > 0:
            scored.append((score, d))
    scored.sort(reverse=True)
    return [d for _, d in scored[:3]]


def _recall_relevant_facts(user_id: str, query: str, db: str = None) -> list:
    """Recall memory facts relevant to this query."""
    words = set(re.findall(r'\b\w{3,}\b', query.lower()))
    now   = datetime.datetime.now().isoformat()
    conn  = _conn(db)
    rows  = conn.execute("""
        SELECT * FROM memory_facts
        WHERE user_id=? AND (expires_at='' OR expires_at > ?)
        ORDER BY confidence DESC, created_at DESC LIMIT 30
    """, (user_id, now)).fetchall()
    conn.close()
    scored = []
    for row in rows:
        d     = dict(row)
        fwords = set(re.findall(r'\b\w{3,}\b', d['fact'].lower()))
        score  = len(words & fwords) * d['confidence']
        if score > 0.5:
            scored.append((score, d))
    scored.sort(reverse=True)
    return [d for _, d in scored[:3]]


def _recall_cross_chat_patterns(user_id: str, db: str = None) -> list:
    """Get cross-chat pattern insights for this user."""
    conn = _conn(db)
    rows = conn.execute("""
        SELECT * FROM chat_relations WHERE user_id=?
        ORDER BY last_updated DESC LIMIT 3
    """, (user_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ══════════════════════════════════════════════════════════════════
# CROSS-CHAT PATTERN DETECTION
# ══════════════════════════════════════════════════════════════════

def detect_patterns(user_id: str, db: str = None):
    """
    Periodically called to detect patterns across the user's chat history.
    Finds topic clusters, decision threads, and progressions.
    """
    try:
        import auth as _auth
        conn_auth = _auth._conn(db)
        chats = conn_auth.execute("""
            SELECT c.id, c.title,
                   GROUP_CONCAT(m.content, ' ||| ') as all_content
            FROM chats c
            JOIN messages m ON m.chat_id = c.id AND m.role = 'user'
            WHERE c.user_id = ?
            GROUP BY c.id
            ORDER BY c.created_at DESC LIMIT 20
        """, (user_id,)).fetchall()
        conn_auth.close()

        if len(chats) < 2:
            return

        # Simple topic clustering by word overlap
        topic_map = {}
        for chat in chats:
            words = set(re.findall(r'\b[a-z]{4,}\b',
                                   (chat['all_content'] or '').lower()))
            # Remove very common words
            words -= {'that','this','with','from','have','been','they',
                      'what','when','where','will','would','could','should',
                      'about','there','their','which'}
            for word in words:
                topic_map.setdefault(word, []).append(chat['id'])

        # Find topics appearing in 3+ chats
        recurring = {w: ids for w, ids in topic_map.items()
                     if len(set(ids)) >= 3}

        if not recurring:
            return

        # Build a pattern summary
        top_topics = sorted(recurring.items(),
                            key=lambda x: len(x[1]), reverse=True)[:5]
        topic_names = [t[0] for t in top_topics]
        summary = (f"You've asked about {', '.join(topic_names[:3])} "
                   f"across {len(set(sum([t[1] for t in top_topics], [])))} conversations. "
                   f"This appears to be a recurring theme in your work.")

        # Store the pattern
        conn = _conn(db)
        pid  = secrets.token_hex(6)
        now  = datetime.datetime.now().isoformat()
        chat_ids = list(set(sum([t[1] for t in top_topics[:3]], [])))[:6]
        existing = conn.execute(
            "SELECT id FROM chat_relations WHERE user_id=? AND relation_type='topic_cluster'",
            (user_id,)
        ).fetchone()
        if existing:
            conn.execute("""
                UPDATE chat_relations
                SET summary=?, topics=?, chat_ids=?, last_updated=?
                WHERE id=?
            """, (summary, json.dumps(topic_names), json.dumps(chat_ids),
                  now, existing['id']))
        else:
            conn.execute("""
                INSERT INTO chat_relations
                  (id,user_id,relation_type,summary,chat_ids,topics,created_at,last_updated)
                VALUES (?,?,?,?,?,?,?,?)
            """, (pid, user_id, 'topic_cluster', summary,
                  json.dumps(chat_ids), json.dumps(topic_names), now, now))
        conn.commit()
        conn.close()

    except Exception as e:
        pass  # Pattern detection is non-critical, fail silently


def get_user_summary(user_id: str, db: str = None) -> dict:
    """Return a full summary of what UTE knows about the user."""
    profile  = get_profile(user_id, db)
    conn     = _conn(db)
    entities = conn.execute("""
        SELECT entity, entity_type, mention_count, last_seen
        FROM entity_memory WHERE user_id=?
        ORDER BY mention_count DESC LIMIT 10
    """, (user_id,)).fetchall()
    facts    = conn.execute("""
        SELECT fact, fact_type, created_at FROM memory_facts
        WHERE user_id=? ORDER BY created_at DESC LIMIT 10
    """, (user_id,)).fetchall()
    patterns = conn.execute("""
        SELECT relation_type, summary, last_updated
        FROM chat_relations WHERE user_id=?
        ORDER BY last_updated DESC LIMIT 5
    """, (user_id,)).fetchall()
    conn.close()
    return {
        'profile':  profile,
        'entities': [dict(e) for e in entities],
        'facts':    [dict(f) for f in facts],
        'patterns': [dict(p) for p in patterns],
    }


def forget(user_id: str, what: str = 'all', db: str = None):
    """
    Let the user delete their memory.
    what: 'all' | 'profile' | 'entities' | 'facts' | 'patterns'
    """
    conn = _conn(db)
    if what in ('all', 'profile'):
        conn.execute("DELETE FROM user_profiles WHERE user_id=?", (user_id,))
    if what in ('all', 'entities'):
        conn.execute("DELETE FROM entity_memory WHERE user_id=?", (user_id,))
    if what in ('all', 'facts'):
        conn.execute("DELETE FROM memory_facts WHERE user_id=?", (user_id,))
    if what in ('all', 'patterns'):
        conn.execute("DELETE FROM chat_relations WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()
