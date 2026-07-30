import re
import json
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any

import numpy as np
import requests

from carrot.database import get_db
from carrot.ollama_client import OllamaClient

EMBEDDING_MODEL = "nomic-embed-text"


TIME_PATTERNS = [
    (r"(\d+)\s*years?\s*ago", lambda n: ("years", int(n))),
    (r"(\d+)\s*months?\s*ago", lambda n: ("months", int(n))),
    (r"(\d+)\s*weeks?\s*ago", lambda n: ("weeks", int(n))),
    (r"(\d+)\s*days?\s*ago", lambda n: ("days", int(n))),
    (r"last\s+year", lambda _: ("years", 1)),
    (r"last\s+month", lambda _: ("months", 1)),
    (r"last\s+week", lambda _: ("weeks", 1)),
    (r"last\s+day", lambda _: ("days", 1)),
    (r"yesterday", lambda _: ("days", 1)),
    (r"today", lambda _: ("days", 0)),
]

SEMANTIC_STOP_WORDS = {
    "what", "where", "when", "how", "who", "which", "why", "was", "were",
    "is", "are", "been", "be", "have", "has", "had", "do", "does", "did",
    "will", "would", "could", "should", "may", "might", "shall", "can",
    "the", "a", "an", "in", "on", "at", "to", "for", "of", "with", "by",
    "from", "about", "into", "through", "during", "before", "after", "above",
    "below", "between", "under", "again", "further", "then", "once",
    "this", "that", "these", "those", "my", "your", "his", "her", "its",
    "our", "their", "i", "me", "he", "she", "it", "we", "they", "you",
    "and", "or", "but", "not", "no", "so", "if", "as", "until", "while",
    "ago", "last", "today", "yesterday",
}


def parse_time_offset(query: str):
    for pattern, handler in TIME_PATTERNS:
        match = re.search(pattern, query, re.IGNORECASE)
        if match:
            unit, amount = handler(match.group(1) if match.lastindex else None)
            now = datetime.now()
            if unit == "years":
                start = now - timedelta(days=amount * 365)
            elif unit == "months":
                start = now - timedelta(days=amount * 30)
            elif unit == "weeks":
                start = now - timedelta(weeks=amount)
            elif unit == "days":
                start = now - timedelta(days=amount)
            else:
                start = now
            end = now
            cleaned = re.sub(pattern, "", query, flags=re.IGNORECASE).strip()
            return {"start": start.isoformat(), "end": end.isoformat(), "keywords": cleaned}
    return None


def extract_keywords(query: str):
    query_lower = query.lower()
    for pattern, _ in TIME_PATTERNS:
        query_lower = re.sub(pattern, "", query_lower, flags=re.IGNORECASE)
    tokens = re.findall(r"[a-z0-9_]+", query_lower)
    keywords = [t for t in tokens if t not in SEMANTIC_STOP_WORDS and len(t) > 1]
    return " ".join(keywords)


