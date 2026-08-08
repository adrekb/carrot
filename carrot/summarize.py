"""Rolling conversation summaries.

Chat history used to be a hard ``messages[-20:]`` slice, so a long conversation
silently forgot its own beginning. Instead we keep a rolling summary of
everything older than the recent window and prepend it, which means a 500-turn
conversation still knows what it decided on turn 3.

The summary is incremental: each pass folds only the newly-aged-out messages
into the existing summary, so cost stays flat no matter how long the chat runs.
"""

from __future__ import annotations

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
    return {
        "conversation_id": row["conversation_id"],
        "summary": row["summary"],
        "covered_through": row["covered_through"],
        "message_count": row["message_count"],
        "updated_at": row["updated_at"],
    }


def save_summary(conversation_id: str, summary: str, covered_through: int, message_count: int):
    conn = get_db()
    conn.execute(
        """INSERT OR REPLACE INTO conversation_summaries
           (conversation_id, summary, covered_through, message_count, updated_at)
           VALUES (?, ?, ?, ?, ?)""",
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
