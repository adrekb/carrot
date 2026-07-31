"""Images to LaTeX — a photographed table or a formula on paper, typed up.

This is the one part of the pack that genuinely needs a model rather than a
parser: reading handwriting and layout off a photograph. It routes through the
ordinary router, so the model is whichever the user picked, and a note that says
``@/model/openai/…`` governs it exactly as it governs everything else.

The prompts insist on returning only LaTeX. Anything a model adds around the
answer has to be stripped before it can be pasted into a document, so the
stripping is done here rather than left to the caller.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict

from ... import agent_tools, router as router_mod

TABLE_PROMPT = """Transcribe the table in this image into LaTeX.

Rules:
- Output ONLY the LaTeX for the table. No explanation, no markdown fences.
- Use the `tabular` environment inside `table`, with `\\toprule`, `\\midrule`
  and `\\bottomrule` from booktabs.
- Preserve the exact cell contents, including numbers and units, as written.
- If a cell is genuinely unreadable, put `\\todo{unreadable}` in it rather than
  guessing a value.
- Keep the column alignment sensible: l for text, r for numbers.
"""

FORMULA_PROMPT = """Transcribe the mathematics in this image into LaTeX.

Rules:
- Output ONLY the LaTeX. No explanation, no markdown fences.
- Wrap it in an `equation` environment unless there are several separate
  expressions, in which case use `align`.
- Preserve subscripts, superscripts, and the exact symbols used.
- If a symbol is genuinely ambiguous, use `\\todo{?}` rather than guessing.
"""

FIGURE_PROMPT = """Describe this figure for an academic paper.

Return two things, in this order and nothing else:
1. A one-sentence caption suitable for `\\caption{}`.
2. A short factual description of what the figure shows: axes, units, trends.
Do not speculate about anything not visible in the image.
"""

PROMPTS = {"table": TABLE_PROMPT, "formula": FORMULA_PROMPT, "figure": FIGURE_PROMPT}

FENCE_PATTERN = re.compile(r"^\s*```(?:latex|tex)?\s*\n(.*?)\n\s*```\s*$", re.DOTALL)


def strip_fences(text: str) -> str:
    """Remove a markdown code fence a model wrapped its answer in."""
    match = FENCE_PATTERN.match(text or "")
    return match.group(1).strip() if match else (text or "").strip()


def transcribe(image_path: str, kind: str = "table", model: str = "",
               provider: str = "") -> str:
    """Read an image and return LaTeX (or a caption, for a figure)."""
    if kind not in PROMPTS:
        raise ValueError(f"kind must be one of {', '.join(sorted(PROMPTS))}")
    resolved_path = agent_tools.resolve(image_path)
    if not os.path.isfile(resolved_path):
        raise ValueError(f"no such file in the workspace: {image_path}")

    route = router_mod.route(
        task=router_mod.TASK_EXTRACT,
        model=model or None,
        provider=provider or None,
    )
    answer = router_mod.vision_complete(route, PROMPTS[kind], resolved_path)
    return strip_fences(answer)


def _tool_transcribe(image_path: str, kind: str = "table", model: str = "",
                     provider: str = "", **_) -> str:
    latex = transcribe(image_path, kind=kind, model=model, provider=provider)
    if not latex:
        return "error: the model returned nothing — it may not be a vision model"
    if "\\todo{" in latex:
        latex += ("\n\n(Note: \\todo{} marks something the model could not read. "
                  "Check those cells against the original.)")
    return latex


TOOLS: Dict[str, Dict[str, Any]] = {
    "image_to_latex": {
        "handler": _tool_transcribe,
        "mutating": False,
        "risk": "low",
        "description": (
            "Transcribe a photograph or scan into LaTeX: a handwritten or printed "
            "table into a booktabs tabular, mathematics into an equation, or a figure "
            "into a caption and description. Requires a vision-capable model."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string",
                               "description": "Workspace-relative image (png, jpg, gif, webp)"},
                "kind": {"type": "string", "enum": ["table", "formula", "figure"],
                         "description": "What the image contains"},
                "model": {"type": "string",
                          "description": "Override the model, e.g. a vision-capable one"},
                "provider": {"type": "string", "description": "Override the provider"},
            },
            "required": ["image_path"],
        },
    },
}
