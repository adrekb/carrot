"""Long-running processes the agent starts and does not wait for.

``run_command`` runs a command to completion with a timeout, which is right
for a test run or a git command and useless for the thing a coding agent most
needs to do: start the app and look at it. ``npm run dev`` never exits. Run
through ``run_command`` it produced sixty seconds of nothing, timed out, was
killed, and the agent concluded the project would not start — the one failure
mode where the tool reports the opposite of the truth.

So a dev server is a different kind of thing here. It is started, it keeps
running after the tool call returns, its output is collected in the
background, and the URL it prints is picked out of that output and handed
back. What the user gets is a link to their own app; what the agent gets is
the log, which is where the stack trace is.

Three things this has to get right or it is worse than not having it:

**Nothing outlives Carrot.** A dev server holding port 5173 after the app has
closed is a process the user did not start, cannot see and has to hunt down
in Task Manager. Every child is killed at exit, and killed as a tree — a
shell that spawned node leaves node behind if you only kill the shell.

**The output is never allowed to grow without limit.** A watch-mode bundler
logs on every keystroke. The buffer is a ring, so a server left running all
day costs a fixed amount of memory and keeps the part anyone reads: the end.

**Starting one is as dangerous as running any other command**, because it is
running any other command. The tool that exposes this goes through the same
approval gate as ``run_command``; nothing here decides that for itself.
"""
from __future__ import annotations

import atexit
import os
import re
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# A ring per server. Two thousand lines is enough to hold a stack trace and a
# startup banner together, which is what anyone actually reads.
MAX_LOG_LINES = 2000

# More than this many at once is a runaway, not a workflow. The agent that
# starts a server on every turn because it forgot the last one is the case
# this is here for.
MAX_SERVERS = 6

_servers: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ===== Finding the URL =====
#
# Read out of the output rather than guessed from the command, because the
# command is frequently `npm run dev` and the port lives in a config file
# nobody passed on the command line. Every dev server in wide use prints the
# URL on startup; that is the one thing they agree on.

_URL_RE = re.compile(
    r"https?://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(?::\d+)?(?:/\S*)?",
    re.I)
# Django's `Starting development server at`, and anything else that prints a
# bare port. Deliberately narrow: a bare `:3000` in a stack trace is a line
# number, so a port is only believed when something nearby says it is one.
_PORT_RE = re.compile(r"(?:port|listening on|running (?:at|on))\D{0,12}(\d{2,5})", re.I)


def find_url(line: str) -> str:
    """The address a line is announcing, or ``""``.

    0.0.0.0 is rewritten to localhost: it means "every interface" to the
    server and is not a thing a browser can usefully open.
    """
    match = _URL_RE.search(line)
    if match:
        url = match.group(0).rstrip(".,;)\"'")
        return url.replace("0.0.0.0", "localhost").replace("[::1]", "localhost")
    port = _PORT_RE.search(line)
    if port and 1 <= int(port.group(1)) <= 65535:
        return f"http://localhost:{port.group(1)}"
    return ""


# ===== Killing =====

def _kill_tree(process: subprocess.Popen) -> None:
    """Kill the process and everything it started.

    `npm run dev` is a shell that spawns node. Killing the shell leaves node
    holding the port, and the next start fails with EADDRINUSE on a server
    the user cannot see — which reads as Carrot breaking their project.
    """
    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)],
                           capture_output=True, timeout=15)
        else:
            os.killpg(os.getpgid(process.pid), 15)
    except Exception:
        pass
    try:
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _popen(command: str, cwd: str) -> subprocess.Popen:
    # Python block-buffers stdout when it is a pipe rather than a terminal, so
    # a Flask or Django server printed nothing at all until it exited — which,
    # for a server, is never. The URL never arrived, the log stayed empty, and
    # the only symptom was a card that said "started but has not printed an
    # address yet" about a server that had been serving for a minute.
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    kwargs: Dict[str, Any] = {"env": env}
    if os.name == "nt":
        # Its own process group, so the tree can be killed without taking the
        # Carrot process with it.
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(
        command, shell=True, cwd=cwd,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, errors="replace", **kwargs,
    )


