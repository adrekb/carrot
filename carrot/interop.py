"""Bridges to the apps people already use.

Nobody should have to abandon Obsidian or VS Code to get value out of
Carrot. This module makes switching between them one click instead of a
copy-paste ritual:

  - **Obsidian**: point Carrot at your vault folder once. Any Carrot note
    can be sent into the vault as a normal Markdown file (under a
    ``Carrot/`` subfolder, so your vault stays tidy), and the vault can be
    imported the other way — new and changed vault notes become Carrot
    notes, so chat search and @/file/ citations work over them. Re-running
    the import is safe: a ledger tracks what came from where, so notes are
    updated in place, never duplicated.
  - **VS Code / Cursor**: the Code tab's "open in editor" button launches
    whichever editor is actually installed (Cursor preferred if both are
    present, since installing it is the stronger signal of preference).

Everything is local file I/O and local process launches — no plugins to
install on the other side.
"""
import os
import re
import json
import shutil
from typing import Optional
from urllib.parse import quote

from carrot.config import CARROT_DIR, get_config, set_config
from carrot import notes as notes_mod

IMPORT_LEDGER_PATH = os.path.join(CARROT_DIR, "config", "obsidian_import.json")
CARROT_SUBFOLDER = "Carrot"  # where sent notes land inside the vault
IMPORT_FOLDER = "obsidian"   # Carrot notes folder for imported vault files


# ===== Editors (VS Code / Cursor) =====

def available_editors() -> list:
    """Editor CLIs present on this machine, preference order first."""
    found = []
    for name, exes in (("cursor", ("cursor", "cursor.cmd")),
                       ("vscode", ("code", "code.cmd"))):
        if any(shutil.which(e) for e in exes):
            found.append(name)
    return found


def editor_command(preferred: Optional[str] = None) -> Optional[tuple]:
    """(label, executable) for the editor to launch, or None."""
    order = [("cursor", ("cursor", "cursor.cmd")), ("vscode", ("code", "code.cmd"))]
    if preferred == "vscode":
        order.reverse()
    for name, exes in order:
        for e in exes:
            path = shutil.which(e)
            if path:
                return name, path
    return None


# ===== Obsidian =====

def vault_path() -> str:
    return get_config().get("obsidian_vault_path", "")


def vault_ok() -> bool:
    v = vault_path()
    return bool(v) and os.path.isdir(v)


def _safe_filename(title: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title).strip().strip(".")
    return (name or "Untitled")[:120]


def send_note_to_obsidian(note_id: str) -> dict:
    """Write a Carrot note into the vault as plain Markdown.

    Returns the file path and an ``obsidian://`` URI the client can open.
    Existing files with the same title get numbered rather than clobbered
    (unless the previous send came from this same note, tracked inline).
    """
    if not vault_ok():
        raise ValueError("Obsidian vault folder is not set (Settings → Your apps)")
    note = notes_mod.get_note(note_id)
    if not note:
        raise ValueError("note not found")
    _, _, body = notes_mod._split_frontmatter(note.get("content", ""))
    title = note.get("title") or note_id
    target_dir = os.path.join(vault_path(), CARROT_SUBFOLDER)
    os.makedirs(target_dir, exist_ok=True)

    marker = f"<!-- carrot:{note_id} -->"
    base = _safe_filename(title)
    path = os.path.join(target_dir, f"{base}.md")
    counter = 2
    while os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            head = f.read(4096)
        if marker in head:
            break  # same Carrot note — update in place
        path = os.path.join(target_dir, f"{base} ({counter}).md")
        counter += 1

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{marker}\n# {title}\n\n{body.strip()}\n")
    uri = "obsidian://open?path=" + quote(os.path.abspath(path))
    return {"path": path, "uri": uri}


def _load_ledger() -> dict:
    try:
        with open(IMPORT_LEDGER_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_ledger(ledger: dict):
    os.makedirs(os.path.dirname(IMPORT_LEDGER_PATH), exist_ok=True)
    with open(IMPORT_LEDGER_PATH, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2)


def import_from_obsidian(max_files: int = 2000) -> dict:
    """Bring the vault's Markdown files into Carrot's notes.

    Idempotent: new files create notes, changed files update the note they
    created last time, unchanged files are skipped. Files Carrot itself
    sent to the vault (the ``Carrot/`` subfolder) are excluded — importing
    your own exports back would just echo.
    """
    if not vault_ok():
        raise ValueError("Obsidian vault folder is not set (Settings → Your apps)")
    ledger = _load_ledger()
    imported = updated = skipped = 0
    root = os.path.abspath(vault_path())
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d != CARROT_SUBFOLDER]
        for fn in sorted(filenames):
            if not fn.lower().endswith(".md"):
                continue
            seen += 1
            if seen > max_files:
                break
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            try:
                mtime = os.path.getmtime(full)
                with open(full, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except OSError:
                continue
            entry = ledger.get(rel)
            title = os.path.splitext(fn)[0]
            if entry is None:
                note = notes_mod.create_note(title, content, folder=IMPORT_FOLDER)
                ledger[rel] = {"note_id": note["id"], "mtime": mtime}
                imported += 1
            elif mtime > entry.get("mtime", 0):
                if notes_mod.update_note(entry["note_id"], content,
                                         folder=IMPORT_FOLDER, title=title):
                    updated += 1
                else:  # note was deleted in Carrot — recreate
                    note = notes_mod.create_note(title, content, folder=IMPORT_FOLDER)
                    ledger[rel] = {"note_id": note["id"], "mtime": mtime}
                    imported += 1
                ledger[rel]["mtime"] = mtime
            else:
                skipped += 1
    _save_ledger(ledger)
    return {"imported": imported, "updated": updated, "skipped": skipped}


def status() -> dict:
    return {
        "vault_path": vault_path(),
        "vault_ok": vault_ok(),
        "editors": available_editors(),
    }
