import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).parent / "leads.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    vk_id INTEGER PRIMARY KEY,
    first_name TEXT,
    last_name TEXT,
    source TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    funnel_stage TEXT NOT NULL DEFAULT 'started',
    name TEXT,
    phone TEXT,
    spend_tier TEXT,
    link_shown_at TEXT,
    reminder1_sent INTEGER NOT NULL DEFAULT 0,
    reminder2_sent INTEGER NOT NULL DEFAULT 0,
    completed_at TEXT,
    blocked INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_users_stage ON users(funnel_stage);
CREATE INDEX IF NOT EXISTS idx_users_source ON users(source);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    vk_id INTEGER,
    event TEXT NOT NULL,
    payload TEXT,
    ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_vk ON events(vk_id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);

CREATE TABLE IF NOT EXISTS user_states (
    vk_id INTEGER PRIMARY KEY,
    state TEXT,
    data TEXT
);
"""


def now():
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init():
    with conn() as c:
        c.executescript(SCHEMA)
    # добавляем blocked если БД старая (до изменения схемы)
    try:
        with conn() as c:
            c.execute("ALTER TABLE users ADD COLUMN blocked INTEGER DEFAULT 0")
    except sqlite3.OperationalError:
        pass  # колонка уже есть


def upsert_user(vk_id: int, first_name: str, last_name: str, source: str):
    ts = now()
    with conn() as c:
        existing = c.execute("SELECT vk_id FROM users WHERE vk_id=?", (vk_id,)).fetchone()
        if existing:
            c.execute(
                "UPDATE users SET first_name=?, last_name=?, last_seen=? WHERE vk_id=?",
                (first_name, last_name, ts, vk_id),
            )
        else:
            c.execute(
                "INSERT INTO users (vk_id, first_name, last_name, source, first_seen, last_seen) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (vk_id, first_name, last_name, source, ts, ts),
            )


def update_stage(vk_id: int, stage: str, **fields):
    fields["funnel_stage"] = stage
    fields["last_seen"] = now()
    sets = ", ".join(f"{k}=?" for k in fields)
    with conn() as c:
        c.execute(f"UPDATE users SET {sets} WHERE vk_id=?", (*fields.values(), vk_id))


def log_event(vk_id: int, event: str, payload: str = None):
    with conn() as c:
        c.execute(
            "INSERT INTO events (vk_id, event, payload, ts) VALUES (?, ?, ?, ?)",
            (vk_id, event, payload, now()),
        )


def get_user(vk_id: int) -> dict | None:
    with conn() as c:
        r = c.execute("SELECT * FROM users WHERE vk_id=?", (vk_id,)).fetchone()
        return dict(r) if r else None


def mark_link_shown(vk_id: int):
    with conn() as c:
        c.execute(
            "UPDATE users SET link_shown_at=?, reminder1_sent=0, reminder2_sent=0 WHERE vk_id=?",
            (now(), vk_id),
        )


def mark_blocked(vk_id: int):
    with conn() as c:
        c.execute("UPDATE users SET blocked=1 WHERE vk_id=?", (vk_id,))


def get_all_vk_ids() -> list[int]:
    with conn() as c:
        rows = c.execute("SELECT vk_id FROM users WHERE blocked = 0").fetchall()
        return [r["vk_id"] for r in rows]


def get_recent_leads(limit: int = 10) -> list[dict]:
    with conn() as c:
        rows = c.execute(
            "SELECT vk_id, first_name, last_name, funnel_stage, first_seen "
            "FROM users ORDER BY first_seen DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def funnel_stats() -> dict:
    with conn() as c:
        stages = c.execute(
            "SELECT funnel_stage, COUNT(*) n FROM users GROUP BY funnel_stage"
        ).fetchall()
        sources = c.execute(
            "SELECT source, COUNT(*) total, "
            "SUM(CASE WHEN funnel_stage='registered' THEN 1 ELSE 0 END) registered "
            "FROM users GROUP BY source ORDER BY total DESC"
        ).fetchall()
        return {
            "stages": [dict(r) for r in stages],
            "sources": [dict(r) for r in sources],
        }


# --- FSM в SQLite ---

def get_state(vk_id: int) -> str | None:
    with conn() as c:
        r = c.execute("SELECT state FROM user_states WHERE vk_id=?", (vk_id,)).fetchone()
        return r["state"] if r else None


def set_state(vk_id: int, state: str, **data):
    with conn() as c:
        c.execute(
            "INSERT INTO user_states (vk_id, state, data) VALUES (?, ?, ?) "
            "ON CONFLICT(vk_id) DO UPDATE SET state=excluded.state, data=excluded.data",
            (vk_id, state, json.dumps(data, ensure_ascii=False)),
        )


def get_state_data(vk_id: int) -> dict:
    with conn() as c:
        r = c.execute("SELECT data FROM user_states WHERE vk_id=?", (vk_id,)).fetchone()
        if r and r["data"]:
            try:
                return json.loads(r["data"])
            except Exception:
                return {}
        return {}


def clear_state(vk_id: int):
    with conn() as c:
        c.execute("DELETE FROM user_states WHERE vk_id=?", (vk_id,))