# ===== Starting, watching, stopping =====

def start(command: str, cwd: str, label: str = "") -> Dict[str, Any]:
    """Start a command in the background and begin collecting its output."""
    with _lock:
        _reap()
        if len([s for s in _servers.values() if s["running"]]) >= MAX_SERVERS:
            return {"error": f"{MAX_SERVERS} servers are already running — stop one first"}

    try:
        process = _popen(command, cwd)
    except Exception as exc:
        return {"error": f"could not start: {exc}"}

    server_id = str(uuid.uuid4())[:8]
    record: Dict[str, Any] = {
        "id": server_id,
        "command": command,
        "label": label or command,
        "cwd": cwd,
        "pid": process.pid,
        "url": "",
        "started_at": _now(),
        "exit_code": None,
        "running": True,
        "_process": process,
        "_log": deque(maxlen=MAX_LOG_LINES),
    }

    def pump():
        """Drain the pipe on its own thread.

        Not optional plumbing: a pipe nobody reads fills its OS buffer and the
        child blocks on its next print. A server that had logged a few
        kilobytes would simply stop serving, which is indistinguishable from
        it having crashed.
        """
        try:
            for line in process.stdout:
                line = line.rstrip("\n")
                record["_log"].append(line)
                if not record["url"]:
                    found = find_url(line)
                    if found:
                        record["url"] = found
        except Exception:
            pass
        finally:
            record["running"] = False
            record["exit_code"] = process.poll()

    threading.Thread(target=pump, daemon=True,
                     name=f"carrot-server-{server_id}").start()

    with _lock:
        _servers[server_id] = record
    return public(record)


def wait_for_url(server_id: str, timeout: float = 20.0) -> Dict[str, Any]:
    """Give the server a moment to announce itself before answering.

    Returning immediately would hand back a record with no URL every time,
    since no server prints its banner in the microsecond after spawning, and
    the agent's next move would be to start a second one.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        record = _servers.get(server_id)
        if not record:
            return {"error": "no such server"}
        if record["url"] or not record["running"]:
            break
        time.sleep(0.2)
    return get(server_id) or {"error": "no such server"}


def logs(server_id: str, lines: int = 80) -> Dict[str, Any]:
    record = _servers.get(server_id)
    if not record:
        return {"error": "no such server"}
    tail = list(record["_log"])[-max(1, lines):]
    return {**public(record), "log": "\n".join(tail)}


def stop(server_id: str) -> Dict[str, Any]:
    record = _servers.get(server_id)
    if not record:
        return {"error": "no such server"}
    _kill_tree(record["_process"])
    record["running"] = False
    record["exit_code"] = record["_process"].poll()
    return public(record)


def stop_all() -> int:
    """Every child, killed. Registered at exit and used by the tests."""
    stopped = 0
    for record in list(_servers.values()):
        if record["running"]:
            _kill_tree(record["_process"])
            record["running"] = False
            stopped += 1
    return stopped


def _reap() -> None:
    """Notice the ones that have exited on their own."""
    for record in _servers.values():
        if record["running"] and record["_process"].poll() is not None:
            record["running"] = False
            record["exit_code"] = record["_process"].poll()


def public(record: Dict[str, Any]) -> Dict[str, Any]:
    """The record without the handles — safe to serialise to the UI."""
    return {key: value for key, value in record.items() if not key.startswith("_")}


def get(server_id: str) -> Optional[Dict[str, Any]]:
    record = _servers.get(server_id)
    return public(record) if record else None


def list_servers() -> List[Dict[str, Any]]:
    with _lock:
        _reap()
        return [public(record) for record in _servers.values()]


atexit.register(stop_all)
