# Carrot - Architecture Plan

## Overview
Carrot is a carrot/rabbit themed on-device AI assistant combining a code editor, Notion-like notes, reminders, and goal tracking. It uses Ollama (local LLM) as its brain and SQLite FTS5 for powerful search across all data.

## One-Click Install & Bootstrap
A core deliverable is a finished product that installs in one click on Windows, even when Ollama is not present.

**Default model:** `gemma4:e4b` (the exact tag matters — not `gemma4:4b` or `gemma3:4b`).

**Runtime bootstrap (`carrot/bootstrap.py`)** runs on first launch:
1. Detect the Ollama CLI (PATH + common Windows install dirs).
2. If missing, run the bundled `assets/ollama-setup.exe` silently (`/S`); fall back to downloading it.
3. Start the Ollama service (`ollama serve`) if not running.
4. `ollama pull gemma4:e4b`, streaming progress to the splash screen.
5. Persist readiness to `data/config/bootstrap.json` and the model into config.

Exposed via `GET /api/bootstrap/status` and `POST /api/bootstrap/run`; the web UI shows a splash until `bootstrap_complete` is true.

**Build-time bundling (`scripts/build_installer.py`)** downloads the Ollama installer into `assets/` and writes a manifest; `gui/package.json` ships it via electron-builder `extraFiles` so it lands beside the packaged exe.

## Frontend: Glassmorphism Dashboard
The web UI (`carrot/web/`) is a single-page glassmorphism dashboard: an animated aurora background with translucent, blurred panels and carrot-orange accents. A sidebar navigates between views (chat, search, terminal, notes, goals, reminders, recap, assignments, status, leaderboard). Chat streams tokens over SSE (`/api/chat/stream`), supports voice input (mic → whisper) and read-aloud replies (Kokoro). The Electron shell (`gui/main.js`) spawns the FastAPI backend and loads this UI from `http://127.0.0.1:8181`.

## Core Features

### 1. Conversation Search (Carrot Recall)
The key feature: natural-language time-relative queries over all conversation history.

**Example query:** "5 months ago what was my bench press stats"

**How search works:**
1. **Query Parser** extracts: time offset ("5 months ago") and keywords ("bench press stats")
2. **Date Engine** computes the date range: today minus 5 months → today
3. **FTS5 Search** queries the full-text index for keyword matches within that date range
4. **Ranking** returns most relevant matching conversation snippets

Time patterns supported:
- "N days/weeks/months/years ago" → relative date range
- "last week/month" → calendar range
- "between X and Y" → explicit date range
- Keyword-only (no time) → search all conversations

**Storage:** SQLite database with an FTS5 virtual table indexing all conversation messages
- Each message stored with `conversation_id`, `timestamp`, `role`, `content`
- FTS5 table `conversation_fts` with columns mapped from messages
- Search query: `SELECT conversation_id, snippet(conv_fts), rank FROM conv_fts WHERE conv_fts MATCH ? AND timestamp BETWEEN ? AND ? ORDER BY rank LIMIT ?`

### 2. Ollama Integration
- Python `requests` library calls `http://localhost:11434/api/generate` (streaming or single call)
- Model selection config per "space" (workspace)
- Fallback model if primary is unavailable

### 3. Computer Use (Canvas/Assignment Discovery)
- File system watcher scanning for Canvas/assignment folders
- OCR on screenshots if needed (using pytesseract or Ollama vision)
- Indexes assignment metadata (due dates, names, files) so Carrot can "look through your canvas"
- When user says "what are my assignments", Carrot queries the index

### 4. Carrot Terminal
- Integrated terminal for running code
- Supports Python, JS, shell, etc.
- Output captured and stored in conversation history
- Can auto-detect file type and suggest terminal commands

### 5. Carrot Recap (Daily Morning Brief)
- Trigger: PC plugged in + idle/sleeping in the morning
- Fetches news from RSS feeds (configurable sources)
- Ollama summarizes and filters for relevance to user's interests
- Stored as conversation entry; user can ask "what was in today's recap?"
- Uses FTS5 so user can search past recaps

### 6. Goal Tracking
- Goals stored as structured notes with `:goal` tag
- Time-series data points (e.g., "bench press: 185lbs on 2026-03-01")
- Search finds historical data points within any time range
- "5 months ago what was my bench press stats" → searches goals + conversations for fitness data

### 7. Notes (Notion-like)
- Markdown files stored in `carrot_data/notes/`
- Indexed in FTS5 for full-text search
- Frontmatter metadata (tags, date, type)
- Supports folders (e.g., `carrot_data/notes/fitness/`)

### 8. Reminders/Planning
- SQLite-backed reminder store
- Date/time triggers
- Can create reminders from natural language via Ollama
- Searchable via FTS5

## Data Architecture

