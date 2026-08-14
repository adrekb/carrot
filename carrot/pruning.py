"""Making room in a turn that is running out of it, instead of ending it.

When the transcript approached the model's context window, chat did one thing:
it took the tools away and told the model to answer from what it had. That is
right for a question — the reading is done, the answer is the point. It is
wrong for work. A coding turn that has read six files, run the tests and found
the failure hits the ceiling holding everything it needs and is told to stop
and write up what it could not get to. The turn ends one step from finished,
and the user's only remedy is a bigger model.

The observation that fixes it is that a transcript near the ceiling is not
full of things worth keeping. It is mostly tool output, and tool output is the
one part that can be thrown away safely, because it is *regenerable*: a file
the model read is still on disk, and re-reading it costs one round. What the
model reasoned, and what the user asked, cannot be recovered by any tool call
at all.

So the budgets are separate, and that separation is the whole design. Trimming
walks the tool results and leaves everything else alone; only if that is not
enough does it touch the assistant's own prose, and it never touches what the
user wrote. A single enormous file read cannot evict the request that led to
it — which is what one shared budget, walked from the back, does eventually do.

Two rules that are not negotiable:

**A tool message is trimmed, never removed.** Every `role: "tool"` entry
answers a `tool_call_id` in an assistant message before it, and a provider
handed a call with no result rejects the whole request. Dropping the message
would free the most space and break the turn, so content is replaced in place
and the envelope stays.

**No model call.** The obvious way to compress a transcript is to ask a model
to summarise it, and for a hosted agent that is affordable. Here it would put
a second inference — latency, and on a 4B local model a bad one — in the path
of a turn that is already in trouble, and it can fail, which a turn in trouble
cannot absorb. Truncation is free, cannot fail, and leaves a note saying what
went and how to get it back.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .context_windows import estimate_tokens

# Tool results kept whole no matter what, newest first.
#
# The most recent result is what the model is acting on this round: trimming it
# is not saving context, it is deleting the thing the next sentence is about.
# Two rather than one because a round that calls `read_file` and then
# `search_files` produces two results that belong to a single thought.
KEEP_RECENT_TOOL_RESULTS = 2

# How much of a trimmed result survives.
#
# Not zero. A result cut to nothing tells the model only that something was
# there, and a model that cannot see what a search returned will run it again —
# spending a round to rediscover what it just deleted. The head of a file
# listing or a search result is the part that identifies it, so this is enough
# to know whether it is worth re-reading.
TRIMMED_RESULT_CHARS = 400

# Assistant prose is trimmed harder, and only after every tool result has been.
# It is the model's own reasoning, which is worth more per character than a
# directory listing and much less than what the user said.
TRIMMED_PROSE_CHARS = 600

_NOTE = ("\n\n[… {dropped:,} characters trimmed to make room in the context "
         "window. The tool still works — call it again if you need the rest.]")

_PROSE_NOTE = "\n\n[… {dropped:,} characters of this earlier reply trimmed for room.]"


def _content(message: Dict[str, Any]) -> str:
    return str(message.get("content") or "")


def _trim(message: Dict[str, Any], keep: int, note: str) -> Tuple[Dict[str, Any], int]:
    """A copy of `message` with its content cut, and the tokens that frees."""
    content = _content(message)
    if len(content) <= keep:
        return message, 0
    shortened = content[:keep] + note.format(dropped=len(content) - keep)
    freed = estimate_tokens(content) - estimate_tokens(shortened)
    trimmed = dict(message)
    trimmed["content"] = shortened
    return trimmed, max(0, freed)


def tokens_in(messages: List[Dict[str, Any]]) -> int:
    """What this transcript costs, by the same estimate the loop meters with."""
    import json

    return estimate_tokens(json.dumps(messages, default=str))


def prunable_tokens(messages: List[Dict[str, Any]]) -> int:
    """What pruning would free, without doing it.

    The caller needs this before it decides between pruning and giving up: a
    transcript that is 95% full of the user's own long prompt has nothing to
    give, and telling the model "I made room" when nothing moved would buy one
    more round and hit the same wall.

    Measured by running the same trim rather than estimating it, so the two can
    never disagree — an estimate that forgets the note each trimmed message
    carries is optimistic by exactly the amount that matters near the ceiling.
    """
    total = 0
    for role, keep, note in (("tool", TRIMMED_RESULT_CHARS, _NOTE),
                             ("assistant", TRIMMED_PROSE_CHARS, _PROSE_NOTE)):
        for _, message in _trimmable(messages, role):
            total += _trim(message, keep, note)[1]
    return total


def _trimmable(messages: List[Dict[str, Any]], role: str):
    """Positions eligible for trimming, oldest first.

    Oldest first because recency is what the budget should buy — the same
    reasoning as the history budget, applied inside a single turn. The last
    message of either kind is never eligible: for a tool result it is what the
    model is working on, and for assistant prose it is the answer being
    written.
    """
    positions = [i for i, m in enumerate(messages) if m.get("role") == role
                 and len(_content(m)) > (TRIMMED_RESULT_CHARS if role == "tool"
                                         else TRIMMED_PROSE_CHARS)]
    keep_back = KEEP_RECENT_TOOL_RESULTS if role == "tool" else 1
    eligible = positions[:-keep_back] if keep_back and len(positions) > keep_back else []
    return [(i, messages[i]) for i in eligible]


def prune(messages: List[Dict[str, Any]], free_tokens: int) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Trim `messages` until roughly `free_tokens` have been recovered.

    Returns a new list and a report of what happened. Stops as soon as the
    target is met: pruning past what the turn needs throws away context for
    nothing, and the next round may not need any more.
    """
    out = list(messages)
    report = {"freed": 0, "tool_results": 0, "replies": 0, "target": free_tokens}
    if free_tokens <= 0:
        return out, report

    # Tool results first, and usually only. This is the ordering that makes
    # separate budgets mean anything: everything here is recoverable by calling
    # the tool again, and nothing below is.
    for index, message in _trimmable(out, "tool"):
        trimmed, freed = _trim(message, TRIMMED_RESULT_CHARS, _NOTE)
        if not freed:
            continue
        out[index] = trimmed
        report["freed"] += freed
        report["tool_results"] += 1
        if report["freed"] >= free_tokens:
            return out, report

    for index, message in _trimmable(out, "assistant"):
        trimmed, freed = _trim(message, TRIMMED_PROSE_CHARS, _PROSE_NOTE)
        if not freed:
            continue
        out[index] = trimmed
        report["freed"] += freed
        report["replies"] += 1
        if report["freed"] >= free_tokens:
            break

    return out, report
