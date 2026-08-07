"""Help, and a tutorial that checks rather than tells.

Carrot has grown enough features that "what can this do" is a real question,
and enough consequential ones — an agent that drives a browser, a vault holding
your credentials — that a user who has not read anything is a user who will be
surprised. So there are two things here.

**Help** is reference material: one topic per feature, each written to answer
the question a person actually has ("what does turning search off do?"), and
searchable across titles and body text so it can back a `?` command as well as
a tab.

**The tutorial is stateful.** Every step names a check that runs against the
live install — is Ollama up, does a workspace exist, has a folder been indexed,
has a site been allowed — so the checklist reflects what you have actually
done rather than what you have clicked past. A tour that says "✓ Done" because
you pressed Next teaches nothing; one that stays unticked until the thing is
true is a to-do list you can trust.

Steps degrade honestly: a check that cannot run (no Ollama, no database yet)
reports "unknown" rather than failing the step, because a tutorial that shows a
red cross for something it could not measure is worse than one that admits it.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

# ===== Help topics =====
#
# `body` is markdown. Keep each topic answerable on its own — someone arriving
# from search reads one topic, not the page around it.

TOPICS: List[Dict[str, Any]] = [
    {
        "id": "what-is-carrot",
        "section": "Getting started",
        "title": "What Carrot is",
        "summary": "A local-first assistant that remembers, searches your files, and can act.",
        "body": """
Carrot runs on your machine. Your conversations, notes, goals and files stay
there, and by default so does the model — Ollama serves it locally, with no
account and no API key.

What that buys you is an assistant that can be given real access. It reads the
folders you point it at, remembers what you tell it, drives a browser to finish
tasks, and researches questions against sources it can cite. None of that
requires trusting a third party with your data, because none of it leaves.

If you *want* a hosted model for some things, add a key in Settings and assign
it per task — a frontier model for hard reasoning, something cheap for
classification, everything else on-device.
""",
    },
    {
        "id": "workspaces",
        "section": "Organising",
        "title": "Workspaces and folders",
        "summary": "Group a project's chats, memories and files — and scope search to them.",
        "body": """
A **workspace** is one project's context: its chats, memories, files, notes and
runs. A **folder** groups workspaces, and folders can nest.

    School/
      Thesis        ← chats, memories, files, notes
      CS 3110
    Personal/
      Fitness

A workspace is a *scope*, not a container. Nothing moves on disk and nothing is
copied. When a workspace is active:

- new chats, notes, research and agent runs are filed into it automatically
- search, memory recall and document lookup are restricted to what is in it
- memories extracted from a chat inherit that chat's workspace

That last point is the reason to bother. Without it, a question about your
thesis can recall a decision you made about a side project in March. With it,
Carrot only knows what is relevant to where you are.

**All workspaces** is the default and stays the default. A fresh install has no
workspaces and behaves exactly as it did before you made one.

Deleting a workspace never deletes its contents — the items simply become
unfiled. Deleting a folder moves its workspaces to the top level.
""",
    },
    {
        "id": "chat-search-modes",
        "section": "Chatting",
        "title": "Search modes",
        "summary": "Off, single-pass, or multi-turn — how far a chat turn reaches.",
        "body": """
The picker beside the model name controls whether a chat turn can reach the web.

- **No search** — Carrot answers from the conversation, your indexed files and
  its memory. The web tools are *removed*, not just discouraged: an instruction
  not to search is a request, but a tool that is not there cannot be called.
- **Search** — one pass. It may search and read a page when the question needs
  something current, and cites the URL for what it takes.
- **Multi-turn search** — it searches, reads, works out what it still cannot
  answer, and searches again. It can also hand the whole question to Carrot
  Research when a written, cited report is what you actually want.

Your choice applies to the turn and is saved as the default, so turning search
off for a private conversation keeps it off.
""",
    },
    {
        "id": "memory",
        "section": "Chatting",
        "title": "How memory works",
        "summary": "Durable facts, traceable to the message they came from.",
        "body": """
After each turn Carrot asks the model to extract durable facts — preferences,
decisions, projects, commitments — and stores them as rows you can read.

Three rules make it auditable:

- **Provenance is mandatory.** Every memory links back to the message it came
  from. A memory with no source cannot be checked, so it is not stored. It also
  records where it was learned — an ordinary chat, the Code tab, a document you
  sent, or your own hand — and the Memory tab filters on that, and on the
  workspace the conversation belonged to.
