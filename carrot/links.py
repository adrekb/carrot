"""Wikilinks between documents, and the graph they make.

A link is `[[Title]]` written in a document body. It resolves by *title*, not by
id, because the person writing it is thinking about the thing rather than about
the twelve hex characters we happen to have filed it under. That choice is the
whole design, and everything awkward here follows from it:

  - Titles are not unique and nothing can make them so. Two notes called
    "Notes" are a thing people do. Resolution is therefore lowest-id-wins so
    that the same text resolves to the same note on every machine and across
    every reload, rather than to whatever `listdir` happened to yield first.
  - A title can change, and when it does the links pointing at the old one stop
    resolving. We do not rewrite other people's documents to compensate. The
    link becomes unresolved and shows up as such, which is honest and which the
    person can fix; silently editing files somebody did not open is not a
    behaviour worth having.
  - A link to a document that does not exist yet is not an error. It is how you
    write — you mention the thing, and you make it later. Those are carried
    through as unresolved targets so the graph can show them and so a click can
    offer to create one.

The index is rebuilt from the notes directory on demand rather than maintained
incrementally. There is no write path that could drift from it, no cache file
to invalidate, and no ordering problem between "the note was saved" and "its
links were recorded". For the number of documents a person actually writes this
costs milliseconds, and when that stops being true the fix is a cache keyed on
mtime, not a second source of truth.
"""

import os
import re

from . import notes as notes_mod


# `[[Target]]` or `[[Target|what to show instead]]`.
#
# The target stops at `|` or `]`, so a display alias never leaks into the thing
# being resolved. Newlines are excluded: an unclosed `[[` at the end of a line
# is somebody mid-keystroke, not a link that spans a paragraph.
WIKILINK_RE = re.compile(r"\[\[([^\[\]|\n]+?)(?:\|([^\[\]\n]*?))?\]\]")

