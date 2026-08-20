"""Applying an edit to a deck or a canvas, as operations against the JSON.

`doctext` renders a document as text a model can read, and says plainly that
nothing there parses back: a deck's source of truth is its JSON, and a round
trip through prose would quietly drop the fill colours, rotations, z-order and
embedded image data that the visual editor exists to set. This is the other
half — the mechanism the rendering was made addressable *for*.

An edit here is not "here is the new document". It is a list of operations
naming what to change:

    {"op": "set_text", "slide": 2, "element": 0, "text": "Postgres"}
    {"op": "add_element", "slide": 3, "type": "cylinder", "text": "Store"}
    {"op": "delete_element", "slide": 1, "element": 4}

The rules these follow, each of which is load-bearing:

**Addresses mean what the model saw.** A model writes `[2] on slide 1` after
reading a rendering of the document as it currently is. So every address in a
batch resolves against the *original* document, before anything moves. The
alternative — resolving each operation against the document as amended by the
ones before it — means a single delete silently shifts the meaning of every
address after it, and the edit lands on the wrong element with nothing to
report. Addresses are resolved to element and slide *ids* in one pass up
front; the second pass mutates by id and cannot be knocked out of step.

**All or nothing.** Every operation is validated before any is applied. A
batch that fails halfway leaves a document in a state nobody asked for and no
one can describe, which is worse than a batch that fails.

**The input is not touched.** Operations are applied to a deep copy and the
new document is returned. The caller decides whether to keep it.

**Unknown keys survive.** A deck body carries `type` and `version` beside its
slides, an element carries styling this module has no opinion about, and a
canvas element carries a dozen Excalidraw fields. Only what an operation names
is changed; everything else is copied through. Dropping a key here would be
the same silent loss the text round trip was rejected for.

**Canvas elements are addressed as rendered.** `doctext.canvas_as_text` skips
Excalidraw's tombstones — deleted elements it keeps in the scene with
`isDeleted` set — and numbers only what is left. So `[3]` on a canvas is the
fourth *live* element, not the fourth in the array, and this module resolves
it through the same filter. Getting this wrong edits a shape the user cannot
see.

Adding an element to a *canvas* is deliberately not implemented. A labelled
shape in Excalidraw is two elements bound to each other by id, with the label
carrying `containerId` and the shape listing it in `boundElements`, and a
scene where that linkage is not right renders in ways a unit test here cannot
see. Decks — where the boxes are Carrot's own and their shape is known — take
the full set.
"""
from __future__ import annotations

import copy
import json
import random
import string
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from carrot import doctext


class DocEditError(ValueError):
    """An operation that cannot be applied, said in a sentence a model can act on.

    These messages go back to the model that wrote the operation, so they name
    what was wrong and what was available rather than only failing.
    """


# The shapes a deck element may be, which is the registry in slides.js. Kept
# here as a literal rather than parsed out of the JavaScript at import time —
# and `tests/test_docedit.py` asserts the two agree, so a shape added to the
# editor and not to this list fails the suite instead of being silently
# accepted here and silently rendered as a rectangle there.
DECK_SHAPES = frozenset({
    "rect", "rounded", "pill", "ellipse", "triangle", "rtriangle", "parallel",
    "trapezoid", "diamond", "pentagon", "hexagon", "octagon", "star", "star4",
    "cross", "chevron", "cylinder", "step", "corner", "heart", "line",
    "arrow", "arrowleft", "arrowup", "arrowdown", "arrowlr", "arrowud",
    "arrowbent", "arrownotch", "arrowquad",
    "speech", "speechleft", "speechup", "banner",
})

# `text` and `image` are element types too, but they are not shapes and are not
# in the editor's shape registry. An operation may add text; it may not add an
# image, because the src of a deck image is a data: URI and a model does not
# have one to give.
DECK_ADDABLE = DECK_SHAPES | {"text"}

