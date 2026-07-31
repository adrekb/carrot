"""Local document indexing.

Carrot's recall was limited to things typed into the chat box. This indexes the
rest of the machine — notes, papers, source files, saved pages — into the same
FTS5 + embedding pipeline, so "what did that paper say about X" works against
files the user never mentioned. It is the one thing a cloud assistant
structurally cannot do.

Indexing is incremental: files are fingerprinted by (size, mtime, sha1) and
re-read only when that changes. A scan over an unchanged tree costs a stat per
file and no parsing at all.
"""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional

from . import vectors
from .config import get_config, set_config
from .database import get_db

# Extension -> extractor kind. Anything not listed is skipped.
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".org", ".log", ".csv", ".tsv",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".env",
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".c", ".h", ".cpp", ".hpp",
    ".cs", ".go", ".rs", ".rb", ".php", ".swift", ".kt", ".scala", ".sh",
    ".bash", ".zsh", ".ps1", ".sql", ".r", ".jl", ".lua", ".vim",
}
HTML_EXTENSIONS = {".html", ".htm", ".xhtml"}
PDF_EXTENSIONS = {".pdf"}
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS | HTML_EXTENSIONS | PDF_EXTENSIONS

# Directories that are never worth indexing.
SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    "env", ".env", "dist", "build", "target", ".next", ".cache", ".idea",
    ".vscode", ".pytest_cache", ".mypy_cache", "site-packages", ".tox",
    "vendor", "Library", "AppData", "$RECYCLE.BIN", "System Volume Information",
}

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_DOC_CHARS = 400_000
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 150

_scan_lock = threading.Lock()
_scan_state: Dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "scanned": 0,
    "indexed": 0,
    "skipped": 0,
    "failed": 0,
    "current": "",
    "error": "",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ===== Configuration =====

def index_dirs() -> List[str]:
    dirs = get_config().get("index_dirs", [])
    return [d for d in dirs if isinstance(d, str) and d]


def add_index_dir(path: str) -> List[str]:
    resolved = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(resolved):
        raise ValueError(f"not a directory: {resolved}")
    dirs = index_dirs()
    if resolved not in dirs:
        dirs.append(resolved)
        set_config("index_dirs", dirs)
    return dirs


def remove_index_dir(path: str) -> List[str]:
    resolved = os.path.abspath(os.path.expanduser(path))
    dirs = [d for d in index_dirs() if d != resolved]
    set_config("index_dirs", dirs)
    return dirs


# ===== Extraction =====