- **Supersede, never overwrite.** A new value marks the old one superseded and
  links forward, so "what did I used to think about X" still works.
- **You are the authority.** Edit, pin, or reject anything in the Memory tab.
  A rejected subject is not extracted again.
""",
    },
    {
        "id": "files",
        "section": "Organising",
        "title": "Indexing your files",
        "summary": "Point Carrot at folders; it reads PDFs, markdown, code and saved pages.",
        "body": """
Add folders under **Your Files**. Carrot walks them, extracts text from
markdown, code, HTML and PDF, chunks it, and indexes it into the same search
that covers your conversations and memories.

Indexing is incremental — an unchanged tree costs one `stat` per file — and
entirely local. Nothing is uploaded.

Documents are filed into whichever workspace is active when they are first
indexed, so indexing a course folder while "CS 3110" is active puts them where
you would expect.
""",
    },
    {
        "id": "doc-to-agent",
        "section": "Notes",
        "title": "Sending a note to the model",
        "summary": "Write the plan in a note; the note is the prompt.",
        "body": """
Type `@` in a note to insert a reference. There are three kinds:

- `@/file/path` — cite a file. It is read *at send time*, so the model sees the
  actual file rather than your description of it.
- `@/model/provider/id` — pick the provider and model for this note.
- `@/to/destination` — say where the note goes: `chat`, `research/deep`,
  `agent/browser`, and so on.

Then press Send. The chips under the note show what resolved, how big it is,
and where it is going, before anything runs.

Sent to **Research**, the files you cited become *evidence* — they take the
first citation numbers and every sub-question researcher reads them. Sent to
the **Agent**, they ride along as background.
""",
    },
    {
        "id": "research",
        "section": "Research and agents",
        "title": "Carrot Research",
        "summary": "Sub-questions researched in parallel, every claim checked against its source.",
        "body": """
Ask a question worth a few minutes. Carrot breaks it into sub-questions, sends
an independent researcher after each one across the web *and* your own indexed
files, then does the part that matters: it re-checks every claim against the
source text it came from before writing anything.

Claims the sources do not support are dropped, not softened. What you get back
is a report where every sentence carries a citation you can open, plus a
"What is still open" section for the parts it could not answer.

**Depths.** Quick is two threads and one round; standard is four and two; deep
is six and three. Depth buys follow-up rounds — reading the *right* second page
— not just more first pages.

If a source tries to give the agent instructions, that is reported in the
report's Caveats rather than obeyed.
""",
    },
    {
        "id": "agent",
        "section": "Research and agents",
        "title": "Carrot Agent",
        "summary": "Give it a task; it drives a real browser, asking before anything irreversible.",
        "body": """
Describe a task — "open my CS 3110 Canvas page, find assignment 4, tell me what
it wants and when it's due" — and Carrot works through it in a real browser.

It shows you its plan before it starts. It takes one action at a time and
re-reads the page after each one, so a page that changed underneath it produces
a loud miss rather than a wrong click. It never works from pixel coordinates:
it sees a numbered list of the visible interactive elements, which is why the
approval prompt can tell you exactly which button it is about to press.

**What it will not do**, whatever you ask: work around a CAPTCHA, download an
executable, or type a password the model produced. See "What the agent is
allowed to do" for the rest.
""",
    },
    {
        "id": "agent-safety",
        "section": "Research and agents",
        "title": "What the agent is allowed to do",
        "summary": "Nothing, until you say so — and some things not even then.",
        "body": """
Everything either agent does passes through one policy layer that answers
allow, ask, or refuse. It never asks the model what it thinks.

**Always asks, and cannot be silenced.** Submitting a form, uploading, running
a command, launching a program — "don't ask again" is not offered for anything
irreversible, and the server refuses to record one even if asked.

**Needs a typed phrase.** Anything that reads as a purchase, transfer or
account deletion is refused unless you enable it, and then requires typing
`CONFIRM`. This matches on the *button's own caption*, so a model cannot
describe its way past it.

**Never sees your credentials.** Store them under Stored credentials in the
Agent tab. Carrot types them straight into the page; the value never enters the
model's context, the transcript, or the audit log. It refuses to type one into
a site that is not on your allowed list — which is a phishing check as much as
a policy one.

**Loses privileges on a hostile page.** If a page tries to instruct the agent,
the run is *tainted*: remembered approvals are dropped, everything after is
confirmed individually, and you are shown the text that did it.

