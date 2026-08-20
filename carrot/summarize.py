"""Rolling conversation summaries.

Chat history used to be a hard ``messages[-20:]`` slice, so a long conversation
silently forgot its own beginning. Instead we keep a rolling summary of
everything older than the recent window and prepend it, which means a 500-turn
conversation still knows what it decided on turn 3.

The summary is incremental: each pass folds only the newly-aged-out messages
into the existing summary, so cost stays flat no matter how long the chat runs.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .database import get_db

# Turns kept verbatim at the end of the context window.
RECENT_WINDOW = 20
# Aged-out messages needed before a summarization pass runs.
SUMMARY_TRIGGER = 10
# Cap on summary length so it can never crowd out the live conversation.
MAX_SUMMARY_CHARS = 2000

SUMMARY_PROMPT = """You maintain a running summary of a conversation.

Merge the new messages into the existing summary. Preserve, in order of priority:
1. Decisions made and their stated reasons
2. Facts about the user, their projects, and their constraints
3. Open questions and unfinished work
4. Corrections — when the user overrode something, keep the final position only

Drop pleasantries, restated questions, and anything superseded. Write compact
prose under {max_chars} characters. Output only the summary text."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_summary(conversation_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM conversation_summaries WHERE conversation_id = ?", (conversation_id,)
    ).fetchone()
    conn.close()
    if row is None:
        return None
    keys = row.keys()
    return {
        "conversation_id": row["conversation_id"],
        "summary": row["summary"],
        "covered_through": row["covered_through"],
        "message_count": row["message_count"],
        "updated_at": row["updated_at"],
        # Read defensively: the columns arrive by migration, and a database
        # opened before `init_db` has run does not have them yet.
        "digest": row["digest"] if "digest" in keys else "",
        "digest_through": row["digest_through"] if "digest_through" in keys else 0,
        "digest_updated_at": row["digest_updated_at"] if "digest_updated_at" in keys else "",
    }


def save_summary(conversation_id: str, summary: str, covered_through: int, message_count: int):
    conn = get_db()
    # Upsert rather than INSERT OR REPLACE. The row now carries the .md digest
    # too, and REPLACE is a delete followed by an insert — so every rolling
    # summarisation pass would have silently thrown the digest away, which is
    # the kind of loss nobody notices until they press the button and get an
    # older document back than the one they read yesterday.
    conn.execute(
        """INSERT INTO conversation_summaries
           (conversation_id, summary, covered_through, message_count, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(conversation_id) DO UPDATE SET
               summary = excluded.summary,
               covered_through = excluded.covered_through,
               message_count = excluded.message_count,
               updated_at = excluded.updated_at""",
        (conversation_id, summary[:MAX_SUMMARY_CHARS], covered_through, message_count, _now()),
    )
    conn.commit()
    conn.close()


def _older_messages(conversation_id: str, covered_through: int) -> List[Dict[str, Any]]:
    """Messages past the recent window that the summary hasn't absorbed yet."""
    conn = get_db()
    rows = conn.execute(
        """SELECT id, role, content FROM messages
           WHERE conversation_id = ? ORDER BY id ASC""",
        (conversation_id,),
    ).fetchall()
    conn.close()
    aged_out = rows[:-RECENT_WINDOW] if len(rows) > RECENT_WINDOW else []
    return [
        {"id": r["id"], "role": r["role"], "content": r["content"]}
        for r in aged_out
        if r["id"] > covered_through
    ]


