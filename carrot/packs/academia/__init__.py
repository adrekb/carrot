"""Academia Pack — the first bundled extension.

One switch for the whole shape of academic work: writing LaTeX, keeping the
bibliography honest, getting formulas and tables out of MATLAB or off a piece of
paper, rendering a CAD model as a figure, and formatting to a venue's rules.

The tools split cleanly into two groups, and the split is the point:

* **Always available** — validation, outline, BibTeX checking, formula
  conversion, venue lookup. Pure Python, no installation, no network.
* **Needs a program you already have** — LaTeX compilation, MATLAB/Octave, CAD
  rendering. Each declares a capability that is probed, so the Extensions tab
  can say what works on *this* machine and a tool whose program is absent
  refuses with the reason instead of failing halfway.

The skills below are written to the ordinary skills directory when the pack is
enabled, so they appear under ``/`` in the command bar like any other skill and
can be edited if the house style is not quite right.
"""

from __future__ import annotations

from ... import extensions
from . import bibliography, compute, latex, venues, vision

TOOLS = {**latex.TOOLS, **bibliography.TOOLS, **venues.TOOLS,
         **compute.TOOLS, **vision.TOOLS}

CAPABILITIES = [
    {
        "id": "latex",
        "label": "LaTeX engine",
        "binaries": list(latex.TEX_ENGINES),
        "purpose": "Compiling documents to PDF. Validation and outlining work without it.",
        "install_hint": "Install TeX Live, MiKTeX, or Tectonic (a single binary).",
    },
    {
        "id": "matlab",
        "label": "MATLAB or Octave",
        "binaries": ["matlab", "octave", "octave-cli"],
        "purpose": "Running scripts. Converting expressions to LaTeX works without it.",
        "install_hint": "Install MATLAB, or GNU Octave, which is free and close enough.",
    },
    {
        "id": "cad",
        "label": "FreeCAD",
        "binaries": ["freecadcmd", "FreeCADCmd", "freecad"],
        "purpose": "Rendering CAD models as figures.",
        "install_hint": "Install FreeCAD; its command-line interpreter ships with it.",
    },
]

SETTINGS = [
    {
        "key": "venue",
        "label": "Target venue",
        "type": "string",
        "default": "",
        "help": "Conference or journal you are submitting to, e.g. 'NeurIPS 2025'. "
                "The writing skills format to its rules.",
    },
    {
        "key": "citation_style",
        "label": "Citation style",
        "type": "select",
        "options": ["venue default", "numeric", "author-year", "IEEE", "ACM", "APA", "Chicago"],
        "default": "venue default",
        "help": "Overrides the venue's default when you need a specific style.",
    },
    {
        "key": "strict_citations",
        "label": "Refuse uncited claims",
        "type": "boolean",
        "default": True,
        "help": "The writing skills will not state a factual claim without a citation, "
                "and will say so rather than inventing a reference.",
    },
]


def _writing_instructions() -> str:
    """Built at enable time so the user's venue and style are baked in."""
    venue = extensions.pack_setting("academia", "venue", "") or "the target venue"
    style = extensions.pack_setting("academia", "citation_style", "venue default")
    strict = extensions.pack_setting("academia", "strict_citations", True)

    lines = [
        f"You are helping write an academic document for {venue}.",
        "",
        "Before formatting anything, call `venue_requirements` for the venue and "
        "follow what it returns. It reports rules recorded from a past year plus "
        "current sources — prefer the current sources, and if the two disagree, say so.",
        "",
        f"Citation style: {style}. Use the venue's own style files rather than "
        "hand-formatting references.",
    ]
    if strict:
        lines += [
            "",
            "Do not state a factual or numerical claim without a citation. If you "
            "do not have a source, write the claim and mark it `\\todo{cite}` — "
            "never invent a reference, an author list, a year, or a DOI. A "
            "fabricated citation is worse than a missing one.",
        ]
    lines += [
        "",
        "Check your work with the tools rather than by eye:",
        "- `latex_validate` after any structural edit.",
        "- `bibliography_check` before declaring the document finished.",
        "- `latex_compile` to confirm it actually builds, if an engine is installed.",
        "",
        "Write in the register of the field: precise, unhedged where the evidence "
        "is strong, explicitly uncertain where it is not.",
    ]
    return "\n".join(lines)


