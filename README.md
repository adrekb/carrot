# Carrot 🥕

A personal AI assistant that lives on your computer and helps you think better, code faster, and stay on top of your life — all without sending your data to the cloud.

## What Carrot Is

Imagine having a really smart friend who knows everything about your projects, remembers every conversation you've ever had, remembers your goals and deadlines, and can pull up old notes or code snippets when you need them — and you never have to leave your computer to find them. That's Carrot.

Carrot combines the tools you already use — a code editor, a notes app, a to-do list, a search engine — into one thing. You talk to it in plain English, and it uses a local AI (through Ollama) to understand you and help you.

## How It Helps People

**You ever had a conversation with yourself and then forgotten what you said?** Carrot remembers everything. You can ask it things like:

- _"What did I decide about my project architecture last month?"_
- _"5 months ago, what were my bench press numbers?"_
- _"I had a discussion about Rust vs Go — what came up?"_

It searches through all your past conversations, notes, and even old goals to find the answer. It doesn't just keyword match — it understands the context and the time frame.

**You're a CS student with assignments scattered across Canvas, folders, and your browser.** Instead of manually digging through files, you can ask Carrot to find your assignments, read them, and even open the relevant code files in a built-in terminal so you can start working immediately.

**You want to track your goals but keep forgetting to update them.** Carrot lets you set goals (fitness, learning, projects), log progress over time, and look back to see how far you've come.

**You want to start your day informed without doom-scrolling Twitter.** When your PC is plugged in and idle in the morning, Carrot fetches the day's tech and science news, summarizes it with AI, and saves it for you. You can ask "what's in today's recap?" anytime.

**You need to remember things.** Carrot has reminders that work like any to-do app, but because everything is connected, you can search across them, link them to conversations, and never lose track of what matters.

**You want an assistant that actually knows you.** Carrot doesn't just search what you typed — it builds a structured memory of what's true about you. Preferences, decisions, projects, commitments. Every belief is traceable back to the message it came from, and you can read, edit, pin, or delete any of it from the Memory tab. Get something wrong once and mark it wrong; Carrot won't record that subject again.

**You have folders full of things you'll never re-read.** Point Carrot at them — papers, notes, code, saved pages — and it indexes them locally into the same search that covers your conversations. "What did that paper say about attention?" works against a PDF you downloaded six months ago and never opened again. Nothing is uploaded anywhere.

## What Makes It Different

- **Everything runs locally.** No accounts, no cloud, no data leaving your machine. Your conversations, notes, goals, and code stay private.
- **It remembers, and shows its work.** Structured long-term memory with provenance, supersession, and a full audit UI — not just a chat log.
- **It reads your files, not just your chats.** A local document index over PDFs, markdown, code, and saved HTML, searchable alongside everything else.
- **It can do things, safely.** Built-in agent tools for reading, editing, searching and running code — every mutating action asks first, and every file edit can be reverted with its diff.
- **Powered by Ollama.** The AI runs on your own hardware using the `gemma4:e4b` model. You don't need an API key — and if you don't have Ollama, Carrot installs it for you on first launch.
- **Your keys, your choice of model.** If you do want a hosted model for some things, bring a key for Anthropic, OpenAI, or anything OpenAI-compatible, and assign it per task — a frontier model for hard reasoning, something cheap for classification, everything else on-device.
- **One-click setup.** On first run Carrot detects whether Ollama is present, silently installs it if not, and pulls `gemma4:e4b` — all with a progress splash screen. No manual terminal steps required.
- **Search-first design.** Every conversation, note, goal entry, and reminder is indexed and searchable. If you've ever typed something that Carrot heard, you can find it again.
- **One app instead of five.** Code editor + notes + reminders + goal tracker + search + AI chat, all in one place with one interface.
- **GUI-first desktop app.** Runs as a native desktop window with a polished multi-pane dashboard, not just a browser tab.
- **100% local speech.** Voice input via whisper.cpp and voice output via Kokoro TTS — realistic, human-like audio, completely offline.

## 2026 Architecture

