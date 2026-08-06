"""Sandboxed file API backing the Code tab (Monaco editor).

All paths are resolved inside a configurable workspace root; anything that
escapes the root is rejected.
"""
import os
import shutil
import subprocess

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

from carrot.config import get_config, set_config

router = APIRouter(prefix="/api/files", tags=["files"])

MAX_FILE_BYTES = 2 * 1024 * 1024
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", "dist", ".qoder"}


class WriteRequest(BaseModel):
    path: str
    content: str


class PathRequest(BaseModel):
    path: Optional[str] = ""


class RootRequest(BaseModel):
    root: str


# Where to send someone who does not have the toolchain. Official download
# pages only — pointing a beginner at a random mirror is how they end up with
# something else entirely.
LANGUAGE_HELP = {
    "Python": "https://www.python.org/downloads/",
    "JavaScript": "https://nodejs.org/en/download",
    "TypeScript": "https://nodejs.org/en/download",
    "Java": "https://adoptium.net/temurin/releases/",
    "C": "https://gcc.gnu.org/install/",
    "C++": "https://gcc.gnu.org/install/",
    "Go": "https://go.dev/dl/",
    "Rust": "https://rustup.rs/",
    "Ruby": "https://www.ruby-lang.org/en/downloads/",
    "C#": "https://dotnet.microsoft.com/download",
    "Kotlin": "https://kotlinlang.org/docs/command-line.html",
    "Swift": "https://www.swift.org/install/",
    "PHP": "https://www.php.net/downloads",
    "Lua": "https://www.lua.org/download.html",
    "Perl": "https://www.perl.org/get.html",
}


class CreateRequest(BaseModel):
    path: str                      # parent directory, "" for the root
    name: str
    is_dir: bool = False


class RenameRequest(BaseModel):
    path: str
    new_name: str


class MoveRequest(BaseModel):
    path: str
    dest_dir: str                  # "" for the root


class DeleteRequest(BaseModel):
    path: str


def get_root() -> str:
    root = get_config().get("code_workspace_dir", "")
    if not root:
        root = os.path.join(os.path.expanduser("~"), "CarrotProjects")
    root = os.path.abspath(root)
    os.makedirs(root, exist_ok=True)
    return root


def resolve(rel_path: str, *, must_exist: bool = False) -> str:
    """A path inside the workspace root, or a 403.

    realpath, not abspath: abspath resolves "..", but it does not follow
    symlinks, so a link inside the workspace pointing at /etc would pass a
    prefix check and then be read or written through. The root is resolved the
    same way so a symlinked root (common on macOS, where /tmp is /private/tmp)
    still compares equal.
    """
    root = os.path.realpath(get_root())
    candidate = os.path.join(root, rel_path or "")
    # Resolve the deepest existing ancestor: a file being created does not
    # exist yet, but the directory it lands in must still be inside the root.
    probe = candidate
    while not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    real_parent = os.path.realpath(probe)
    if real_parent != root and not real_parent.startswith(root + os.sep):
        raise HTTPException(status_code=403, detail="path escapes the workspace root")
    full = os.path.join(real_parent, os.path.relpath(candidate, probe)) \
        if candidate != probe else real_parent
    full = os.path.normpath(full)
    if full != root and not full.startswith(root + os.sep):
        raise HTTPException(status_code=403, detail="path escapes the workspace root")
    if must_exist and not os.path.exists(full):
        raise HTTPException(status_code=404, detail="not found")
    return full


def rel_of(full: str) -> str:
    return os.path.relpath(full, os.path.realpath(get_root())).replace(os.sep, "/")


def _reject_reserved(name: str):
    """Names that would escape or corrupt the tree if taken literally."""
    if not name or name in (".", ".."):
        raise HTTPException(status_code=400, detail="invalid name")
    if "/" in name or "\\" in name or "\x00" in name:
        raise HTTPException(status_code=400, detail="a name cannot contain a path separator")


@router.get("/root")
async def files_root():
    return {"root": get_root()}


@router.post("/root")
async def set_files_root(req: RootRequest):
    root = os.path.abspath(os.path.expanduser(req.root))
    os.makedirs(root, exist_ok=True)
    set_config("code_workspace_dir", root)
    return {"root": root}


