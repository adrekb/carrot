"""Unified vector store for Carrot.

Every embeddable thing in Carrot — chat messages, extracted memories, indexed
document chunks — stores its vector here under a ``namespace``. Embeddings are
packed as float32 blobs rather than JSON so a 768-dim vector costs ~3KB instead
of ~15KB and can be scored with a single numpy matmul.

Two backends, picked at import time:

* ``sqlite-vec`` when the extension is installed — ANN search inside SQLite.
* a numpy brute-force scan otherwise — loads the namespace into one matrix and
  scores it in a single dot product. Fine well past 100k vectors.

Embedding happens on a background worker so writing a message never blocks on
Ollama. ``backfill`` catches up anything written while the model was offline.
"""

from __future__ import annotations

import json
import queue
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from .database import get_db

EMBEDDING_MODEL = "nomic-embed-text"

# Namespaces
NS_MESSAGE = "message"
NS_MEMORY = "memory"
NS_CHUNK = "chunk"

_HAS_SQLITE_VEC: Optional[bool] = None
_worker: Optional[threading.Thread] = None
_work_queue: "queue.Queue[Optional[Tuple[str, str, str]]]" = queue.Queue()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def has_sqlite_vec() -> bool:
    """True when the sqlite-vec extension can be loaded on this install."""
    global _HAS_SQLITE_VEC
    if _HAS_SQLITE_VEC is not None:
        return _HAS_SQLITE_VEC
    try:
        import sqlite_vec  # noqa: F401

        conn = sqlite3.connect(":memory:")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.close()
        _HAS_SQLITE_VEC = True
    except Exception:
        _HAS_SQLITE_VEC = False
    return _HAS_SQLITE_VEC


# ===== Encoding =====

def pack(embedding: Sequence[float]) -> bytes:
    """Pack a float sequence into a compact float32 blob."""
    return np.asarray(embedding, dtype=np.float32).tobytes()


