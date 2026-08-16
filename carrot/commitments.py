"""Noticing a commitment in a conversation, and asking before keeping it.

Carrot's reason to exist is continuity: you say something once, and three
months later in a different application it still knows. Goals are where that
is most obviously true and were where it least obviously worked — a goal was
something you went to a tab and typed, which means the only goals Carrot ever
had were the ones you thought to enter twice.

So chat proposes. You say "I need to finish the thesis by March", and a chip
appears under that reply: *finish the thesis — 12 Mar 2027*, with a tick. Tick
it and it becomes a goal and a memory, with provenance back to the sentence.
Ignore it and nothing is stored; dismiss it and nothing is stored *and* the
subject stops being offered, because a proposal declined and then made again
next week is worse than never proposing at all.

The hard part is not extraction. It is not chipping every wish.

"I should really learn Portuguese one day" is a wish. "I want to get fitter"
is a wish. Neither is a commitment and neither is checkable, and an assistant
that turns both into tracked goals has built a nagging list nobody agreed to —
which is how these features become something people switch off. The bar is
therefore two things at once: language that commits, and something a person
could later point at and say whether it happened. A date, a number, or a named
project.

That bar is enforced here in code and not only in the prompt. A local 4B asked
"is this a commitment?" says yes far more often than it should, and a rule
that lives only in a prompt is a rule that holds for the models you tested.
`_is_checkable` is the part that does not drift.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import goals as goals_mod

LOG = logging.getLogger(__name__)

# Proposals are cheap to ignore and expensive to be wrong about, so the model
# is asked for one thing at a time and given no room to editorialise.
PROPOSAL_SCHEMA = {
    "type": "object",
    "properties": {
        "is_commitment": {"type": "boolean"},
        "title": {"type": "string"},
        "subject": {"type": "string"},
        "deadline": {"type": "string"},
        "target": {"type": "string"},
    },
    "required": ["is_commitment"],
}

PROPOSAL_PROMPT = """You decide whether the user just committed to something trackable.

A commitment is something the user said they WILL do, with something checkable
attached: a date, a deadline, a number, or a named project or deliverable.

YES — these are commitments:
- "I need to finish the thesis by March 12th"  -> title: "Finish the thesis", deadline: "2027-03-12"
- "I'm going to run the half marathon in April" -> title: "Run the half marathon", deadline: "2027-04"
- "Ship v2 of the parser before the demo"       -> title: "Ship v2 of the parser", target: "before the demo"

NO — these are not, and you must answer is_commitment false:
- "I should really learn Portuguese one day"    (a wish, no date, no target)
- "I want to get fitter"                        (no way to check it)
- "Maybe I'll rewrite the backend at some point" (hedged, unscheduled)
- Anything the ASSISTANT suggested rather than the user committing to
- Anything phrased as a question

`title` is the commitment in the user's own words, imperative, under 80
characters. `subject` is two or three words naming what it is about — "thesis",
"half marathon", "parser rewrite" — used to avoid asking twice about the same
thing. `deadline` is ISO 8601 (YYYY-MM-DD, or YYYY-MM if only a month was
given) and empty if no date was stated. `target` is any other checkable thing
said, and empty otherwise.