### GUI Desktop App
- **Electron wrapper** around the FastAPI backend (`gui/` directory)
- **Glassmorphism dashboard**: translucent, blurred panels over an animated aurora background, with carrot-orange accents. Views for chat, search, terminal, notes, goals, reminders, recap, assignments, status, and leaderboard.
- **Streaming chat** with live token-by-token responses, plus a voice-input mic and read-aloud replies.
- **Global shortcut**: Press `Alt+Space` to drop down a compact overlay over any app — speak or type commands instantly
- **Native window** with system tray integration

### Local Speech Stack
- **Voice Input (STT)**: `whisper.cpp` — lightweight, blazing fast, runs on CPU or GPU
- **Voice Output (TTS)**: `Kokoro-82M` — compact, human-like, expressive TTS under 100MB
- **Voice Profiles**: Multiple voices (`af_heart`, `bf_qlwn`, etc.) with adjustable speed/volume

### Memory & Recall
- **Structured memory** (`carrot/memory.py`): the model extracts durable facts from each turn — preferences, decisions, projects — and stores them as first-class rows with a link back to the source message. New values *supersede* rather than overwrite, so "what did I used to think about X" still works.
- **Rolling conversation summaries** (`carrot/summarize.py`): everything older than the recent window is folded into an incremental summary, so a 500-turn conversation still knows what it decided on turn 3.
- **Local document index** (`carrot/indexer.py`): walks configured folders, extracts text from markdown, code, HTML and PDF, chunks it with overlap, and indexes it into FTS5 + embeddings. Incremental — an unchanged tree costs one stat per file.
- **Unified vector store** (`carrot/vectors.py`): one packed-float32 table shared by messages, memories and document chunks. Uses `sqlite-vec` when installed, otherwise a single numpy matmul (fine well past 100k vectors). Embedding happens on a background worker; a backfill catches anything written while Ollama was down.
- **SQLite FTS5** full-text search with a time-aware query parser
- **Hybrid search**: exact phrases (FTS5) reranked by conceptual similarity (embeddings via `nomic-embed-text`)
- **`/api/search/all`**: one query across conversations, indexed files, and memory

### Agent Tools
- **Built-in tools** (`carrot/agent_tools.py`) alongside MCP: read/write files, list directories, regex search, run commands, and search memory, documents and past conversations
- **Approval gate**: every mutating tool blocks until you allow or deny it, with "don't ask again this session" per tool
- **Undo journal**: file writes record their previous contents, so any agent edit can be reverted with its diff shown first

### Model Routing
- **Task-aware** (`carrot/router.py`): each call names its task (chat, code, reasoning, classify, summarize, extract, recap) and the router picks the provider and model
- **Bring your own key** (`carrot/providers.py`): Ollama on-device, plus Anthropic, OpenAI, or any endpoint speaking the OpenAI format — OpenRouter, Groq, Together, DeepSeek, Mistral, LM Studio, vLLM, your own server. Adding one is a name, a base URL and a key; nothing about it is special-cased.
- **Per-task assignment**: pin any task to any provider and model — model A for recap, model B for code, a cheap local model for classification. An assignment always beats the automatic rules.
- **Custom tasks**: define your own routing targets in Settings and call them with `task=<id>`. They route exactly like the built-ins.
- **Optional automatic escalation**: with a key attached, send only the hardest reasoning and coding work to a frontier model. Off by default; high-volume tasks like classification never escalate on their own.
- **Provenance**: every chat turn announces which provider and model served it, in the stream and in the UI
- **Hardware-aware suggestions**: recommends a local model sized to your available RAM
- **Keys stay local**: stored in the local config, resolved from the environment if you already export them, and reduced to booleans by the config API — a saved key is never readable over HTTP

### Security
- **Session token**: `127.0.0.1` keeps Carrot off the network but not away from the browser. Every `/api` call requires a token injected into the app's own HTML, which the same-origin policy keeps out of reach of other pages.
- **Destructive-command screening**: the terminal returns `428` for commands that match known-destructive patterns until you confirm
- **Optional working-directory containment** for the terminal and agent tools

