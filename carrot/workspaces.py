"""Workspaces — one project's context, and the folders that group them.

Carrot accumulates. After a few months there are two hundred conversations,
a thousand memories and an indexed drive, and a question about your thesis
retrieves something you said about a side project in March. The answer is not
better ranking; it is telling Carrot which part of your life you are in.

    Folder            "School"
      └─ Workspace    "Thesis"      ← chats, memories, files, notes, runs
      └─ Workspace    "CS 3110"
    Folder            "Personal"
      └─ Workspace    "Fitness"

A workspace is a **scope, not a container**. Nothing is moved on disk and
nothing is copied. Filing a chat into "Thesis" means that when "Thesis" is the
active workspace, search, memory recall and document lookup are restricted to
what lives there — and new chats, notes and runs are filed there automatically.
Switch to *All workspaces* and everything is visible again, which is the
default and stays the default: a fresh install has no workspaces and behaves
exactly as it did before.

Two decisions worth stating:

**One home per item.** ``workspace_items`` is keyed on ``(kind, item_id)``, so
a conversation is in exactly one workspace. A many-to-many would be more
expressive and much worse to use — "which workspace is this in" needs a single
answer for the UI to be comprehensible, and moving an item should be one
action, not a set-difference.

**Membership lives in one table** rather than a ``workspace_id`` column on six
others. Adding a new kind of thing to a workspace is then a string, not a
migration, and the scoping predicate is written once.

Folders group workspaces and may nest. They deliberately hold no content of
their own: a folder that could contain both workspaces and chats would make
"where does this go" ambiguous every time.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .config import get_config, set_config
from .database import get_db

# The kinds of thing a workspace can hold. A new one is a string here plus a
# call to `file_item` wherever that thing is created.
KIND_CONVERSATION = "conversation"
KIND_MEMORY = "memory"
KIND_DOCUMENT = "document"
KIND_NOTE = "note"
KIND_RESEARCH = "research"
KIND_AGENT = "agent"

KINDS = (
    KIND_CONVERSATION, KIND_MEMORY, KIND_DOCUMENT,
    KIND_NOTE, KIND_RESEARCH, KIND_AGENT,
)

KIND_LABELS = {
    KIND_CONVERSATION: "Chats",
    KIND_MEMORY: "Memories",
    KIND_DOCUMENT: "Files",
    KIND_NOTE: "Notes",
    KIND_RESEARCH: "Research",
    KIND_AGENT: "Agent runs",
}

MAX_FOLDER_DEPTH = 5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return str(uuid.uuid4())[:12]


# ===== Folders =====

def list_folders() -> List[Dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM folders ORDER BY position, created_at"
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_folder(folder_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def _depth_of(folder_id: Optional[str], folders: Dict[str, Dict[str, Any]]) -> int:
    depth = 0
    seen = set()
    while folder_id and folder_id in folders and folder_id not in seen:
        seen.add(folder_id)
        folder_id = folders[folder_id].get("parent_id")
        depth += 1
    return depth


def create_folder(name: str, parent_id: Optional[str] = None) -> Dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ValueError("a folder needs a name")

    folders = {f["id"]: f for f in list_folders()}
    if parent_id:
        if parent_id not in folders:
            raise ValueError("no such parent folder")
        if _depth_of(parent_id, folders) >= MAX_FOLDER_DEPTH:
            raise ValueError(f"folders may not nest more than {MAX_FOLDER_DEPTH} deep")

    folder_id = _new_id()
    conn = get_db()
    position = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 AS next FROM folders WHERE "
        + ("parent_id IS ?" if parent_id is None else "parent_id = ?"),
        (parent_id,),
    ).fetchone()["next"]
    conn.execute(
        "INSERT INTO folders (id, name, parent_id, position, created_at) VALUES (?, ?, ?, ?, ?)",
        (folder_id, name, parent_id or None, position, _now()),
    )
    conn.commit()
    conn.close()
    return get_folder(folder_id)


def update_folder(folder_id: str, name: Optional[str] = None,
                  parent_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Rename or re-parent a folder. ``parent_id=""`` moves it to the top level."""
    folder = get_folder(folder_id)
    if folder is None:
        return None

    if parent_id is not None and parent_id != "":
        if parent_id == folder_id:
            raise ValueError("a folder cannot contain itself")
        folders = {f["id"]: f for f in list_folders()}
        # Walk up from the proposed parent: meeting this folder would make a
        # cycle, and a cycle here hangs every tree render.
        cursor = parent_id
        seen = set()
        while cursor and cursor not in seen:
            if cursor == folder_id:
                raise ValueError("that would put the folder inside itself")
            seen.add(cursor)
            cursor = folders.get(cursor, {}).get("parent_id")

    conn = get_db()
    if name is not None and name.strip():
        conn.execute("UPDATE folders SET name = ? WHERE id = ?", (name.strip(), folder_id))
    if parent_id is not None:
        conn.execute(
            "UPDATE folders SET parent_id = ? WHERE id = ?", (parent_id or None, folder_id)
        )
    conn.commit()
    conn.close()
    return get_folder(folder_id)


