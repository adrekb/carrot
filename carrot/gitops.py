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
import re
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


def _run(args: List[str], cwd: str, env: Optional[Dict[str, str]] = None) -> str:
    """One git invocation. No shell, ever."""
    try:
        proc = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
            env={**os.environ, **env} if env else None,
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


# ===== Checkpoints, backed by git's own object store =====
#
# A checkpoint wants to be atomic, cheap, and complete. Git already provides
# exactly that: a tree object is a content-addressed snapshot of the whole
# worktree, and restoring one is two plumbing commands. Copying files into a
# database duplicates work git has already done far better.
#
# The one hazard is the index. `git add -A` against the user's real index would
# quietly stage their work, so every checkpoint operation runs against a private
# index file under .carrot/ via GIT_INDEX_FILE. The user's staged changes are
# never touched.

CHECKPOINT_DIR = ".carrot"
CHECKPOINT_INDEX = "checkpoint.index"


def checkpoint_index_path(root: str) -> str:
    folder = os.path.join(root, CHECKPOINT_DIR)
    os.makedirs(folder, exist_ok=True)
    # Carrot's own state is not part of the user's project. Without this the
    # private index would be swept into the snapshot by `add -A`, and into the
    # user's next commit by `stage` — a tool leaving its scratch files in
    # someone's history is unforgivable.
    ignore = os.path.join(folder, ".gitignore")
    if not os.path.exists(ignore):
        try:
            with open(ignore, "w", encoding="utf-8") as handle:
                handle.write("# Carrot's checkpoint state — not part of your project.\n*\n")
        except OSError:
            pass
    return os.path.join(folder, CHECKPOINT_INDEX)


def _private_index(root: str) -> Dict[str, str]:
    return {"GIT_INDEX_FILE": checkpoint_index_path(root)}


def write_tree(root: str) -> Dict[str, Any]:
    """Snapshot the worktree as a git tree object, without staging anything.

    Returns the tree sha and the HEAD it was taken from. Both are needed:
    the tree restores file contents, and HEAD tells the UI where the run
    started from.
    """
    _require_repo(root)
    env = _private_index(root)
    # Seed the private index from HEAD when there is one, so the snapshot is a
    # diff against real history rather than a from-scratch add.
    try:
        _run(["read-tree", "HEAD"], cwd=root, env=env)
    except GitError:
        pass  # A repo with no commits yet: an empty index is correct.
    _run(["add", "-A", "--", "."], cwd=root, env=env)
    tree = _run(["write-tree"], cwd=root, env=env).strip()
    head = ""
    try:
        head = _run(["rev-parse", "HEAD"], cwd=root).strip()
    except GitError:
        pass
    return {"tree": tree, "head": head}


def restore_tree(root: str, tree: str) -> Dict[str, Any]:
    """Put the worktree back to a tree, and delete anything created since.

    `checkout-index -a -f` rewrites every tracked file; `clean -fd` removes the
    ghost files a nine-step rabbit hole leaves behind. Without the second step
    "restore" would mean "mostly restore", which is what makes people stop
    trusting undo.
    """
    _require_repo(root)
    if not re.fullmatch(r"[0-9a-f]{7,64}", tree or ""):
        raise GitError("that does not look like a checkpoint")
    env = _private_index(root)
    before = {c["path"] for c in status(root)["changes"]}
    _run(["read-tree", tree], cwd=root, env=env)

    # A file the OS will not let go of stops `checkout-index` dead, and the
    # first version let that escape as a raw git error — after some files had
    # already been rewritten. So "restore" could leave the tree half-way and
    # report only `unable to unlink old 'x'`, which is the opposite of the
    # promise above. It happens for real: a SQLite database open in another
    # program cannot be replaced on Windows.
    #
    # So the failure is caught, the rest of the restore is still attempted,
    # and the files that could not be written are named. A partial restore the
    # user knows about is recoverable; a partial restore reported as a git
    # error is what makes people stop trusting undo.
    blocked: List[str] = []
    try:
        _run(["checkout-index", "-a", "-f"], cwd=root, env=env)
    except GitError as exc:
        blocked = _unwritable_paths(str(exc))
        if not blocked:
            raise
    try:
        # Never clean the checkpoint state itself, and never touch .git.
        _run(["clean", "-fd", "-e", CHECKPOINT_DIR], cwd=root, env=env)
    except GitError as exc:
        blocked += [p for p in _unwritable_paths(str(exc)) if p not in blocked]

    after = {c["path"] for c in status(root)["changes"]}
    return {"tree": tree, "reverted": sorted(before - after),
            "remaining": sorted(after), "blocked": sorted(set(blocked))}