# The defaults in `makeElement` in slides.js. An element written without these
# is an element the editor has to guess about.
DECK_ELEMENT_DEFAULTS: Dict[str, Any] = {
    "x": 160, "y": 160, "w": 480, "h": 120,
    "text": "", "font": "var(--sans)", "size": 32,
    "color": "var(--text)", "fill": "transparent", "align": "left",
    "bold": False, "italic": False,
}

DECK_OPS = frozenset({
    "set_text", "set_notes", "add_element", "delete_element", "move_element",
    "add_slide", "delete_slide",
})
CANVAS_OPS = frozenset({"set_text", "delete_element", "move_element"})


def _new_id(prefix: str) -> str:
    """`newId` from slides.js: a prefix and seven base36 characters."""
    alphabet = string.digits + string.ascii_lowercase
    return prefix + "".join(random.choice(alphabet) for _ in range(7))


# ===== The entry point =====

def apply_operations(
    body: str,
    doc_format: str,
    operations: List[Dict[str, Any]],
    new_id: Optional[Callable[[str], str]] = None,
) -> str:
    """One document and a list of operations in, one new document out.

    Returns the new body as JSON, serialised the way the editor serialises it.
    Raises `DocEditError` if any operation is unapplicable, having changed
    nothing.

    `new_id` exists so a test can be deterministic; it defaults to the same
    random id the editor generates.
    """
    doc_format = (doc_format or "").strip().lower()
    if not isinstance(operations, list):
        raise DocEditError("Operations must be a list.")
    if not operations:
        return body or ""
    mint = new_id or _new_id
    if doc_format == doctext.FORMAT_SLIDES:
        return _apply_deck(body, operations, mint)
    if doc_format == doctext.FORMAT_CANVAS:
        return _apply_canvas(body, operations, mint)
    raise DocEditError(
        f"{doc_format or 'This format'} is edited as text, not as operations. "
        "Only slides and canvas take operations."
    )


# ===== Decks =====

def _apply_deck(body: str, operations: List[Dict[str, Any]],
                mint: Callable[[str], str]) -> str:
    doc = _deck_document(body)
    slides: List[Dict[str, Any]] = doc["slides"]

    # Every slide and element gets an id, because ids are what the second pass
    # addresses by. A deck written by the editor has them already; one that
    # arrived some other way may not, and an element without an id cannot be
    # named after its neighbour is deleted.
    for slide in slides:
        if not slide.get("id"):
            slide["id"] = mint("s")
        for element in slide.get("elements") or []:
            if isinstance(element, dict) and not element.get("id"):
                element["id"] = mint("e")

    _refuse_to_empty_the_deck(slides, operations)
    plans = [_plan_deck_op(slides, index, op, mint)
             for index, op in enumerate(operations)]
    for plan in plans:
        plan(doc)
    return json.dumps(doc, indent=2)


def _refuse_to_empty_the_deck(slides: List[Dict[str, Any]],
                              operations: List[Dict[str, Any]]) -> None:
    """A batch may not delete every slide, only some of them.

    Checked across the batch rather than per operation. Each delete is
    validated against the deck as it was — that is the addressing rule — so
    three operations deleting slides 1, 2 and 3 of a three-slide deck each look
    survivable on their own and together leave nothing for the editor to open.
    """
    doomed = {int(op["slide"]) for op in operations
              if isinstance(op, dict)
              and str(op.get("op") or "").strip().lower() == "delete_slide"
              and _is_index(op.get("slide"))
              and 1 <= int(op["slide"]) <= len(slides)}
    if doomed and len(doomed) >= len(slides):
        only = "this is the deck's only slide" if len(slides) == 1 else \
               "this would delete every slide"
        raise DocEditError(
            f"Refusing: {only}, and a deck with no slides is not a deck. "
            "Clear the elements instead, or keep one slide."
        )


