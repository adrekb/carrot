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

**You think in documents, not in chat boxes.** Write the plan in a note — with `@/file/` references to the code it's about, an `@/model/` line picking which model should do it, and a `@/to/` line saying where it goes — then hit Send. The note *is* the prompt. No copy-and-paste, and the cited files are read fresh at send time. `@/to/research/deep` sends the note to Carrot Research with your cited papers already loaded as evidence; `@/to/agent/browser` hands it to Carrot Agent as a task.

**You need to remember things.** Carrot has reminders that work like any to-do app, but because everything is connected, you can search across them, link them to conversations, and never lose track of what matters.

**You want an assistant that actually knows you.** Carrot doesn't just search what you typed — it builds a structured memory of what's true about you. Preferences, decisions, projects, commitments. Every belief is traceable back to the message it came from, and you can read, edit, pin, or delete any of it from the Memory tab. Get something wrong once and mark it wrong; Carrot won't record that subject again.

**You have a question that deserves more than a search box.** Ask Carrot Research. It breaks your question into sub-questions, sends a researcher after each one in parallel, reads the actual pages — plus your own indexed files and past conversations — and then does the part nobody else does: it re-checks every claim against the source text it came from before writing a word. Claims the sources don't support get dropped, not softened. What you get back is a report where every sentence carries a citation you can click.

**You have a form to fill out and a browser tab you've been avoiding.** Tell Carrot Agent. It opens a real browser and works through the task — finding the page, filling the fields, pulling up your assignment — showing you its plan first and asking before anything that can't be undone. It never types your password itself: credentials live in a local vault, and Carrot enters them without the model ever seeing the value.

**You have folders full of things you'll never re-read.** Point Carrot at them — papers, notes, code, saved pages — and it indexes them locally into the same search that covers your conversations. "What did that paper say about attention?" works against a PDF you downloaded six months ago and never opened again. Nothing is uploaded anywhere.

## What Makes It Different

- **Everything runs locally.** No accounts, no cloud, no data leaving your machine. Your conversations, notes, goals, and code stay private.
- **It remembers, and shows its work.** Structured long-term memory with provenance, supersession, and a full audit UI — not just a chat log.
- **It reads your files, not just your chats.** A local document index over PDFs, markdown, code, and saved HTML, searchable alongside everything else.
- **It can do things, safely.** Built-in agent tools for reading, editing, searching and running code — every mutating action asks first, and every file edit can be reverted with its diff.
- **Powered by Ollama.** The AI runs on your own hardware using the `gemma4:e4b` model. You don't need an API key — and if you don't have Ollama, Carrot installs it for you on first launch.
- **Your keys, your choice of model.** If you do want a hosted model for some things, bring a key for Anthropic, OpenAI, or anything OpenAI-compatible, and assign it per task — a frontier model for hard reasoning, something cheap for classification, everything else on-device.
- **One-click setup.** On first run Carrot detects whether Ollama is present, silently installs it if not, and pulls `gemma4:e4b` — all with a progress splash screen. No manual terminal steps required.
- **It knows which part of your life you're in.** Group a project's chats, memories and files into a workspace, and search and recall stop reaching into everything else. An assistant that remembers everything is only useful if it can also tell what's relevant.
- **Research that shows its evidence.** Every claim in a report is traced back to text that was actually read, and re-checked against it before the report is written. A citation can be wrong; it cannot be invented.
- **It can act, and it can be stopped.** Carrot Agent drives a real browser to finish real tasks — but nothing irreversible happens without you, credentials never reach the model, and a hostile page costs the agent its privileges rather than gaining it new ones.
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

### Doc to Agent
- **Write the plan, then send it** (`carrot/doc_agent.py`): think something through in a note and hand it straight to the model — no copy-and-paste, no losing the structure. Select part of a note to send only that.
- **`@/file/` citations**: type `@` in the editor, pick `file`, and choose from your workspace and indexed folders. The file is read *at send time* and attached as context, so the model sees the actual file rather than your description of it.
- **`@/model/` selection**: a three-step picker — `@` → `model` → provider (`openai`, `google`, `anthropic`, `local`, or anything you added) → a scrollable list of the models *that provider serves for your key*, fetched live rather than hardcoded. A research note can name a frontier model while a scratch note stays on-device.
- **`@/to/` destinations**: a note is not always a chat turn. `@/to/research/deep` sends it to Carrot Research, `@/to/agent/browser` hands it to Carrot Agent, and the picker beside the Send button follows whatever the note says. Writing the destination into the note means the note stays the whole instruction — you don't have to remember which button a document you wrote three days ago was meant for.
- **Citations follow the note**: sent to Research, cited files are *seeded as evidence* — they take the first citation numbers, every sub-question researcher reads them, and claims drawn from them are verified against their text like anything found on the web. That's the difference between "research this" and "research this, starting from what I already collected." Sent to the Agent, they ride along as background.
- **Shown before it runs**: chips under the note say which citations resolved, how large they are, where it's going, and which model will serve it. A citation that cannot be read is reported, never silently dropped.
- **Confined to what you opted into**: citations reach the agent workspace and your indexed folders, and nothing else.

