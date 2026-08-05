"""Running the file that is open in the Code tab.

The Code tab could edit Python, Java and C++ and then do nothing with any of
them. What existed was a one-line extension→command map that returned "gcc"
for a .c file and "java" for a .java file — neither of which runs anything.
Compiled languages need two steps and a place to put the binary, and every
one of them needs a real answer when the toolchain is simply not installed,
because on a fresh Windows machine most of them are not.

Each recipe therefore says three things: how to build, how to run, and which
executable has to exist for either to work. When it is missing the user gets
the name of the thing to install rather than "command not found".
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from typing import Any, Dict, List, Optional

DEFAULT_TIMEOUT = 60
MAX_OUTPUT_CHARS = 40_000


class Recipe:
    """How to run one language.

    `build` and `run` are argv templates. {file} is the absolute source path,
    {stem} its name without extension, {out} a path for the built artifact,
    and {dir} the directory the source lives in.
    """

    def __init__(self, language, tool, run, build=None, install=None, note=""):
        self.language = language
        self.tool = tool              # the executable that must exist
        self.run = run
        self.build = build
        self.install = install or tool
        self.note = note


# Interpreted languages run straight from source; compiled ones build to a
# temporary directory so a Run never litters the workspace with binaries and
# .class files.
RECIPES: Dict[str, Recipe] = {
    ".py": Recipe("Python", sys.executable or "python3", ["{tool}", "{file}"],
                  install="Python"),
    ".js": Recipe("JavaScript", "node", ["node", "{file}"], install="Node.js"),
    ".mjs": Recipe("JavaScript", "node", ["node", "{file}"], install="Node.js"),
    ".ts": Recipe("TypeScript", "npx", ["npx", "--yes", "tsx", "{file}"],
                  install="Node.js", note="uses npx tsx, downloaded on first run"),
    ".rb": Recipe("Ruby", "ruby", ["ruby", "{file}"], install="Ruby"),
    ".sh": Recipe("Shell", "bash", ["bash", "{file}"], install="bash"),
    ".ps1": Recipe("PowerShell", "powershell",
                   ["powershell", "-ExecutionPolicy", "Bypass", "-File", "{file}"],
                   install="PowerShell"),
    ".php": Recipe("PHP", "php", ["php", "{file}"], install="PHP"),
    ".lua": Recipe("Lua", "lua", ["lua", "{file}"], install="Lua"),
    ".pl": Recipe("Perl", "perl", ["perl", "{file}"], install="Perl"),

    # Compiled. The build step is what the old map was missing entirely.
    ".c": Recipe("C", "gcc",
                 build=["gcc", "{file}", "-o", "{out}", "-lm"],
                 run=["{out}"], install="GCC (build-essential, or MinGW on Windows)"),
    ".cpp": Recipe("C++", "g++",
                   build=["g++", "-std=c++17", "{file}", "-o", "{out}"],
                   run=["{out}"], install="G++ (build-essential, or MinGW on Windows)"),
    ".cc": Recipe("C++", "g++",
                  build=["g++", "-std=c++17", "{file}", "-o", "{out}"],
                  run=["{out}"], install="G++"),
    ".m": Recipe("Objective-C", "clang",
                 build=["clang", "{file}", "-o", "{out}", "-framework", "Foundation"],
                 run=["{out}"], install="Xcode command line tools"),
    ".rs": Recipe("Rust", "rustc",
                  build=["rustc", "{file}", "-o", "{out}"],
                  run=["{out}"], install="Rust (rustup)"),
    ".go": Recipe("Go", "go", ["go", "run", "{file}"], install="Go"),
    ".cs": Recipe("C#", "dotnet", ["dotnet", "run", "--project", "{dir}"],
                  install=".NET SDK", note="runs the project in this folder"),
    ".swift": Recipe("Swift", "swift", ["swift", "{file}"], install="Swift"),
    ".kt": Recipe("Kotlin", "kotlinc",
                  build=["kotlinc", "{file}", "-include-runtime", "-d", "{out}.jar"],
                  run=["java", "-jar", "{out}.jar"], install="Kotlin compiler"),
    # Single-file source launch, JDK 11+. The old map's bare "java" only works
    # on a .class file, so it never ran a .java the user was editing.
    ".java": Recipe("Java", "java", ["java", "{file}"], install="a JDK (11 or newer)",
                    note="single-file source launch; needs JDK 11+"),
}


def recipe_for(path: str) -> Optional[Recipe]:
    return RECIPES.get(os.path.splitext(path)[1].lower())


def _resolve_tool(recipe: Recipe) -> Optional[str]:
    # sys.executable is an absolute path in a normal install, but inside the
    # frozen backend it points at carrot-backend itself, which would re-launch
    # the app instead of running the script.
    if recipe.language == "Python":
        if getattr(sys, "frozen", False):
            return shutil.which("python3") or shutil.which("python")
        return sys.executable or shutil.which("python3")
    return shutil.which(recipe.tool)


def languages() -> List[Dict[str, Any]]:
    """Every language the Run button knows, and whether it can run here."""
    seen = {}
    for ext, recipe in RECIPES.items():
        entry = seen.setdefault(recipe.language, {
            "language": recipe.language,
            "extensions": [],
            "install": recipe.install,
            "note": recipe.note,
        })
        entry["extensions"].append(ext)
        entry["available"] = _resolve_tool(recipe) is not None
    return sorted(seen.values(), key=lambda e: e["language"].lower())


def run_file(rel_path: str, timeout: int = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """Build if needed, then run, inside the workspace sandbox."""
    from .files_api import resolve

    full = resolve(rel_path, must_exist=True)
    recipe = recipe_for(full)
    if recipe is None:
        return {
            "ok": False,
            "language": "",
            "output": f"Carrot does not know how to run {os.path.splitext(full)[1] or 'that file'}.",
            "missing_tool": "",
        }

    tool = _resolve_tool(recipe)
    if not tool:
        # Pressing Run is the moment someone finds out they have no Python, so
        # this answer has to carry the download page and not just a complaint.
        from .files_api import LANGUAGE_HELP

        return {
            "ok": False,
            "language": recipe.language,
            "output": (f"{recipe.language} is not set up on this computer.\n"
                       f"Install {recipe.install}, then try again."),
            "missing_tool": recipe.install,
            "help_url": LANGUAGE_HELP.get(recipe.language, ""),
        }

    workdir = os.path.dirname(full)
    stem = os.path.splitext(os.path.basename(full))[0]
    steps: List[Dict[str, Any]] = []

    # Compiled languages build into a temp directory, so running a file never
    # leaves a binary or a .class beside the source.
    with tempfile.TemporaryDirectory(prefix="carrot-run-") as build_dir:
        out = os.path.join(build_dir, stem + (".exe" if os.name == "nt" else ""))
        fields = {"tool": tool, "file": full, "stem": stem, "out": out, "dir": workdir}

        def fill(template):
            return [part.format(**fields) for part in template]

        if recipe.build:
            built = _spawn(fill(recipe.build), workdir, timeout)
            steps.append({"stage": "build", **built})
            if not built["ok"]:
                return {
                    "ok": False,
                    "language": recipe.language,
                    "output": built["output"],
                    "stage": "build",
                    "steps": steps,
                    "missing_tool": "",
                    "missing_package": _missing_package(built["output"], recipe.language),
                }

        ran = _spawn(fill(recipe.run), workdir, timeout)
        steps.append({"stage": "run", **ran})
        return {
            "ok": ran["ok"],
            "language": recipe.language,
            "output": ran["output"],
            "exit_code": ran["exit_code"],
            "stage": "run",
            "steps": steps,
            "missing_tool": "",
            # A traceback whose real content is one line near the bottom is not
            # an answer for someone who just wanted to import pandas.
            "missing_package": None if ran["ok"] else _missing_package(
                ran["output"], recipe.language
            ),
        }


def _missing_package(output: str, language: str):
    """Never let dependency detection be the thing that breaks a run."""
    try:
        from . import packages

        return packages.detect(output, language)
    except Exception:
        return None


def _spawn(argv: List[str], cwd: str, timeout: int) -> Dict[str, Any]:
    """One step. No shell: argv is built from a recipe and a real path, and
    a filename with a space in it would otherwise split into two arguments."""
    try:
        result = subprocess.run(
            argv, cwd=cwd, capture_output=True, text=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": -1, "timed_out": True,
                "output": f"Stopped after {timeout} seconds — is it waiting for input?"}
    except OSError as exc:
        return {"ok": False, "exit_code": -1, "timed_out": False,
                "output": f"Could not start {argv[0]}: {exc}"}
    output = result.stdout or ""
    if result.stderr:
        output += ("\n" if output else "") + result.stderr
    return {
        "ok": result.returncode == 0,
        "exit_code": result.returncode,
        "timed_out": False,
        "output": output[:MAX_OUTPUT_CHARS] or "(no output)",
    }
