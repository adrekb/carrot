"""Documents that are views of the database, not files on disk.

Write holds a document called **Goals**. It is not a note somebody created and
it is not a note anybody can delete: it is the goals table, rendered, sitting
in the list beside the things you did write, because "what have I committed
to" is a document-shaped question and Write is where documents live.

The important part is the direction it flows. The doc is generated *from* the
rows every time it is opened. It is not a markdown file that gets written when
a goal changes and parsed back when someone edits it — that design is the one
that eventually loses data, because the moment a person edits the prose you
have two sources of truth and a regex standing between them, and the regex
always loses. So this is read-only by construction: a goal changes by ticking
a chip, by asking chat, or through the API. Never by rewriting a sentence
about it.

That also means the doc cannot go stale, cannot be half-migrated, and does not
need a sync job. It is a SELECT with a template.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# Ids are reserved so a note created by hand cannot collide with one of these
# and quietly shadow it.
GOALS_ID = "system-goals"

SYSTEM_IDS = (GOALS_ID,)


def _goals_markdown() -> str:
    from . import goals as goals_mod

    report = goals_mod.status_report()
    lines = ["# Goals", ""]

    if not (report["overdue"] or report["open"] or report["done"] or report["proposed"]):
        lines += [
            "Nothing tracked yet.",
            "",
            "Carrot offers a goal when you say you will do something and attach",
            "a date or a target to it — *\"I need to finish the thesis by March",
            "12th\"*. A chip appears under the reply; ticking it lands here.",
        ]
        return "\n".join(lines)

    def row(goal: Dict[str, Any]) -> str:
        bits = [f"**{goal['title']}**"]
        if goal.get("deadline"):
            bits.append(f"due {goal['deadline']}")
        target = (goal.get("metadata") or {}).get("target")
        if target and not goal.get("deadline"):
            bits.append(str(target))
        # Provenance, because a goal you cannot trace back to something you
        # said is one you have to take Carrot's word for. The date it was
        # agreed is the cheapest form of that and fits on the line.
        if goal.get("decided_at"):
            bits.append(f"agreed {goal['decided_at'][:10]}")
        line = "- " + " — ".join(bits)
        progress = (goal.get("metadata") or {}).get("progress") or []
        for note in progress[-3:]:
            line += f"\n  - {note.get('at', '')[:10]}: {note.get('note', '')}"
        return line

    if report["overdue"]:
        lines += ["## Past their date", ""]
        lines += [row(g) for g in report["overdue"]] + [""]
    if report["open"]:
        lines += ["## Open", ""]
        lines += [row(g) for g in report["open"]] + [""]
    if report["proposed"]:
        # Proposed and accepted are different things and the ledger says so.
        # A list that mixes "you agreed to this" with "Carrot wondered about
        # this" is one nobody can trust as a record of what they promised.
        lines += ["## Waiting on you", "",
                  "Carrot noticed these but you have not answered yet. "
                  "The chip is in the conversation each came from.", ""]
        lines += [row(g) for g in report["proposed"]] + [""]
    if report["done"]:
        lines += ["## Finished", ""]
        lines += [row(g) for g in report["done"][:20]] + [""]

    lines += ["---", "",
              "*This page is the goals table, rendered. Edit it by ticking a chip "
              "in a conversation or by asking Carrot — not by typing here.*"]
    return "\n".join(lines)


DOCS = {
    GOALS_ID: {
        "title": "Goals",
        "render": _goals_markdown,
    },
}


def get(doc_id: str) -> Optional[Dict[str, Any]]:
    spec = DOCS.get(doc_id)
    if not spec:
        return None
    body = spec["render"]()
    return {
        "id": doc_id,
        "filename": f"{doc_id}.md",
        "folder": "",
        "path": "",
        "title": spec["title"],
        "created_at": 0,
        "content": body,
        "body": body,
        # The three flags the UI needs: pin it to the top, do not offer to
        # delete it, and do not let anybody type into it.
        "system": True,
        "pinned": True,
        "readonly": True,
    }


def listing() -> List[Dict[str, Any]]:
    """Every system doc, for the top of the Write list.

    Rendered here rather than stubbed, because a start screen that shows
    "Goals" and then loads something different when you click it is worse than
    not showing it — and the render is a database read, not a file walk.
    """
    return [doc for doc in (get(doc_id) for doc_id in SYSTEM_IDS) if doc]
