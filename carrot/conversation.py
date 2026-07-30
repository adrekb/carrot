import json
import uuid
import os
from datetime import datetime, timezone

from carrot.database import get_db


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def create_conversation(title: str = "", metadata: dict = None):
    conv_id = str(uuid.uuid4())[:12]
    ts = now_iso()
    conn = get_db()
    conn.execute(
        "INSERT INTO conversations (id, title, created_at, updated_at, metadata) VALUES (?, ?, ?, ?, ?)",
        (conv_id, title, ts, ts, json.dumps(metadata or {})),
    )
    conn.commit()
    conn.close()
    return {"id": conv_id, "title": title, "created_at": ts, "metadata": metadata or {}}


def get_conversation(conv_id: str):
    conn = get_db()
    conv = conn.execute(
        "SELECT * FROM conversations WHERE id = ?", (conv_id,)
    ).fetchone()
    if conv is None:
        conn.close()
        return None
    messages = conn.execute(
        "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id ASC",
        (conv_id,),
    ).fetchall()
    conn.close()
    return {
        "id": conv["id"],
        "title": conv["title"],
        "created_at": conv["created_at"],
        "updated_at": conv["updated_at"],
        "metadata": json.loads(conv["metadata"] or "{}"),
        "messages": [
            {
                "id": m["id"],
                "role": m["role"],
                "content": m["content"],
                "timestamp": m["timestamp"],
                "metadata": json.loads(m["metadata"] or "{}"),
            }
            for m in messages
        ],
    }


def list_conversations(limit: int = 50):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM conversations ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "created_at": r["created_at"],
            "updated_at": r["updated_at"],
            "metadata": json.loads(r["metadata"] or "{}"),
        }
        for r in rows
    ]


def add_message(conv_id: str, role: str, content: str, metadata: dict = None):
    conn = get_db()
    conv = conn.execute(
        "SELECT id FROM conversations WHERE id = ?", (conv_id,)
    ).fetchone()
    if conv is None:
        conn.close()
        raise ValueError(f"Conversation {conv_id} not found")
    ts = now_iso()
    cursor = conn.execute(
        """INSERT INTO messages (conversation_id, role, content, timestamp, metadata)
           VALUES (?, ?, ?, ?, ?)""",
        (conv_id, role, content, ts, json.dumps(metadata or {})),
    )
    conn.execute(
        "UPDATE conversations SET updated_at = ? WHERE id = ?",
        (ts, conv_id),
    )
    conn.commit()
    conn.close()
    return {"id": cursor.lastrowid, "role": role, "content": content, "timestamp": ts}


def update_message_metadata(conv_id: str, message_id: int, metadata: dict):
    conn = get_db()
    conn.execute(
        "UPDATE messages SET metadata = ? WHERE id = ? AND conversation_id = ?",
        (json.dumps(metadata), message_id, conv_id),
    )
    conn.commit()
    conn.close()


def search_messages(query: str, limit: int = 20):
    from carrot.search import search_conversations

    return search_conversations(query, limit=limit)


# ===== Conversation management (star / folder / delete) =====

def update_conversation_meta(conv_id: str, folder_id=None, starred=None, title=None):
    """Update a conversation's title and/or metadata (folder_id, starred).

    ``folder_id``/``starred``/``title`` of ``None`` mean "leave unchanged".
    Pass ``folder_id=""`` to clear the folder assignment.
    """
    conn = get_db()
    row = conn.execute(
        "SELECT metadata, title FROM conversations WHERE id = ?", (conv_id,)
    ).fetchone()
    if row is None:
        conn.close()
        return None
    meta = json.loads(row["metadata"] or "{}")
    if folder_id is not None:
        if folder_id == "":
            meta.pop("folder_id", None)
        else:
            meta["folder_id"] = folder_id
    if starred is not None:
        meta["starred"] = bool(starred)
    new_title = row["title"] if title is None else title
    conn.execute(
        "UPDATE conversations SET metadata = ?, title = ?, updated_at = ? WHERE id = ?",
        (json.dumps(meta), new_title, now_iso(), conv_id),
    )
    conn.commit()
    conn.close()
    return {
        "id": conv_id,
        "title": new_title,
        "metadata": meta,
    }


def delete_conversation(conv_id: str) -> bool:
    """Delete a conversation. Messages are removed via ON DELETE CASCADE."""
    conn = get_db()
    cur = conn.execute("DELETE FROM conversations WHERE id = ?", (conv_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


# ===== Chat folders =====

def list_folders():
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM chat_folders ORDER BY position, created_at"
    ).fetchall()
    conn.close()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "position": r["position"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def create_folder(name: str):
    folder_id = str(uuid.uuid4())[:12]
    ts = now_iso()
    conn = get_db()
    max_pos = conn.execute(
        "SELECT COALESCE(MAX(position), -1) AS m FROM chat_folders"
    ).fetchone()["m"]
    conn.execute(
        "INSERT INTO chat_folders (id, name, position, created_at) VALUES (?, ?, ?, ?)",
        (folder_id, name, max_pos + 1, ts),
    )
    conn.commit()
    conn.close()
    return {"id": folder_id, "name": name, "position": max_pos + 1, "created_at": ts}


def rename_folder(folder_id: str, name: str) -> bool:
    conn = get_db()
    cur = conn.execute(
        "UPDATE chat_folders SET name = ? WHERE id = ?", (name, folder_id)
    )
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def delete_folder(folder_id: str) -> bool:
    """Delete a folder, unassigning any conversations that referenced it."""
    conn = get_db()
    rows = conn.execute("SELECT id, metadata FROM conversations").fetchall()
    for r in rows:
        meta = json.loads(r["metadata"] or "{}")
        if meta.get("folder_id") == folder_id:
            meta.pop("folder_id", None)
            conn.execute(
                "UPDATE conversations SET metadata = ? WHERE id = ?",
                (json.dumps(meta), r["id"]),
            )
    cur = conn.execute("DELETE FROM chat_folders WHERE id = ?", (folder_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0