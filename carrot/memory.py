"""Structured long-term memory.

Chat search answers "what did I say"; memory answers "what is true about me".
After each turn Carrot asks the model to extract durable facts — preferences,
decisions, stable attributes — and stores them as first-class rows with a link
back to the message they came from, so every belief can be traced to its source
and audited by the user.

Design rules that matter:

* **Provenance is mandatory.** Every memory records the message and conversation
  it was derived from. A memory with no source is a hallucination we can't check.
* **Supersede, never overwrite.** A new value for an existing (kind, subject)
  marks the old row ``superseded`` and links forward. History stays intact, so
  "what did I used to think about X" still works.
* **The user is the authority.** Memories can be edited, pinned, or rejected;
  rejected subjects are not re-extracted.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import vectors
from .database import get_db

KINDS = ("fact", "preference", "decision", "attribute", "relationship", "project")

STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_REJECTED = "rejected"

# Ollama structured-output schema for the extractor.
EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(KINDS)},
                    "subject": {"type": "string"},
                    "content": {"type": "string"},
                    "confidence": {"type": "number"},
                },
                "required": ["kind", "subject", "content"],
            },
        }
    },
    "required": ["memories"],
}

EXTRACTION_PROMPT = """You extract durable facts about the user from a conversation turn.

Record only information that stays true beyond this conversation: stable
preferences, personal attributes, commitments, decisions, ongoing projects, and
relationships.

Do NOT record:
- questions the user asked, or anything the assistant said about itself
- one-off task instructions ("summarize this", "fix that bug")
- transient state ("I'm tired right now")
- anything you inferred rather than read

Each memory needs:
- kind: one of {kinds}
- subject: 1-3 word key for the thing it is about (e.g. "bench press", "editor")
- content: one self-contained sentence, written in the third person about the user
- confidence: 0.0-1.0, how certain you are this is durable and explicitly stated