def _deck_document(body: str) -> Dict[str, Any]:
    """The deck as a dict, with its non-slide keys kept.

    A deck that has never been through the visual editor is still markdown on
    disk — the editor converts on open. Operations cannot be applied to that,
    and saying so is better than converting it here and guessing at the layout
    the user would have chosen.
    """
    try:
        data = json.loads(body or "")
    except (TypeError, ValueError):
        raise DocEditError(
            "This deck is not JSON yet — it is still the markdown it will be "
            "converted from when it is next opened in the editor. Open it "
            "once before editing it by operation."
        )
    if not isinstance(data, dict) or not isinstance(data.get("slides"), list):
        raise DocEditError("This deck has no slides array to edit.")
    return copy.deepcopy(data)


def _plan_deck_op(slides: List[Dict[str, Any]], index: int, op: Any,
                  mint: Callable[[str], str]) -> Callable[[Dict[str, Any]], None]:
    """Validate one operation and return the change it will make.

    The returned closure captures ids, never positions, which is what keeps a
    batch's later addresses meaning what they meant before its earlier deletes.
    """
    where = f"Operation {index + 1}"
    name = _op_name(op, where, DECK_OPS)

    if name == "add_slide":
        at = op.get("at")
        if at is not None and not _is_index(at):
            raise DocEditError(f"{where}: 'at' must be a slide number.")
        # 1-based like every slide number the model was shown; out of range
        # appends, which is what "add a slide" means when nobody said where.
        position = len(slides) if at is None else max(0, min(len(slides), int(at) - 1))
        fresh = {"id": mint("s"), "background": "", "elements": [], "notes": ""}

        def add_slide(doc: Dict[str, Any]) -> None:
            doc["slides"].insert(position, fresh)
        return add_slide

    slide, slide_number = _deck_slide(slides, op, where)
    slide_id = slide["id"]

    if name == "delete_slide":
        # That this does not empty the deck is checked across the whole batch,
        # in _refuse_to_empty_the_deck, not here.
        def delete_slide(doc: Dict[str, Any]) -> None:
            doc["slides"][:] = [s for s in doc["slides"] if s.get("id") != slide_id]
        return delete_slide

    if name == "set_notes":
        notes = op.get("notes", op.get("text"))
        if not isinstance(notes, str):
            raise DocEditError(f"{where}: 'notes' must be text.")

        def set_notes(doc: Dict[str, Any]) -> None:
            _slide_by_id(doc, slide_id)["notes"] = notes
        return set_notes

    if name == "add_element":
        kind = str(op.get("type") or "").strip().lower()
        if kind not in DECK_ADDABLE:
            raise DocEditError(
                f"{where}: '{op.get('type')}' is not a shape this deck can "
                f"hold. Available: {', '.join(sorted(DECK_ADDABLE))}."
            )
        element = dict(DECK_ELEMENT_DEFAULTS)
        element.update({"id": mint("e"), "type": kind})
        text = op.get("text")
        if text is not None:
            if not isinstance(text, str):
                raise DocEditError(f"{where}: 'text' must be text.")
            element["text"] = text
        for key in ("x", "y", "w", "h", "size"):
            if op.get(key) is not None:
                element[key] = _number(op[key], key, where)
        for key in ("color", "fill", "align", "font"):
            if isinstance(op.get(key), str):
                element[key] = op[key]

        def add_element(doc: Dict[str, Any]) -> None:
            target = _slide_by_id(doc, slide_id)
            target.setdefault("elements", []).append(element)
        return add_element

    element, element_index = _deck_element(slide, op, where, slide_number)
    element_id = element["id"]

    if name == "set_text":
        text = op.get("text")
        if not isinstance(text, str):
            raise DocEditError(f"{where}: 'text' must be text.")
        if element.get("type") == "image":
            raise DocEditError(
                f"{where}: element [{element_index}] on slide {slide_number} "
                "is an image and has no text to set."
            )

        def set_text(doc: Dict[str, Any]) -> None:
            _deck_element_by_id(doc, slide_id, element_id)["text"] = text
        return set_text

    if name == "delete_element":
        def delete_element(doc: Dict[str, Any]) -> None:
            target = _slide_by_id(doc, slide_id)
            target["elements"] = [
                e for e in target.get("elements") or []
                if not (isinstance(e, dict) and e.get("id") == element_id)
            ]
        return delete_element

    # move_element
    moves = {key: _number(op[key], key, where)
             for key in ("x", "y", "w", "h") if op.get(key) is not None}
    if not moves:
        raise DocEditError(f"{where}: a move needs at least one of x, y, w, h.")

    def move_element(doc: Dict[str, Any]) -> None:
        _deck_element_by_id(doc, slide_id, element_id).update(moves)
    return move_element