def _read_text(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read(MAX_DOC_CHARS)


def _read_html(path: str) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return _read_text(path)
    soup = BeautifulSoup(_read_text(path), "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    return re.sub(r"\n{3,}", "\n\n", soup.get_text("\n"))


def _read_pdf(path: str) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(path)
    except Exception:
        return ""
    parts = []
    total = 0
    for page in reader.pages:
        try:
            text = page.extract_text() or ""
        except Exception:
            continue
        parts.append(text)
        total += len(text)
        if total >= MAX_DOC_CHARS:
            break
    return "\n\n".join(parts)


def extract_text(path: str) -> str:
    """Extract plain text from a supported file, or '' when unsupported."""
    ext = os.path.splitext(path)[1].lower()
    if ext in PDF_EXTENSIONS:
        return _read_pdf(path)
    if ext in HTML_EXTENSIONS:
        return _read_html(path)
    if ext in TEXT_EXTENSIONS:
        return _read_text(path)
    return ""


def chunk_text(text: str, size: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> List[str]:
    """Split text into overlapping chunks, preferring paragraph boundaries.

    Overlap keeps a sentence that straddles a boundary retrievable from either
    side; the boundary search stops a chunk mid-paragraph only when no break is
    available in the last quarter of the window.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    chunks: List[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            window_start = start + int(size * 0.75)
            boundary = text.rfind("\n\n", window_start, end)
            if boundary == -1:
                boundary = text.rfind("\n", window_start, end)
            if boundary == -1:
                boundary = text.rfind(". ", window_start, end)
            if boundary > start:
                end = boundary + 1
        chunk = text[start:end].strip()
        if chunk:
            chunks.append(chunk)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


# ===== Fingerprinting =====

def file_hash(path: str) -> str:
    """SHA1 of the file head — enough to detect edits without reading 8MB."""
    digest = hashlib.sha1()
    try:
        with open(path, "rb") as handle:
            digest.update(handle.read(1024 * 1024))
    except OSError:
        return ""
    return digest.hexdigest()


def _should_index(path: str, stat: os.stat_result) -> bool:
    if stat.st_size == 0 or stat.st_size > MAX_FILE_BYTES:
        return False
    return os.path.splitext(path)[1].lower() in SUPPORTED_EXTENSIONS


def walk_indexable(roots: Optional[List[str]] = None) -> Iterator[str]:
    """Yield indexable file paths beneath the configured roots."""
    for root in roots if roots is not None else index_dirs():
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")
            ]
            for filename in filenames:
                path = os.path.join(dirpath, filename)
                try:
                    stat = os.stat(path)
                except OSError:
                    continue
                if _should_index(path, stat):
                    yield path


# ===== Document store =====

def get_document(path: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute("SELECT * FROM documents WHERE path = ?", (path,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_documents(limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM documents ORDER BY indexed_at DESC LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def remove_document(path: str) -> bool:
    """Delete a document, its chunks, FTS rows, and vectors."""
    conn = get_db()
    row = conn.execute("SELECT id FROM documents WHERE path = ?", (path,)).fetchone()
    if row is None:
        conn.close()
        return False
    document_id = row["id"]
    chunk_ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM document_chunks WHERE document_id = ?", (document_id,)
        ).fetchall()
    ]
    conn.execute("DELETE FROM document_chunks WHERE document_id = ?", (document_id,))
    conn.execute("DELETE FROM documents WHERE id = ?", (document_id,))
    conn.commit()
    conn.close()
    for chunk_id in chunk_ids:
        vectors.delete(vectors.NS_CHUNK, str(chunk_id))
    return True


def index_file(path: str, force: bool = False) -> Dict[str, Any]:
    """Index one file. Returns {'status': indexed|unchanged|skipped|failed}."""
    path = os.path.abspath(path)
    try:
        stat = os.stat(path)
    except OSError as exc:
        return {"path": path, "status": "failed", "error": str(exc)}

    if not _should_index(path, stat):
        return {"path": path, "status": "skipped", "reason": "unsupported or too large"}

    digest = file_hash(path)
    existing = get_document(path)
    if (
        not force
        and existing
        and existing["hash"] == digest
        and existing["size"] == stat.st_size
        and existing["mtime"] == stat.st_mtime
    ):
        return {"path": path, "status": "unchanged", "document_id": existing["id"]}

    try:
        text = extract_text(path)
    except Exception as exc:
        return {"path": path, "status": "failed", "error": str(exc)}
    chunks = chunk_text(text)
    if not chunks:
        return {"path": path, "status": "skipped", "reason": "no extractable text"}

    if existing:
        remove_document(path)

    document_id = str(uuid.uuid4())[:12]
    conn = get_db()
    conn.execute(
        """INSERT INTO documents
           (id, path, title, ext, size, mtime, hash, chunk_count, char_count, indexed_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            document_id, path, os.path.basename(path), os.path.splitext(path)[1].lower(),
            stat.st_size, stat.st_mtime, digest, len(chunks), len(text), _now(),
        ),
    )
    chunk_ids = []
    for ordinal, chunk in enumerate(chunks):
        cursor = conn.execute(
            """INSERT INTO document_chunks (document_id, ordinal, content, created_at)
               VALUES (?, ?, ?, ?)""",
            (document_id, ordinal, chunk, _now()),
        )
        chunk_ids.append((cursor.lastrowid, chunk))
    conn.commit()
    conn.close()

    for chunk_id, chunk in chunk_ids:
        vectors.enqueue(vectors.NS_CHUNK, str(chunk_id), chunk)

    return {
        "path": path,
        "status": "indexed",
        "document_id": document_id,
        "chunks": len(chunks),
    }


# ===== Scanning =====

def scan_state() -> Dict[str, Any]:
    with _scan_lock:
        return dict(_scan_state, dirs=index_dirs(), documents=document_count())


def document_count() -> int:
    conn = get_db()
    try:
        return conn.execute("SELECT COUNT(*) AS c FROM documents").fetchone()["c"]
    except sqlite3.OperationalError:
        return 0
    finally:
        conn.close()


def run_scan(roots: Optional[List[str]] = None, force: bool = False, prune: bool = True) -> Dict[str, Any]:
    """Walk the configured roots and index everything new or changed."""
    with _scan_lock:
        if _scan_state["running"]:
            return dict(_scan_state)
        _scan_state.update(
            running=True, started_at=_now(), finished_at=None,
            scanned=0, indexed=0, skipped=0, failed=0, current="", error="",
        )

    seen = set()
    try:
        for path in walk_indexable(roots):
            seen.add(path)
            with _scan_lock:
                _scan_state["scanned"] += 1
                _scan_state["current"] = path
            result = index_file(path, force=force)
            with _scan_lock:
                if result["status"] == "indexed":
                    _scan_state["indexed"] += 1
                elif result["status"] == "failed":
                    _scan_state["failed"] += 1
                elif result["status"] == "skipped":
                    _scan_state["skipped"] += 1
        if prune:
            _prune_missing(seen, roots)
    except Exception as exc:
        with _scan_lock:
            _scan_state["error"] = str(exc)
    finally:
        with _scan_lock:
            _scan_state["running"] = False
            _scan_state["finished_at"] = _now()
            _scan_state["current"] = ""
    return scan_state()


def _prune_missing(seen: set, roots: Optional[List[str]]):
    """Drop documents whose files vanished since the last scan."""
    active_roots = roots if roots is not None else index_dirs()
    for document in list_documents(limit=100_000):
        path = document["path"]
        if path in seen:
            continue
        if not any(path.startswith(root + os.sep) or path == root for root in active_roots):
            continue
        if not os.path.exists(path):
            remove_document(path)


def start_scan_async(roots: Optional[List[str]] = None, force: bool = False) -> Dict[str, Any]:
    """Kick off a scan on a background thread and return immediately."""
    with _scan_lock:
        if _scan_state["running"]:
            return dict(_scan_state)
    threading.Thread(
        target=run_scan, kwargs={"roots": roots, "force": force},
        daemon=True, name="carrot-indexer",
    ).start()
    return {"started": True}


# ===== Search =====

def _fts_query(query: str) -> str:
    terms = [t for t in re.findall(r"\w+", query or "") if len(t) > 1]
    return " OR ".join(f'"{t}"' for t in terms)


def search_documents(query: str, limit: int = 10, hybrid_weight: float = 0.5) -> Dict[str, Any]:
    """Hybrid search over indexed chunks: FTS candidates reranked by embedding."""
    match = _fts_query(query)
    if not match:
        return {"results": [], "count": 0, "mode": "empty_query"}

    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT c.id, c.content, c.ordinal, d.path, d.title, d.ext, rank
               FROM chunks_fts f
               JOIN document_chunks c ON c.id = f.chunk_id
               JOIN documents d ON d.id = c.document_id
               WHERE chunks_fts MATCH ? ORDER BY rank LIMIT ?""",
            (match, limit * 4),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        conn.close()

    if not rows:
        return {"results": [], "count": 0, "mode": "no_match"}

    candidates = {
        str(r["id"]): {
            "chunk_id": r["id"],
            "path": r["path"],
            "title": r["title"],
            "ext": r["ext"],
            "ordinal": r["ordinal"],
            "content": r["content"],
            "fts_score": 1.0 / (1.0 + abs(r["rank"])) if r["rank"] else 0.5,
        }
        for r in rows
    }

    ranked = vectors.search_text(vectors.NS_CHUNK, query, limit=limit * 4, candidates=list(candidates))
    if not ranked:
        results = sorted(candidates.values(), key=lambda c: -c["fts_score"])[:limit]
        return {"results": results, "count": len(results), "mode": "fts_only"}

    semantic = {ref_id: score for ref_id, score in ranked}
    for ref_id, item in candidates.items():
        item["semantic_score"] = round(semantic.get(ref_id, 0.0), 4)
        item["combined_score"] = round(
            hybrid_weight * semantic.get(ref_id, 0.0) + (1 - hybrid_weight) * item["fts_score"], 4
        )
    results = sorted(candidates.values(), key=lambda c: -c["combined_score"])[:limit]
    return {"results": results, "count": len(results), "mode": "hybrid"}


def stats() -> Dict[str, Any]:
    conn = get_db()
    try:
        documents = conn.execute("SELECT COUNT(*) AS c FROM documents").fetchone()["c"]
        chunks = conn.execute("SELECT COUNT(*) AS c FROM document_chunks").fetchone()["c"]
        by_ext = conn.execute(
            "SELECT ext, COUNT(*) AS c FROM documents GROUP BY ext ORDER BY c DESC LIMIT 15"
        ).fetchall()
    except sqlite3.OperationalError:
        return {"documents": 0, "chunks": 0, "by_ext": {}, "dirs": index_dirs()}
    finally:
        conn.close()
    return {
        "documents": documents,
        "chunks": chunks,
        "by_ext": {r["ext"]: r["c"] for r in by_ext},
        "dirs": index_dirs(),
        "embedded_chunks": vectors.count(vectors.NS_CHUNK),
    }
