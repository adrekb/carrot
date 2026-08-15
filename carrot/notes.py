import json
import os
import uuid
from datetime import datetime, timezone


# Where notes live, from the same resolver as everything else rather than from
# this file's own location.
#
# It was `__file__/data/notes`, which for a checkout is the same path — so
# nothing moves for anyone who has notes today. It was wrong in the two cases
# that are not a checkout. An installed build puts the code in a read-only
# directory, so notes had nowhere to be written; and `CARROT_DATA_DIR`, which
# every other part of Carrot honours, was ignored here — which is why the dev
# preview, whose whole promise is that it touches nothing real, opened with a
# hundred and sixty of the developer's own notes in the sidebar.
from carrot.config import CARROT_DIR

NOTES_DIR = os.path.join(CARROT_DIR, "notes")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def ensure_notes_dir():
    os.makedirs(NOTES_DIR, exist_ok=True)


def get_note_path(note_id: str):
    ensure_notes_dir()
    return os.path.join(NOTES_DIR, f"{note_id}.md")


def _split_frontmatter(text: str):
    """Split '---\n...\n---\n' frontmatter from a note body.

    Returns (frontmatter_dict, frontmatter_raw, body).
    """
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            raw = parts[1]
            meta = {}
            for line in raw.splitlines():
                if ":" in line:
                    key, _, value = line.partition(":")
                    meta[key.strip()] = value.strip()
            return meta, raw, parts[2].lstrip("\n")
    return {}, "", text


def list_notes(folder: str = None):
    ensure_notes_dir()
    search_dir = NOTES_DIR
    if folder:
        search_dir = os.path.join(NOTES_DIR, folder)
        os.makedirs(search_dir, exist_ok=True)
    notes = []
    if os.path.isdir(search_dir):
        for f in sorted(os.listdir(search_dir)):
            if f.endswith(".md"):
                filepath = os.path.join(search_dir, f)
                with open(filepath, "r", encoding="utf-8") as fh:
                    content = fh.read()
                meta, _, body = _split_frontmatter(content)
                notes.append(
                    {
                        "id": f.replace(".md", ""),
                        "filename": f,
                        "folder": folder or "",
                        "path": filepath,
                        "title": meta.get("title", "") or f.replace(".md", ""),
                        "created_at": os.path.getmtime(filepath),
                        "content": content,
                        "body": body,
                    }
                )
    return notes


def get_note(note_id: str):
    filepath = get_note_path(note_id)
    if not os.path.exists(filepath):
        return None
    with open(filepath, "r", encoding="utf-8") as f:
        content = f.read()
    meta, _, body = _split_frontmatter(content)
    return {
        "id": note_id,
        "path": filepath,
        "content": content,
        "title": meta.get("title", "") or note_id,
        "body": body,
    }


def create_note(title: str, content: str = "", folder: str = None):
    note_id = str(uuid.uuid4())[:12]
    if folder:
        notes_dir = os.path.join(NOTES_DIR, folder)
        os.makedirs(notes_dir, exist_ok=True)
        filepath = os.path.join(notes_dir, f"{note_id}.md")
    else:
        filepath = get_note_path(note_id)
    frontmatter = f"---\ntitle: {title}\ncreated: {now_iso()}\n"
    if folder:
        frontmatter += f"folder: {folder}\n"
    frontmatter += "---\n"
    full_content = frontmatter + content
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(full_content)

    try:
        from . import workspaces as workspaces_mod

        workspaces_mod.file_item(workspaces_mod.KIND_NOTE, note_id)
    except Exception:
        pass

    return {"id": note_id, "title": title, "folder": folder or "", "path": filepath}


def update_note(note_id: str, content: str, folder: str = None, title: str = None):
    """Replace a note's body (and optionally title) while preserving frontmatter."""
    filepath = get_note_path(note_id)
    if folder:
        candidate = os.path.join(NOTES_DIR, folder, f"{note_id}.md")
        if os.path.exists(candidate):
            filepath = candidate
    if not os.path.exists(filepath):
        return None
    with open(filepath, "r", encoding="utf-8") as f:
        existing = f.read()
    meta, _, _ = _split_frontmatter(existing)
    if title is not None:
        meta["title"] = title
    meta.setdefault("title", note_id)
    meta.setdefault("created", now_iso())
    meta["updated"] = now_iso()
    frontmatter = "---\n" + "".join(f"{k}: {v}\n" for k, v in meta.items()) + "---\n"
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(frontmatter + content)
    return {"id": note_id, "path": filepath, "title": meta["title"]}


def delete_note(note_id: str):
    filepath = get_note_path(note_id)
    if os.path.exists(filepath):
        os.remove(filepath)
        return True
    return False