"""A run, as a shape rather than as a transcript.

The trace already tells you what happened; it does not tell you where the run
*went*. Scrolling a long agent session to answer "how many times did it read a
file", "which turn took the minute", "did it search before or after it edited"
means reading the whole thing and holding it in your head — and those are the
three questions anybody actually has about a run they did not watch.

So the same stored events, laid out as turns. Nothing new is recorded: every
assistant row already carries its trace (`TRACE_EVENTS` in app.py) and its
metrics (`_turn_metrics`), which is why this is an assembler and not a feature
with a schema.

Two decisions worth stating.

**A turn is a question and everything that answered it.** Not a message and not
a tool call — those are the units the transcript already has, and neither is
the thing you count. "Six turns, nineteen tool calls" is the sentence a
trajectory exists to produce.

**Durations come from the model's own counters, and are absent when it did not
run.** `_turn_metrics` reports nothing for a hosted model or a reply built
entirely from tool output. A turn with no number shows no number, because the
alternative — timing it from the row timestamps — measures the gap between two
writes to a database, which includes however long the browser was closed.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

# Two lengths for the same thing.
#
# The row is scanned: which tools ran, in which order, and did any fail. A
# result on that line is a hint, not a reading, so it is short enough that
# twenty rows still fit on a screen.
#
# The expansion is read. It carries as much as the trace actually kept — the
# recorder already clipped results to 400 characters — so opening a row shows
# everything there is rather than a second, longer summary of it.
MAX_RESULT_CHARS = 160
MAX_ARGS_CHARS = 120

# Thinking is the one field big enough to matter: a single turn may hold 12,000
# characters of it. Capped for the expansion because this is a panel you open
# to see the shape of a run, and a wall of reasoning in the middle of it hides
# the four tool calls underneath. The transcript is where you read it whole.
MAX_THINKING_CHARS = 2000


def _clip(text: Any, limit: int) -> str:
    text = " ".join(str(text if text is not None else "").split())
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


def _bare(name: str) -> str:
    """`carrot__read_file` is `read_file`. The namespace is plumbing."""
    return str(name or "").split("__")[-1]


def for_conversation(conversation: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Every turn in one conversation, with what it did and what it cost."""
    messages = (conversation or {}).get("messages") or []
    turns: List[Dict[str, Any]] = []
    pending_question: Optional[Dict[str, Any]] = None

    for message in messages:
        role = message.get("role")
        if role == "user":
            # A question with no answer yet is still a turn — a run that was
            # stopped, or is still going, is exactly the one you want to look at.
            pending_question = {
                "question": _clip(message.get("content"), 200),
                "at": message.get("timestamp") or "",
            }
            turns.append({**pending_question, "index": len(turns) + 1,
                          "steps": [], "tools": 0, "seconds": None,
                          "tokens": None, "model": ""})
            continue
        if role != "assistant":
            continue
        if not turns:
            # An assistant row with no question before it: a recap, a scheduled
            # run, anything the app started on its own. It gets a turn of its
            # own rather than being dropped, because it did happen.
            turns.append({"question": "", "at": message.get("timestamp") or "",
                          "index": len(turns) + 1, "steps": [], "tools": 0,
                          "seconds": None, "tokens": None, "model": ""})
        _fill_turn(turns[-1], message)

    totals = {
        "turns": len(turns),
        "tools": sum(turn["tools"] for turn in turns),
        "seconds": _sum(turn["seconds"] for turn in turns),
        "tokens": _sum(turn["tokens"] for turn in turns),
    }
    return {"turns": turns, "totals": totals}


def _sum(values) -> Optional[float]:
    """None when nothing reported, rather than a total of zero.

    Zero is a measurement and this is its absence — a run of hosted turns would
    otherwise claim to have taken no time at all.
    """
    kept = [v for v in values if isinstance(v, (int, float))]
    return round(sum(kept), 2) if kept else None


def _fill_turn(turn: Dict[str, Any], message: Dict[str, Any]) -> None:
    metadata = message.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata or "{}")
        except (TypeError, ValueError):
            metadata = {}
    metadata = metadata or {}

    for key in ("seconds", "tokens"):
        value = metadata.get(key)
        if isinstance(value, (int, float)):
            turn[key] = value
    if metadata.get("model"):
        turn["model"] = str(metadata["model"])

    pending_tool: Optional[Dict[str, Any]] = None
    for event in metadata.get("trace") or []:
        if not isinstance(event, dict):
            continue
        if "route" in event and isinstance(event["route"], dict):
            route = event["route"]
            turn["steps"].append({
                "kind": "route",
                "label": f"{route.get('provider', '')}/{route.get('model', '')}".strip("/"),
                "local": bool(route.get("local")),
            })
            if not turn["model"]:
                turn["model"] = str(route.get("model") or "")
        elif "thinking" in event:
            thought = str(event["thinking"] or "")
            turn["steps"].append({"kind": "thinking",
                                  "chars": len(thought),
                                  "detail": _clip(thought, MAX_THINKING_CHARS)})
        elif "plan" in event and isinstance(event["plan"], dict):
            plan = event["plan"]
            goals = [str(g) for g in (plan.get("goals") or [])]
            done = {str(g) for g in (plan.get("done") or [])}
            turn["steps"].append({
                "kind": "plan",
                "goals": len(goals),
                "done": len(done),
                # Which ones, not just how many. "2/3" tells you it did not
                # finish; this tells you what it did not finish.
                "detail": "\n".join(("[x] " if g in done else "[ ] ") + g for g in goals),
            })
        elif "tool" in event and isinstance(event["tool"], dict):
            tool = event["tool"]
            spelled = _args(tool.get("args"))
            pending_tool = {
                "kind": "tool",
                "name": _bare(tool.get("name")),
                "args": _clip(spelled, MAX_ARGS_CHARS),
                "args_full": spelled,
                "rejected": bool(tool.get("rejected")),
                "result": "",
                "result_full": "",
                "ok": None,
            }
            turn["steps"].append(pending_tool)
            if not tool.get("rejected"):
                turn["tools"] += 1
        elif "tool_result" in event and isinstance(event["tool_result"], dict):
            result = str(event["tool_result"].get("result") or "")
            # Paired by order, the same way the live stream pairs a card with
            # its result: the backend runs one tool at a time.
            if pending_tool is not None:
                pending_tool["result"] = _clip(result, MAX_RESULT_CHARS)
                # Newlines kept here and flattened in the row. A command's
                # output is its lines, and joining them into a paragraph is
                # what made the trace unreadable before the cards existed.
                pending_tool["result_full"] = result
                pending_tool["ok"] = not _failed(result)
                pending_tool = None
        elif "provider_error" in event or "error" in event:
            detail = event.get("provider_error") or event.get("error")
            if isinstance(detail, dict):
                detail = detail.get("message") or ""
            turn["steps"].append({"kind": "error", "detail": _clip(detail, MAX_RESULT_CHARS)})

    text = str(message.get("content") or "")
    if text.strip():
        turn["steps"].append({"kind": "answer", "chars": len(text),
                              "detail": _clip(text, MAX_THINKING_CHARS)})
    if metadata.get("interrupted"):
        turn["steps"].append({"kind": "stopped"})


def _args(args: Any) -> str:
    if not isinstance(args, dict):
        return ""
    return ", ".join(f"{key}={_clip(value, 40)}" for key, value in args.items())


def _failed(result: str) -> bool:
    """The same test the tool cards use, so a run reads the same in both."""
    text = result.strip()
    return text.startswith("error:") or text.startswith("[failed]") or (
        text.startswith("[exit ") and not text.startswith("[exit 0]"))