@router.get("/tree")
async def files_tree(path: str = ""):
    full = resolve(path)
    if not os.path.isdir(full):
        raise HTTPException(status_code=404, detail="directory not found")
    entries = []
    for name in sorted(os.listdir(full)):
        if name in SKIP_DIRS or name.startswith("."):
            continue
        child = os.path.join(full, name)
        rel = os.path.relpath(child, get_root()).replace(os.sep, "/")
        entries.append({
            "name": name,
            "path": rel,
            "is_dir": os.path.isdir(child),
            "size": os.path.getsize(child) if os.path.isfile(child) else 0,
        })
    entries.sort(key=lambda e: (not e["is_dir"], e["name"].lower()))
    return {"path": path, "entries": entries}


@router.get("/read")
async def files_read(path: str):
    full = resolve(path)
    if not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="file not found")
    if os.path.getsize(full) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="file too large to edit here")
    try:
        with open(full, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        raise HTTPException(status_code=415, detail="binary file")
    return {"path": path, "content": content}


@router.post("/write")
async def files_write(req: WriteRequest):
    full = resolve(req.path)
    if os.path.isdir(full):
        raise HTTPException(status_code=409, detail="that path is a directory")
    # The read side refuses anything over MAX_FILE_BYTES, so accepting a larger
    # write would create a file the editor can no longer open.
    encoded = req.content.encode("utf-8")
    if len(encoded) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="file too large to save here")
    os.makedirs(os.path.dirname(full) or get_root(), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(req.content)
    return {"path": req.path, "size": len(encoded)}


@router.post("/create")
async def files_create(req: CreateRequest):
    """Make a new file or folder. The Code tab's "New file" / "New folder".

    Also reports whether the toolchain for the new file's language is present.
    Finding out that Python is not installed *after* writing a program is a
    bad order to learn it in, and "python is not recognised as an internal or
    external command" is not something a non-technical user can act on.
    """
    _reject_reserved(req.name)
    parent = resolve(req.path)
    if not os.path.isdir(parent):
        raise HTTPException(status_code=404, detail="parent directory not found")
    full = resolve(os.path.join(req.path or "", req.name))
    if os.path.exists(full):
        raise HTTPException(status_code=409, detail=f"'{req.name}' already exists")
    if req.is_dir:
        os.makedirs(full)
    else:
        with open(full, "x", encoding="utf-8"):
            pass

    toolchain = {}
    if not req.is_dir:
        from carrot import runner

        recipe = runner.recipe_for(full)
        if recipe is not None:
            available = runner._resolve_tool(recipe) is not None
            toolchain = {
                "language": recipe.language,
                "available": available,
                "install": "" if available else recipe.install,
                "help_url": "" if available else LANGUAGE_HELP.get(recipe.language, ""),
            }
    return {"path": rel_of(full), "is_dir": req.is_dir, "toolchain": toolchain}


@router.post("/rename")
async def files_rename(req: RenameRequest):
    _reject_reserved(req.new_name)
    full = resolve(req.path, must_exist=True)
    if full == os.path.realpath(get_root()):
        raise HTTPException(status_code=400, detail="cannot rename the workspace root")
    target = resolve(os.path.join(os.path.dirname(req.path), req.new_name))
    if os.path.exists(target):
        raise HTTPException(status_code=409, detail=f"'{req.new_name}' already exists")
    os.rename(full, target)
    return {"path": rel_of(target), "was": req.path}


@router.post("/move")
async def files_move(req: MoveRequest):
    """Drag-and-drop in the tree."""
    full = resolve(req.path, must_exist=True)
    dest_dir = resolve(req.dest_dir)
    if not os.path.isdir(dest_dir):
        raise HTTPException(status_code=404, detail="destination is not a directory")
    # Moving a directory inside itself detaches the whole subtree.
    if os.path.isdir(full) and (dest_dir == full or dest_dir.startswith(full + os.sep)):
        raise HTTPException(status_code=400, detail="cannot move a folder into itself")
    target = os.path.join(dest_dir, os.path.basename(full))
    if os.path.exists(target):
        raise HTTPException(status_code=409, detail="something with that name is already there")
    shutil.move(full, target)
    return {"path": rel_of(target), "was": req.path}


@router.post("/delete")
async def files_delete(req: DeleteRequest):
    full = resolve(req.path, must_exist=True)
    if full == os.path.realpath(get_root()):
        raise HTTPException(status_code=400, detail="cannot delete the workspace root")
    if os.path.isdir(full):
        shutil.rmtree(full)
    else:
        os.remove(full)
    return {"deleted": req.path}


# Project-wide find. Bounded on every axis — a workspace can be a monorepo,
# and an unbounded walk would hang the UI and pin a core.
SEARCH_MAX_FILE_BYTES = 512 * 1024
SEARCH_MAX_HITS = 200
SEARCH_MAX_FILES = 4000
SEARCH_TEXT_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".md", ".txt", ".html", ".css",
    ".scss", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".sh", ".bash", ".zsh",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".java", ".go", ".rs", ".rb", ".php",
    ".sql", ".xml", ".svg", ".vue", ".svelte", ".kt", ".swift", ".r", ".lua",
    ".dockerfile", ".env", ".gitignore", ".conf", ".properties",
}