def maybe_summarize(conversation_id: str, model: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Fold newly-aged-out messages into the rolling summary, if enough exist."""
    from .ollama_client import OllamaClient

    existing = get_summary(conversation_id)
    covered_through = existing["covered_through"] if existing else 0
    pending = _older_messages(conversation_id, covered_through)
    if len(pending) < SUMMARY_TRIGGER:
        return existing

    client = OllamaClient()
    try:
        if not client.is_available():
            return existing
    except Exception:
        return existing

    transcript = "\n".join(f"{m['role']}: {m['content'][:1000]}" for m in pending)
    prior = existing["summary"] if existing else "(no summary yet)"
    try:
        summary = client.chat(
            [
                {"role": "system", "content": SUMMARY_PROMPT.format(max_chars=MAX_SUMMARY_CHARS)},
                {
                    "role": "user",
                    "content": f"Existing summary:\n{prior}\n\nNew messages:\n{transcript}",
                },
            ],
            model=model,
        )
    except Exception:
        return existing
    if not summary or not summary.strip():
        return existing

    save_summary(
        conversation_id,
        summary.strip(),
        covered_through=pending[-1]["id"],
        message_count=(existing["message_count"] if existing else 0) + len(pending),
    )
    return get_summary(conversation_id)


def build_history(conversation: Dict[str, Any], recent_window: int = RECENT_WINDOW) -> List[Dict[str, str]]:
    """Model-ready history: rolling summary (if any) plus the recent window."""
    history: List[Dict[str, str]] = []
    messages = conversation.get("messages", [])
    # A conversation that was never persisted has no id and so no summary.
    conversation_id = conversation.get("id")
    summary = get_summary(conversation_id) if conversation_id else None

    if summary and summary["summary"] and len(messages) > recent_window:
        history.append(
            {
                "role": "system",
                "content": (
                    "Summary of the earlier part of this conversation:\n"
                    f"{summary['summary']}"
                ),
            }
        )
    history += _within_budget(messages[-recent_window:])
    return history


# How much of the window the conversation may take.
#
# A count of messages is not a size. Twenty short exchanges are 18k characters
# and twenty long ones are 152k — measured on this database, where the biggest
# stored answer is 7,599 characters. At the top of that range the recent window
# alone is 116% of a 32k context, and a turn that reads three pages adds
# another 44%, so the request goes out at 160% of what the model can hold.
#
# Nothing errors when that happens. The provider silently drops the *front* of
# the prompt, which is where the system directive and the tool definitions
# live, and the model answers without either — the same failure as running in a
# 4k window, arriving from the other end.
#
# So the window is a budget as well as a count. Roughly a third of a 32k
# context, leaving room for the directive, the tools and the pages a turn
# reads.
HISTORY_BUDGET_CHARS = 40000
# Never drop below this many, however long they are. A single enormous answer
# must not be able to take the question that produced it out of the history.
MIN_RECENT_MESSAGES = 4


def _within_budget(messages: List[Dict[str, Any]],
                   budget: int = HISTORY_BUDGET_CHARS) -> List[Dict[str, str]]:
    """The newest messages that fit, oldest-first.

    Walked backwards from the newest because recency is what the budget should
    buy. Anything dropped is already covered by the rolling summary above.
    """
    kept: List[Dict[str, str]] = []
    spent = 0
    for message in reversed(messages):
        content = message["content"] or ""
        if kept and len(kept) >= MIN_RECENT_MESSAGES and spent + len(content) > budget:
            break
        kept.append({"role": message["role"], "content": content})
        spent += len(content)
    kept.reverse()
    return kept


def delete_summary(conversation_id: str):
    conn = get_db()
    conn.execute("DELETE FROM conversation_summaries WHERE conversation_id = ?", (conversation_id,))
    conn.commit()
    conn.close()
# ===== The conversation as a document =====
#
# A chat you had on Tuesday is context for the one you are having now, and the
# only way to carry it across was to scroll up, select a few hundred lines and
# paste them into the box. That is the wrong artefact twice over: a transcript
# is mostly the shape of the conversation rather than what it concluded, and
# pasting it spends the context window on prose the model has to re-derive the
# point from.
#
# So every conversation can produce a **document**: a short markdown file that
# says what the thread was about, what was decided and what is still open. It
# is attachable — it rides the ordinary attachment tray as a `.md`, the same
# route a note takes into the composer — which is the whole reason it is
# markdown with a filename rather than a paragraph in a popover.
#
# It is *not* the rolling summary above, and the difference is worth keeping
# straight because they share a row:
#
#   * the rolling summary is for the model. It covers only the turns that have
#     aged out of the recent window, it is plain prose, and it exists to stop a
#     long conversation forgetting its own beginning.
#   * the digest is for a person, and for a *different* conversation. It covers
#     the whole thread, it has headings, and nothing reads it unless somebody
#     attaches it.
#
# The rolling summary is used as *input* when one exists, which is what lets a
# 500-turn thread be digested without sending 500 turns to do it.

# Long enough to hold three sections, short enough that attaching it to another
# chat costs less than the transcript it replaces — which is the entire point.
MAX_DIGEST_CHARS = 6000
# What is read to write one.
DIGEST_TURNS = 60
DIGEST_MESSAGE_CHARS = 1200
DIGEST_TRANSCRIPT_CHARS = 24000

DIGEST_PROMPT = """You are writing a short reference document about a conversation, for someone who will attach it to a *different* conversation later instead of pasting the transcript.

Write markdown. Use these three sections, in this order, and no others:

## What this was about
One short paragraph.

## What was decided
Bullets. Decisions, conclusions and facts established, with the reason where one was given. Omit the section entirely if nothing was decided.

## Still open
Bullets. Questions left unanswered and work left unfinished. Omit the section entirely if nothing is open.

Do not write a title or a top-level heading — one is added for you. Do not describe the conversation as a conversation ("the user asked", "the assistant explained"); state what is true. Invent nothing that is not in the transcript. Stay under {max_chars} characters."""


def _digest_row(conversation_id: str) -> Dict[str, Any]:
    existing = get_summary(conversation_id) or {}
    return {
        "markdown": existing.get("digest") or "",
        "covered_through": existing.get("digest_through") or 0,
        "updated_at": existing.get("digest_updated_at") or "",
    }


def _conversation_messages(conversation_id: str) -> List[Dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        """SELECT id, role, content FROM messages
           WHERE conversation_id = ? AND role IN ('user', 'assistant')
           ORDER BY id ASC""",
        (conversation_id,),
    ).fetchall()
    conn.close()
    return [{"id": r["id"], "role": r["role"], "content": r["content"] or ""} for r in rows]


def save_digest(conversation_id: str, markdown: str, covered_through: int):
    conn = get_db()
    conn.execute(
        """INSERT INTO conversation_summaries
           (conversation_id, summary, covered_through, message_count, updated_at,
            digest, digest_through, digest_updated_at)
           VALUES (?, '', 0, 0, ?, ?, ?, ?)
           ON CONFLICT(conversation_id) DO UPDATE SET
               digest = excluded.digest,
               digest_through = excluded.digest_through,
               digest_updated_at = excluded.digest_updated_at""",
        (conversation_id, _now(), markdown[:MAX_DIGEST_CHARS], covered_through, _now()),
    )
    conn.commit()
    conn.close()


def digest_filename(title: str) -> str:
    """A filename a person recognises in an attachment tray.

    The conversation's own title, reduced to what is safe on every filesystem.
    Not the id: a tray showing `c8f31a02.md` is a tray you cannot read.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    return f"{slug[:60] or 'conversation'}.md"


def _digest_header(title: str, count: int, model: str) -> str:
    """Provenance, written by us rather than by the model.

    Who wrote it and over how much is a fact about the file, and a file that
    says it summarises 24 messages is one you can decide whether to trust. Left
    to the model it comes out differently every time, and sometimes not at all.
    """
    when = f"{datetime.now(timezone.utc):%d %B %Y}".lstrip("0")
    written = model or "no model — written from the transcript"
    return (f"# {title or 'Untitled conversation'}\n\n"
            f"*Summary of {count} message{'' if count == 1 else 's'} · "
            f"{written} · {when}*")


def _one_line(text: str, limit: int) -> str:
    collapsed = " ".join(str(text or "").split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit].rstrip() + "…"


def _digest_without_a_model(messages: List[Dict[str, Any]]) -> str:
    """The transcript's own shape, when there is no model to summarise it.

    The same reasoning as the resume brief's fallback: this runs on machines
    where the local model is exactly the thing that is unavailable, and a button
    that answers "Ollama is not running" is a button that gets pressed once.
    What can always be said — what was asked, and where it got to — is most of
    what an attached summary is for.
    """
    asked = [m for m in messages if m["role"] == "user"]
    answered = [m for m in messages if m["role"] == "assistant"]
    parts = ["## What was asked", ""]
    if asked:
        parts += [f"- {_one_line(m['content'], 200)}" for m in asked[:8]]
    else:
        parts.append("- (nothing yet)")
    if answered:
        parts += ["", "## Where it got to", "", f"> {_one_line(answered[-1]['content'], 600)}"]
    parts += ["", "*Written from the transcript without a model, so it lists what was "
                  "said rather than what it amounted to.*"]
    return "\n".join(parts)


def _strip_own_title(text: str) -> str:
    """Drop a leading ``# …`` the model wrote anyway.

    It is asked not to, and mostly does not; when it does, the file opens with
    two titles that disagree, and the wrong one is the one the model invented.
    """
    lines = text.split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and re.match(r"^#\s+\S", lines[0]):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    return "\n".join(lines).strip()


def digest_state(conversation_id: str, title: str = "") -> Dict[str, Any]:
    """What the summary button should say, without writing anything.

    ``stale`` is the load-bearing field: a document that was true forty turns
    ago and says nothing about it is worse than no document, because it is the
    one you would attach.
    """
    messages = _conversation_messages(conversation_id)
    stored = _digest_row(conversation_id)
    latest = messages[-1]["id"] if messages else 0
    return {
        "conversation_id": conversation_id,
        "title": title,
        "filename": digest_filename(title),
        "markdown": stored["markdown"],
        "updated_at": stored["updated_at"],
        "covers": stored["covered_through"],
        "messages": len(messages),
        "exists": bool(stored["markdown"]),
        "stale": bool(stored["markdown"]) and latest > stored["covered_through"],
    }


def build_digest(conversation_id: str, title: str = "",
                 route=None, complete=None) -> Dict[str, Any]:
    """Write the conversation's document and store it.

    ``route`` and ``complete`` are injected so the caller decides which model
    writes it — the app passes the router's, and a caller that passes neither
    gets the deterministic version, which is a real code path rather than a
    stub for one.
    """
    messages = _conversation_messages(conversation_id)
    if not messages:
        raise ValueError("That conversation has nothing in it yet.")

    # The rolling summary is the beginning of a long thread, already condensed.
    # Using it here is what keeps a 500-turn conversation from having to be sent
    # in full to describe itself.
    rolling = get_summary(conversation_id) or {}
    earlier = (rolling.get("summary") or "").strip()
    recent = messages[-DIGEST_TURNS:]
    lines = [f"{m['role']}: {m['content'][:DIGEST_MESSAGE_CHARS]}" for m in recent]
    transcript = "\n".join(lines)[-DIGEST_TRANSCRIPT_CHARS:]

    body = ""
    model_name = ""
    if route is not None and complete is not None:
        prompt = (f"Conversation: {title or 'untitled'}\n\n"
                  + (f"Summary of the earlier part:\n{earlier}\n\n" if earlier else "")
                  + f"Transcript:\n{transcript}")
        try:
            written = complete(route, [
                {"role": "system", "content": DIGEST_PROMPT.format(max_chars=MAX_DIGEST_CHARS)},
                {"role": "user", "content": prompt},
            ])
        except Exception:
            written = ""
        if written and written.strip():
            body = _strip_own_title(written.strip())
            model_name = getattr(route, "model", "") or ""
    if not body:
        body = _digest_without_a_model(messages)

    markdown = f"{_digest_header(title, len(messages), model_name)}\n\n{body}"
    save_digest(conversation_id, markdown, covered_through=messages[-1]["id"])
    return digest_state(conversation_id, title=title)
