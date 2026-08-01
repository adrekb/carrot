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

-- Unified vector store. Embeddings are float32 blobs, not JSON, and are keyed
-- by (namespace, ref_id) so messages, memories and document chunks share one
-- table and one search path. Supersedes the JSON message_embeddings table,
-- which is migrated on startup and then left in place for older builds.
CREATE TABLE IF NOT EXISTS vectors (
    namespace TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    model TEXT DEFAULT 'nomic-embed-text',
    dim INTEGER DEFAULT 0,
    embedding BLOB,
    created_at TEXT NOT NULL,
    PRIMARY KEY (namespace, ref_id)
);

CREATE INDEX IF NOT EXISTS idx_vectors_namespace ON vectors(namespace);

-- Structured long-term memory. Every row carries provenance back to the
-- message it came from; updates supersede rather than overwrite so the history
-- of a belief is preserved.
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'fact',
    subject TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    confidence REAL DEFAULT 1.0,
    status TEXT NOT NULL DEFAULT 'active',
    pinned INTEGER DEFAULT 0,
    source_message_id INTEGER,
    source_conversation_id TEXT,
    superseded_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_subject ON memories(subject, status);

CREATE TABLE IF NOT EXISTS conversation_summaries (
    conversation_id TEXT PRIMARY KEY REFERENCES conversations(id) ON DELETE CASCADE,
    summary TEXT DEFAULT '',
    covered_through INTEGER DEFAULT 0,
    message_count INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL
);

-- Indexed local files and their chunks.
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    title TEXT DEFAULT '',
    ext TEXT DEFAULT '',
    size INTEGER DEFAULT 0,
    mtime REAL DEFAULT 0,
    hash TEXT DEFAULT '',
    chunk_count INTEGER DEFAULT 0,
    char_count INTEGER DEFAULT 0,
    indexed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    ordinal INTEGER DEFAULT 0,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_chunks_document ON document_chunks(document_id);

-- Proactive notifications raised by the background watcher.
CREATE TABLE IF NOT EXISTS notifications (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT DEFAULT '',
    severity TEXT DEFAULT 'info',
    dedupe_key TEXT,
    read INTEGER DEFAULT 0,
    dismissed INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    metadata TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_notifications_dedupe ON notifications(dedupe_key);

-- Undo journal for agent-initiated file writes. before_content is the full
-- prior file (NULL when the agent created the file), which is what makes a
-- one-click revert possible.
CREATE TABLE IF NOT EXISTS file_journal (
    id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    operation TEXT NOT NULL,
    before_content TEXT,
    after_content TEXT,
    reverted INTEGER DEFAULT 0,
    conversation_id TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_file_journal_created ON file_journal(created_at DESC);

-- ===== Carrot Research =====

-- One research run. `question` is what was asked, `report` the final cited
-- markdown. Sub-questions and their findings hang off this row, so a finished
-- run can be re-opened and audited claim by claim.
CREATE TABLE IF NOT EXISTS research_runs (
    id TEXT PRIMARY KEY,
    question TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    depth TEXT NOT NULL DEFAULT 'standard',
    plan TEXT DEFAULT '[]',
    report TEXT DEFAULT '',
    error TEXT DEFAULT '',
    conversation_id TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_research_runs_created ON research_runs(created_at DESC);

-- Every source a run actually read, web or local, deduplicated per run by
-- locator. Findings cite these by id, which is what makes verification a
-- lookup rather than a second guess.
CREATE TABLE IF NOT EXISTS research_sources (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    kind TEXT NOT NULL,
    title TEXT DEFAULT '',
    locator TEXT NOT NULL,
    snippet TEXT DEFAULT '',
    content TEXT DEFAULT '',
    tainted INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_sources_run ON research_sources(run_id, ordinal);

-- A single supported statement produced by a sub-question researcher.
-- `source_ids` is a JSON array into research_sources; `verdict` is what the
-- verification pass concluded about that support.
CREATE TABLE IF NOT EXISTS research_findings (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES research_runs(id) ON DELETE CASCADE,
    subquestion TEXT NOT NULL,
    claim TEXT NOT NULL,
    source_ids TEXT DEFAULT '[]',
    confidence REAL DEFAULT 0.5,
    verdict TEXT DEFAULT 'unchecked',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_research_findings_run ON research_findings(run_id);

-- ===== Carrot Agent =====

-- One agent task. Budgets are stored with the run so a resumed or inspected
-- run shows the limits it actually operated under, not today's config.
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    task TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    surface TEXT NOT NULL DEFAULT 'browser',
    plan TEXT DEFAULT '',
    result TEXT DEFAULT '',
    error TEXT DEFAULT '',
    budget TEXT DEFAULT '{}',
    steps_used INTEGER DEFAULT 0,
    conversation_id TEXT,
    created_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_created ON agent_runs(created_at DESC);

-- The audit trail: every action the agent proposed, what the policy decided,
-- and what came back. Arguments are stored post-redaction — a secret value is
-- never written here, only the name of the vault entry that was used.
CREATE TABLE IF NOT EXISTS agent_steps (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
    ordinal INTEGER NOT NULL,
    action TEXT NOT NULL,
    arguments TEXT DEFAULT '{}',
    rationale TEXT DEFAULT '',
    risk TEXT DEFAULT 'low',
    decision TEXT DEFAULT 'allow',
    decision_reason TEXT DEFAULT '',
    observation TEXT DEFAULT '',
    screenshot TEXT DEFAULT '',
    tainted INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_steps_run ON agent_steps(run_id, ordinal);
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

# FTS indexes for memories and document chunks. Both are content-storing (the
# same choice messages_fts settled on) and kept in sync by triggers so callers
# only ever write to the base table.
AUX_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    content,
    subject,
    memory_id UNINDEXED,
    kind UNINDEXED
);

CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memories_fts(memory_id, content, subject, kind)
    VALUES (new.id, new.content, new.subject, new.kind);
END;

CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    DELETE FROM memories_fts WHERE memory_id = old.id;
END;

CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    UPDATE memories_fts SET content = new.content, subject = new.subject, kind = new.kind
    WHERE memory_id = old.id;
END;

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    content,
    chunk_id UNINDEXED,
    document_id UNINDEXED
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON document_chunks BEGIN
    INSERT INTO chunks_fts(rowid, chunk_id, content, document_id)
    VALUES (new.id, new.id, new.content, new.document_id);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON document_chunks BEGIN
    DELETE FROM chunks_fts WHERE rowid = old.id;
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON document_chunks BEGIN
    DELETE FROM chunks_fts WHERE rowid = old.id;
    INSERT INTO chunks_fts(rowid, chunk_id, content, document_id)
    VALUES (new.id, new.id, new.content, new.document_id);
END;
"""

SCHEMA = SCHEMA + FTS_SCHEMA + AUX_FTS_SCHEMA


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