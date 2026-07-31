"""Bridges to programs that are not Python: MATLAB/Octave and CAD kernels.

None of these ship with Carrot and none can be assumed. Each tool resolves its
capability first, so a machine without MATLAB gets one clear sentence naming
what is missing and how to get it, rather than a stack trace from a subprocess
that was never going to start.

Octave is accepted wherever MATLAB is asked for. The dialects are close enough
for the numeric and symbolic work this pack is about, and a student without a
MATLAB licence should not be locked out of the feature.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List

from ... import agent_tools, extensions

# CAD formats a kernel can usually open. Anything else is refused by name rather
# than handed to a converter that will fail obscurely.
CAD_EXTENSIONS = {
    ".step", ".stp", ".iges", ".igs", ".brep", ".stl", ".obj", ".ply",
    ".fcstd", ".dxf", ".dwg", ".sat", ".3ds",
}

MATLAB_TIMEOUT = 120
CAD_TIMEOUT = 180


# ===== MATLAB / Octave =====

def _matlab_command(binary: str, script_path: str, workdir: str) -> List[str]:
    """Batch-mode invocation for whichever engine was found."""
    if os.path.basename(binary).startswith("octave"):
        return [binary, "--no-gui", "--quiet", script_path]
    # MATLAB's -batch runs a script non-interactively and exits non-zero on error.
    return [binary, "-batch", f"cd('{workdir}'); run('{script_path}');"]


def run_matlab(code: str, timeout: int = MATLAB_TIMEOUT) -> Dict[str, Any]:
    """Run a MATLAB/Octave script and capture its output."""
    status = extensions.require_capability("matlab")
    with tempfile.TemporaryDirectory(prefix="carrot-matlab-") as scratch:
        script = os.path.join(scratch, "carrot_script.m")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(code)
        try:
            completed = subprocess.run(
                _matlab_command(status["path"], script, scratch),
                cwd=scratch, capture_output=True, text=True, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "engine": status["path"], "stdout": "", "stderr": "",
                    "error": f"the script did not finish within {timeout} seconds"}

        # A script may have written figures or data next to itself.
        artifacts = [
            entry for entry in sorted(os.listdir(scratch))
            if entry != "carrot_script.m"
        ]
        saved = []
        for entry in artifacts:
            destination = agent_tools.resolve(entry)
            shutil.copyfile(os.path.join(scratch, entry), destination)
            saved.append(destination)

        return {
            "ok": completed.returncode == 0,
            "engine": os.path.basename(status["path"]),
            "stdout": completed.stdout.strip(),
            "stderr": completed.stderr.strip(),
            "artifacts": saved,
            "error": "" if completed.returncode == 0 else "the script exited with an error",
        }


def _tool_matlab(code: str, **_) -> str:
    if not code.strip():
        raise ValueError("give the MATLAB code to run")
    result = run_matlab(code)
    parts = [f"[{result['engine']}] {'ok' if result['ok'] else result['error']}"]
    if result["stdout"]:
        parts.append(result["stdout"][:8000])
    if result["stderr"]:
        parts.append("stderr:\n" + result["stderr"][:4000])
    if result.get("artifacts"):
        parts.append("Wrote: " + ", ".join(result["artifacts"]))
    return "\n\n".join(parts)


# ===== MATLAB expression -> LaTeX =====

# Handled locally rather than by shelling out, so this works with no MATLAB at
# all. It covers the operators that actually appear in a paper's formulas.
def matlab_expression_to_latex(expression: str) -> str:
    """Translate a MATLAB-style expression into LaTeX.

    Deliberately syntactic and conservative: it rewrites the constructs it
    recognises and leaves anything else alone, so the result is always something
    the user can read and correct rather than a confident mistranslation.
    """
    text = expression.strip().rstrip(";")

    # Elementwise operators mean the same thing as their plain forms in maths.
    text = text.replace(".*", "*").replace("./", "/").replace(".^", "^")

    # sqrt(x) -> \sqrt{x}, and the usual named functions.
    text = re.sub(r"\bsqrt\s*\(([^()]*)\)", r"\\sqrt{\1}", text)
    text = re.sub(r"\babs\s*\(([^()]*)\)", r"\\left|\1\\right|", text)
    text = re.sub(r"\bexp\s*\(([^()]*)\)", r"e^{\1}", text)
    for name in ("sin", "cos", "tan", "log", "ln", "min", "max", "det"):
        text = re.sub(rf"\b{name}\s*\(", rf"\\{name}(", text)

    # a/b -> \frac{a}{b} for simple operands, which is the common case.
    text = re.sub(
        r"(?<![\\\w])([A-Za-z0-9_]+|\([^()]*\))\s*/\s*([A-Za-z0-9_]+|\([^()]*\))",
        r"\\frac{\1}{\2}", text)

    # pi -> \pi, and Greek names spelled out.
    for greek in ("alpha", "beta", "gamma", "delta", "theta", "lambda", "mu",
                  "sigma", "phi", "omega", "pi", "rho", "tau"):
        text = re.sub(rf"(?<![\\\w]){greek}(?![\w])", rf"\\{greek}", text)

    # x^(n) -> x^{n}; multi-character exponents need braces in LaTeX.
    text = re.sub(r"\^\s*\(([^()]*)\)", r"^{\1}", text)
    text = re.sub(r"\^\s*([A-Za-z0-9]{2,})", r"^{\1}", text)

    # Subscripts written with underscores.
    text = re.sub(r"_\s*([A-Za-z0-9]{2,})", r"_{\1}", text)

    text = text.replace("*", " \\cdot ")
    return re.sub(r"\s+", " ", text).strip()


def _tool_formula(expression: str, display: bool = True, **_) -> str:
    if not expression.strip():
        raise ValueError("give the expression to convert")
    latex = matlab_expression_to_latex(expression)
    if display:
        return f"\\begin{{equation}}\n  {latex}\n\\end{{equation}}"
    return f"${latex}$"


# ===== CAD =====

def render_cad(path: str, out_path: str = "", timeout: int = CAD_TIMEOUT) -> Dict[str, Any]:
    """Render a CAD model to a PNG using FreeCAD's headless interpreter."""
    status = extensions.require_capability("cad")
    source = agent_tools.resolve(path)
    if not os.path.isfile(source):
        raise ValueError(f"no such file in the workspace: {path}")
    extension = os.path.splitext(source)[1].lower()
    if extension not in CAD_EXTENSIONS:
        raise ValueError(
            f"'{extension}' is not a CAD format this can open. Supported: "
            + ", ".join(sorted(CAD_EXTENSIONS))
        )

    destination = agent_tools.resolve(
        out_path or os.path.splitext(os.path.basename(source))[0] + ".png")

    script = f"""
import FreeCAD, Import, Part
doc = FreeCAD.newDocument("carrot")
Import.insert({source!r}, "carrot")
try:
    import FreeCADGui
    FreeCADGui.showMainWindow()
    view = FreeCADGui.ActiveDocument.ActiveView
    view.viewAxonometric()
    view.fitAll()
    view.saveImage({destination!r}, 1600, 1200, "White")
except Exception as exc:
    # No GUI available: fall back to reporting the model's geometry.
    shapes = [o.Shape for o in doc.Objects if hasattr(o, "Shape")]
    box = shapes[0].BoundBox if shapes else None
    print("NOGUI", len(doc.Objects), box)
    raise SystemExit(3)
"""
    with tempfile.TemporaryDirectory(prefix="carrot-cad-") as scratch:
        script_path = os.path.join(scratch, "render.py")
        with open(script_path, "w", encoding="utf-8") as handle:
            handle.write(script)
        try:
            completed = subprocess.run(
                [status["path"], script_path],
                cwd=scratch, capture_output=True, text=True, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            return {"ok": False, "image_path": "",
                    "error": f"the CAD kernel did not finish within {timeout} seconds"}

    if os.path.isfile(destination):
        return {"ok": True, "image_path": destination, "error": ""}
    detail = (completed.stdout + completed.stderr).strip()[-800:]
    if "NOGUI" in detail:
        return {"ok": False, "image_path": "",
                "error": "this FreeCAD build is headless and cannot render images; "
                         "geometry can still be inspected but not pictured"}
    return {"ok": False, "image_path": "", "error": detail or "the CAD kernel produced no image"}


def _tool_cad(path: str, out_path: str = "", **_) -> str:
    result = render_cad(path, out_path)
    if not result["ok"]:
        return f"error: {result['error']}"
    return f"Rendered {path} to {result['image_path']}"


TOOLS: Dict[str, Dict[str, Any]] = {
    "matlab_run": {
        "handler": _tool_matlab,
        "mutating": True,
        "risk": "high",
        "requires": "matlab",
        "description": (
            "Run a MATLAB or Octave script and return its output. Files the script "
            "writes are saved into the workspace. Requires MATLAB or Octave installed."
        ),
        "parameters": {
            "type": "object",
            "properties": {"code": {"type": "string", "description": "MATLAB/Octave source"}},
            "required": ["code"],
        },
    },
    "formula_to_latex": {
        "handler": _tool_formula,
        "mutating": False,
        "risk": "low",
        "description": (
            "Convert a MATLAB-style expression such as 'sqrt(x^2 + y^2)/(2*pi)' into "
            "LaTeX. Purely syntactic and needs no MATLAB; leaves anything it does not "
            "recognise untouched so it can be corrected rather than silently wrong."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {"type": "string", "description": "The expression to convert"},
                "display": {"type": "boolean",
                            "description": "Wrap in an equation environment rather than inline math"},
            },
            "required": ["expression"],
        },
    },
    "cad_render": {
        "handler": _tool_cad,
        "mutating": True,
        "risk": "medium",
        "requires": "cad",
        "description": (
            "Render a CAD model (STEP, IGES, STL, FCStd and similar) to a PNG in the "
            "workspace so it can be used as a figure. Requires FreeCAD installed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Workspace-relative CAD file"},
                "out_path": {"type": "string", "description": "Workspace-relative output PNG"},
            },
            "required": ["path"],
        },
    },
}