Today is {today}. Resolve relative dates against it.
Answer with JSON only."""

# The words that carry commitment. A sentence without one of these is somebody
# thinking aloud, and thinking aloud is most of what people do in a chat.
_COMMITTING = re.compile(
    r"\b(i(?:'m| am)? (?:going to|gonna)|i (?:will|must|need to|have to|plan to|"
    r"intend to|promised)|by (?:the end of )?\w+|deadline|due|submit|ship|deliver|"
    r"finish|complete|hand in)\b",
    re.I,
)

# Hedges that cancel it however committing the rest of the sentence sounds.
_HEDGED = re.compile(
    r"\b(maybe|might|perhaps|someday|some day|one day|eventually|"
    r"i should really|thinking about|considering|would be nice|i wish)\b",
    re.I,
)

_ISO_DATE = re.compile(r"^\d{4}-\d{2}(-\d{2})?$")
MAX_TITLE = 80


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_checkable(proposal: Dict[str, Any]) -> bool:
    """Could a person later say whether this happened?

    The whole bar, in one function, deliberately away from the prompt. A
    deadline in a format nobody can compare against is not a deadline, so the
    date has to parse; a target has to be words rather than a shrug.
    """
    deadline = (proposal.get("deadline") or "").strip()
    if deadline and _ISO_DATE.match(deadline):
        return True
    target = (proposal.get("target") or "").strip()
    return len(target) >= 3


def looks_like_a_commitment(text: str) -> bool:
    """A cheap gate in front of the model.

    Most turns are not commitments, and asking a local model about every one of
    them costs a second inference on every message the user sends — which on a
    machine already running the answer is the difference between a chat that
    keeps up and one that does not. This is wrong in the safe direction: it
    lets through more than it should, and the model and `_is_checkable` refuse
    the rest.
    """
    if not text or len(text) > 2000:
        return False
    if _HEDGED.search(text):
        return False
    return bool(_COMMITTING.search(text))


def propose_from_turn(user_text: str, conversation_id: str = "",
                      message_id: str = "", model: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """A proposed goal from this turn, or None. Never raises.

    Stores nothing. A proposal is a question, and the answer is a tick.
    """
    from .ollama_client import OllamaClient

    if not looks_like_a_commitment(user_text or ""):
        return None
    if _subject_is_settled(user_text):
        return None

    try:
        client = OllamaClient()
        if not client.is_available():
            return None
        raw = client.structured_chat(
            [
                {"role": "system",
                 "content": PROPOSAL_PROMPT.format(today=datetime.now().strftime("%Y-%m-%d"))},
                {"role": "user", "content": user_text[:2000]},
            ],
            model=model,
            response_format=PROPOSAL_SCHEMA,
        )
        payload = json.loads(raw)
    except Exception:
        LOG.debug("commitment extraction failed", exc_info=True)
        return None

    if not isinstance(payload, dict) or not payload.get("is_commitment"):
        return None
    title = str(payload.get("title") or "").strip()[:MAX_TITLE]
    if not title:
        return None
    if not _is_checkable(payload):
        # The model said yes and the rule says no. The rule wins: this is the
        # case the prompt is worst at, and a tracked goal nobody can check is
        # the failure that makes people turn the feature off.
        return None

    subject = str(payload.get("subject") or "").strip().lower()[:60] or title.lower()[:60]
    if _subject_declined(subject):
        return None

    return goals_mod.propose(
        title=title,
        subject=subject,
        deadline=(payload.get("deadline") or "").strip(),
        target=(payload.get("target") or "").strip(),
        conversation_id=conversation_id or "",
        message_id=str(message_id or ""),
        source_text=user_text[:500],
    )


def _subject_declined(subject: str) -> bool:
    return subject in goals_mod.declined_subjects()


def _subject_is_settled(text: str) -> bool:
    """Skip a turn whose subject the user has already answered about.

    Cheaper than asking the model and then throwing the answer away, and it is
    the difference between "Carrot proposed this once" and "Carrot keeps
    bringing this up", which is the whole complaint people have about features
    like this one.
    """
    lowered = (text or "").lower()
    return any(subject and subject in lowered for subject in goals_mod.declined_subjects())


# ===== Progress =====
#
# "Finished chapter 3, moving to 4" is not a new commitment and it is not a
# question. It is a fact about a goal that already exists, and the manifesto
# for this feature is one word: cheap. It should cost nothing and ask nothing —
# no chip, no confirmation, no second inference — because a tracker that makes
# you confirm every step is one you stop telling things to.
#
# So it is matched, not classified. The subject of an open goal appearing in a
# sentence with progress language is enough, and the worst case is a note
# attached to the right goal that the user did not think of as an update. That
# is a line in a list. Getting it wrong in the other direction — asking — is
# what makes people close the feature.
_PROGRESS = re.compile(
    r"\b(finished|done with|completed|wrapped up|submitted|shipped|handed in|"
    r"moving on to|moving to|started|halfway|nearly done|almost done)\b",
    re.I,
)


def note_progress_from_turn(user_text: str) -> Optional[Dict[str, Any]]:
    """Attach a progress note to an open goal this sentence is plainly about.

    Returns the updated goal, or None. Never asks and never raises.
    """
    text = (user_text or "").strip()
    if not text or len(text) > 1000 or not _PROGRESS.search(text):
        return None
    lowered = text.lower()

    best = None
    for goal in goals_mod.by_status(goals_mod.STATUS_ACCEPTED):
        subject = (goal.get("subject") or "").strip().lower()
        # The subject has to actually appear. A goal is not "plainly about"
        # a sentence that merely sounds like progress — otherwise the first
        # open goal collects every "done!" in the conversation.
        if subject and len(subject) >= 3 and subject in lowered:
            # Longest match wins, so "thesis chapter" beats "thesis" when both
            # are goals and both appear.
            if best is None or len(subject) > len(best[0]):
                best = (subject, goal)
    if best is None:
        return None
    return goals_mod.note_progress(best[1]["id"], text)