**Cannot reach your local network.** A URL resolving to loopback or a private
range is refused, re-checked on every redirect.

**Stops.** Every run is capped on steps, time, navigations and distinct sites,
with a Stop button that lands before the next action.

**Two things it refuses outright**, which no setting turns on: working around a
CAPTCHA or human-verification step, and downloading an executable. If a task
needs a CAPTCHA solved, take the window over, solve it, and tell Carrot to
continue.

Off by default and changed only in Settings: desktop mouse/keyboard control,
high-consequence actions, the list of sites it may visit, and the apps it may
launch.
""",
    },
    {
        "id": "extensions",
        "section": "Extending",
        "title": "Packs, skills and MCP",
        "summary": "Turn on a whole kind of work; add your own instructions and tools.",
        "body": """
**Packs** bundle tools, skills and settings for one kind of work. The Academia
Pack covers LaTeX authoring and compilation, BibTeX checking, MATLAB/Octave,
CAD figures, image-to-LaTeX, and venue formatting rules. A pack lists the
external programs its tools want and probes for each, so the Extensions tab
tells you what works on *this* machine — and a tool whose program is missing
refuses up front instead of failing halfway.

**Skills** are instruction packs you invoke with `/` in the command bar. A
pack's skills are written into your ordinary skills, so you can edit the
wording if the house style is not quite right.

**MCP servers** add external tools over stdio. They appear to the chat agent
alongside the built-ins and go through the same approval gate.
""",
    },
    {
        "id": "models",
        "section": "Models",
        "title": "Choosing and routing models",
        "summary": "Local by default; bring a key and assign a model per task.",
        "body": """
Carrot names a *task* for every call — chat, code, reasoning, classify,
summarize, extract, recap, research, agent — and routes each to a model.

Out of the box everything runs on Ollama. Add a provider key in Settings and
you can pin any task to any model: a frontier model for research, something
cheap for classification, everything else on-device. An explicit assignment
always beats the automatic rules.

**Auto**, at the top of the model picker, is the one that decides *for* you.
It reads the message, decides whether it is chat, code or a reasoning problem,
and then routes that task exactly as the table above would — so your
assignments still apply. Every turn says which task it read and why, and
picking a model by name turns it off again.

Keys are stored locally and reduced to booleans by the config API — a saved key
is never readable over HTTP. Every chat turn reports which provider and model
served it.
""",
    },
    {
        "id": "privacy",
        "section": "Getting started",
        "title": "What leaves your machine",
        "summary": "By default: web searches and pages you ask for. Nothing else.",
        "body": """
With the default setup — Ollama, no provider keys — the only outbound traffic
is what you explicitly ask for: a web search, a page fetched by Research or the
Agent, and the RSS feeds in your recap.

Your conversations, memories, notes, goals and indexed files are never
uploaded. There is no account and no telemetry.

If you add a provider key, calls routed to that provider go to it — the chat
header names the provider serving each turn, so this is never a surprise.

The API binds to `127.0.0.1` and requires a session token that is injected into
Carrot's own page. Another site open in your browser cannot read it, so it
cannot call the API.
""",
    },
    {
        "id": "shortcuts",
        "section": "Getting started",
        "title": "Keyboard shortcuts",
        "summary": "Ctrl+K to focus, / for skills, @ for references, Alt+Space anywhere.",
        "body": """
| Shortcut | Does |
| --- | --- |
| `Ctrl+K` / `Cmd+K` | Focus the command bar |
| `/` in the command bar | Skill picker |
| `@` in a note | Insert a file, model, or destination reference |
| `Alt+Space` | Drop the overlay over any app (desktop build) |
| `Enter` | Send |
""",
    },
    {
        "id": "backup",
        "section": "Getting started",
        "title": "Backup and restore",
        "summary": "One archive holding the database, notes, skills and config.",
        "body": """
Settings → Backup exports the database, your notes, skills, briefings and
config into a single archive you own. The database is snapshotted with SQLite's
backup API, so an export taken while Carrot is running is consistent.

