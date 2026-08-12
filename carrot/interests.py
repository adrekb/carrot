"""What you have actually been asking about lately.

The recap was a news reader with an AI summary bolted on: four fixed RSS feeds,
a DuckDuckGo query hardcoded to "latest tech breakthroughs AI programming
science news", and one prompt telling the model to brief "a CS student". It was
the same briefing for everybody, and Carrot already knew enough to do better —
it just never looked.

This is the part that looks. It reads the conversations of the last few days
and the memories extracted from them, and works out what the person has been
*returning to*. Someone who has asked about aircraft four times this week and
cars twice has told Carrot what they are interested in far more reliably than
any settings page would, and without being asked to fill one in.

Two things it is careful about, because both are ways this gets creepy or
useless rather than helpful:

**Recurrence, not volume.** One long conversation about a tax form is not an
interest, it is an errand. A topic has to appear across *separate*
conversations before it counts, which is the difference between a thing you
keep thinking about and a thing you happened to spend Tuesday on.

**Interests are proposed, never asserted.** The output says "you have been
asking about X" with the evidence attached, and the recap says so too. An
assistant that silently decides what you care about and acts on it is
unnerving in a way one that shows its working is not.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .config import get_config
from .database import get_db

LOG = logging.getLogger(__name__)

# How far back to look. Long enough for a pattern to show up across days,
# short enough that last month's project does not dominate this week's
# briefing.
DEFAULT_DAYS = 7

# A topic has to appear in at least this many *distinct* conversations. One is
# an errand; two is a pattern. This is the single most important number in the
# module — at 1 the recap becomes a summary of whatever you did last, which is
# the thing you least need told back to you.
MIN_CONVERSATIONS = 2

MAX_TOPICS = 6
MAX_MESSAGES = 120

# Recap conversations are themselves stored as conversations. Reading them back
# would make the briefing self-reinforcing: yesterday's recap mentioned drones,
# so today's decides you are interested in drones, forever.
_EXCLUDED_PREFIX = "recap_"


def _since(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()


def recent_questions(days: int = DEFAULT_DAYS) -> List[Dict[str, Any]]:
    """The user's own messages from the last ``days``, newest first.

    Only `user` rows. The assistant's replies are far longer and would swamp
    any term count with Carrot's own vocabulary — you would end up with a
    briefing about the words Carrot likes to use.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT m.content, m.conversation_id, m.timestamp
                 FROM messages m
                WHERE m.role = 'user'
                  AND m.timestamp >= ?
                  AND m.conversation_id NOT LIKE ?
             ORDER BY m.timestamp DESC
                LIMIT ?""",
            (_since(days), f"{_EXCLUDED_PREFIX}%", MAX_MESSAGES),
        ).fetchall()
    except Exception:
        LOG.debug("could not read recent questions", exc_info=True)
        return []
    finally:
        conn.close()
    return [{"text": r["content"] or "", "conversation_id": r["conversation_id"],
             "at": r["timestamp"]} for r in rows]


def recent_memories(days: int = DEFAULT_DAYS, limit: int = 40) -> List[Dict[str, Any]]:
    """Durable facts learned recently — the structured half of the picture.

    A memory is worth more per row than a message: it survived extraction, so
    something already judged it durable. `project` and `preference` especially,
    which are the two kinds that describe what somebody is *doing*.
    """
    from . import memory as memory_mod

    try:
        rows = memory_mod.list_memories(limit=limit)
    except Exception:
        return []
    cutoff = _since(days)
    return [m for m in rows
            if (m.get("updated_at") or "") >= cutoff or m.get("pinned")]


# ===== Deriving topics =====

TOPIC_PROMPT = """Below are the questions someone has asked their assistant over the last {days} days, and some things it has learned about them.

Work out what they have been repeatedly interested in. You are looking for subjects a person would recognise as an interest of theirs — "military aircraft", "sports cars", "getting into grad school" — not the shape of the request ("comparisons", "definitions") and not the tool ("search", "coding").