def _deck_slide(slides: List[Dict[str, Any]], op: Dict[str, Any],
                where: str) -> Tuple[Dict[str, Any], int]:
    number = op.get("slide")
    if not _is_index(number):
        raise DocEditError(f"{where}: needs a slide number.")
    number = int(number)
    if not 1 <= number <= len(slides):
        raise DocEditError(
            f"{where}: there is no slide {number}. The deck has "
            f"{len(slides)} slide{'' if len(slides) == 1 else 's'}."
        )
    return slides[number - 1], number


def _deck_element(slide: Dict[str, Any], op: Dict[str, Any], where: str,
                  slide_number: int) -> Tuple[Dict[str, Any], int]:
    index = op.get("element")
    if not _is_index(index, zero_based=True):
        raise DocEditError(f"{where}: needs an element index.")
    index = int(index)
    # Numbered over the raw list, because `doctext.deck_as_text` enumerates the
    # raw list too — an element it cannot read still renders as
    # "[i] (unreadable element)" and still occupies an index. Skipping those
    # here would shift every address after one of them.
    elements = slide.get("elements") or []
    if not 0 <= index < len(elements):
        raise DocEditError(
            f"{where}: slide {slide_number} has no element [{index}]. It has "
            f"{len(elements)} element{'' if len(elements) == 1 else 's'}."
        )
    target = elements[index]
    if not isinstance(target, dict):
        raise DocEditError(
            f"{where}: element [{index}] on slide {slide_number} is not a "
            "readable element and cannot be edited."
        )
    return target, index


def _slide_by_id(doc: Dict[str, Any], slide_id: str) -> Dict[str, Any]:
    for slide in doc["slides"]:
        if slide.get("id") == slide_id:
            return slide
    raise DocEditError(
        "An earlier operation in this batch deleted the slide a later one "
        "edits. Delete a slide last, or edit it in a separate batch."
    )


def _deck_element_by_id(doc: Dict[str, Any], slide_id: str,
                        element_id: str) -> Dict[str, Any]:
    for element in _slide_by_id(doc, slide_id).get("elements") or []:
        # An element list may hold entries this module cannot read; they keep
        # their place in the numbering but are never a lookup's answer.
        if isinstance(element, dict) and element.get("id") == element_id:
            return element
    raise DocEditError(
        "An earlier operation in this batch deleted the element a later one "
        "edits. Delete last, or edit in a separate batch."
    )


# ===== Canvases =====

def _apply_canvas(body: str, operations: List[Dict[str, Any]],
                  mint: Callable[[str], str]) -> str:
    doc, is_bare_list = _canvas_document(body)
    elements: List[Dict[str, Any]] = doc["elements"]
    for element in elements:
        if isinstance(element, dict) and not element.get("id"):
            element["id"] = mint("c")

    # The same filter `doctext.canvas_as_text` numbers by, so an address here
    # names the element the model was actually shown.
    live = [e for e in elements if isinstance(e, dict) and not e.get("isDeleted")]

    plans = [_plan_canvas_op(live, index, op)
             for index, op in enumerate(operations)]
    for plan in plans:
        plan(doc)
    return json.dumps(doc["elements"] if is_bare_list else doc, indent=2)