### Workspaces and Folders
A **workspace** is one project's context — its chats, memories, files, notes and runs. A **folder** groups workspaces, and folders nest.

```
School/
  Thesis        ← chats, memories, files, notes, runs
  CS 3110
Personal/
  Fitness
```

- **A workspace is a scope, not a container.** Nothing moves on disk and nothing is copied. While one is active, new chats, notes, research and agent runs are filed into it, and search, memory recall and document lookup are restricted to what lives there.
- **That restriction is the point.** Without it, a question about your thesis can recall a decision you made about a side project in March. Memories inherit the workspace of the conversation that produced them, so a background extraction lands where the chat was — not wherever you drifted to while it ran.
- **Re-opening an old chat brings back its own context**, not today's: recall is scoped to the conversation's workspace rather than the active one.
- **A pin does not follow you between projects.** Pinned means "always relevant here"; something never filed belongs to no project, so it stays visible everywhere.
- **All workspaces is the default and stays the default.** A fresh install has none and behaves exactly as before.
- **Deleting is never destructive.** Deleting a workspace unfiles its contents; deleting a folder moves its workspaces to the top level. Neither loses a chat.

### Help and the Tutorial
- A **Help** tab with a topic per feature — including a plain account of what the agent may do and what leaves your machine — searchable across titles and body text.
- A **getting-started tutorial whose steps check the live install**: is Ollama up, does a workspace exist, has a folder been indexed, is a site allowed. A tour that ticks because you pressed Next teaches nothing; these stay unticked until the thing is actually true, and un-tick if you undo it.
- A check that cannot run reports **unknown** rather than failing the step — a red cross for something unmeasurable is worse than admitting the measurement failed.

### Chat Search Modes
How much a chat turn may reach the web is a setting in the composer, next to the model picker:

- **No search** — Carrot answers from the conversation, your indexed files and its memory. The web tools are *removed from the tool list*, not just discouraged: an instruction not to search is a request, but a tool that isn't there cannot be called. A question about your own notes gets worse, not better, when the model decides to search first.
- **Search** — a single pass. It may search and read a page when the question needs something current, and cites the URL for anything it takes.
- **Multi-turn search** — it searches, reads, works out what it still cannot answer, and searches again, with double the tool-round budget to do it in. If the question deserves a written report with checked citations, it can hand the whole thing to Carrot Research.

Your choice is sent with the turn *and* saved as the default, so turning search off for a private conversation stays off.

### Extension Packs
- **One switch for a whole kind of work** (`carrot/extensions.py`). A pack ships tools, skills, settings, and a list of the external programs its tools would like to have.
- **Honest about what your machine can do**: every capability is probed, and the Extensions tab shows what is present. A tool whose program is missing refuses up front with the reason and how to install it, rather than failing halfway through a task.
- **The Academia Pack** is the first one: LaTeX authoring with validation and compilation, BibTeX and citation checking, MATLAB/Octave, CAD figure rendering, image-to-LaTeX transcription for photographed tables and formulas, and venue-specific formatting rules. Set your target venue and citation style and the pack's skills are rewritten to name them.
- **Its skills become ordinary skills**, written to your skills directory and reachable with `/` in the command bar — so you can edit the wording if the house style isn't quite right.

### Carrot Research
- **A real multi-agent pipeline** (`carrot/research.py`): plan → parallel researchers → gap reflection → verification → cited synthesis. Sub-questions are researched by independent agents on their own budgets, so one dead end costs one thread rather than the run.
- **Reflection is a loop, not a flourish**: after extracting findings a researcher is asked what it still cannot answer, and those gaps become the next round's queries. Depth comes from reading the *right* second page, not from reading more first pages.
- **Evidence is stored before it is used**: every page read lands in `research_sources` with its full text. Findings cite sources by id, and a claim citing an id that does not exist is dropped rather than repaired.
- **Every claim is re-checked** against the source text by a separate pass that sees the claim and the evidence and nothing else — no question, no narrative to protect. `unsupported` and `contradicted` claims never reach the writer.
- **Your files are a first-class source**: indexed documents, past conversations and stored memories are searched alongside the web and cited the same way.
- **Three depths** — quick, standard, deep — and a full trace in the UI showing each researcher working in parallel.

### Carrot Agent
- **Drives a real browser** (`carrot/browser.py`) through the accessibility tree, not pixels. Every observation is a numbered list of the visible interactive elements, and every action names a number — so what the approval prompt says ("Click *Submit assignment*") is exactly what gets clicked.
- **One action per turn, always re-observed**: element numbers are re-derived after every action, so a page that changed underneath the agent produces a loud miss rather than a quiet wrong click.
- **Plan first**: the agent says what it intends to do and you approve *that*, not just a series of clicks whose shape becomes clear halfway through.
- **Two desktop tiers** (`carrot/desktop.py`): opening a file with its normal application is a bounded request the OS validates, and it is on by default behind an approval. Taking the mouse is unbounded, and it is off until you switch it on.
- **Failure is information**: a denial comes back to the model with its reason attached, so the agent routes around it instead of hammering the same button.