Restoring replaces the current instance and takes a safety copy first. Archives
with unsafe paths or an unreadable database are refused.
""",
    },
]

SECTIONS = ["Getting started", "Chatting", "Organising", "Notes",
            "Research and agents", "Extending", "Models"]


# A bullet marker is the character *followed by a space*. Matching a bare "*"
# would treat every paragraph opening with **bold** as a list and leave its
# hard wraps in place.
_BLOCK_PREFIXES = ("|", "#", "    ", "\t")
_BLOCK_MARKERS = ("- ", "* ", "+ ", "> ")
_ORDERED_ITEM = re.compile(r"^\d+[.)]\s")


def unwrap(body: str) -> str:
    """Join lines inside a paragraph, leaving lists, tables and code alone.

    The source is wrapped at a readable width, but the renderer treats a single
    newline as a line break, so an unmodified body arrives with breaks mid
    sentence. Rewrapping here rather than writing long lines keeps this file
    editable.
    """
    blocks = []
    for block in (body or "").strip().split("\n\n"):
        lines = block.split("\n")
        structured = any(
            line.startswith(_BLOCK_PREFIXES)
            or line.startswith(_BLOCK_MARKERS)
            or _ORDERED_ITEM.match(line)
            for line in lines
        )
        blocks.append(block if structured else " ".join(line.strip() for line in lines))
    return "\n\n".join(blocks)


def topics() -> List[Dict[str, Any]]:
    """Every topic, with its body unwrapped for rendering."""
    return [{**topic, "body": unwrap(topic["body"])} for topic in TOPICS]


def get_topic(topic_id: str) -> Optional[Dict[str, Any]]:
    return next((topic for topic in topics() if topic["id"] == topic_id), None)


def search_topics(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    """Rank topics by where the query matches: title beats summary beats body."""
    terms = [term for term in re.findall(r"\w+", (query or "").lower()) if term]
    if not terms:
        return topics()

    scored = []
    for topic in topics():
        title = topic["title"].lower()
        summary = topic["summary"].lower()
        body = topic["body"].lower()
        score = 0
        for term in terms:
            if term in title:
                score += 10
            if term in summary:
                score += 4
            if term in body:
                score += 1
        if score:
            scored.append((score, topic))
    scored.sort(key=lambda pair: -pair[0])
    return [topic for _, topic in scored[:limit]]


# ===== The tutorial =====
#
# Each step names a check that runs against the live install. `done` is derived,
# never stored, so a step un-ticks itself if you undo the thing.


def _check_ollama() -> Dict[str, Any]:
    try:
        from .ollama_client import OllamaClient

        client = OllamaClient()
        if client.is_available():
            return {"done": True, "detail": "Ollama is running"}
        return {"done": False, "detail": "Ollama is not reachable yet"}
    except Exception as exc:
        return {"done": None, "detail": f"could not check: {exc}"}


def _check_first_chat() -> Dict[str, Any]:
    try:
        from .database import get_db

        conn = get_db()
        count = conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
        conn.close()
        return {
            "done": count > 0,
            "detail": f"{count} message{'' if count == 1 else 's'} so far",
        }
    except Exception as exc:
        return {"done": None, "detail": f"could not check: {exc}"}


def _check_workspace() -> Dict[str, Any]:
    try:
        from . import workspaces

        spaces = workspaces.list_workspaces()
        if not spaces:
            return {"done": False, "detail": "no workspaces yet"}
        active = workspaces.active_workspace()
        where = f"active: {active['name']}" if active else "none active — showing everything"
        return {"done": True, "detail": f"{len(spaces)} workspace(s), {where}"}
    except Exception as exc:
        return {"done": None, "detail": f"could not check: {exc}"}


def _check_indexed() -> Dict[str, Any]:
    try:
        from . import indexer

        stats = indexer.stats()
        count = stats.get("documents", 0)
        return {
            "done": count > 0,
            "detail": f"{count} document(s) indexed" if count else "no folders indexed yet",
        }
    except Exception as exc:
        return {"done": None, "detail": f"could not check: {exc}"}


def _check_memory() -> Dict[str, Any]:
    try:
        from . import memory

        count = memory.stats().get("by_status", {}).get("active", 0)
        return {
            "done": count > 0,
            "detail": f"{count} thing(s) remembered" if count else "nothing remembered yet",
        }
    except Exception as exc:
        return {"done": None, "detail": f"could not check: {exc}"}


def _check_research() -> Dict[str, Any]:
    try:
        from . import research

        runs = research.list_runs(limit=1)
        return {
            "done": bool(runs),
            "detail": f"last: {runs[0]['question'][:60]}" if runs else "no research runs yet",
        }
    except Exception as exc:
        return {"done": None, "detail": f"could not check: {exc}"}


def _check_agent_ready() -> Dict[str, Any]:
    """Ready means both halves: a browser to drive and a site it may visit."""
    try:
        from . import browser, policy

        available = browser.is_available()["available"]
        domains = policy.allowed_domains()
        if not available:
            return {"done": False, "detail": "Playwright is not installed"}
        if not domains:
            return {"done": False, "detail": "browser ready; no sites allowed yet"}
        return {"done": True, "detail": f"{len(domains)} site(s) allowed"}
    except Exception as exc:
        return {"done": None, "detail": f"could not check: {exc}"}


def _check_backup() -> Dict[str, Any]:
    try:
        from . import backup

        archives = backup.list_backups()
        return {
            "done": bool(archives),
            "detail": f"{len(archives)} backup(s)" if archives else "no backup taken yet",
        }
    except Exception as exc:
        return {"done": None, "detail": f"could not check: {exc}"}


STEPS: List[Dict[str, Any]] = [
    {
        "id": "engine",
        "title": "Get the engine running",
        "body": "Carrot installs Ollama for you on first launch and pulls its model. "
                "Until that finishes, anything that needs the model will wait.",
        "action": {"label": "Open Settings", "tab": "settings"},
        "check": _check_ollama,
    },
    {
        "id": "first-chat",
        "title": "Say something",
        "body": "Ask Carrot anything from the command bar. The picker beside the model "
                "name decides whether it may search the web for the answer.",
        "action": {"label": "Open chat", "tab": "workspace"},
        "check": _check_first_chat,
        "topic": "chat-search-modes",
    },
    {
        "id": "workspace",
        "title": "Make a workspace",
        "body": "Group one project's chats, memories and files. While it is active, "
                "new work is filed there and search only looks inside it — which is "
                "what stops an old side project from surfacing in a thesis question.",
        "action": {"label": "Open Workspaces", "tab": "workspaces"},
        "check": _check_workspace,
        "topic": "workspaces",
    },
    {
        "id": "index",
        "title": "Point Carrot at your files",
        "body": "Add a folder of papers, notes or code. Carrot indexes it locally and "
                "searches it alongside your conversations. Nothing is uploaded.",
        "action": {"label": "Open Your Files", "tab": "files"},
        "check": _check_indexed,
        "topic": "files",
    },
    {
        "id": "memory",
        "title": "Let it remember something",
        "body": "Tell Carrot a preference or a decision. It stores durable facts with a "
                "link back to the message they came from, and you can read, edit or "
                "delete any of them.",
        "action": {"label": "Open Memory", "tab": "memory"},
        "check": _check_memory,
        "topic": "memory",
    },
    {
        "id": "research",
        "title": "Run a research question",
        "body": "Ask something worth a few minutes. Carrot researches sub-questions in "
                "parallel and checks every claim against the source it came from before "
                "writing the report.",
        "action": {"label": "Open Research", "tab": "research"},
        "check": _check_research,
        "topic": "research",
    },
    {
        "id": "agent",
        "title": "Set up the agent",
        "body": "Install browser control, then allow the sites Carrot may visit. It asks "
                "before anything irreversible and never sees your passwords — read what "
                "it is allowed to do before the first run.",
        "action": {"label": "Open Agent", "tab": "agent"},
        "check": _check_agent_ready,
        "topic": "agent-safety",
    },
    {
        "id": "backup",
        "title": "Take a backup",
        "body": "Everything lives on your machine, which means nobody else is keeping a "
                "copy. One archive holds the database, notes, skills and config.",
        "action": {"label": "Open Settings", "tab": "settings"},
        "check": _check_backup,
        "topic": "backup",
    },
]


def tutorial() -> Dict[str, Any]:
    """Every step with its live status.

    A check that raises returns ``done: None`` — "unknown" — rather than
    failing the step, because a red cross for something that could not be
    measured is worse than admitting the measurement failed.
    """
    steps = []
    for step in STEPS:
        try:
            result = step["check"]()
        except Exception as exc:
            result = {"done": None, "detail": f"could not check: {exc}"}
        steps.append({
            "id": step["id"],
            "title": step["title"],
            "body": step["body"],
            "action": step.get("action"),
            "topic": step.get("topic"),
            "done": result.get("done"),
            "detail": result.get("detail", ""),
        })

    completed = sum(1 for step in steps if step["done"] is True)
    return {
        "steps": steps,
        "completed": completed,
        "total": len(steps),
        "next": next((step["id"] for step in steps if step["done"] is not True), None),
    }