def unpack(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def _normalize(matrix: np.ndarray) -> np.ndarray:
    """L2-normalize rows so cosine similarity becomes a plain dot product."""
    norms = np.linalg.norm(matrix, axis=-1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


# ===== Embedding =====

def embed(text: str, model: str = EMBEDDING_MODEL) -> Optional[List[float]]:
    """Embed text via Ollama. Returns None when the model is unreachable."""
    from .ollama_client import OllamaClient

    if not text or not text.strip():
        return None
    try:
        return OllamaClient().get_embedding(text, model=model)
    except Exception:
        return None


def store(namespace: str, ref_id: str, embedding: Sequence[float], model: str = EMBEDDING_MODEL):
    """Persist one vector, replacing any existing vector for the same ref."""
    vec = np.asarray(embedding, dtype=np.float32)
    conn = get_db()
    conn.execute(
        """INSERT OR REPLACE INTO vectors
           (namespace, ref_id, model, dim, embedding, created_at)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (namespace, str(ref_id), model, int(vec.shape[0]), vec.tobytes(), _now()),
    )
    conn.commit()
    conn.close()


def embed_and_store(namespace: str, ref_id: str, text: str, model: str = EMBEDDING_MODEL) -> bool:
    """Embed text and store it. False when embedding was unavailable."""
    vector = embed(text, model=model)
    if vector is None:
        return False
    store(namespace, ref_id, vector, model=model)
    return True


def get(namespace: str, ref_id: str) -> Optional[np.ndarray]:
    conn = get_db()
    row = conn.execute(
        "SELECT embedding FROM vectors WHERE namespace = ? AND ref_id = ?",
        (namespace, str(ref_id)),
    ).fetchone()
    conn.close()
    if row is None or row["embedding"] is None:
        return None
    return unpack(row["embedding"])


def delete(namespace: str, ref_id: str):
    conn = get_db()
    conn.execute("DELETE FROM vectors WHERE namespace = ? AND ref_id = ?", (namespace, str(ref_id)))
    conn.commit()
    conn.close()


def delete_namespace(namespace: str):
    conn = get_db()
    conn.execute("DELETE FROM vectors WHERE namespace = ?", (namespace,))
    conn.commit()
    conn.close()


def count(namespace: Optional[str] = None) -> int:
    conn = get_db()
    if namespace:
        row = conn.execute("SELECT COUNT(*) AS c FROM vectors WHERE namespace = ?", (namespace,)).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS c FROM vectors").fetchone()
    conn.close()
    return row["c"]


# ===== Search =====

def _load_matrix(namespace: str, ref_ids: Optional[Iterable[str]] = None):
    """Load a namespace (or a subset) as (ref_ids, normalized matrix)."""
    conn = get_db()
    if ref_ids is not None:
        ids = [str(r) for r in ref_ids]
        if not ids:
            conn.close()
            return [], None
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"SELECT ref_id, embedding, dim FROM vectors WHERE namespace = ? AND ref_id IN ({placeholders})",
            (namespace, *ids),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT ref_id, embedding, dim FROM vectors WHERE namespace = ?", (namespace,)
        ).fetchall()
    conn.close()
    if not rows:
        return [], None

    # Mixed dimensions can appear if the embedding model was swapped; keep the
    # majority dimension and ignore stale vectors rather than crashing.
    dims = [r["dim"] for r in rows]
    target_dim = max(set(dims), key=dims.count)
    kept_ids, buffers = [], []
    for row in rows:
        if row["dim"] != target_dim or not row["embedding"]:
            continue
        kept_ids.append(row["ref_id"])
        buffers.append(unpack(row["embedding"]))
    if not kept_ids:
        return [], None
    return kept_ids, _normalize(np.vstack(buffers))


def search(
    namespace: str,
    query_embedding: Sequence[float],
    limit: int = 20,
    candidates: Optional[Iterable[str]] = None,
) -> List[Tuple[str, float]]:
    """Return [(ref_id, cosine_similarity)] ranked best-first.

    ``candidates`` restricts scoring to a pre-filtered set of refs, which is how
    hybrid search reranks FTS hits without loading the whole namespace.
    """
    ref_ids, matrix = _load_matrix(namespace, candidates)
    if matrix is None:
        return []
    query = np.asarray(query_embedding, dtype=np.float32)
    if query.shape[0] != matrix.shape[1]:
        return []
    query = query / (np.linalg.norm(query) or 1.0)
    scores = matrix @ query
    top = np.argsort(-scores)[:limit]
    return [(ref_ids[i], float(scores[i])) for i in top]


def search_text(
    namespace: str,
    text: str,
    limit: int = 20,
    candidates: Optional[Iterable[str]] = None,
) -> List[Tuple[str, float]]:
    """Embed a query string and search. Empty list when embeddings are down."""
    query = embed(text)
    if query is None:
        return []
    return search(namespace, query, limit=limit, candidates=candidates)


# ===== Background embedding worker =====

def enqueue(namespace: str, ref_id: str, text: str):
    """Queue an embed job; started lazily so tests never spawn the worker."""
    _work_queue.put((namespace, str(ref_id), text))
    start_worker()


def _worker_loop():
    while True:
        job = _work_queue.get()
        if job is None:
            break
        namespace, ref_id, text = job
        try:
            embed_and_store(namespace, ref_id, text)
        except Exception:
            pass
        finally:
            _work_queue.task_done()


def start_worker():
    """Start the embed worker thread (idempotent)."""
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    _worker = threading.Thread(target=_worker_loop, daemon=True, name="carrot-embed-worker")
    _worker.start()


def drain(timeout: float = 30.0) -> bool:
    """Block until queued embed jobs finish. Used by tests and backfill."""
    done = threading.Event()

    def _wait():
        _work_queue.join()
        done.set()

    threading.Thread(target=_wait, daemon=True).start()
    return done.wait(timeout)


# ===== Backfill =====

# What each namespace is derived from: (table, id column, text column).
BACKFILL_SOURCES: Dict[str, Tuple[str, str, str]] = {
    NS_MESSAGE: ("messages", "id", "content"),
    NS_MEMORY: ("memories", "id", "content"),
    NS_CHUNK: ("document_chunks", "id", "content"),
}


def pending(namespace: str) -> int:
    """How many rows in a namespace still have no vector."""
    source = BACKFILL_SOURCES.get(namespace)
    if source is None:
        return 0
    table, id_col, _ = source
    conn = get_db()
    try:
        row = conn.execute(
            f"""SELECT COUNT(*) AS c FROM {table} t
                WHERE NOT EXISTS (
                    SELECT 1 FROM vectors v
                    WHERE v.namespace = ? AND v.ref_id = CAST(t.{id_col} AS TEXT)
                )""",
            (namespace,),
        ).fetchone()
        return row["c"]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def backfill(namespace: str, limit: int = 200) -> Dict[str, Any]:
    """Embed rows in a namespace that have no vector yet.

    Runs synchronously and stops early when the embedding model goes away, so a
    backfill against a stopped Ollama costs one failed call rather than ``limit``.
    """
    source = BACKFILL_SOURCES.get(namespace)
    if source is None:
        return {"namespace": namespace, "embedded": 0, "error": "unknown namespace"}
    table, id_col, text_col = source

    conn = get_db()
    try:
        rows = conn.execute(
            f"""SELECT t.{id_col} AS ref_id, t.{text_col} AS content FROM {table} t
                WHERE NOT EXISTS (
                    SELECT 1 FROM vectors v
                    WHERE v.namespace = ? AND v.ref_id = CAST(t.{id_col} AS TEXT)
                )
                LIMIT ?""",
            (namespace, limit),
        ).fetchall()
    except sqlite3.OperationalError as exc:
        return {"namespace": namespace, "embedded": 0, "error": str(exc)}
    finally:
        conn.close()

    embedded = 0
    for row in rows:
        if not row["content"]:
            continue
        if not embed_and_store(namespace, str(row["ref_id"]), row["content"]):
            return {
                "namespace": namespace,
                "embedded": embedded,
                "remaining": pending(namespace),
                "error": "embedding model unavailable",
            }
        embedded += 1
    return {"namespace": namespace, "embedded": embedded, "remaining": pending(namespace)}


def backfill_all(limit_per_namespace: int = 200) -> Dict[str, Any]:
    return {
        "results": [backfill(ns, limit=limit_per_namespace) for ns in BACKFILL_SOURCES],
        "backend": "sqlite-vec" if has_sqlite_vec() else "numpy",
    }


def stats() -> Dict[str, Any]:
    return {
        "backend": "sqlite-vec" if has_sqlite_vec() else "numpy",
        "model": EMBEDDING_MODEL,
        "namespaces": [
            {"namespace": ns, "vectors": count(ns), "pending": pending(ns)}
            for ns in BACKFILL_SOURCES
        ],
        "total": count(),
    }


# ===== Legacy migration =====

def migrate_legacy_embeddings() -> int:
    """Move rows from the old JSON ``message_embeddings`` table into ``vectors``."""
    conn = get_db()
    try:
        rows = conn.execute("SELECT message_id, model, embedding FROM message_embeddings").fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return 0
    conn.close()

    moved = 0
    for row in rows:
        if not row["embedding"]:
            continue
        try:
            values = json.loads(row["embedding"])
        except (json.JSONDecodeError, TypeError):
            continue
        if not values:
            continue
        store(NS_MESSAGE, str(row["message_id"]), values, model=row["model"] or EMBEDDING_MODEL)
        moved += 1
    return moved