QUESTIONS:
{questions}

WHAT IS KNOWN ABOUT THEM:
{memories}

Rules:
- Only include a subject that appears in at least two SEPARATE questions. One long thread about one thing is a task they were doing, not an interest.
- Ignore anything that is obviously an errand: a bug they were fixing, a form they were filling in, a one-off lookup.
- At most {limit} subjects, most-returned-to first.
- For each, write a `why` that quotes or closely paraphrases what they actually asked. This is shown to them, and a subject they cannot trace back to something they said reads as a guess about their personality.

Return JSON only:
{{"topics": [{{"topic": "military aircraft", "why": "asked about the F-35's radar cross-section and again about the F-22", "questions": ["a specific thing worth researching about this now"]}}]}}"""


def _clean(text: str) -> str:
    return " ".join((text or "").split())


def derive_topics(days: int = DEFAULT_DAYS, limit: int = MAX_TOPICS) -> Dict[str, Any]:
    """What this person has been coming back to, with the evidence.

    Returns ``{"topics": [...], "questions": n, "why": str}``. An empty topic
    list is a normal outcome — a fresh install, or a week of one-off errands —
    and the caller falls back to a general briefing rather than inventing an
    interest to have.
    """
    from . import research as research_mod, router as router_mod

    questions = recent_questions(days)
    if len(questions) < MIN_CONVERSATIONS:
        return {"topics": [], "questions": len(questions),
                "why": "not enough recent conversation to tell"}

    # The recurrence rule, enforced here rather than left to the prompt. A
    # model asked to "only include things mentioned twice" will cheerfully
    # include something mentioned once, and this is the property the whole
    # module rests on.
    conversations = {q["conversation_id"] for q in questions}
    if len(conversations) < MIN_CONVERSATIONS:
        return {"topics": [], "questions": len(questions),
                "why": "everything recent came from a single conversation"}

    listed = "\n".join(f"- {_clean(q['text'])[:220]}" for q in questions[:60])
    memories = recent_memories(days)
    memory_lines = "\n".join(
        f"- {m['kind']}: {_clean(m['content'])[:160]}" for m in memories[:20]
    ) or "(nothing recorded yet)"

    prompt = TOPIC_PROMPT.format(days=days, questions=listed,
                                 memories=memory_lines, limit=limit)
    try:
        raw = router_mod.complete(
            router_mod.route(task=router_mod.TASK_EXTRACT),
            [{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        LOG.info("could not derive interests: %s", exc)
        return {"topics": [], "questions": len(questions),
                "why": f"could not read your recent conversations: {exc}"}

    parsed = research_mod.extract_json(raw)
    topics: List[Dict[str, Any]] = []
    if isinstance(parsed, dict):
        for item in parsed.get("topics", [])[:limit]:
            if not isinstance(item, dict):
                continue
            name = _clean(item.get("topic", ""))
            if not 2 <= len(name) <= 60:
                continue
            asks = [_clean(q) for q in (item.get("questions") or []) if _clean(q)]
            topics.append({
                "topic": name,
                # Shown to the user. A subject they cannot trace back to
                # something they said reads as a guess about their personality
                # rather than an observation about their week.
                "why": _clean(item.get("why", ""))[:300],
                "questions": asks[:2],
            })

    return {
        "topics": topics,
        "questions": len(questions),
        "conversations": len(conversations),
        "why": "" if topics else "nothing recurred often enough to count as an interest",
    }


def topic_query(topic: Dict[str, Any]) -> str:
    """The research question for one interest.

    Uses the model's own suggested question when there is one — it was written
    knowing what the person actually asked — and falls back to a plain "what is
    new" otherwise. Not a search query: this goes to Carrot Research, which
    writes its own queries from the question.
    """
    for question in topic.get("questions") or []:
        if len(question) > 12:
            return question[:200]
    return f"What is new or notable about {topic['topic']} in the last week?"