def _unwritable_paths(message: str) -> List[str]:
    """The files git said it could not replace, out of its error output.

    Only these two shapes, and only when git actually named a path: anything
    else is a different failure and has to keep propagating rather than being
    quietly downgraded to "some files were skipped".
    """
    return re.findall(r"unable to (?:unlink old|write file|create file) '([^']+)'",
                      message)


def tree_files(root: str, tree: str) -> List[str]:
    """What a checkpoint contains, for the panel to show a count."""
    _require_repo(root)
    raw = _run(["ls-tree", "-r", "--name-only", tree], cwd=root)
    return [line for line in raw.splitlines() if line]


# ===== Worktrees =====
#
# A second checkout of the same repository, on its own branch, in its own
# directory. The reason it belongs in a coding assistant: "try this refactor"
# and "keep working" are the same directory otherwise, so the agent's edits
# land on top of whatever you had open, and undoing them means undoing yours
# too. In a worktree the agent has a whole checkout to itself, shares the
# object database, and costs a directory rather than a clone.

def worktrees(root: str) -> List[Dict[str, str]]:
    """Every checkout of this repository, main one first.

    Parsed from the porcelain form because the human-readable one puts the
    path, the commit and the branch on one line with no delimiter, and paths
    on Windows contain spaces.
    """
    _require_repo(root)
    found: List[Dict[str, str]] = []
    current: Dict[str, str] = {}
    for line in _run(["worktree", "list", "--porcelain"], cwd=root).splitlines():
        if not line.strip():
            if current:
                found.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            current = {"path": os.path.abspath(value), "branch": "", "detached": False}
        elif key == "branch":
            current["branch"] = value.replace("refs/heads/", "")
        elif key == "detached":
            current["detached"] = True
    if current:
        found.append(current)
    return found


def add_worktree(root: str, branch: str, path: str = "") -> Dict[str, str]:
    """A new checkout on a new branch, beside the repository by default.

    Beside rather than inside: a worktree in a subdirectory of its own
    repository is a directory git ignores but every other tool walks, so the
    indexer, the file tree and any test runner would all see two copies of
    the project and report every result twice.
    """
    _require_repo(root)
    branch = (branch or "").strip()
    if not branch:
        raise GitError("a worktree needs a branch name")
    # Git refuses these anyway, but its message is about ref formats and this
    # one is about what the user typed.
    if any(ch in branch for ch in " ~^:?*[\\") or branch.startswith("-"):
        raise GitError(r"a branch name cannot contain spaces or ~^:?*[\ or start with -")

    parent = os.path.dirname(os.path.abspath(root))
    folder = os.path.basename(os.path.abspath(root))
    target = os.path.abspath(path or os.path.join(
        parent, f"{folder}-{branch.replace('/', '-')}"))
    if os.path.exists(target):
        raise GitError(f"{target} already exists")

    _run(["worktree", "add", "-b", branch, target], cwd=root)
    return {"path": target, "branch": branch, "detached": False}


def remove_worktree(root: str, path: str) -> Dict[str, Any]:
    """Drop a worktree, refusing while it still holds uncommitted work.

    `--force` is not offered here. The whole point of working in one is that
    the work in it is real, and a one-click button that discards it is a
    button somebody presses on the wrong row.
    """
    _require_repo(root)
    target = os.path.abspath(path)
    if os.path.abspath(root) == target:
        raise GitError("that is the checkout you are working in")
    try:
        _run(["worktree", "remove", target], cwd=root)
    except GitError as exc:
        if "contains modified or untracked files" in str(exc):
            raise GitError(
                "that worktree still has uncommitted changes. Commit them, or "
                "delete the folder yourself if you meant to throw them away.")
        raise
    return {"removed": target}
