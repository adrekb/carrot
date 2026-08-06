"""Submission venues — the house style you are actually writing to.

"Format this for NeurIPS" is a real instruction with a real answer, and getting
it wrong wastes a submission. A handful of venues are common enough to be worth
knowing offline; for everything else the pack searches the web for the call for
papers rather than inventing a page limit.

The offline table is deliberately small and conservative. Every entry carries
the year its rules were recorded, and the tool says so, because these change
annually and a confidently stale answer is worse than "look it up".
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# Recorded from each venue's public author instructions. Treated as a starting
# point to verify, never as authority — hence `recorded`.
KNOWN_VENUES: Dict[str, Dict[str, Any]] = {
    "neurips": {
        "name": "NeurIPS", "recorded": 2024,
        "citation_style": "numeric (natbib, \\citep/\\citet)",
        "template": "neurips_20xx.sty (official LaTeX style)",
        "notes": "Anonymous for review; page limit excludes references and appendix.",
    },
    "icml": {
        "name": "ICML", "recorded": 2024,
        "citation_style": "author-year (natbib, \\citep/\\citet)",
        "template": "icml20xx.sty",
        "notes": "Anonymous for review; references excluded from the page limit.",
    },
    "iclr": {
        "name": "ICLR", "recorded": 2024,
        "citation_style": "author-year (natbib)",
        "template": "iclr20xx_conference.sty",
        "notes": "Anonymous, double-blind; appendix allowed after references.",
    },
    "acl": {
        "name": "ACL", "recorded": 2024,
        "citation_style": "author-year (natbib, acl_natbib.bst)",
        "template": "acl.sty (ACL rolling review)",
        "notes": "Anonymous; a mandatory limitations section sits outside the page limit.",
    },
    "cvpr": {
        "name": "CVPR", "recorded": 2024,
        "citation_style": "numeric (ieee)",
        "template": "cvpr.sty",
        "notes": "Anonymous; paper ID required on the review version.",
    },
    "ieee": {
        "name": "IEEE (generic)", "recorded": 2024,
        "citation_style": "numeric (IEEEtran)",
        "template": "IEEEtran.cls",
        "notes": "Two-column; \\bibliographystyle{IEEEtran}.",
    },
    "acm": {
        "name": "ACM (generic)", "recorded": 2024,
        "citation_style": "numeric or author-year (ACM-Reference-Format)",
        "template": "acmart.cls",
        "notes": "sigconf for conferences; CCS concepts and keywords required.",
    },
    "arxiv": {
        "name": "arXiv preprint", "recorded": 2024,
        "citation_style": "any consistent style",
        "template": "any; article or the venue's style",
        "notes": "No page limit. Not anonymous.",
    },
}


def lookup(name: str) -> Optional[Dict[str, Any]]:
    """Match a venue by a loose name, so 'NeurIPS 2025' finds neurips."""
    needle = (name or "").lower()
    for key, venue in KNOWN_VENUES.items():
        if key in needle or venue["name"].lower() in needle:
            return {**venue, "id": key}
    return None


def search_web(name: str, max_results: int = 5) -> List[Dict[str, str]]:
    """Find the venue's own author instructions.

    Deliberately searches for the primary source rather than summarising from
    memory — the formatting rules that matter are the ones on the venue's page
    this year, not the ones in a model's training data.
    """
    try:
        from duckduckgo_search import DDGS
    except ImportError:
        return []
    query = f"{name} call for papers author instructions LaTeX style page limit"
    try:
        with DDGS() as ddgs:
            return [
                {"title": r.get("title", ""), "url": r.get("href", ""),
                 "snippet": r.get("body", "")[:300]}
                for r in ddgs.text(query, max_results=max_results)
            ]
    except Exception:
        return []


def _tool_venue(name: str, search: bool = True, **_) -> str:
    if not name:
        raise ValueError("name the venue you are submitting to")

    lines: List[str] = []
    known = lookup(name)
    if known:
        lines += [
            f"{known['name']} — recorded from the {known['recorded']} author instructions.",
            f"  citation style: {known['citation_style']}",
            f"  template:       {known['template']}",
            f"  notes:          {known['notes']}",
            "",
            "These rules change annually. Confirm against the current call for "
            "papers before relying on them.",
        ]
    else:
        lines.append(f"'{name}' is not in the offline table, so nothing is assumed about it.")

    if search:
        results = search_web(name)
        if results:
            lines += ["", "Current sources:"]
            for result in results:
                lines.append(f"  - {result['title']}\n    {result['url']}")
        elif not known:
            lines.append("The web search returned nothing; ask the user for the venue's page.")
    return "\n".join(lines)


TOOLS: Dict[str, Dict[str, Any]] = {
    "venue_requirements": {
        "handler": _tool_venue,
        "mutating": False,
        "risk": "low",
        "description": (
            "Look up a conference or journal's formatting requirements — citation "
            "style, LaTeX template, anonymity and page-limit rules — from an offline "
            "table of common venues plus a live web search for the current call for "
            "papers. Use before formatting a submission."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Venue name, e.g. 'NeurIPS 2025'"},
                "search": {"type": "boolean",
                           "description": "Also search the web for the current instructions"},
            },
            "required": ["name"],
        },
    },
}
