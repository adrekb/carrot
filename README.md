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

## What Makes It Different

- **Everything runs locally.** No accounts, no cloud, no data leaving your machine. Your conversations, notes, goals, and code stay private.
- **Powered by Ollama.** The AI runs on your own hardware using the `gemma4:e4b` model. You don't need an API key — and if you don't have Ollama, Carrot installs it for you on first launch.
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

### Search & Recall
- **SQLite FTS5** full-text search with time-aware query parser
- **Hybrid search**: Matches exact phrases (FTS5) and conceptual meaning (vector embeddings via `nomic-embed-text`)
- **Query classification**: Ollama-powered intent extraction for structured search

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

# Run a recap
carrot recap

# Scan for assignments
carrot scan

# Check status
carrot status
```

## The Name

Carrot is named after the carrot and the rabbit — your rabbit assistant keeps you organized and motivated, and the carrot is the reward at the end of the work.