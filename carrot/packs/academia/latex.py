"""LaTeX authoring: structure, validation, and compilation.

Most of what goes wrong in a LaTeX document goes wrong before any engine runs —
an unclosed environment, a stray brace, a ``\\cite`` with no matching entry. All
of that is answerable without a TeX installation, and answering it locally means
the editor can be useful on a machine that has none.

Compilation is the part that genuinely needs an engine. When one is present the
tools drive it for real; when it is not they say so in one sentence rather than
failing somewhere inside a subprocess.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List

from ... import agent_tools, extensions

# \begin{...} / \end{...} pairs, and the inline/display math delimiters that
# have to balance for a document to compile at all.
BEGIN_PATTERN = re.compile(r"\\begin\{(\w+\*?)\}")
END_PATTERN = re.compile(r"\\end\{(\w+\*?)\}")
CITE_PATTERN = re.compile(r"\\cite[a-zA-Z]*\s*(?:\[[^\]]*\])*\s*\{([^}]*)\}")
LABEL_PATTERN = re.compile(r"\\label\{([^}]*)\}")
REF_PATTERN = re.compile(r"\\(?:page|eq|auto|c)?ref\{([^}]*)\}")
SECTION_PATTERN = re.compile(
    r"\\(part|chapter|section|subsection|subsubsection)\*?\{([^}]*)\}")
INCLUDE_PATTERN = re.compile(r"\\(?:input|include)\{([^}]*)\}")

# Comments must be stripped before counting braces, but an escaped \% is not a
# comment. Match a % that is not preceded by a backslash.
COMMENT_PATTERN = re.compile(r"(?<!\\)%.*$", re.MULTILINE)

TEX_ENGINES = ("tectonic", "latexmk", "pdflatex", "xelatex", "lualatex")


def strip_comments(source: str) -> str:
    return COMMENT_PATTERN.sub("", source)


# ===== Validation =====

def validate(source: str) -> Dict[str, Any]:
    """Structural problems a TeX engine would only report as a cryptic error."""
    body = strip_comments(source)
    problems: List[Dict[str, str]] = []

    # Environments, tracked as a stack so a mismatch names both ends.
    stack: List[tuple] = []
    for match in re.finditer(r"\\(begin|end)\{(\w+\*?)\}", body):
        kind, name = match.group(1), match.group(2)
        line = body.count("\n", 0, match.start()) + 1
        if kind == "begin":
            stack.append((name, line))
        elif not stack:
            problems.append({
                "severity": "error", "line": line,
                "message": rf"\end{{{name}}} with no matching \begin",
            })
        else:
            open_name, open_line = stack.pop()
            if open_name != name:
                problems.append({
                    "severity": "error", "line": line,
                    "message": rf"\end{{{name}}} closes \begin{{{open_name}}} from line {open_line}",
                })
    for name, line in stack:
        problems.append({
            "severity": "error", "line": line,
            "message": rf"\begin{{{name}}} is never closed",
        })

    # Braces, ignoring escaped ones.
    depth = 0
    for match in re.finditer(r"(?<!\\)[{}]", body):
        depth += 1 if match.group(0) == "{" else -1
        if depth < 0:
            problems.append({
                "severity": "error",
                "line": body.count("\n", 0, match.start()) + 1,
                "message": "closing brace with no opening brace",
            })
            depth = 0
    if depth > 0:
        problems.append({
            "severity": "error", "line": body.count("\n") + 1,
            "message": f"{depth} unclosed {'brace' if depth == 1 else 'braces'}",
        })

    if body.count("$$") % 2:
        problems.append({"severity": "error", "line": 0,
                         "message": "unbalanced $$ display math"})

    # Cross-references that point nowhere.
    labels = set(LABEL_PATTERN.findall(body))
    for match in REF_PATTERN.finditer(body):
        for target in match.group(1).split(","):
            target = target.strip()
            if target and target not in labels:
                problems.append({
                    "severity": "warning",
                    "line": body.count("\n", 0, match.start()) + 1,
                    "message": rf"\ref to '{target}', which has no \label",
                })

    if re.search(r"\\documentclass", body) is None:
        problems.append({"severity": "warning", "line": 0,
                         "message": r"no \documentclass — this is a fragment, not a document"})

    return {
        "ok": not any(p["severity"] == "error" for p in problems),
        "problems": problems,
        "errors": sum(1 for p in problems if p["severity"] == "error"),
        "warnings": sum(1 for p in problems if p["severity"] == "warning"),
    }


def outline(source: str) -> List[Dict[str, Any]]:
    """The document's sectioning tree, for the editor's outline pane."""
    depths = {"part": 0, "chapter": 1, "section": 2,
              "subsection": 3, "subsubsection": 4}
    body = strip_comments(source)
    return [
        {
            "level": depths[match.group(1)],
            "kind": match.group(1),
            "title": match.group(2),
            "line": body.count("\n", 0, match.start()) + 1,
        }
        for match in SECTION_PATTERN.finditer(body)
    ]


def math_blocks(source: str) -> List[Dict[str, Any]]:
    """Every display-math block, so a preview can render them one by one."""
    body = strip_comments(source)
    blocks = []
    for environment in ("equation", "equation*", "align", "align*", "gather", "gather*"):
        pattern = re.compile(
            r"\\begin\{" + re.escape(environment) + r"\}(.*?)\\end\{"
            + re.escape(environment) + r"\}", re.DOTALL)
        for match in pattern.finditer(body):
            blocks.append({
                "environment": environment,
                "latex": match.group(1).strip(),
                "line": body.count("\n", 0, match.start()) + 1,
            })
    for match in re.finditer(r"(?<!\\)\$\$(.+?)\$\$", body, re.DOTALL):
        blocks.append({"environment": "display", "latex": match.group(1).strip(),
                       "line": body.count("\n", 0, match.start()) + 1})
    return sorted(blocks, key=lambda b: b["line"])


# ===== Compilation =====

def available_engine() -> str:
    for engine in TEX_ENGINES:
        if shutil.which(engine):
            return engine
    return ""


def _engine_args(engine: str, tex_name: str, out_dir: str) -> List[str]:
    if engine == "tectonic":
        return ["--outdir", out_dir, "--keep-logs", tex_name]
    if engine == "latexmk":
        return ["-pdf", "-interaction=nonstopmode", f"-outdir={out_dir}", tex_name]
    return ["-interaction=nonstopmode", "-halt-on-error",
            f"-output-directory={out_dir}", tex_name]


def _useful_log_lines(log: str, limit: int = 25) -> List[str]:
    """The lines of a TeX log a human would actually read."""
    keep = []
    for line in log.splitlines():
        if line.startswith("!") or "Error" in line or line.startswith("l."):
            keep.append(line.rstrip())
        elif "Warning" in line and len(keep) < limit:
            keep.append(line.rstrip())
    return keep[:limit]


def compile_document(source: str, out_path: str = "") -> Dict[str, Any]:
    """Compile LaTeX to PDF with whichever engine is installed.

    Runs in a scratch directory so a failed build leaves nothing behind, and
    returns the log either way — a compile that fails is exactly when the log
    matters most.
    """
    engine = available_engine()
    if not engine:
        raise RuntimeError(
            "No LaTeX engine found. Install TeX Live, MiKTeX or Tectonic to "
            "compile; validation and the outline work without one."
        )

    with tempfile.TemporaryDirectory(prefix="carrot-tex-") as scratch:
        tex_path = os.path.join(scratch, "document.tex")
        with open(tex_path, "w", encoding="utf-8") as handle:
            handle.write(source)
        out_dir = os.path.join(scratch, "out")
        os.makedirs(out_dir, exist_ok=True)

        try:
            completed = subprocess.run(
                [shutil.which(engine), *_engine_args(engine, "document.tex", out_dir)],
                cwd=scratch, capture_output=True, text=True, timeout=180, check=False,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "engine": engine, "log": [], "pdf_path": "",
                    "error": "the LaTeX engine did not finish within 180 seconds"}

        log = ""
        log_path = os.path.join(out_dir, "document.log")
        if os.path.isfile(log_path):
            with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
                log = handle.read()
        log = log or (completed.stdout + completed.stderr)

        pdf = os.path.join(out_dir, "document.pdf")
        if not os.path.isfile(pdf):
            return {"ok": False, "engine": engine, "pdf_path": "",
                    "log": _useful_log_lines(log),
                    "error": "the engine produced no PDF"}

        destination = agent_tools.resolve(out_path or "document.pdf")
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copyfile(pdf, destination)
        return {
            "ok": True, "engine": engine, "pdf_path": destination,
            "log": _useful_log_lines(log), "error": "",
        }


# ===== Tools =====

def _tool_validate(source: str = "", path: str = "", **_) -> str:
    source = source or _read(path)
    result = validate(source)
    lines = [f"{result['errors']} errors, {result['warnings']} warnings"]
    for problem in result["problems"][:40]:
        where = f"line {problem['line']}" if problem["line"] else "document"
        lines.append(f"  [{problem['severity']}] {where}: {problem['message']}")
    structure = outline(source)
    if structure:
        lines.append("")
        lines.append("Outline:")
        for entry in structure[:40]:
            lines.append(f"  {'  ' * entry['level']}{entry['kind']}: {entry['title']}")
    return "\n".join(lines)


def _tool_compile(source: str = "", path: str = "", out_path: str = "document.pdf", **_) -> str:
    source = source or _read(path)
    checked = validate(source)
    if not checked["ok"]:
        detail = "; ".join(
            f"line {p['line']}: {p['message']}"
            for p in checked["problems"] if p["severity"] == "error"
        )
        return f"error: the document has structural errors, so compiling would fail — {detail}"
    result = compile_document(source, out_path)
    if not result["ok"]:
        return f"error: {result['error']}\n" + "\n".join(result["log"])
    return f"Compiled with {result['engine']} to {result['pdf_path']}"


def _read(path: str) -> str:
    if not path:
        raise ValueError("give either the LaTeX source or a workspace path to it")
    with open(agent_tools.resolve(path), "r", encoding="utf-8") as handle:
        return handle.read()


TOOLS: Dict[str, Dict[str, Any]] = {
    "latex_validate": {
        "handler": _tool_validate,
        "mutating": False,
        "risk": "low",
        "description": (
            "Check LaTeX for unclosed environments, unbalanced braces and broken "
            "cross-references, and return the document outline. Needs no TeX "
            "installation."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "LaTeX source to check"},
                "path": {"type": "string", "description": "Or a workspace-relative .tex path"},
            },
        },
    },
    "latex_compile": {
        "handler": _tool_compile,
        "mutating": True,
        "risk": "medium",
        "requires": "latex",
        "description": (
            "Compile LaTeX to a PDF in the workspace using the installed TeX "
            "engine. Validates first and refuses rather than producing a broken build."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "LaTeX source to compile"},
                "path": {"type": "string", "description": "Or a workspace-relative .tex path"},
                "out_path": {"type": "string",
                             "description": "Workspace-relative output PDF path"},
            },
        },
    },
}