def _looks_textual(name: str) -> bool:
    ext = os.path.splitext(name)[1].lower()
    return ext in SEARCH_TEXT_EXTENSIONS or not ext


@router.get("/search")
async def files_search(q: str, case_sensitive: bool = False, max_hits: int = SEARCH_MAX_HITS):
    """Grep the workspace. Returns file, line number and the matching line."""
    query = q if case_sensitive else q.lower()
    if not query.strip():
        return {"query": q, "hits": [], "truncated": False}
    root = os.path.realpath(get_root())
    hits = []
    scanned = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for name in sorted(filenames):
            if name.startswith(".") or not _looks_textual(name):
                continue
            scanned += 1
            if scanned > SEARCH_MAX_FILES or len(hits) >= max_hits:
                truncated = True
                break
            full = os.path.join(dirpath, name)
            try:
                if os.path.getsize(full) > SEARCH_MAX_FILE_BYTES:
                    continue
                with open(full, "r", encoding="utf-8") as handle:
                    for lineno, line in enumerate(handle, 1):
                        haystack = line if case_sensitive else line.lower()
                        if query in haystack:
                            hits.append({
                                "path": rel_of(full),
                                "line": lineno,
                                "text": line.rstrip("\n")[:300],
                            })
                            if len(hits) >= max_hits:
                                truncated = True
                                break
            except (OSError, UnicodeDecodeError):
                continue          # binary or unreadable; not an error worth raising
        if truncated:
            break
    return {"query": q, "hits": hits, "truncated": truncated}


class RunRequest(BaseModel):
    path: str
    timeout: Optional[int] = None


@router.post("/run")
async def files_run(req: RunRequest):
    """Run the open file. Compiled languages build first; see carrot/runner.py."""
    from carrot import runner

    timeout = max(1, min(int(req.timeout or runner.DEFAULT_TIMEOUT), 300))
    return runner.run_file(req.path, timeout=timeout)


class InstallRequest(BaseModel):
    package: str
    manager: str


@router.post("/install")
async def files_install(req: InstallRequest):
    """Install one package the last run said was missing.

    Only ever reached by the user clicking the offer that a failed run
    produced — the name is re-validated here regardless, since an endpoint has
    no way to know which button called it.
    """
    from carrot import packages

    result = packages.install(req.package, req.manager, cwd=get_root())
    if not result["ok"] and "not a valid package name" in result["output"]:
        raise HTTPException(status_code=400, detail=result["output"])
    return result


@router.get("/languages")
async def files_languages():
    """What the Run button can do here, and what is missing."""
    from carrot import runner

    return {"languages": runner.languages()}


@router.get("/editors")
async def list_editors():
    """Which editor CLIs are installed (cursor preferred over vscode)."""
    from carrot import interop
    return {"editors": interop.available_editors()}


@router.post("/open-vscode")
async def open_vscode(req: PathRequest):
    """Open the file/workspace in the user's editor — Cursor if installed,
    else VS Code. The endpoint keeps its historical name."""
    from carrot import interop
    target = resolve(req.path or "")
    editor = interop.editor_command()
    if not editor:
        raise HTTPException(
            status_code=404,
            detail="No editor CLI found — install VS Code (enable the 'code' shell command) or Cursor",
        )
    name, exe = editor
    try:
        subprocess.Popen(
            [exe, target],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"failed to launch {name}: {e}")
    return {"opened": target, "editor": name}