def _canvas_document(body: str) -> Tuple[Dict[str, Any], bool]:
    try:
        data = json.loads(body or "")
    except (TypeError, ValueError):
        raise DocEditError("This canvas is not a readable Excalidraw scene.")
    if isinstance(data, list):
        return {"elements": copy.deepcopy(data)}, True
    if isinstance(data, dict) and isinstance(data.get("elements"), list):
        return copy.deepcopy(data), False
    raise DocEditError("This canvas has no elements array to edit.")


def _plan_canvas_op(live: List[Dict[str, Any]], index: int,
                    op: Any) -> Callable[[Dict[str, Any]], None]:
    where = f"Operation {index + 1}"
    name = _op_name(op, where, CANVAS_OPS)

    position = op.get("element")
    if not _is_index(position, zero_based=True):
        raise DocEditError(f"{where}: needs an element index.")
    position = int(position)
    if not 0 <= position < len(live):
        raise DocEditError(
            f"{where}: this canvas has no element [{position}]. It has "
            f"{len(live)} element{'' if len(live) == 1 else 's'}."
        )
    element_id = live[position]["id"]

    if name == "set_text":
        text = op.get("text")
        if not isinstance(text, str):
            raise DocEditError(f"{where}: 'text' must be text.")
        if "text" not in live[position]:
            raise DocEditError(
                f"{where}: element [{position}] is a "
                f"{live[position].get('type') or 'shape'} with no text on it. "
                "A label is a separate bound element, which this cannot add."
            )

        def set_text(doc: Dict[str, Any]) -> None:
            target = _canvas_element_by_id(doc, element_id)
            target["text"] = text
            # Excalidraw keeps the text as typed beside the text as wrapped;
            # setting one and not the other reverts on the next reflow.
            if "originalText" in target:
                target["originalText"] = text
            _touch(target)
        return set_text

    if name == "delete_element":
        # Excalidraw tombstones rather than removes, and reconciliation
        # resurrects an element that merely went missing from the array.
        def delete_element(doc: Dict[str, Any]) -> None:
            target = _canvas_element_by_id(doc, element_id)
            target["isDeleted"] = True
            _touch(target)
        return delete_element

    # move_element
    moves: Dict[str, Any] = {}
    for key, field in (("x", "x"), ("y", "y"), ("w", "width"), ("h", "height")):
        if op.get(key) is not None:
            moves[field] = _number(op[key], key, where)
    if not moves:
        raise DocEditError(f"{where}: a move needs at least one of x, y, w, h.")

    def move_element(doc: Dict[str, Any]) -> None:
        target = _canvas_element_by_id(doc, element_id)
        target.update(moves)
        _touch(target)
    return move_element


def _canvas_element_by_id(doc: Dict[str, Any], element_id: str) -> Dict[str, Any]:
    for element in doc["elements"]:
        if isinstance(element, dict) and element.get("id") == element_id:
            return element
    raise DocEditError("The element this operation edits is no longer in the scene.")


def _touch(element: Dict[str, Any]) -> None:
    """Mark an element changed, so Excalidraw's reconciliation takes the change.

    A scene that is open elsewhere merges by version: an element edited without
    its version moving is an element the other copy considers stale and
    overwrites with what it already had.
    """
    version = element.get("version")
    element["version"] = (version + 1) if isinstance(version, int) else 1
    element["versionNonce"] = random.randint(0, 2 ** 31 - 1)
    element["updated"] = int(time.time() * 1000)


# ===== Shared checking =====

def _op_name(op: Any, where: str, allowed: frozenset) -> str:
    if not isinstance(op, dict):
        raise DocEditError(f"{where}: each operation must be an object.")
    name = str(op.get("op") or "").strip().lower()
    if name not in allowed:
        raise DocEditError(
            f"{where}: '{op.get('op')}' is not an operation this document "
            f"takes. Available: {', '.join(sorted(allowed))}."
        )
    return name


def _is_index(value: Any, zero_based: bool = False) -> bool:
    # bool is an int in Python, and True is not an element index.
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    return value >= 0 if zero_based else True


def _number(value: Any, key: str, where: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DocEditError(f"{where}: '{key}' must be a number.")
    return value