# Fenced blocks and inline spans, removed before scanning.
#
# Without this, documentation about this very feature becomes a note that links
# to everything it mentions — writing ``[[Title]]`` to explain the syntax would
# forge an edge. Code is quoted text, and quoted text is not a link.
_FENCE_RE = re.compile(r"^```.*?^```", re.MULTILINE | re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")


def _strip_code(text: str) -> str:
    """Blank out code so links inside it are not links.

    Replaced with spaces rather than deleted so that any offset computed
    against the result still lines up with the original text.
    """
    def blank(match):
        return re.sub(r"\S", " ", match.group(0))

    return _INLINE_CODE_RE.sub(blank, _FENCE_RE.sub(blank, text or ""))


def normalize_title(title: str) -> str:
    """The key a link resolves on: case- and space-insensitive.

    `[[lecture 1]]`, `[[Lecture 1]]` and `[[Lecture  1]]` are the same
    reference, because a person retyping the name of their own note is not
    trying to make a different one.
    """
    return re.sub(r"\s+", " ", (title or "").strip()).lower()


def extract_links(body: str):
    """Every wikilink in a document body, in the order written.

    Returns a list of `{"target", "alias", "key"}`. Duplicates are kept — the
    same note linked three times is three links, and the graph decides for
    itself whether to collapse them into one edge.
    """
    out = []
    for match in WIKILINK_RE.finditer(_strip_code(body)):
        target = (match.group(1) or "").strip()
        if not target:
            continue
        alias = (match.group(2) or "").strip()
        out.append({"target": target, "alias": alias, "key": normalize_title(target)})
    return out


def _title_index(notes):
    """Map normalized title -> note id, lowest id winning a collision.

    Sorted so the winner does not depend on directory order. See the module
    docstring: two notes with one title is a situation to be deterministic
    about, not one to be correct about.
    """
    index = {}
    for note in sorted(notes, key=lambda n: n.get("id") or ""):
        key = normalize_title(note.get("title") or "")
        if key and key not in index:
            index[key] = note.get("id")
    return index


def _linkable(notes):
    """Documents that participate in the link graph.

    Canvases and slide decks are excluded as *sources*: their bodies are JSON
    and slide markup, and scanning them yields nothing but noise. They remain
    valid *targets*, because linking to your canvas from a note is a reasonable
    thing to want.
    """
    return [n for n in notes
            if notes_mod.normalize_format(n.get("format")) == notes_mod.FORMAT_MARKDOWN]


def build_index():
    """The whole link graph, as nodes and edges.

    Nodes are documents plus one synthetic node per unresolved target, so that
    a note you have mentioned but not written yet still appears — that is the
    thing the graph is most useful for.
    """
    notes = notes_mod.list_notes()
    by_title = _title_index(notes)
    by_id = {n.get("id"): n for n in notes}

    edges = []
    seen_edges = set()
    unresolved = {}

    for note in _linkable(notes):
        source = note.get("id")
        for link in extract_links(note.get("body") or ""):
            target_id = by_title.get(link["key"])
            if target_id == source:
                continue  # A note linking to itself is not an edge.
            if target_id:
                pair = (source, target_id)
                if pair in seen_edges:
                    continue
                seen_edges.add(pair)
                edges.append({"source": source, "target": target_id, "resolved": True})
            else:
                # Keep the first spelling seen, for display.
                unresolved.setdefault(link["key"], link["target"])
                ghost = "ghost:" + link["key"]
                pair = (source, ghost)
                if pair in seen_edges:
                    continue
                seen_edges.add(pair)
                edges.append({"source": source, "target": ghost, "resolved": False})

    nodes = [
        {
            "id": note.get("id"),
            "title": note.get("title") or note.get("id"),
            "format": notes_mod.normalize_format(note.get("format")),
            "exists": True,
        }
        for note in notes
    ]
    nodes.extend(
        {"id": "ghost:" + key, "title": title, "format": "", "exists": False}
        for key, title in sorted(unresolved.items())
    )

    # Degree drives node size in the graph, and counts both directions: a note
    # everything points at is as central as one pointing everywhere.
    degree = {n["id"]: 0 for n in nodes}
    for edge in edges:
        for end in (edge["source"], edge["target"]):
            if end in degree:
                degree[end] += 1
    for node in nodes:
        node["degree"] = degree.get(node["id"], 0)

    return {"nodes": nodes, "edges": edges, "by_id": by_id}


def graph():
    """Nodes and edges only — what the graph view renders."""
    index = build_index()
    return {"nodes": index["nodes"], "edges": index["edges"]}


def backlinks(note_id: str):
    """Documents linking *to* this one, with the surrounding sentence.

    The context is the point. A bare list of titles makes you open each one to
    remember why it points here; the line it was written on usually answers
    that without leaving the document you are in.
    """
    notes = notes_mod.list_notes()
    target = notes_mod.get_note(note_id)
    if target is None:
        return []
    target_key = normalize_title(target.get("title") or "")
    if not target_key:
        return []
    by_title = _title_index(notes)
    # Only report backlinks if this note is the one that title resolves to —
    # otherwise the loser of a title collision claims the winner's inbound links.
    if by_title.get(target_key) != note_id:
        return []

    out = []
    for note in _linkable(notes):
        if note.get("id") == note_id:
            continue
        body = note.get("body") or ""
        stripped = _strip_code(body)
        contexts = []
        for match in WIKILINK_RE.finditer(stripped):
            if normalize_title(match.group(1)) != target_key:
                continue
            contexts.append(_context_around(body, match.start(), match.end()))
        if contexts:
            out.append({
                "id": note.get("id"),
                "title": note.get("title") or note.get("id"),
                "count": len(contexts),
                "contexts": contexts[:3],
            })
    return sorted(out, key=lambda item: item["title"].lower())


def _context_around(body: str, start: int, end: int, window: int = 90) -> str:
    """The text around a link, trimmed to something readable on one line."""
    left = max(0, start - window)
    right = min(len(body), end + window)
    snippet = body[left:right].replace("\n", " ")
    snippet = re.sub(r"\s+", " ", snippet).strip()
    if left > 0:
        snippet = "…" + snippet
    if right < len(body):
        snippet = snippet + "…"
    return snippet


def suggest(query: str, limit: int = 8):
    """Autocomplete for `[[`.

    Titles starting with what has been typed come before titles merely
    containing it, because the first is almost always what somebody means and
    the difference is obvious the moment the list is wrong.
    """
    key = normalize_title(query)
    notes = notes_mod.list_notes()
    starts, contains = [], []
    for note in notes:
        title = note.get("title") or note.get("id")
        norm = normalize_title(title)
        if not norm:
            continue
        item = {
            "id": note.get("id"),
            "title": title,
            "format": notes_mod.normalize_format(note.get("format")),
        }
        if not key or norm.startswith(key):
            starts.append(item)
        elif key in norm:
            contains.append(item)
    ordered = (sorted(starts, key=lambda i: i["title"].lower())
               + sorted(contains, key=lambda i: i["title"].lower()))
    return ordered[:limit]


def resolve(title: str):
    """The note a `[[title]]` points at, or None if nothing is called that."""
    notes = notes_mod.list_notes()
    note_id = _title_index(notes).get(normalize_title(title))
    return notes_mod.get_note(note_id) if note_id else None
