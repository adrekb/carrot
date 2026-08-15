"""Several read-only investigations of one codebase, run at the same time.

Understanding a project you have not seen is mostly breadth. "How does this
app work" means reading the entry point, the routes, the data layer and the
build config, and those four questions have nothing to say to each other
until they are all answered. Done in one conversation they are done in
sequence, each one's file dumps pushed into the same context window, and by
the fourth the first has been summarised into a sentence.

So they are handed to subagents: named investigations, run in parallel, each
with its own context and its own budget, each returning a written answer
rather than the pages it read. What comes back to the main agent is four
paragraphs instead of forty files.

Two rules make this safe rather than clever.

**They cannot change anything.** The tool list is reads and searches, and it
is a whitelist rather than a subtraction — a new mutating tool added anywhere
else in the app does not quietly become something four parallel agents can
call at once. Parallel writers would race on the same files, and no user
approving an edit could tell which of four agents was asking.

**They cannot spawn more of themselves.** Recursion here is not a runaway
loop, it is a runaway *fan-out*: three agents each spawning three is nine
conversations against a metered endpoint, from one sentence the user typed.
"""
from __future__ import annotations

import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from . import router as router_mod
from .config import get_config

# Reads and searches only, named explicitly. See the module docstring for why
# this is a whitelist and not "everything except the write tools".
SUBAGENT_TOOLS = (
    "read_file", "list_dir", "search_files", "search_documents",
    "search_memory", "search_conversations", "web_search", "read_url",
)

# Fan-out, not depth. Four is what fits on the screen as cards and what a
# free-tier rate limit survives.
MAX_SUBAGENTS = 4
# Each one gets few rounds on purpose: a subagent is for breadth, and one that
# needs eight rounds is a question that should have been given to the main
# agent whole.
MAX_ROUNDS = 4
# What comes back is a written answer. A subagent returning its transcript
# would put the forty files back into the context this exists to protect.
MAX_REPORT_CHARS = 4000

SUBAGENT_SYSTEM = (
    "You are one of several agents investigating a codebase at the same time. "
    "You have been given one specific question and read-only tools. Answer it "
    "from what you actually read: name files and functions, quote the lines "
    "that matter, and say plainly when you could not find something. Do not "
    "speculate about parts of the system you were not asked about — another "
    "agent has that question. End with a short answer to your question, not a "
    "summary of your search."
)


def _digest(name: str, task: str, report: str, error: str = "") -> str:
    head = f"### {name}\n_{task}_\n\n"
    if error:
        return head + f"(this investigation failed: {error})\n"
    return head + (report.strip()[:MAX_REPORT_CHARS] or "(found nothing)") + "\n"


def read_only_runner():
    """``(run_tool, tools)`` for an agent that may look but not touch.

    Shared with the scheduler, which needs exactly the same thing for exactly
    the same reason: an agent running with nobody watching it. Kept here
    because the whitelist and the enforcement belong together — a caller that
    took the tool list and wrote its own dispatcher would be one refactor
    away from losing the second check.
    """
    from . import agent_tools

    tools = agent_tools.ollama_tools(enabled=list(SUBAGENT_TOOLS))

    def run_tool(name: str, arguments: Dict[str, Any]) -> str:
        # Checked here as well as in the schema. A tool list is a suggestion:
        # a model will call a name it saw in training and was never offered,
        # and the one place that must not work is where nobody is watching.
        bare = str(name).split("__").pop()
        if bare not in SUBAGENT_TOOLS:
            return (f"error: {bare} is not available here. "
                    "You can read and search; you cannot change anything.")
        spec = agent_tools.TOOLS.get(bare)
        if not spec:
            return f"error: no tool called {bare}"
        try:
            return str(spec["handler"](**(arguments or {})))
        except TypeError:
            return agent_tools._bad_call_message(bare, spec, [])
        except Exception as exc:
            return f"error: {bare} failed: {exc}"

    return run_tool, tools