### The Policy Kernel
Everything both agents do passes through `carrot/policy.py`, which answers allow, ask, or refuse. It is the one component that never asks the model what it thinks.

- **Irreversible actions always ask.** Submit, upload, launch, run — "don't ask again" is not offered for any of them, and the server refuses to record one even if a client sends it.
- **Money and destruction need a typed phrase.** Anything that reads as a purchase, transfer, or account deletion is refused outright unless you have enabled it, and then the prompt requires typing `CONFIRM` rather than clicking a button. The button's own caption is what trips this — a model cannot describe its way past it.
- **The model never sees a credential.** Secrets live in a local vault keyed by name. The agent asks to type `secret:canvas`; the value is substituted at the keyboard layer and appears in no transcript, no audit row, and no screenshot. Typing one into a site that is not on your allowed list is refused, which is a phishing check as much as a policy one.
- **Untrusted text cannot escalate.** Page content is enveloped and screened for injection. A run that reads flagged content is *tainted*: it loses its remembered approvals, every subsequent action is confirmed individually, and the offending text is shown to you. The agent does not get to decide whether the attempt was serious.
- **The network boundary is real.** A URL that resolves to loopback, a private range, or link-local is refused, and every redirect hop is re-checked — an agent that can be talked into fetching `192.168.1.1/admin` is a router exploit with a chat interface.
- **Nothing runs forever.** Steps, wall-clock seconds, navigations and distinct domains are all capped, with a kill switch that takes effect before the next action.
- **Everything is on the record.** `agent_steps` holds every action proposed, what the policy decided and why, and what came back — secrets stripped before the row is written.

### Agent Tools
- **Built-in tools** (`carrot/agent_tools.py`) alongside MCP: read/write files, list directories, regex search, run commands, and search memory, documents and past conversations
- **Approval gate**: every mutating tool blocks until you allow or deny it, with "don't ask again this session" per tool
- **Undo journal**: file writes record their previous contents, so any agent edit can be reverted with its diff shown first

### Model Routing
- **Task-aware** (`carrot/router.py`): each call names its task (chat, code, reasoning, classify, summarize, extract, recap) and the router picks the provider and model
- **Bring your own key** (`carrot/providers.py`): Ollama on-device, plus Anthropic, OpenAI, Google (Gemini), or any endpoint speaking the OpenAI format — OpenRouter, Groq, Together, DeepSeek, Mistral, LM Studio, vLLM, your own server. Adding one is a name, a base URL and a key; nothing about it is special-cased.
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
- For Carrot Agent's browser control: `pip install playwright && python -m playwright install chromium` (optional; the Agent tab says what is missing rather than failing mid-task)

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
pip install 'carrot[browser]'  # Playwright, for Carrot Agent's browser control
pip install 'carrot[desktop]'  # pyautogui, for direct mouse and keyboard control
```

Browser control needs a one-time browser download after installing the extra:

```bash
python -m playwright install chromium
```

Without it, Carrot Research still works (it reads pages over HTTP) and the
Agent tab tells you what to install rather than failing partway through a task.

## What Carrot Agent Is Allowed To Do

Nothing, until you say so. The defaults are the cautious ones, and they are
changed in Settings — never by a prompt, and never by the agent itself.

| Setting | Default | What it controls |
| --- | --- | --- |
| Allowed sites | empty | Sites Carrot may visit without asking. It still asks for anything else, per run — an approval never writes to this list. |
| Stored credentials | empty | Values Carrot can type without the model seeing them. Only usable on allowed sites. |
| Desktop control | **off** | Whether Carrot may move the mouse and type directly. Every action asks, every time. |
| High-consequence actions | **off** | Whether purchases, transfers and deletions are possible at all. Even on, each one needs a typed `CONFIRM`. |
| Apps Carrot may launch | empty | Programs it can start. A name that is not listed is refused before anything is resolved on disk. |
| Budgets | 40 steps / 15 min / 30 navigations / 10 sites | Hard caps per run, with a kill switch that lands before the next action. |

Two things are refused outright and cannot be enabled: working around a CAPTCHA
or human-verification step, and downloading an executable.

## API Access

The API is gated behind a per-install session token. The web UI receives it
automatically; scripts and shortcuts can read it with `carrot token`:

```bash
curl -H "X-Carrot-Token: $(carrot token)" http://127.0.0.1:8181/api/status
```

## The Name

Carrot is named after the carrot and the rabbit — your rabbit assistant keeps you organized and motivated, and the carrot is the reward at the end of the work.