"""LaTeX Notes — a writing surface for documents that are mostly mathematics.

Carrot already renders `$…$` and `$$…$$` anywhere markdown is shown, so the
missing thing was never the renderer. It was the place to *write*: a pane you
can see the source and the result in at once, an outline that tells you where
you are in a long document, and a way to get the finished thing out as
something other than a note in a database.

What this pack adds is that surface, plus the one editing idea worth stealing
from the current crop of LaTeX editors: you select a formula or a paragraph
and ask for a change to *that*, in place, rather than describing it to a chat
window on the other side of the screen and pasting the answer back. The
selection is the prompt.

Deliberately not a second LaTeX toolchain. The Academia pack already compiles,
validates and outlines .tex through a real engine; this is the editor, and if
both are installed they meet at the same files.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List

from ... import extensions

# ===== Tools =====
#
# Two, and both pure Python. Anything needing an actual TeX engine belongs to
# the Academia pack, which probes for one and says so when it is missing —
# duplicating that here would give the user two answers to "can I compile" and
# one of them would be wrong.

_HEADING = re.compile(
    r"^(#{1,6})\s+(.+?)\s*$|^\\(section|subsection|subsubsection|chapter)\*?\{([^}]*)\}",
    re.M)

_MATH_BLOCK = re.compile(r"\$\$(.+?)\$\$", re.S)
_MATH_INLINE = re.compile(r"(?<!\$)\$(?!\$)(.+?)(?<!\$)\$(?!\$)", re.S)


def outline(text: str) -> List[Dict[str, Any]]:
    """Headings in order, in markdown or LaTeX form, with their line numbers.

    Both notations at once because a document that mixes them is the normal
    case here: prose in markdown, structure in LaTeX, or the other way round
    depending on where the text came from.
    """
    found: List[Dict[str, Any]] = []
    for match in _HEADING.finditer(text or ""):
        line = (text or "").count("\n", 0, match.start()) + 1
        if match.group(1):
            found.append({"level": len(match.group(1)), "title": match.group(2), "line": line})
        else:
            depth = {"chapter": 1, "section": 1, "subsection": 2,
                     "subsubsection": 3}.get(match.group(3), 2)
            found.append({"level": depth, "title": match.group(4), "line": line})
    return found


def _tool_outline(text: str = "", **_) -> str:
    found = outline(text)
    if not found:
        return "no headings"
    return "\n".join(f"{'  ' * (h['level'] - 1)}- {h['title']} (line {h['line']})"
                     for h in found)


def statistics(text: str) -> Dict[str, int]:
    """Words, lines, and how much of the document is mathematics.

    The maths counts are the ones this pack exists for. "1800 words" says
    nothing about a paper that is forty displayed equations, and the shape of
    the document is what you are checking when you glance at a status line.
    """
    text = text or ""
    display = _MATH_BLOCK.findall(text)
    without_display = _MATH_BLOCK.sub(" ", text)
    inline = _MATH_INLINE.findall(without_display)
    prose = _MATH_INLINE.sub(" ", without_display)
    return {
        "characters": len(text),
        "lines": text.count("\n") + 1 if text else 0,
        "words": len(prose.split()),
        "display_math": len(display),
        "inline_math": len(inline),
        "headings": len(outline(text)),
    }


def _tool_statistics(text: str = "", **_) -> str:
    stats = statistics(text)
    return (f"{stats['words']} words, {stats['lines']} lines, "
            f"{stats['headings']} headings, {stats['display_math']} displayed "
            f"and {stats['inline_math']} inline equations")


TOOLS: Dict[str, Dict[str, Any]] = {
    "note_outline": {
        "handler": _tool_outline,
        "mutating": False,
        "risk": "low",
        "description": ("The heading structure of a document, in markdown or LaTeX "
                        "notation, with line numbers. Use it to navigate a long note "
                        "before reading it whole."),
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    "note_statistics": {
        "handler": _tool_statistics,
        "mutating": False,
        "risk": "low",
        "description": ("Words, lines, headings and equation counts for a document. "
                        "The equation counts are the useful part: a word count says "
                        "nothing about a paper that is mostly mathematics."),
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
}


SKILLS = [
    {
        "slug": "latex-editing",
        "name": "Editing LaTeX in place",
        "description": "How to rewrite a selected formula or passage without disturbing the rest",
        "instructions": (
            "You are editing a fragment of a larger document, and the user has "
            "selected exactly the part they want changed.\n\n"
            "Return the replacement fragment and nothing else. No preamble, no "
            "explanation, no code fence, no restating of the surrounding text. "
            "What you return is substituted directly for what was selected, so a "
            "sentence of commentary becomes a sentence of commentary in the "
            "middle of their document.\n\n"
            "Keep the notation the fragment already uses. If it is inline "
            "mathematics between single dollars, stay inline and keep the "
            "dollars. If it is a displayed block, keep it displayed. If it is "
            "prose with mathematics in it, it stays prose with mathematics in "
            "it. Matching what is around it matters more than what you would "
            "have written from scratch.\n\n"
            "Preserve every label, reference and citation key unless the change "
            "is about them. A rewritten equation that silently drops its \\label "
            "breaks every \\ref pointing at it, and nothing on screen will say so."
        ),
    },
]


PACK = extensions.register(extensions.Pack(
    pack_id="latexnote",
    name="LaTeX Notes",
    description=("A writing surface for documents that are mostly mathematics: source "
                 "and rendered output side by side, an outline of where you are, and "
                 "selection-based editing where the thing you highlighted is the prompt."),
    version="1.0",
    tools=TOOLS,
    skills=SKILLS,
    tabs=["latex"],
    tutorial=[
        {"step": "Open the LaTeX tab",
         "detail": "It opens on an empty document with the source on the left and the "
                   "rendered result on the right."},
        {"step": "Write some mathematics",
         "detail": "$e^{i\\pi} + 1 = 0$ inline, or $$…$$ on its own line for a displayed "
                   "equation. The right-hand pane keeps up as you type."},
        {"step": "Select something and press Edit with AI",
         "detail": "Highlight a formula or a paragraph and describe the change. What comes "
                   "back replaces exactly what you selected — you see the before and after "
                   "and decide whether to keep it."},
        {"step": "Export when it is done",
         "detail": "Markdown, HTML with the mathematics rendered, or plain text. The HTML "
                   "carries its own styling, so it opens anywhere."},
    ],
))
