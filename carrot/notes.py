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
                        "format": normalize_format(meta.get("format")),
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
        "format": normalize_format(meta.get("format")),
        "body": body,
    }


# What kind of document this is, written into the file.
#
# The format belongs on the file rather than in a setting, because a workspace
# is allowed to hold a thesis in TeX beside a shopping list in markdown, and a
# preference forces every new document to be whatever the last one was. It also
# saves the editor from guessing: opening a document reads what it is, instead
# of sniffing the text for a \documentclass — which is exactly wrong for the
# LaTeX document somebody has not started writing yet.
FORMAT_MARKDOWN = "markdown"
FORMAT_LATEX = "latex"
# A canvas and a slide deck are documents, not places. They live in the same
# directory as everything else, are listed in the same sidebar, and are opened
# by the same click — what differs is the editor that click lands you in, which
# is a thing `format` already decided. The alternative was a tab each, and the
# app does not need two more tabs.
#
# Their bodies are not prose. A canvas is JSON after the frontmatter; a deck is
# markdown with `---` between slides. Both stay in `.md` files with the same
# frontmatter so that every existing thing — listing, renaming, deleting, the
# workspace file history — keeps working without being taught about them.
FORMAT_CANVAS = "canvas"
FORMAT_SLIDES = "slides"
FORMATS = (FORMAT_MARKDOWN, FORMAT_LATEX, FORMAT_CANVAS, FORMAT_SLIDES)


def normalize_format(value) -> str:
    """Anything unrecognised is markdown. A note written before this existed
    has no `format:` line, and markdown is the truth about every one of them —
    LaTeX documents had nowhere to record that they were LaTeX."""
    value = (value or "").strip().lower()
    return value if value in FORMATS else FORMAT_MARKDOWN


def create_note(title: str, content: str = "", folder: str = None,
                doc_format: str = FORMAT_MARKDOWN):
    note_id = str(uuid.uuid4())[:12]
    if folder:
        notes_dir = os.path.join(NOTES_DIR, folder)
        os.makedirs(notes_dir, exist_ok=True)
        filepath = os.path.join(notes_dir, f"{note_id}.md")
    else:
        filepath = get_note_path(note_id)
    frontmatter = f"---\ntitle: {title}\ncreated: {now_iso()}\n"
    frontmatter += f"format: {normalize_format(doc_format)}\n"
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

    return {"id": note_id, "title": title, "folder": folder or "", "path": filepath,
            "format": normalize_format(doc_format)}


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
    # `format` rides in `meta` and is written straight back, so a document does
    # not silently become markdown the first time somebody edits it.
    return {"id": note_id, "path": filepath, "title": meta["title"],
            "format": normalize_format(meta.get("format"))}


def delete_note(note_id: str):
    filepath = get_note_path(note_id)
    if os.path.exists(filepath):
        os.remove(filepath)
        return True
    return False