def _skills():
    return [
        {
            "slug": "academic-writing",
            "name": "Academic writing",
            "description": "Draft and edit to a venue's formatting and citation rules.",
            "instructions": _writing_instructions(),
        },
        {
            "slug": "latex-formulas",
            "name": "LaTeX formulas",
            "description": "Turn expressions, images or descriptions into correct LaTeX maths.",
            "instructions": (
                "You turn mathematics into LaTeX.\n\n"
                "Sources you may be given: a MATLAB-style expression, a photograph of "
                "something handwritten, or a description in words.\n\n"
                "- For a MATLAB-style expression, call `formula_to_latex`. It is "
                "syntactic and conservative; read its output and fix anything it left "
                "alone rather than trusting it blindly.\n"
                "- For an image, call `image_to_latex` with kind='formula'.\n"
                "- For a description, write the LaTeX directly.\n\n"
                "Always run `latex_validate` on the result before returning it. "
                "Return the LaTeX itself, not a description of it. If a symbol is "
                "ambiguous, mark it `\\todo{?}` and say which one — do not guess."
            ),
        },
        {
            "slug": "citations",
            "name": "Citations",
            "description": "Keep the bibliography complete, consistent and real.",
            "instructions": (
                "You maintain a bibliography.\n\n"
                "Run `bibliography_check` first, then work through what it reports: "
                "keys cited but missing (these render as ?? in the PDF), entries never "
                "cited, duplicate keys, the same work under two keys, and entries "
                "missing fields their type requires.\n\n"
                "Never invent an entry to satisfy a missing key. If a citation has no "
                "source, say which key is unbacked and ask for the reference. Author "
                "lists, years, venues and DOIs must come from something real — a "
                "plausible-looking fabricated reference is the worst possible output "
                "here, because it survives review by looking correct."
            ),
        },
        {
            "slug": "figures-and-tables",
            "name": "Figures and tables",
            "description": "Build tables and figures from images, CAD models or data.",
            "instructions": (
                "You produce figures and tables for a paper.\n\n"
                "- A photograph of a table: `image_to_latex` with kind='table'. It uses "
                "booktabs. Check the transcription against the image and flag any "
                "`\\todo{unreadable}` cells to the user rather than filling them in.\n"
                "- A CAD model: `cad_render` to a PNG, then write the `figure` "
                "environment around it.\n"
                "- Data to plot: `matlab_run` if MATLAB or Octave is installed; "
                "otherwise write the plotting code and say what is needed to run it.\n\n"
                "Every float needs a `\\caption` and a `\\label`, and the caption should "
                "say what the reader should notice, not just name the contents. "
                "Reference every float from the text — `latex_validate` reports labels "
                "that are never referenced."
            ),
        },
    ]


PACK = extensions.Pack(
    pack_id="academia",
    name="Academia Pack",
    description=(
        "LaTeX authoring with validation and compilation, BibTeX and citation "
        "checking, MATLAB/Octave, CAD figure rendering, image-to-LaTeX "
        "transcription, and venue-specific formatting rules."
    ),
    version="1.0",
    tools=TOOLS,
    skills=_skills,
    capabilities=CAPABILITIES,
    settings=SETTINGS,
    tutorial=[
        {"title": "Check what this machine has",
         "body": "The 'On this machine' list above is probed, not assumed. LaTeX "
                 "compilation needs a TeX distribution and MATLAB tools need "
                 "MATLAB or Octave; anything missing is named with how to install "
                 "it. A tool whose program is absent refuses up front rather than "
                 "failing halfway through a document."},
        {"title": "Set your venue and citation style",
         "body": "In Settings above. This is not cosmetic — changing it rewrites "
                 "the pack's skills, so the instructions name your actual target "
                 "rather than a generic one, and the formatting rules the model "
                 "follows are the ones that venue publishes."},
        {"title": "Write, then ask it to check",
         "body": "Paste or open a LaTeX document and ask Carrot to validate it. "
                 "It reports what will not compile before you run a compile, "
                 "which is faster than reading a TeX error log."},
        {"title": "Point it at your bibliography",
         "body": "Ask it to check your BibTeX: it finds entries cited but not "
                 "defined, defined but never cited, and fields the venue requires "
                 "that are missing."},
        {"title": "Photograph a table or a formula",
         "body": "Attach a photo of a printed table or equation and ask for it as "
                 "LaTeX. This is what the image-to-LaTeX transcription is for, and "
                 "it is faster than retyping a matrix by hand."},
        {"title": "Use the skills directly",
         "body": "Enabling the pack writes its skills into your ordinary skills "
                 "directory, so they appear under / in the command bar like any "
                 "other. If the house style is not quite right, edit the wording — "
                 "they are plain instruction files."},
    ],
    default_enabled=False,
)

extensions.register(PACK)
