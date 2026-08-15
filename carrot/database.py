import sqlite3
import os
import json
from datetime import datetime, timezone


# One source of truth for where data lives: carrot.config resolves the
# checkout dir vs. the per-user dir for installed (frozen) builds.
from carrot.config import CARROT_DIR as DBCORE_DIR

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

-- Charts, diagrams and generated images the assistant produced. Content is
-- model-authored markup, so it is never inlined into the app document; see
-- carrot/artifacts.py for how it is isolated when shown.
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    conversation_id TEXT,
    message_id TEXT,
    kind TEXT NOT NULL,
    title TEXT,
    content TEXT NOT NULL,
    meta TEXT DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_conversation
    ON artifacts(conversation_id, created_at);

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
    -- Which part of Carrot was running when this was learned. The message id
    -- above says *where* it came from; this says *what kind of work* produced
    -- it, which is the question people actually ask of a memory list.
    origin TEXT NOT NULL DEFAULT 'chat',
    superseded_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_memories_subject ON memories(subject, status);
-- The index on `origin` is NOT here; see ADDED_INDEXES below.

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

-- A whole-workspace snapshot taken before the coding agent acts. The journal
-- above reverses one write; this reverses a whole train of thought, which is
-- what you want when the agent went wrong nine steps ago. `files` is a JSON
-- map of relative path -> contents, text files only.
-- `tree` is a git tree object when the workspace is a repository, which
-- makes the snapshot atomic and free; `files` is the fallback copy for a
-- workspace that is not under version control. Exactly one is populated.
CREATE TABLE IF NOT EXISTS coder_checkpoints (
    id TEXT PRIMARY KEY,
    label TEXT DEFAULT '',
    root TEXT NOT NULL,
    files TEXT NOT NULL DEFAULT '{}',
    conversation_id TEXT,
    created_at TEXT NOT NULL,
    tree TEXT DEFAULT '',
    head TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_coder_checkpoints_created ON coder_checkpoints(created_at DESC);

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
    -- Who is speaking: first-party, official, reputable, unknown, low. Set
    -- once when the source is first stored, because the first-party test needs
    -- the question and the question is not stored alongside the page.
    tier TEXT NOT NULL DEFAULT 'unknown',
    tier_reason TEXT DEFAULT '',
    -- When the page says it was written. Separate from authority: a launch
    -- announcement stays first-party forever and stops being current.
    published TEXT DEFAULT '',
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
    -- Best tier among the cited sources: how strong the support is, as
    -- distinct from `verdict`, which is whether the support exists at all.
    tier TEXT NOT NULL DEFAULT 'unknown',
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

-- ===== Workspaces =====

-- A folder holds workspaces, and may hold other folders. Distinct from
-- `chat_folders`, which tidies conversations *inside* a workspace — this one
-- is a level above, grouping whole projects.
CREATE TABLE IF NOT EXISTS folders (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    parent_id TEXT REFERENCES folders(id) ON DELETE SET NULL,
    position INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_folders_parent ON folders(parent_id, position);

-- One project's context: the chats, memories, files and runs that belong to it.
CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT DEFAULT '',
    folder_id TEXT REFERENCES folders(id) ON DELETE SET NULL,
    color TEXT DEFAULT '',
    position INTEGER DEFAULT 0,
    archived INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workspaces_folder ON workspaces(folder_id, position);

-- Membership, kept in one table rather than a column on six others. An item
-- has exactly one home — that is what the primary key enforces — so "which
-- workspace is this in" always has a single answer, and moving something is
-- one upsert rather than a migration per content type.
CREATE TABLE IF NOT EXISTS workspace_items (
    kind TEXT NOT NULL,
    item_id TEXT NOT NULL,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    added_at TEXT NOT NULL,
    PRIMARY KEY (kind, item_id)
);

CREATE INDEX IF NOT EXISTS idx_workspace_items_ws ON workspace_items(workspace_id, kind);
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

-- Ambient screen capture. Text only, never an image: see the note at the top
-- of carrot/ambient_capture.py for why a rolling video of someone's screen is
-- a thing this app declines to keep.
--
-- `ended_at` and `seen` exist because staying on one document for ten minutes
-- should be one row that lasted ten minutes, not seventy-five identical ones.
CREATE TABLE IF NOT EXISTS ambient_frames (
    id TEXT PRIMARY KEY,
    captured_at TEXT NOT NULL,
    ended_at TEXT NOT NULL,
    app TEXT DEFAULT '',
    title TEXT DEFAULT '',
    url TEXT DEFAULT '',
    text TEXT NOT NULL,
    engine TEXT DEFAULT '',
    seen INTEGER DEFAULT 1,
    workspace_id TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_ambient_at ON ambient_frames(captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_ambient_app ON ambient_frames(app, captured_at DESC);

CREATE VIRTUAL TABLE IF NOT EXISTS ambient_fts USING fts5(
    text,
    title,
    app,
    frame_id UNINDEXED
);

CREATE TRIGGER IF NOT EXISTS ambient_ai AFTER INSERT ON ambient_frames BEGIN
    INSERT INTO ambient_fts(frame_id, text, title, app)
    VALUES (new.id, new.text, new.title, new.app);
END;

CREATE TRIGGER IF NOT EXISTS ambient_ad AFTER DELETE ON ambient_frames BEGIN
    DELETE FROM ambient_fts WHERE frame_id = old.id;
END;
"""

SCHEMA = SCHEMA + FTS_SCHEMA + AUX_FTS_SCHEMA


# Databases this process has already run the schema against.
#
# `get_db()` used to execute all 73 DDL statements on every connection, and
# most endpoints open one per request. It is correct — everything is
# IF NOT EXISTS — and it is the safety net that means a caller never has to
# have run `init_db()` first. It also costs 0.35 ms of parsing every time,
# which buys nothing after the first.
#
# Keyed by path rather than a bare flag on purpose: the tests point `DB_PATH`
# at a fresh temporary file per test, and a single boolean would let the
# second test skip creating its schema and fail on a missing table.
_schema_ready: set = set()


def get_db():
    os.makedirs(DBCORE_DIR, exist_ok=True)
    # Checked before connecting, because connecting creates the file. A
    # database that has been deleted or replaced underneath us — a restored
    # checkpoint, a test tearing its directory down — has to be rebuilt, and
    # the marker from the file that used to be there would prevent it.
    if not os.path.exists(DB_PATH):
        _schema_ready.discard(DB_PATH)
    # Wait for a lock rather than failing at the default five seconds. WAL
    # lets readers and writers coexist, but two writers still serialise, and
    # this app now has several: a chat turn saving messages, a scheduled task
    # writing its output, the indexer, the ambient capture. Five seconds is
    # long for a mouse click and short for a sqlite checkpoint on a spinning
    # disk, and "database is locked" surfaces as a lost message rather than
    # as a wait.
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Wait for readers to finish a checkpoint instead of erroring, same
    # reasoning. Cheap on a database this size and worth it on a laptop that
    # is also compiling something.
    conn.execute("PRAGMA busy_timeout=30000")
    conn.row_factory = sqlite3.Row
    if DB_PATH not in _schema_ready:
        conn.executescript(SCHEMA)
        conn.commit()
        _schema_ready.add(DB_PATH)
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


# Columns added to tables that already exist in the wild. CREATE TABLE IF NOT
# EXISTS does nothing for an existing table, so a new column has to be added
# explicitly or an upgraded install reads a schema it does not have.
ADDED_COLUMNS = (
    ("coder_checkpoints", "tree", "TEXT DEFAULT ''"),
    ("coder_checkpoints", "head", "TEXT DEFAULT ''"),
    # Everything remembered before this column existed came from an ordinary
    # chat turn, because chat was the only thing that wrote memories. The
    # default is the truth about the old rows, not a placeholder.
    ("memories", "origin", "TEXT NOT NULL DEFAULT 'chat'"),
    # Who was speaking on a cited page. Runs recorded before this column
    # existed genuinely do not know, and 'unknown' is the honest label for
    # that rather than a guess back-filled from the domain today — the
    # first-party test depends on the question, which is not re-runnable here.
    ("research_sources", "tier", "TEXT NOT NULL DEFAULT 'unknown'"),
    ("research_sources", "tier_reason", "TEXT DEFAULT ''"),
    ("research_sources", "published", "TEXT DEFAULT ''"),
    # The best tier among the sources a claim cites — what the writer is
    # allowed to state flat and what it has to attribute.
    ("research_findings", "tier", "TEXT NOT NULL DEFAULT 'unknown'"),
)

# Indexes over columns that ADDED_COLUMNS creates. These cannot live in SCHEMA,
# and finding out why cost an upgrade path: `get_db()` runs SCHEMA on *every*
# connection, `CREATE TABLE IF NOT EXISTS` does nothing to a table that already
# exists, and `init_db` opens a connection before it can migrate anything. So
# an index naming a migrated column, placed in SCHEMA, fails on every single
# connection to an existing database — including the one the migration needed
# to open. They are created here instead, after the column is guaranteed.
ADDED_INDEXES = (
    ("memories", "origin",
     "CREATE INDEX IF NOT EXISTS idx_memories_origin ON memories(origin, status)"),
)


def _migrate_columns(conn):
    for table, column, spec in ADDED_COLUMNS:
        try:
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error:
            continue
        if existing and column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {spec}")

    for table, column, statement in ADDED_INDEXES:
        try:
            existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if column in existing:
                conn.execute(statement)
        except sqlite3.Error:
            continue


def init_db():
    conn = get_db()
    conn.executescript(SCHEMA)
    _migrate_fts(conn)
    _migrate_columns(conn)
    conn.commit()
    conn.close()