### Directory Structure
```
carrot/
├── PLAN.md                    # This file
├── README.md
├── pyproject.toml             # Python project config
├── build.bat                  # One-click Windows build pipeline
├── carrot/
│   ├── __init__.py
│   ├── main.py                # CLI entry point
│   ├── app.py                 # FastAPI app with all routes (chat/stream, bootstrap, speech, ...)
│   ├── bootstrap.py           # One-click Ollama install + gemma4:e4b pull
│   ├── config.py              # Configuration management (default model: gemma4:e4b)
│   ├── database.py            # SQLite connection + setup + FTS migration
│   ├── search.py              # FTS5 search engine + query parser + hybrid ranking
│   ├── ollama_client.py       # Ollama API wrapper (streaming)
│   ├── conversation.py        # Conversation CRUD + storage
│   ├── computer_use.py        # File discovery + canvas scanning
│   ├── terminal.py            # Code execution in subprocess
│   ├── recap.py               # Daily news summarizer
│   ├── goals.py               # Goal tracking
│   ├── reminders.py           # Reminder system
│   ├── notes.py               # Note management
│   ├── leaderboard.py         # Crowd-sourced hardware/model directory
│   ├── speech/
│   │   ├── whisper_stt.py     # Voice input (whisper.cpp)
│   │   └── kokoro_tts.py      # Voice output (Kokoro-82M)
│   └── web/                   # Glassmorphism frontend (static files)
│       ├── index.html         # Main SPA dashboard
│       ├── css/style.css      # Glass design system
│       └── js/app.js          # Chat/streaming/voice/views logic
├── gui/                       # Electron desktop wrapper
│   ├── main.js                # Spawns FastAPI, loads web UI, Alt+Space overlay
│   ├── preload.js
│   └── package.json           # electron-builder config (bundles Ollama installer)
├── scripts/
│   └── build_installer.py     # Downloads bundled Ollama installer + builds package
├── assets/                    # Bundled Ollama installer (generated, gitignored)
├── data/                      # User data (gitignored)
│   ├── carrot.db              # SQLite database
│   └── config/bootstrap.json  # Bootstrap/Ollama readiness state
└── tests/                     # pytest suite (bootstrap, chat, search, speech)
```

### Database Schema (SQLite)
```sql
-- Conversations
CREATE TABLE conversations (
    id TEXT PRIMARY KEY,
    title TEXT,
    created_at TEXT,
    updated_at TEXT,
    metadata TEXT  -- JSON
);

-- Messages
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT REFERENCES conversations(id),
    role TEXT,         -- user, assistant, system, tool
    content TEXT,
    timestamp TEXT,
    metadata TEXT      -- JSON (e.g., file references, tool results)
);

-- FTS5 Virtual Table for full-text search (content-storing).
-- NOTE: an earlier revision used external-content mode (content=messages) with a
-- message_id column that does not exist on `messages`, which broke reads. The
-- index is now a standalone content-storing table; database.py migrates old DBs.
CREATE VIRTUAL TABLE messages_fts USING fts5(
    content,
    message_id UNINDEXED,
    conversation_id UNINDEXED,
    role UNINDEXED,
    timestamp UNINDEXED
);

-- Triggers to keep FTS5 in sync
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, message_id, content, conversation_id, role, timestamp)
    VALUES (new.id, new.id, new.content, new.conversation_id, new.role, new.timestamp);
END;

CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
END;

CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
    INSERT INTO messages_fts(rowid, message_id, content, conversation_id, role, timestamp)
    VALUES (new.id, new.id, new.content, new.conversation_id, new.role, new.timestamp);
END;

-- Goals
CREATE TABLE goals (
    id TEXT PRIMARY KEY,
    title TEXT,
    description TEXT,
    category TEXT,
    created_at TEXT,
    updated_at TEXT,
    metadata TEXT  -- JSON (time-series data points)
);

-- Reminders
CREATE TABLE reminders (
    id TEXT PRIMARY KEY,
    title TEXT,
    description TEXT,
    due_at TEXT,
    completed INTEGER DEFAULT 0,
    created_at TEXT,
    metadata TEXT
);

-- Config
CREATE TABLE config (
    key TEXT PRIMARY KEY,
    value TEXT
);
```

## Search Implementation Detail

### Query Parser
1. Input: raw user string like "5 months ago what was my bench press stats"
2. Tokenize and detect time expressions using regex patterns
3. Extract time offset: `{"months": 5, "ago": true}`
4. Extract keywords: everything that's not a time expression
5. Compute date range using `datetime('now', '-5 months')`

### FTS5 Search Query
```sql
SELECT 
    conversation_id,
    snippet(messages_fts, 3, '<b>', '</b>', '...', 15) as snippet,
    rank
FROM messages_fts 
WHERE messages_fts MATCH ?
    AND timestamp >= ?
    AND timestamp <= ?
ORDER BY rank
LIMIT 20;
```

### Time-aware Ranking
- Messages with more keyword matches ranked higher
- Recency bias: more recent matches within the range get slightly higher rank
- Snippet extraction uses FTS5's built-in `snippet()` function

## Tech Stack
- **Python 3** (primary language, available on system)
- **FastAPI** (HTTP API for the web frontend)
- **SQLite3** (built into Python, with FTS5 extension)
- **Ollama API** (local LLM at localhost:11434)
- **Jinja2** (HTML template rendering for web UI)
- **subprocess** (terminal execution)
- **watchdog** (file system monitoring for computer use)
- **feedparser** (RSS feed parsing for Carrot Recap)

## Running
```bash
cd carrot
pip install -e .
carrot start          # Starts the FastAPI server + opens browser
carrot terminal       # Opens just the terminal
carrot recap          # Manually trigger a recap
carrot search "5 months ago bench press"  # Search conversations
```