Return {{"memories": []}} when the turn contains nothing durable. That is the
common case — do not invent memories to seem useful."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_memory(row: sqlite3.Row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "subject": row["subject"],
        "content": row["content"],
        "confidence": row["confidence"],
        "status": row["status"],
        "pinned": bool(row["pinned"]),
        "source_message_id": row["source_message_id"],
        "source_conversation_id": row["source_conversation_id"],
        "superseded_by": row["superseded_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


# ===== CRUD =====

def create(
    kind: str,
    subject: str,
    content: str,
    confidence: float = 1.0,
    source_message_id: Optional[int] = None,
    source_conversation_id: Optional[str] = None,
    pinned: bool = False,
) -> Dict[str, Any]:
    """Store a memory, superseding any active memory with the same key."""
    kind = kind if kind in KINDS else "fact"
    subject = (subject or "").strip().lower()
    memory_id = str(uuid.uuid4())[:12]
    now = _now()

    conn = get_db()
    previous = conn.execute(
        """SELECT id FROM memories
           WHERE kind = ? AND subject = ? AND status = ?""",
        (kind, subject, STATUS_ACTIVE),
    ).fetchall()
    conn.execute(
        """INSERT INTO memories
           (id, kind, subject, content, confidence, status, pinned,
            source_message_id, source_conversation_id, superseded_by, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)""",
        (
            memory_id, kind, subject, content.strip(), float(confidence), STATUS_ACTIVE,
            1 if pinned else 0, source_message_id, source_conversation_id, now, now,
        ),
    )
    for row in previous:
        conn.execute(
            "UPDATE memories SET status = ?, superseded_by = ?, updated_at = ? WHERE id = ?",
            (STATUS_SUPERSEDED, memory_id, now, row["id"]),
        )
    conn.commit()
    conn.close()

    vectors.enqueue(vectors.NS_MEMORY, memory_id, content)

    # A memory belongs where the conversation that produced it belongs, which
    # keeps an extraction running in the background from landing in whatever
    # workspace happens to be active when it finishes.
    try:
        from . import workspaces as workspaces_mod

        target = None
        if source_conversation_id:
            target = workspaces_mod.workspace_of(
                workspaces_mod.KIND_CONVERSATION, source_conversation_id
            )
        workspaces_mod.file_item(workspaces_mod.KIND_MEMORY, memory_id, target)
    except Exception:
        pass

    return get(memory_id)


def get(memory_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    conn.close()
    return _row_to_memory(row) if row else None


def list_memories(
    kind: Optional[str] = None,
    status: Optional[str] = STATUS_ACTIVE,
    subject: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    query = "SELECT * FROM memories WHERE 1=1"
    params: List[Any] = []
    if kind:
        query += " AND kind = ?"
        params.append(kind)
    if status:
        query += " AND status = ?"
        params.append(status)
    if subject:
        query += " AND subject LIKE ?"
        params.append(f"%{subject.lower()}%")
    query += " ORDER BY pinned DESC, updated_at DESC LIMIT ?"
    params.append(limit)

    conn = get_db()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [_row_to_memory(r) for r in rows]


def update(memory_id: str, **fields) -> Optional[Dict[str, Any]]:
    """Edit a memory. Accepts content, subject, kind, confidence, pinned, status."""
    allowed = {"content", "subject", "kind", "confidence", "pinned", "status"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return get(memory_id)
    if "pinned" in updates:
        updates["pinned"] = 1 if updates["pinned"] else 0
    if "subject" in updates:
        updates["subject"] = str(updates["subject"]).strip().lower()

    assignments = ", ".join(f"{k} = ?" for k in updates)
    conn = get_db()
    conn.execute(
        f"UPDATE memories SET {assignments}, updated_at = ? WHERE id = ?",
        (*updates.values(), _now(), memory_id),
    )
    conn.commit()
    conn.close()

    if "content" in updates:
        vectors.enqueue(vectors.NS_MEMORY, memory_id, updates["content"])
    return get(memory_id)


def delete(memory_id: str) -> bool:
    conn = get_db()
    cursor = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    if deleted:
        vectors.delete(vectors.NS_MEMORY, memory_id)
    return deleted


def reject(memory_id: str) -> Optional[Dict[str, Any]]:
    """Mark a memory wrong. Rejected subjects are excluded from re-extraction."""
    return update(memory_id, status=STATUS_REJECTED)


def history(subject: str, kind: Optional[str] = None) -> List[Dict[str, Any]]:
    """Every version of a subject, oldest first — the "what changed" view."""
    query = "SELECT * FROM memories WHERE subject = ?"
    params: List[Any] = [subject.strip().lower()]
    if kind:
        query += " AND kind = ?"
        params.append(kind)
    # `rowid` breaks the tie, and has to. Two memories written in the same
    # clock tick — which is a whole millisecond or more on Windows — carry the
    # identical `created_at`, and SQLite is then free to return them in either
    # order. That is how a superseding fact could appear *before* the fact it
    # replaced, in the one view whose entire job is saying what changed.
    query += " ORDER BY created_at ASC, rowid ASC"
    conn = get_db()
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return [_row_to_memory(r) for r in rows]


# ===== Search & recall =====

def search(query: str, limit: int = 10, include_superseded: bool = False,
           workspace_id: str = "") -> List[Dict[str, Any]]:
    """Hybrid memory search: FTS candidates reranked by embedding similarity.

    Scoped to a workspace, this is what stops a question about your thesis from
    recalling what you decided about a side project in March.
    """
    candidates = _fts_candidates(query, limit * 4, include_superseded, workspace_id)
    if not candidates:
        candidates = {
            m["id"]: m
            for m in list_memories(status=None if include_superseded else STATUS_ACTIVE, limit=limit * 4)
        }
        candidates = _keep_in_workspace(candidates, workspace_id)
        if not candidates:
            return []

    ranked = vectors.search_text(vectors.NS_MEMORY, query, limit=limit, candidates=list(candidates))
    if not ranked:
        return list(candidates.values())[:limit]

    results = []
    for ref_id, score in ranked:
        memory = candidates.get(ref_id)
        if memory:
            results.append({**memory, "score": round(score, 4)})
    return results


def _keep_in_workspace(candidates: Dict[str, Dict[str, Any]], workspace_id: str) -> Dict[str, Dict[str, Any]]:
    """Drop memories filed outside the active workspace.

    Used on the fallback path, where the candidates did not come from a query
    that could carry the scope in SQL.
    """
    if not workspace_id or not candidates:
        return candidates
    from . import workspaces as workspaces_mod

    allowed = set(workspaces_mod.item_ids(workspace_id, workspaces_mod.KIND_MEMORY))
    return {mid: memory for mid, memory in candidates.items() if mid in allowed}


def _fts_candidates(query: str, limit: int, include_superseded: bool,
                    workspace_id: str = "") -> Dict[str, Dict[str, Any]]:
    match = _fts_query(query)
    if not match:
        return {}
    from . import workspaces as workspaces_mod

    scope_sql, scope_params = workspaces_mod.scope_clause(
        workspaces_mod.KIND_MEMORY, workspace_id, "m.id"
    )
    sql = """SELECT m.* FROM memories_fts f
             JOIN memories m ON m.id = f.memory_id
             WHERE memories_fts MATCH ?"""
    params: List[Any] = [match]
    if not include_superseded:
        sql += " AND m.status = ?"
        params.append(STATUS_ACTIVE)
    sql += scope_sql
    params.extend(scope_params)
    sql += " ORDER BY rank LIMIT ?"
    params.append(limit)

    conn = get_db()
    try:
        rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return {}
    finally:
        conn.close()
    return {r["id"]: _row_to_memory(r) for r in rows}


def _fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 OR-query.

    User text goes straight into MATCH, so bare quotes or operators like NEAR
    would be parsed as syntax and raise. Keeping word characters only and
    quoting each term makes any input safe.
    """
    terms = [t for t in re.findall(r"\w+", query or "") if len(t) > 1]
    return " OR ".join(f'"{t}"' for t in terms)


def recall(query: str, limit: int = 8, workspace_id: str = "") -> List[Dict[str, Any]]:
    """Memories relevant to a query, with pinned ones always included.

    This is what gets injected into the chat prompt. A pinned memory survives
    the workspace scope only if it is filed there or unfiled — pinning means
    "always relevant", but a pin inside one project should not follow you into
    another.
    """
    pinned = [m for m in list_memories(limit=limit) if m["pinned"]]
    if workspace_id:
        from . import workspaces as workspaces_mod

        pinned = [
            m for m in pinned
            if workspaces_mod.workspace_of(workspaces_mod.KIND_MEMORY, m["id"]) in (workspace_id, None)
        ]
    relevant = search(query, limit=limit, workspace_id=workspace_id)
    combined: Dict[str, Dict[str, Any]] = {m["id"]: m for m in pinned}
    for memory in relevant:
        combined.setdefault(memory["id"], memory)
    return list(combined.values())[:limit]


def as_prompt_block(memories: List[Dict[str, Any]]) -> str:
    """Render memories as a system-prompt block, or '' when there are none."""
    if not memories:
        return ""
    lines = [f"- ({m['kind']}) {m['content']}" for m in memories]
    return (
        "What you know about the user from previous conversations:\n"
        + "\n".join(lines)
        + "\n\nUse this context when it is relevant. Do not recite it back "
        "unprompted, and prefer what the user says now over what you remember."
    )


# ===== Extraction =====

def _rejected_subjects() -> set:
    conn = get_db()
    rows = conn.execute(
        "SELECT DISTINCT subject FROM memories WHERE status = ?", (STATUS_REJECTED,)
    ).fetchall()
    conn.close()
    return {r["subject"] for r in rows}


def extract_from_turn(
    user_text: str,
    assistant_text: str = "",
    conversation_id: Optional[str] = None,
    message_id: Optional[int] = None,
    model: Optional[str] = None,
    min_confidence: float = 0.6,
) -> List[Dict[str, Any]]:
    """Ask the model for durable facts in a turn and store the confident ones."""
    from .ollama_client import OllamaClient

    if not user_text or not user_text.strip():
        return []

    client = OllamaClient()
    try:
        if not client.is_available():
            return []
    except Exception:
        return []

    turn = f"User: {user_text}"
    if assistant_text:
        turn += f"\n\nAssistant: {assistant_text[:1500]}"

    try:
        raw = client.structured_chat(
            [
                {"role": "system", "content": EXTRACTION_PROMPT.format(kinds=", ".join(KINDS))},
                {"role": "user", "content": turn},
            ],
            model=model,
            response_format=EXTRACTION_SCHEMA,
        )
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError, Exception):
        return []

    rejected = _rejected_subjects()
    stored = []
    for item in (payload or {}).get("memories", [])[:10]:
        content = str(item.get("content", "")).strip()
        subject = str(item.get("subject", "")).strip().lower()
        if not content or not subject or subject in rejected:
            continue
        try:
            confidence = float(item.get("confidence", 0.8))
        except (TypeError, ValueError):
            confidence = 0.8
        if confidence < min_confidence:
            continue
        if _is_duplicate(subject, content):
            continue
        stored.append(
            create(
                kind=str(item.get("kind", "fact")),
                subject=subject,
                content=content,
                confidence=confidence,
                source_message_id=message_id,
                source_conversation_id=conversation_id,
            )
        )
    return stored


def _is_duplicate(subject: str, content: str) -> bool:
    """Skip re-storing a memory whose text already exists for that subject."""
    conn = get_db()
    row = conn.execute(
        """SELECT id FROM memories
           WHERE subject = ? AND status = ? AND LOWER(content) = LOWER(?)""",
        (subject, STATUS_ACTIVE, content.strip()),
    ).fetchone()
    conn.close()
    return row is not None


def stats() -> Dict[str, Any]:
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) AS c FROM memories").fetchone()["c"]
    by_kind = conn.execute(
        "SELECT kind, COUNT(*) AS c FROM memories WHERE status = ? GROUP BY kind",
        (STATUS_ACTIVE,),
    ).fetchall()
    by_status = conn.execute("SELECT status, COUNT(*) AS c FROM memories GROUP BY status").fetchall()
    conn.close()
    return {
        "total": total,
        "by_kind": {r["kind"]: r["c"] for r in by_kind},
        "by_status": {r["status"]: r["c"] for r in by_status},
    }
