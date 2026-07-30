import sqlite3
import os
import json
from datetime import datetime, timezone


DBCORE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
DB_PATH = os.path.join(DBCORE_DIR, "carrot.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    metadata TEXT DEFAULT '{}',
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS goals (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    category TEXT DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    metadata TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS reminders (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    due_at TEXT,
    completed INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    metadata TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS leaderboard (
    id TEXT PRIMARY KEY,
    os TEXT,
    cpu TEXT,
    ram_gb REAL,
    gpu TEXT,
    active_model TEXT,
    task_type TEXT DEFAULT 'general',
    tokens_per_second REAL DEFAULT 0,
    submitted_at TEXT NOT NULL,
    anonymous_id TEXT NOT NULL,
    metadata TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS message_embeddings (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id) ON DELETE CASCADE,
    model TEXT DEFAULT 'nomic-embed-text',
    embedding TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS widgets (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    slot TEXT DEFAULT 'rail',
    position INTEGER DEFAULT 0,
    config TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS health_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    metric TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT DEFAULT '',
    source TEXT DEFAULT 'apple_shortcuts',
    updated_at TEXT NOT NULL,
    UNIQUE(date, metric)
);

CREATE TABLE IF NOT EXISTS github_cache (
    key TEXT PRIMARY KEY,
    data TEXT DEFAULT '{}',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chat_folders (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    position INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);
"""

# Content-storing FTS5 index. Kept separate from SCHEMA so it can be re-applied
# during migration from the legacy external-content (content=messages) schema,
# which referenced a message_id column that does not exist on the messages table.
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    message_id UNINDEXED,
    conversation_id UNINDEXED,
    role UNINDEXED,
    timestamp UNINDEXED
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, message_id, content, conversation_id, role, timestamp)
    VALUES (new.id, new.id, new.content, new.conversation_id, new.role, new.timestamp);
END;

CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
    INSERT INTO messages_fts(rowid, message_id, content, conversation_id, role, timestamp)
    VALUES (new.id, new.id, new.content, new.conversation_id, new.role, new.timestamp);
END;
"""

SCHEMA = SCHEMA + FTS_SCHEMA


def get_db():
    os.makedirs(DBCORE_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _migrate_fts(conn):
    """Rebuild the FTS index if it uses the legacy external-content schema."""
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages_fts'"
    ).fetchone()
    if row and row["sql"] and "content=" in row["sql"]:
        conn.executescript(
            """
            DROP TRIGGER IF EXISTS messages_ai;
            DROP TRIGGER IF EXISTS messages_ad;
            DROP TRIGGER IF EXISTS messages_au;
            DROP TABLE IF EXISTS messages_fts;
            """
        )
        conn.executescript(FTS_SCHEMA)
        conn.execute(
            """
            INSERT INTO messages_fts(rowid, message_id, content, conversation_id, role, timestamp)
            SELECT id, id, content, conversation_id, role, timestamp FROM messages
            """
        )
        conn.commit()


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    _migrate_fts(conn)
    conn.commit()
    conn.close()