def run_one(name: str, task: str, run_tool: Callable, tools: List[Dict[str, Any]],
            emit: Callable, context_note: str = "", rounds: int = MAX_ROUNDS,
            deadline: Optional[float] = None) -> str:
    """One subagent, to its own budget. Returns its written answer.

    ``deadline`` is a ``time.monotonic()`` stamp, checked between rounds. A
    round already in flight is not interrupted — there is no safe way to kill
    a provider call mid-stream — so this bounds how long the agent keeps
    *starting* work, which is what an unattended run needs: something stuck at
    4am must not still be stuck at 4pm.
    """
    import time

    resolved = router_mod.route(task=router_mod.TASK_CODE)
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": SUBAGENT_SYSTEM},
        {"role": "user", "content": (f"{context_note}\n\n" if context_note else "")
         + f"Your question: {task}"},
    ]

    answer_parts: List[str] = []
    for _ in range(rounds):
        if deadline is not None and time.monotonic() > deadline:
            answer_parts.append(
                "(stopped: this run hit its time limit before finishing)")
            break
        calls: List[Dict[str, Any]] = []
        text: List[str] = []
        try:
            for event in router_mod.stream_events(resolved, messages, tools=tools):
                if event["type"] == "content":
                    text.append(event["text"])
                elif event["type"] == "tool_calls":
                    calls.extend(event["calls"])
        except Exception as exc:
            # One subagent failing is not the run failing. Its card says so
            # and the other three carry on — the whole point of splitting the
            # question up is that the parts are independent.
            return _digest(name, task, "", str(exc))

        content = "".join(text).strip()
        if content:
            answer_parts.append(content)
        if not calls:
            break

        messages.append({"role": "assistant", "content": content, "tool_calls": calls})
        for call in calls:
            function = call.get("function", {})
            tool_name = str(function.get("name", ""))
            arguments = function.get("arguments") or {}
            emit({"subagent_step": {"name": name, "tool": tool_name,
                                    "detail": str(arguments.get("path")
                                                  or arguments.get("query")
                                                  or arguments.get("pattern") or "")[:120]}})
            result = run_tool(tool_name, arguments)
            messages.append({"role": "tool", "content": str(result)[:6000],
                             "name": tool_name, "tool_call_id": call.get("id", tool_name)})

    return _digest(name, task, "\n\n".join(answer_parts))


def explore(jobs: List[Dict[str, str]], run_tool: Callable,
            tools: List[Dict[str, Any]], emit: Callable,
            context_note: str = "") -> str:
    """Run every named investigation at once and return them as one document.

    Events are drained onto the caller's channel as they happen, so the panel
    shows four agents working rather than one long pause — the same reason
    research streams its sub-questions instead of reporting at the end.
    """
    jobs = [j for j in jobs if str(j.get("task", "")).strip()][:MAX_SUBAGENTS]
    if not jobs:
        return "error: no investigations were given"

    events: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue()
    reports: Dict[int, str] = {}

    def work(index: int, job: Dict[str, str]):
        name = str(job.get("name") or f"Investigation {index + 1}").strip()[:60]
        task = str(job["task"]).strip()
        events.put({"subagent": {"name": name, "task": task, "state": "running"}})
        try:
            reports[index] = run_one(name, task, run_tool, tools, events.put, context_note)
            events.put({"subagent": {"name": name, "task": task, "state": "done"}})
        except Exception as exc:
            reports[index] = _digest(name, task, "", str(exc))
            events.put({"subagent": {"name": name, "task": task, "state": "failed",
                                     "detail": str(exc)[:200]}})

    pool = ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="carrot-subagent")
    futures = [pool.submit(work, index, job) for index, job in enumerate(jobs)]

    def watch():
        for future in futures:
            future.result()
        events.put(None)

    threading.Thread(target=watch, daemon=True, name="carrot-subagent-watch").start()
    while True:
        event = events.get()
        if event is None:
            break
        emit(event)
    pool.shutdown(wait=True)

    # In the order they were asked for, not the order they finished. The
    # agent wrote the list; reading it back shuffled is a small tax on every
    # answer that follows.
    return "\n\n".join(reports[index] for index in sorted(reports))


def enabled() -> bool:
    """Off for a local model by default.

    Four parallel conversations against one 8B on one GPU is not four times
    the work — it is the same work, serialised, with the queueing on top. The
    setting exists so a user with the hardware can say otherwise.
    """
    setting = get_config().get("subagents_enabled")
    if setting is not None:
        return bool(setting)
    try:
        return not router_mod.route(task=router_mod.TASK_CODE).local
    except Exception:
        return False