### Proactive Notifications
- A background watcher (`carrot/proactive.py`) checks for overdue and upcoming reminders, stale goals, assignments due soon, and embedding backlogs
- Notifications stream to the UI over SSE and become native OS toasts in the desktop app
- Every check produces a stable dedupe key, so a reminder that stays overdue notifies once, not a hundred times

### Backup & Restore
- Full export (`carrot/backup.py`) of the database, notes, skills, briefings and config into a single archive you own
- The database is snapshotted with SQLite's backup API, so an export taken while Carrot is running is consistent
- Restore replaces the current instance, taking a safety copy first, and refuses archives with unsafe paths or an unreadable database

### Daily Recap with Free Web Search
- **DuckDuckGo web search** integration — no API keys needed
- **RSS feeds** as a complementary data source
- Ollama synthesizes a clean morning briefing

## One-Click Install (Windows)

Carrot is designed to be installed and launched with a single click — even on a machine that doesn't have Ollama.

```bat
:: From the project root, run the full build pipeline:
build.bat
```

This installs Python deps, downloads the bundled Ollama installer into `assets/`, and packages the Electron app into `gui\dist\Carrot Setup.exe`.

On **first launch**, Carrot automatically:
1. Detects whether Ollama is installed (installs it silently from the bundled installer if not).
2. Starts the Ollama service.
3. Pulls the default model `gemma4:e4b` with progress shown on the splash screen.

You can also trigger or inspect this at runtime via `POST /api/bootstrap/run` and `GET /api/bootstrap/status`.

## Building the Desktop App

```bash
# Prerequisites: Python 3.10+, Node.js 18+
# (Ollama is NOT required — Carrot installs it on first launch)

# Install Python deps
pip install -e .

# Download the bundled Ollama installer (into assets/)
python scripts/build_installer.py

# Build the GUI installer
cd gui && npm install && npm run dist
cd ..

# The installer will be at: gui\dist\Carrot Setup.exe

# Or run the desktop app in dev mode
cd gui && npm start

# Or run just the web UI (no Electron)
python -m carrot.app   # then open http://127.0.0.1:8181
```

## Requirements

- Python 3.10+
- [Ollama](https://ollama.com/) — **optional**: Carrot installs it automatically on first launch and pulls `gemma4:e4b`
- Node.js 18+ (only for building the Electron desktop app)
- For speech: `pip install kokoro-onnx sounddevice numpy` (optional; voice features degrade gracefully without them)

## CLI Quick Start

```bash
# Install
pip install -e .

# Start the server
carrot start

# Search conversations
carrot search "5 months ago bench press"

# Index a folder of documents, then search everything at once
carrot index ~/Documents/papers
carrot index-scan
carrot find "scaled dot-product attention"

# See what Carrot remembers about you
carrot memory
carrot memory editor

# See which provider and model serves each task
carrot route

# Back up and restore everything
carrot backup
carrot restore ~/carrot-backup-20260731.zip

# Run a recap
carrot recap

# Scan for assignments
carrot scan

# Check status
carrot status
```

## Optional Extras

Carrot is fully functional with the base install. These are opt-in:

Note that only the Anthropic provider needs an extra package. Every
OpenAI-compatible provider — OpenAI itself, OpenRouter, Groq, Together,
DeepSeek, Mistral, LM Studio, vLLM — works with the base install.

```bash
pip install 'carrot[cloud]'    # the Anthropic SDK, for the Anthropic provider
pip install 'carrot[vectors]'  # sqlite-vec ANN backend (numpy fallback otherwise)
pip install 'carrot[speech]'   # Kokoro TTS voice output
```

## API Access

The API is gated behind a per-install session token. The web UI receives it
automatically; scripts and shortcuts can read it with `carrot token`:

```bash
curl -H "X-Carrot-Token: $(carrot token)" http://127.0.0.1:8181/api/status
```

## The Name

Carrot is named after the carrot and the rabbit — your rabbit assistant keeps you organized and motivated, and the carrot is the reward at the end of the work.