def delete_folder(folder_id: str) -> bool:
    """Delete a folder. Its workspaces and subfolders move to the top level.

    Never cascades into workspaces: deleting a folder is a tidying action, and
    losing a project's whole context to one is not a risk worth taking.
    """
    conn = get_db()
    conn.execute("UPDATE workspaces SET folder_id = NULL WHERE folder_id = ?", (folder_id,))
    conn.execute("UPDATE folders SET parent_id = NULL WHERE parent_id = ?", (folder_id,))
    cursor = conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted


# ===== Workspaces =====

def list_workspaces(include_archived: bool = False) -> List[Dict[str, Any]]:
    conn = get_db()
    sql = "SELECT * FROM workspaces"
    if not include_archived:
        sql += " WHERE archived = 0"
    sql += " ORDER BY position, created_at"
    rows = conn.execute(sql).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_workspace(workspace_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    conn.close()
    if row is None:
        return None
    workspace = dict(row)
    workspace["archived"] = bool(workspace["archived"])
    workspace["counts"] = item_counts(workspace_id)
    return workspace


def create_workspace(name: str, description: str = "", folder_id: Optional[str] = None,
                     color: str = "") -> Dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ValueError("a workspace needs a name")
    if folder_id and get_folder(folder_id) is None:
        raise ValueError("no such folder")

    workspace_id = _new_id()
    conn = get_db()
    position = conn.execute(
        "SELECT COALESCE(MAX(position), -1) + 1 AS next FROM workspaces"
    ).fetchone()["next"]
    conn.execute(
        """INSERT INTO workspaces (id, name, description, folder_id, color, position, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (workspace_id, name, description or "", folder_id or None, color or "", position, _now()),
    )
    conn.commit()
    conn.close()
    return get_workspace(workspace_id)


def update_workspace(workspace_id: str, **fields) -> Optional[Dict[str, Any]]:
    if get_workspace(workspace_id) is None:
        return None
    allowed = {"name", "description", "folder_id", "color", "archived"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return get_workspace(workspace_id)

    if "folder_id" in updates:
        folder_id = updates["folder_id"] or None
        if folder_id and get_folder(folder_id) is None:
            raise ValueError("no such folder")
        updates["folder_id"] = folder_id
    if "archived" in updates:
        updates["archived"] = int(bool(updates["archived"]))
    if "name" in updates:
        updates["name"] = str(updates["name"]).strip() or None
        if updates["name"] is None:
            raise ValueError("a workspace needs a name")

    conn = get_db()
    conn.execute(
        f"UPDATE workspaces SET {', '.join(f'{k} = ?' for k in updates)} WHERE id = ?",
        (*updates.values(), workspace_id),
    )
    conn.commit()
    conn.close()
    return get_workspace(workspace_id)


def delete_workspace(workspace_id: str) -> bool:
    """Delete a workspace. Its items are unfiled, never deleted.

    The membership rows cascade; the conversations, memories and documents they
    referenced are untouched and simply become unfiled again. Deleting a
    grouping must not be a way to lose six months of chats.
    """
    conn = get_db()
    cursor = conn.execute("DELETE FROM workspaces WHERE id = ?", (workspace_id,))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    if deleted and active_workspace_id() == workspace_id:
        set_active_workspace("")
    return deleted


# ===== The active workspace =====

def active_workspace_id() -> str:
    """The workspace new work is filed into, or "" for all workspaces."""
    current = get_config().get("active_workspace_id", "") or ""
    if current and get_workspace(current) is None:
        # It was deleted out from under us; fall back rather than scoping
        # everything to a workspace that no longer exists.
        set_active_workspace("")
        return ""
    return current


def set_active_workspace(workspace_id: str) -> str:
    workspace_id = (workspace_id or "").strip()
    if workspace_id and get_workspace(workspace_id) is None:
        raise ValueError("no such workspace")
    set_config("active_workspace_id", workspace_id)
    return workspace_id


def active_workspace() -> Optional[Dict[str, Any]]:
    workspace_id = active_workspace_id()
    return get_workspace(workspace_id) if workspace_id else None


# ===== Membership =====

def file_item(kind: str, item_id: str, workspace_id: Optional[str] = None) -> Optional[str]:
    """Put an item in a workspace. Called wherever content is created.

    ``workspace_id=None`` means "the active one", which is how a new chat ends
    up in the project you are working on without anyone filing it by hand.
    Passing "" unfiles the item.
    """
    if not kind or not item_id:
        return None
    if workspace_id is None:
        workspace_id = active_workspace_id()
    if not workspace_id:
        unfile_item(kind, item_id)
        return None
    if get_workspace(workspace_id) is None:
        return None

    conn = get_db()
    conn.execute(
        """INSERT INTO workspace_items (kind, item_id, workspace_id, added_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(kind, item_id) DO UPDATE SET workspace_id = excluded.workspace_id,
                                                    added_at = excluded.added_at""",
        (kind, str(item_id), workspace_id, _now()),
    )
    conn.commit()
    conn.close()
    return workspace_id


def unfile_item(kind: str, item_id: str) -> bool:
    conn = get_db()
    cursor = conn.execute(
        "DELETE FROM workspace_items WHERE kind = ? AND item_id = ?", (kind, str(item_id))
    )
    conn.commit()
    removed = cursor.rowcount > 0
    conn.close()
    return removed


def workspace_of(kind: str, item_id: str) -> Optional[str]:
    conn = get_db()
    row = conn.execute(
        "SELECT workspace_id FROM workspace_items WHERE kind = ? AND item_id = ?",
        (kind, str(item_id)),
    ).fetchone()
    conn.close()
    return row["workspace_id"] if row else None


def workspace_map(kind: str) -> Dict[str, str]:
    """Every item of one kind, mapped to the workspace it is in.

    The bulk form of `workspace_of`. Filtering a document list by workspace
    needs the answer for every document at once, and asking one at a time is a
    query per document on a screen that exists to be fast.
    """
    conn = get_db()
    rows = conn.execute(
        "SELECT item_id, workspace_id FROM workspace_items WHERE kind = ?", (kind,)
    ).fetchall()
    conn.close()
    return {row["item_id"]: row["workspace_id"] for row in rows}


def item_ids(workspace_id: str, kind: str) -> List[str]:
    conn = get_db()
    rows = conn.execute(
        "SELECT item_id FROM workspace_items WHERE workspace_id = ? AND kind = ?",
        (workspace_id, kind),
    ).fetchall()
    conn.close()
    return [row["item_id"] for row in rows]


def item_counts(workspace_id: str) -> Dict[str, int]:
    conn = get_db()
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM workspace_items WHERE workspace_id = ? GROUP BY kind",
        (workspace_id,),
    ).fetchall()
    conn.close()
    counts = {kind: 0 for kind in KINDS}
    for row in rows:
        counts[row["kind"]] = row["n"]
    return counts


def move_items(workspace_id: str, items: List[Dict[str, str]]) -> int:
    """File several items at once — what a multi-select drag ends up calling."""
    filed = 0
    for item in items or []:
        kind, item_id = item.get("kind", ""), item.get("item_id", "")
        if kind in KINDS and item_id:
            file_item(kind, item_id, workspace_id)
            filed += 1
    return filed


# ===== Scoping =====

def scope_clause(kind: str, workspace_id: Optional[str], column: str) -> Tuple[str, List[Any]]:
    """A SQL fragment restricting ``column`` to one workspace's items.

    Returns ``("", [])`` when there is nothing to scope to, so every call site
    is the same two lines whether or not a workspace is active. The fragment
    starts with AND and is meant to be appended inside an existing WHERE.
    """
    if not workspace_id:
        return "", []
    return (
        f" AND {column} IN (SELECT item_id FROM workspace_items "
        f"WHERE workspace_id = ? AND kind = ?)",
        [workspace_id, kind],
    )


def resolve_scope(requested: Optional[str]) -> str:
    """Turn a request's workspace parameter into an id to scope by.

    ``None`` means "use whatever is active" — the common case, and what makes a
    workspace feel like a mode rather than a filter you re-apply everywhere.
    ``"all"`` or ``""`` explicitly means no scoping.
    """
    if requested is None:
        return active_workspace_id()
    requested = requested.strip()
    if requested in ("", "all"):
        return ""
    return requested if get_workspace(requested) else ""


def in_scope(kind: str, item_id: str, workspace_id: Optional[str]) -> bool:
    """Whether one item passes the current scope. For non-SQL filtering."""
    if not workspace_id:
        return True
    return workspace_of(kind, item_id) == workspace_id


# ===== Views =====

def tree(include_archived: bool = False) -> Dict[str, Any]:
    """Folders and workspaces as a nested structure for the sidebar.

    Top-level workspaces (those in no folder) are returned alongside the root
    folders rather than under a synthetic "Uncategorised" node — a workspace
    that was never filed is not in an unnamed folder, it is simply loose.
    """
    folders = list_folders()
    workspaces = list_workspaces(include_archived=include_archived)

    by_parent: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for folder in folders:
        by_parent.setdefault(folder["parent_id"], []).append(folder)

    ws_by_folder: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for workspace in workspaces:
        entry = {**workspace, "archived": bool(workspace["archived"]),
                 "counts": item_counts(workspace["id"])}
        ws_by_folder.setdefault(workspace["folder_id"], []).append(entry)

    def build(parent_id: Optional[str], depth: int) -> List[Dict[str, Any]]:
        if depth > MAX_FOLDER_DEPTH:
            return []
        nodes = []
        for folder in by_parent.get(parent_id, []):
            nodes.append({
                "type": "folder",
                **folder,
                "children": build(folder["id"], depth + 1),
                "workspaces": ws_by_folder.get(folder["id"], []),
            })
        return nodes

    return {
        "folders": build(None, 0),
        "workspaces": ws_by_folder.get(None, []),
        "active": active_workspace_id(),
    }


def contents(workspace_id: str, limit: int = 50) -> Dict[str, Any]:
    """What is actually in a workspace, resolved to titles the UI can show.

    Every lookup is contained separately: a workspace holding a note whose file
    was deleted should still list its chats.
    """
    workspace = get_workspace(workspace_id)
    if workspace is None:
        return {}

    conn = get_db()
    rows = conn.execute(
        "SELECT kind, item_id, added_at FROM workspace_items WHERE workspace_id = ? "
        "ORDER BY added_at DESC LIMIT ?",
        (workspace_id, limit * len(KINDS)),
    ).fetchall()
    conn.close()

    grouped: Dict[str, List[Dict[str, Any]]] = {kind: [] for kind in KINDS}
    for row in rows:
        kind = row["kind"]
        if kind not in grouped or len(grouped[kind]) >= limit:
            continue
        grouped[kind].append({
            "item_id": row["item_id"],
            "added_at": row["added_at"],
            "label": _label_for(kind, row["item_id"]),
        })

    return {"workspace": workspace, "items": grouped, "labels": KIND_LABELS}


def _label_for(kind: str, item_id: str) -> str:
    """A human-readable name for one filed item, or a fallback."""
    lookups = {
        KIND_CONVERSATION: ("SELECT title FROM conversations WHERE id = ?", "title"),
        KIND_MEMORY: ("SELECT content FROM memories WHERE id = ?", "content"),
        KIND_DOCUMENT: ("SELECT path FROM documents WHERE id = ?", "path"),
        KIND_RESEARCH: ("SELECT question FROM research_runs WHERE id = ?", "question"),
        KIND_AGENT: ("SELECT task FROM agent_runs WHERE id = ?", "task"),
    }
    if kind == KIND_NOTE:
        try:
            from . import notes as notes_mod

            note = notes_mod.get_note(item_id)
            return (note or {}).get("title") or item_id
        except Exception:
            return item_id

    query = lookups.get(kind)
    if not query:
        return item_id
    try:
        conn = get_db()
        row = conn.execute(query[0], (item_id,)).fetchone()
        conn.close()
    except Exception:
        return item_id
    return (row[query[1]] if row else "") or item_id


def status() -> Dict[str, Any]:
    """A summary for the switcher and the status endpoint."""
    workspaces = list_workspaces()
    return {
        "active": active_workspace_id(),
        "active_workspace": active_workspace(),
        "workspace_count": len(workspaces),
        "folder_count": len(list_folders()),
        "kinds": [{"id": kind, "label": KIND_LABELS[kind]} for kind in KINDS],
    }
