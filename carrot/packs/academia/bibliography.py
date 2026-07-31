"""BibTeX handling and citation hygiene.

The failure this exists to prevent is the one that gets papers desk-rejected:
a ``\\cite`` whose key is not in the ``.bib``, an entry missing the fields its
type requires, or two entries for the same paper under different keys. None of
that needs a model or a network — it is parsing and set arithmetic — so it runs
locally and deterministically, and the model calls it rather than eyeballing it.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ... import agent_tools
from .latex import CITE_PATTERN, strip_comments

ENTRY_PATTERN = re.compile(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", re.MULTILINE)

# Fields BibTeX itself requires. Getting these wrong is the most common reason
# a bibliography renders with a silent "??" in the PDF.
REQUIRED_FIELDS = {
    "article": ["author", "title", "journal", "year"],
    "book": ["author", "title", "publisher", "year"],
    "inproceedings": ["author", "title", "booktitle", "year"],
    "incollection": ["author", "title", "booktitle", "publisher", "year"],
    "phdthesis": ["author", "title", "school", "year"],
    "mastersthesis": ["author", "title", "school", "year"],
    "techreport": ["author", "title", "institution", "year"],
    "misc": [],
    "unpublished": ["author", "title", "note"],
}

NOISE_WORDS = {"a", "an", "the", "on", "of", "for", "and", "in", "with", "to"}


def _split_fields(block: str) -> Dict[str, str]:
    """Pull ``key = {value}`` pairs out of one entry body, brace-aware."""
    fields: Dict[str, str] = {}
    index = 0
    while index < len(block):
        match = re.compile(r"(\w+)\s*=\s*").search(block, index)
        if not match:
            break
        key = match.group(1).lower()
        cursor = match.end()
        if cursor >= len(block):
            break

        if block[cursor] in "{\"":
            opening = block[cursor]
            closing = "}" if opening == "{" else '"'
            depth = 0
            start = cursor + 1
            while cursor < len(block):
                char = block[cursor]
                if char == "{":
                    depth += 1
                elif char == "}":
                    depth -= 1
                    if opening == "{" and depth == 0:
                        break
                elif char == closing and opening == '"' and cursor > start:
                    break
                cursor += 1
            value = block[start:cursor]
            index = cursor + 1
        else:
            end = block.find(",", cursor)
            end = len(block) if end == -1 else end
            value = block[cursor:end]
            index = end + 1
        fields[key] = re.sub(r"\s+", " ", value).strip()
    return fields


def parse_bibtex(source: str) -> List[Dict[str, Any]]:
    """Parse a .bib file into entries. Tolerant — a bad entry is skipped, not fatal."""
    entries: List[Dict[str, Any]] = []
    for match in ENTRY_PATTERN.finditer(source):
        kind, key = match.group(1).lower(), match.group(2)
        if kind in ("comment", "preamble", "string"):
            continue

        # Walk to the entry's closing brace so nested braces stay intact.
        depth, cursor = 1, match.end()
        while cursor < len(source) and depth:
            if source[cursor] == "{":
                depth += 1
            elif source[cursor] == "}":
                depth -= 1
            cursor += 1
        entries.append({
            "type": kind,
            "key": key,
            "fields": _split_fields(source[match.end():cursor - 1]),
        })
    return entries


def _title_signature(entry: Dict[str, Any]) -> str:
    """A normalized title, for spotting the same paper cited under two keys."""
    title = entry["fields"].get("title", "").lower()
    words = [w for w in re.findall(r"[a-z0-9]+", title) if w not in NOISE_WORDS]
    return " ".join(words)


def check(bib_source: str, tex_source: str = "") -> Dict[str, Any]:
    """Cross-check a bibliography against the document that cites it."""
    entries = parse_bibtex(bib_source)
    by_key = {entry["key"]: entry for entry in entries}

    duplicate_keys = sorted({
        entry["key"] for entry in entries
        if sum(1 for e in entries if e["key"] == entry["key"]) > 1
    })

    seen: Dict[str, str] = {}
    duplicate_works: List[Dict[str, str]] = []
    for entry in entries:
        signature = _title_signature(entry)
        if not signature:
            continue
        if signature in seen and seen[signature] != entry["key"]:
            duplicate_works.append({"keys": f"{seen[signature]}, {entry['key']}",
                                    "title": entry["fields"].get("title", "")})
        else:
            seen[signature] = entry["key"]

    incomplete = []
    for entry in entries:
        missing = [
            field for field in REQUIRED_FIELDS.get(entry["type"], [])
            if not entry["fields"].get(field)
        ]
        if missing:
            incomplete.append({"key": entry["key"], "type": entry["type"],
                               "missing": missing})

    cited: List[str] = []
    if tex_source:
        for match in CITE_PATTERN.finditer(strip_comments(tex_source)):
            cited.extend(key.strip() for key in match.group(1).split(",") if key.strip())

    unique_cited = sorted(set(cited))
    return {
        "entries": len(entries),
        "cited": len(unique_cited),
        "missing_entries": [key for key in unique_cited if key not in by_key],
        "uncited_entries": (
            sorted(key for key in by_key if key not in set(unique_cited))
            if tex_source else []
        ),
        "duplicate_keys": duplicate_keys,
        "duplicate_works": duplicate_works,
        "incomplete": incomplete,
    }


def format_report(result: Dict[str, Any]) -> str:
    lines = [f"{result['entries']} bibliography entries, {result['cited']} cited"]

    def section(title: str, items: List[str]):
        if items:
            lines.append(f"\n{title}:")
            lines.extend(f"  - {item}" for item in items[:40])

    section("Cited but not in the bibliography (these render as ??)",
            result["missing_entries"])
    section("In the bibliography but never cited", result["uncited_entries"])
    section("Duplicate keys", result["duplicate_keys"])
    section("Same work under two keys",
            [f"{d['keys']} — {d['title']}" for d in result["duplicate_works"]])
    section("Missing required fields",
            [f"{i['key']} ({i['type']}): {', '.join(i['missing'])}" for i in result["incomplete"]])

    if len(lines) == 1:
        lines.append("No problems found.")
    return "\n".join(lines)


# ===== Tools =====

def _load(source: str, path: str, what: str) -> str:
    if source:
        return source
    if not path:
        raise ValueError(f"give either the {what} or a workspace path to it")
    with open(agent_tools.resolve(path), "r", encoding="utf-8") as handle:
        return handle.read()


def _tool_check(bib: str = "", bib_path: str = "", tex: str = "",
                tex_path: str = "", **_) -> str:
    bib_source = _load(bib, bib_path, "BibTeX")
    tex_source = tex or (_load("", tex_path, "LaTeX") if tex_path else "")
    return format_report(check(bib_source, tex_source))


def _tool_list_entries(bib: str = "", bib_path: str = "", **_) -> str:
    entries = parse_bibtex(_load(bib, bib_path, "BibTeX"))
    if not entries:
        return "No entries found."
    lines = []
    for entry in entries:
        fields = entry["fields"]
        lines.append(
            f"{entry['key']} [{entry['type']}] {fields.get('author', '?')} "
            f"({fields.get('year', '?')}) — {fields.get('title', '?')}"
        )
    return "\n".join(lines)


TOOLS: Dict[str, Dict[str, Any]] = {
    "bibliography_check": {
        "handler": _tool_check,
        "mutating": False,
        "risk": "low",
        "description": (
            "Cross-check a BibTeX file against a LaTeX document: keys cited but "
            "missing, entries never cited, duplicate keys, the same work under two "
            "keys, and entries missing fields their type requires."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "bib": {"type": "string", "description": "BibTeX source"},
                "bib_path": {"type": "string", "description": "Or a workspace-relative .bib path"},
                "tex": {"type": "string", "description": "LaTeX source that cites it"},
                "tex_path": {"type": "string", "description": "Or a workspace-relative .tex path"},
            },
        },
    },
    "bibliography_list": {
        "handler": _tool_list_entries,
        "mutating": False,
        "risk": "low",
        "description": "List every entry in a BibTeX file with its key, type, author, year and title.",
        "parameters": {
            "type": "object",
            "properties": {
                "bib": {"type": "string", "description": "BibTeX source"},
                "bib_path": {"type": "string", "description": "Or a workspace-relative .bib path"},
            },
        },
    },
}
