"""Every document format, as text a model can read.

Two of Carrot's four document formats are prose and go to a model unchanged.
The other two are not: a deck is a JSON array of positioned boxes and a canvas
is an Excalidraw scene, and sending either of those to a model means sending it
several kilobytes of coordinates, element ids and style keys — which is how
"summarise this deck" comes back as a description of a data structure.

So each format gets a rendering. The rules the renderings follow:

**Content before geometry.** What a slide *says* is the first thing on its
line; where the box sits is a parenthetical after it. A model asked to rewrite
the second bullet should not have to parse a layout to find it.

**Every element is addressable.** Each carries its index, because the point of
this is not only reading. An edit that can be described as "element 3 on slide
2" is an edit that can be applied without re-serialising the whole document and
hoping the diff lands where it was meant to.

**Empty is empty.** A slide with nothing on it renders as a slide with nothing
on it, not as an absence. A model that cannot see the blank slide will not
notice it is there.

**Lossy, and only in one direction.** These renderings are for *reading*.
Nothing here parses back: a deck's source of truth is its JSON, and a round
trip through prose would quietly drop the fill colours, rotations and z-order
that the visual editor exists to set. Editing a deck is a separate mechanism
against the JSON, not a rewrite of this text.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

FORMAT_MARKDOWN = "markdown"
FORMAT_LATEX = "latex"
FORMAT_CANVAS = "canvas"
FORMAT_SLIDES = "slides"

# Long documents are clipped by the caller, which knows its own budget. This is
# the guard against one pathological element — a text box holding a pasted
# essay — spending the whole of it before the second slide is described.
MAX_ELEMENT_CHARS = 600


def _clip(text: str, limit: int = MAX_ELEMENT_CHARS) -> str:
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + f"… (+{len(text) - limit} chars)"


def as_text(body: str, doc_format: str = FORMAT_MARKDOWN, title: str = "") -> str:
    """One document, as something worth putting in a prompt."""
    doc_format = (doc_format or FORMAT_MARKDOWN).strip().lower()
    if doc_format == FORMAT_SLIDES:
        return deck_as_text(body, title)
    if doc_format == FORMAT_CANVAS:
        return canvas_as_text(body, title)
    # Markdown and LaTeX are already the thing. A heading naming the file is
    # added by the caller if it wants one; adding it here would double it up
    # for the two formats that need no rendering at all.
    return body or ""


# ===== Decks =====

def deck_as_text(body: str, title: str = "") -> str:
    """A deck as an outline: slide by slide, element by element.

    A deck that never went through the visual editor is still markdown on
    disk — the editor converts on open — so unparseable JSON is returned as
    itself rather than as an error. That is the honest reading: it *is* the
    document, and it happens to already be prose.
    """
    slides = _deck_slides(body)
    if slides is None:
        return body or ""
    lines: List[str] = []
    if title:
        lines.append(f"Deck: {title}")
    lines.append(f"{len(slides)} slide{'' if len(slides) == 1 else 's'}")
    for number, slide in enumerate(slides, 1):
        lines.append("")
        lines.append(f"## Slide {number}")
        elements = slide.get("elements") or []
        if not elements:
            lines.append("(empty)")
        for index, element in enumerate(elements):
            lines.append(_deck_element_line(index, element))
        notes = str(slide.get("notes") or "").strip()
        if notes:
            lines.append(f"Notes: {_clip(notes)}")
    return "\n".join(lines)


def _deck_slides(body: str) -> Optional[List[Dict[str, Any]]]:
    try:
        data = json.loads(body or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    slides = data.get("slides")
    return slides if isinstance(slides, list) else None


def _deck_element_line(index: int, element: Any) -> str:
    if not isinstance(element, dict):
        return f"[{index}] (unreadable element)"
    kind = str(element.get("type") or "?")
    where = _where(element.get("x"), element.get("y"),
                   element.get("w"), element.get("h"))
    if kind == "text":
        text = _clip(element.get("text") or "")
        return f'[{index}] text: "{text}"{where}' if text else f"[{index}] text: (empty){where}"
    if kind == "image":
        src = str(element.get("src") or "")
        # A data: URI is a whole image. Its length is not information.
        name = "embedded image" if src.startswith("data:") else (src or "no source")
        return f"[{index}] image: {_clip(name, 120)}{where}"
    if kind == "line":
        return f"[{index}] line{where}"
    # A shape, possibly with a label typed into it.
    label = _clip(element.get("text") or "")
    described = f"[{index}] shape {kind}"
    if label:
        described += f': "{label}"'
    return described + where


def _where(x, y, w, h) -> str:
    """The geometry, after the content, in one parenthetical or not at all."""
    numbers = [x, y, w, h]
    if any(not isinstance(n, (int, float)) for n in numbers):
        return ""
    return f" (at {round(x)},{round(y)} · {round(w)}×{round(h)})"


# ===== Canvases =====

def canvas_as_text(body: str, title: str = "") -> str:
    """An Excalidraw scene as a list of what is on it.

    Deleted elements are skipped: Excalidraw tombstones rather than removes, so
    a scene someone has been working in for an hour carries every shape they
    ever drew with `isDeleted` set. Describing those is describing a canvas the
    user cannot see.
    """
    elements = _canvas_elements(body)
    if elements is None:
        return body or ""
    live = [e for e in elements
            if isinstance(e, dict) and not e.get("isDeleted")]
    lines: List[str] = []
    if title:
        lines.append(f"Canvas: {title}")
    lines.append(f"{len(live)} element{'' if len(live) == 1 else 's'}")
    if not live:
        lines.append("(empty)")
    for index, element in enumerate(live):
        lines.append(_canvas_element_line(index, element))
    return "\n".join(lines)


def _canvas_elements(body: str) -> Optional[List[Any]]:
    try:
        data = json.loads(body or "")
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict):
        elements = data.get("elements")
        return elements if isinstance(elements, list) else None
    return data if isinstance(data, list) else None


def _canvas_element_line(index: int, element: Dict[str, Any]) -> str:
    kind = str(element.get("type") or "?")
    where = _where(element.get("x"), element.get("y"),
                   element.get("width"), element.get("height"))
    text = _clip(element.get("text") or element.get("label") or "")
    if kind == "text":
        return f'[{index}] text: "{text}"{where}'
    if text:
        return f'[{index}] {kind}: "{text}"{where}'
    return f"[{index}] {kind}{where}"