def get_embedding(text: str) -> Optional[List[float]]:
    client = OllamaClient()
    if not client.is_available():
        return None
    try:
        resp = requests.post(
            client._url("/api/embeddings"),
            json={"model": EMBEDDING_MODEL, "prompt": text},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("embedding")
    except Exception:
        return None


def cosine_similarity(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    a_np = np.array(a, dtype=np.float32)
    b_np = np.array(b, dtype=np.float32)
    dot = np.dot(a_np, b_np)
    norm_a = np.linalg.norm(a_np)
    norm_b = np.linalg.norm(b_np)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(dot / (norm_a * norm_b))


def store_embedding(message_id: int, embedding: List[float]):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO message_embeddings (message_id, embedding) VALUES (?, ?)",
        (message_id, json.dumps(embedding)),
    )
    conn.commit()
    conn.close()


def get_embedding_for_message(message_id: int) -> Optional[List[float]]:
    conn = get_db()
    row = conn.execute(
        "SELECT embedding FROM message_embeddings WHERE message_id = ?",
        (message_id,),
    ).fetchone()
    conn.close()
    if row and row["embedding"]:
        return json.loads(row["embedding"])
    return None


def classify_query(query: str) -> Dict[str, Any]:
    client = OllamaClient()
    if not client.is_available():
        return {
            "search_keywords": extract_keywords(query),
            "time_cutoff_days": 0,
            "intent": "general",
            "entities": [],
        }
    return client.classify_query(query)


def search_conversations(query: str, limit: int = 20, hybrid_weight: float = 0.5):
    time_info = parse_time_offset(query)
    if time_info:
        keywords = extract_keywords(query)
        start = time_info["start"]
        end = time_info["end"]
    else:
        keywords = query.strip()
        conn = get_db()
        row = conn.execute(
            "SELECT updated_at FROM conversations ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if row and row["updated_at"]:
            start = "2020-01-01T00:00:00"
            end = datetime.now().isoformat()
        else:
            start = "2020-01-01T00:00:00"
            end = datetime.now().isoformat()
        conn.close()

    if not keywords:
        return {"results": [], "query": query, "time_range": None}

    conn = get_db()
    if time_info:
        rows = conn.execute(
            """
            SELECT m.rowid as message_id, m.conversation_id, c.title as conversation_title,
                   m.role, m.content, m.timestamp, rank
            FROM messages_fts m
            JOIN conversations c ON m.conversation_id = c.id
            WHERE m.messages_fts MATCH ?
              AND m.timestamp >= ?
              AND m.timestamp <= ?
            ORDER BY rank
            LIMIT ?
            """,
            (keywords, start, end, limit * 3),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT m.rowid as message_id, m.conversation_id, c.title as conversation_title,
                   m.role, m.content, m.timestamp, rank
            FROM messages_fts m
            JOIN conversations c ON m.conversation_id = c.id
            WHERE m.messages_fts MATCH ?
            ORDER BY rank
            LIMIT ?
            """,
            (keywords, limit * 3),
        ).fetchall()

    fts_results = []
    for row in rows:
        fts_results.append(
            {
                "message_id": row["message_id"],
                "conversation_id": row["conversation_id"],
                "conversation_title": row["conversation_title"],
                "role": row["role"],
                "content": row["content"],
                "timestamp": row["timestamp"],
                "fts_rank": row["rank"],
            }
        )
    conn.close()

    if not fts_results:
        return {
            "results": [],
            "query": query,
            "time_range": time_info,
            "count": 0,
            "mode": "fts_only",
        }

    query_embedding = get_embedding(keywords)
    if query_embedding is None:
        return _format_results(fts_results[:limit], query, time_info, "fts_fallback")

    scored_results = []
    for r in fts_results:
        msg_embedding = get_embedding_for_message(r["message_id"])
        if msg_embedding is not None:
            sem_score = cosine_similarity(query_embedding, msg_embedding)
        else:
            sem_score = 0.0

        fts_score = 1.0 / (1.0 + abs(r["fts_rank"])) if r["fts_rank"] != 0 else 0.5

        combined = hybrid_weight * sem_score + (1 - hybrid_weight) * fts_score
        scored_results.append({**r, "semantic_score": sem_score, "fts_score": fts_score, "combined_score": combined})

    scored_results.sort(key=lambda x: x["combined_score"], reverse=True)
    return _format_results(scored_results[:limit], query, time_info, "hybrid")


def _format_results(results, query, time_info, mode):
    return {
        "results": [
            {
                "conversation_id": r["conversation_id"],
                "conversation_title": r["conversation_title"],
                "role": r["role"],
                "content": r["content"],
                "timestamp": r["timestamp"],
                "semantic_score": round(r.get("semantic_score", 0), 4),
                "fts_score": round(r.get("fts_score", 0), 4),
                "combined_score": round(r.get("combined_score", 0), 4),
            }
            for r in results
        ],
        "query": query,
        "time_range": time_info,
        "count": len(results),
        "mode": mode,
    }