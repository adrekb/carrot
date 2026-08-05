"""Git, as tools the coding agent can actually use.

Every good coding agent knows what it has changed. Cline shows a running diff,
Continue offers `@git diff` as context, Goose leans on the repo being version
controlled. All three assume the agent can see git; Carrot could only see it by
shelling out through the terminal, which is approval-gated and unstructured.

Two rules shape what is here:

* **Read is free, write is not.** status, diff, log and branch listing are
  read-only and run unattended. Commit, checkout and stage change repository
  state and go through the same approval path as any other mutating tool.
* **No shell.** Every call is an argument vector passed straight to git. A
  branch named ``; rm -rf ~`` is then a branch name and not a catastrophe.
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Dict, List, Optional

TIMEOUT_SECONDS = 30
MAX_OUTPUT_CHARS = 20000
# A commit is a record. Refusing an empty message is not pedantry — an agent
# that commits with "" produces history nobody can read later.
MIN_MESSAGE_CHARS = 3


class GitError(RuntimeError):
    pass


def git_available() -> bool:
    try:
        _run(["--version"], cwd=os.getcwd())
        return True
    except (GitError, OSError):
        return False


def is_repo(root: str) -> bool:
    try:
        out = _run(["rev-parse", "--is-inside-work-tree"], cwd=root)
        return out.strip() == "true"
    except (GitError, OSError):
        return False


def _run(args: List[str], cwd: str) -> str:
    """One git invocation. No shell, ever."""
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        raise GitError("git is not installed, or not on PATH")
    except subprocess.TimeoutExpired:
        raise GitError(f"git {args[0]} timed out after {TIMEOUT_SECONDS}s")
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout or "").strip()
        raise GitError(message or f"git {args[0]} failed ({proc.returncode})")
    out = proc.stdout or ""
    if len(out) > MAX_OUTPUT_CHARS:
        out = out[:MAX_OUTPUT_CHARS] + f"\n\n[truncated at {MAX_OUTPUT_CHARS} characters]"
    return out


def _require_repo(root: str) -> None:
    if not is_repo(root):
        raise GitError(
            "this workspace is not a git repository. Run `git init` in it first "
            "if you want the agent's changes tracked."
        )


# ===== Read-only =====

def status(root: str) -> Dict[str, Any]:
    """Branch, upstream position and the changed-file list, already parsed."""
    _require_repo(root)
    raw = _run(["status", "--porcelain=v1", "--branch"], cwd=root)
    branch, ahead, behind = "", 0, 0
    changes: List[Dict[str, str]] = []
    for line in raw.splitlines():
        if line.startswith("## "):
            head = line[3:]
            branch = head.split("...", 1)[0].strip()
            if "[ahead " in head:
                ahead = int(head.split("[ahead ", 1)[1].split("]", 1)[0].split(",")[0])
            if "behind " in head:
                behind = int(head.split("behind ", 1)[1].split("]", 1)[0].strip())
            continue
        if len(line) < 4:
            continue
        changes.append({"code": line[:2].strip() or "?", "path": line[3:]})
    return {
        "branch": branch,
        "ahead": ahead,
        "behind": behind,
        "changes": changes,
        "clean": not changes,
    }


def diff(root: str, path: str = "", staged: bool = False) -> str:
    """The working-tree diff, or one file's. This is what the agent reviews."""
    _require_repo(root)
    args = ["diff"]
    if staged:
        args.append("--cached")
    if path:
        args += ["--", path]
    out = _run(args, cwd=root)
    return out or ("(no staged changes)" if staged else "(no unstaged changes)")


def log(root: str, limit: int = 15) -> List[Dict[str, str]]:
    _require_repo(root)
    limit = max(1, min(int(limit or 15), 100))
    # A record separator that cannot occur in a subject line.
    raw = _run(
        ["log", f"-{limit}", "--pretty=format:%h\x1f%an\x1f%ar\x1f%s"], cwd=root
    )
    entries = []
    for line in raw.splitlines():
        parts = line.split("\x1f")
        if len(parts) == 4:
            entries.append({
                "sha": parts[0], "author": parts[1],
                "when": parts[2], "subject": parts[3],
            })
    return entries


def branches(root: str) -> Dict[str, Any]:
    _require_repo(root)
    raw = _run(["branch", "--format=%(refname:short)\x1f%(HEAD)"], cwd=root)
    names, current = [], ""
    for line in raw.splitlines():
        parts = line.split("\x1f")
        if not parts[0]:
            continue
        names.append(parts[0])
        if len(parts) > 1 and parts[1].strip() == "*":
            current = parts[0]
    return {"current": current, "branches": names}


# ===== Mutating =====

def stage(root: str, paths: Optional[List[str]] = None) -> str:
    _require_repo(root)
    args = ["add", "--"] + list(paths) if paths else ["add", "-A"]
    _run(args, cwd=root)
    return f"staged {', '.join(paths)}" if paths else "staged all changes"


def commit(root: str, message: str, paths: Optional[List[str]] = None) -> Dict[str, Any]:
    """Stage and commit. Refuses an empty message and an empty commit."""
    _require_repo(root)
    text = (message or "").strip()
    if len(text) < MIN_MESSAGE_CHARS:
        raise GitError("a commit needs a message describing the change")
    stage(root, paths)
    if not status(root)["changes"] and not _run(["diff", "--cached", "--name-only"], cwd=root).strip():
        raise GitError("nothing to commit — the working tree is clean")
    _run(["commit", "-m", text], cwd=root)
    head = log(root, 1)
    return {"committed": True, "message": text, "head": head[0] if head else None}


def create_branch(root: str, name: str, checkout: bool = True) -> str:
    """New branch off HEAD. The name is an argument, never shell text."""
    _require_repo(root)
    clean = (name or "").strip()
    if not clean or clean.startswith("-"):
        raise GitError("a branch needs a name")
    _run(["checkout", "-b", clean] if checkout else ["branch", clean], cwd=root)
    return f"created branch {clean}" + (" and switched to it" if checkout else "")


def checkout(root: str, name: str) -> str:
    _require_repo(root)
    clean = (name or "").strip()
    if not clean or clean.startswith("-"):
        raise GitError("name a branch to switch to")
    _run(["checkout", clean], cwd=root)
    return f"switched to {clean}"
