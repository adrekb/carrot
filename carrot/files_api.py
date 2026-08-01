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


def get_root() -> str:
    root = get_config().get("code_workspace_dir", "")
    if not root:
        root = os.path.join(os.path.expanduser("~"), "CarrotProjects")
    root = os.path.abspath(root)
    os.makedirs(root, exist_ok=True)
    return root


def resolve(rel_path: str) -> str:
    root = get_root()
    full = os.path.abspath(os.path.join(root, rel_path or ""))
    if full != root and not full.startswith(root + os.sep):
        raise HTTPException(status_code=403, detail="path escapes the workspace root")
    return full


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
    os.makedirs(os.path.dirname(full) or get_root(), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(req.content)
    return {"path": req.path, "size": len(req.content)}


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
