"""Turning "ModuleNotFoundError" into a button that fixes it.

Someone who is not a programmer writes `import pandas`, presses Run, and gets
a red wall of traceback whose actual content is one line near the bottom. They
have no way to know that the fix is a single command, what that command is, or
that the name in the error is not always the name you install — `import cv2`
is fixed by installing `opencv-python`, and nothing in the error says so.

So Carrot reads the failure. Every runnable language reports a missing
dependency in its own words; this module knows those words, works out what to
install, and hands the Code tab a one-click offer.

Two rules keep this from being dangerous:

* **The package name is validated, never interpolated.** A name is matched
  against a conservative pattern and then passed as a single argv element. It
  never reaches a shell, and a name that does not match the pattern produces no
  offer at all rather than a best effort.
* **Nothing installs itself.** Detection produces a suggestion. Installing
  happens only when the user clicks the button, which is also why the exact
  command is shown next to it.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

INSTALL_TIMEOUT = 300
MAX_OUTPUT_CHARS = 20_000

# Deliberately strict. Real package names live well inside this; anything with
# a shell metacharacter, a leading dash (which would read as a flag), a path
# separator or a space does not, and gets no offer.
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@/+-]{0,99}$")

# Import name -> the thing you actually install. This mapping is the single
# most useful thing in the module: the error says one word, the fix needs
# another, and a beginner has no way to bridge that.
PYTHON_ALIASES = {
    "cv2": "opencv-python",
    "PIL": "Pillow",
    "sklearn": "scikit-learn",
    "skimage": "scikit-image",
    "yaml": "PyYAML",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "serial": "pyserial",
    "OpenGL": "PyOpenGL",
    "win32com": "pywin32",
    "win32api": "pywin32",
    "Crypto": "pycryptodome",
    "google": "google-api-python-client",
    "docx": "python-docx",
    "pptx": "python-pptx",
    "fitz": "PyMuPDF",
    "attr": "attrs",
    "jwt": "PyJWT",
    "magic": "python-magic",
    "psycopg2": "psycopg2-binary",
    "MySQLdb": "mysqlclient",
    "usb": "pyusb",
    "zmq": "pyzmq",
    "lxml": "lxml",
    "nacl": "PyNaCl",
    "pkg_resources": "setuptools",
}

# Modules that ship with Python. Offering to pip install one of these sends the
# user to a package that is either missing entirely or, worse, a typosquat.
PYTHON_STDLIB_HINT = {
    "tkinter": (
        "tkinter comes with Python but is packaged separately on Linux — "
        "install python3-tk with your system package manager."
    ),
    "_tkinter": (
        "tkinter comes with Python but is packaged separately on Linux — "
        "install python3-tk with your system package manager."
    ),
    "distutils": "distutils was removed in Python 3.12 — install setuptools instead.",
}


class Manager:
    """One package manager: how to invoke it, and what it is called."""

    def __init__(self, manager_id: str, label: str, argv, tool: str, note: str = ""):
        self.id = manager_id
        self.label = label
        self.argv = argv          # callable(name) -> list[str]
        self.tool = tool          # executable that must exist
        self.note = note


def _python_argv(name: str) -> List[str]:
    # `-m pip` against the interpreter that will actually run the file, so the
    # package lands where the import will look for it. Installing with a
    # different python is the classic "but I did install it" failure.
    return [python_executable(), "-m", "pip", "install", name]


def python_executable() -> str:
    """The interpreter Run uses — which is the one that must get the package."""
    if getattr(sys, "frozen", False):
        return shutil.which("python3") or shutil.which("python") or "python3"
    return sys.executable or shutil.which("python3") or "python3"


MANAGERS: Dict[str, Manager] = {
    "pip": Manager("pip", "pip", _python_argv, "", note="installs for the Python that runs your file"),
    "npm": Manager("npm", "npm", lambda n: ["npm", "install", n], "npm"),
    "gem": Manager("gem", "gem", lambda n: ["gem", "install", n], "gem"),
    "cargo": Manager("cargo", "cargo", lambda n: ["cargo", "add", n], "cargo"),
    "go": Manager("go", "go get", lambda n: ["go", "get", n], "go"),
    "cpan": Manager("cpan", "cpan", lambda n: ["cpan", "-i", n], "cpan"),
    "luarocks": Manager("luarocks", "luarocks", lambda n: ["luarocks", "install", n], "luarocks"),
    "composer": Manager("composer", "composer", lambda n: ["composer", "require", n], "composer"),
}


# ===== Detection =====
#
# One pattern list per language. Each entry is (regex, manager). The captured
# group is the name as the runtime reported it, before aliasing.

PATTERNS: Dict[str, List] = {
    "Python": [
        (re.compile(r"ModuleNotFoundError: No module named ['\"]([^'\"]+)['\"]"), "pip"),
        (re.compile(r"ImportError: No module named ['\"]?([A-Za-z0-9_.]+)"), "pip"),
    ],
    "JavaScript": [
        (re.compile(r"Cannot find module ['\"]([^'\"]+)['\"]"), "npm"),
        (re.compile(r"Cannot find package ['\"]([^'\"]+)['\"]"), "npm"),
        (re.compile(r"ERR_MODULE_NOT_FOUND.*?['\"]([^'\"]+)['\"]", re.S), "npm"),
    ],
    "Ruby": [
        (re.compile(r"cannot load such file -- ([A-Za-z0-9_./-]+)"), "gem"),
        (re.compile(r"Could not find gem ['\"]([^'\"]+)['\"]"), "gem"),
    ],
    "Rust": [
        (re.compile(r"can't find crate for `([A-Za-z0-9_-]+)`"), "cargo"),
        (re.compile(r"use of undeclared crate or module `([A-Za-z0-9_-]+)`"), "cargo"),
    ],
    "Go": [
        (re.compile(r"no required module provides package ([^\s;]+)"), "go"),
        (re.compile(r"cannot find package \"([^\"]+)\""), "go"),
    ],
    "Perl": [
        (re.compile(r"Can't locate ([A-Za-z0-9_/]+)\.pm in @INC"), "cpan"),
    ],
    "Lua": [
        (re.compile(r"module '([A-Za-z0-9_.-]+)' not found"), "luarocks"),
    ],
    "PHP": [
        (re.compile(r"Class ['\"]([A-Za-z0-9_\\\\]+)['\"] not found"), "composer"),
    ],
}

# TypeScript runs through node, so it fails the way node does.
PATTERNS["TypeScript"] = PATTERNS["JavaScript"]

# Languages where the missing piece is a system library rather than a package
# any tool here can fetch. Guessing `apt install` on someone's machine is worse
# than telling them plainly what is missing.
HEADER_PATTERN = re.compile(r"fatal error: ([A-Za-z0-9_/.+-]+\.(?:h|hpp|hh)): No such file")
JAVA_PACKAGE = re.compile(r"package ([A-Za-z0-9_.]+) does not exist")


def detect(output: str, language: str) -> Optional[Dict[str, Any]]:
    """Read a failed run's output and work out what is missing.

    Returns ``None`` when nothing recognizable is wrong — a syntax error is
    not a missing package, and offering to install something would be noise on
    top of a real error message.
    """
    text = output or ""
    if not text.strip():
        return None

    for pattern, manager_id in PATTERNS.get(language, []):
        found = pattern.search(text)
        if not found:
            continue
        raw = found.group(1).strip()
        if language == "Python":
            return _python_offer(raw)
        return _offer(raw, manager_id, language)

    # C and C++ report a header, not a package; there is no portable installer.
    header = HEADER_PATTERN.search(text)
    if header and language in ("C", "C++", "Objective-C"):
        return {
            "missing": header.group(1),
            "installable": False,
            "message": (
                f"{header.group(1)} is missing. That is a system library, not "
                f"something Carrot can fetch — install the development package "
                f"for it with your system's package manager."
            ),
        }

    java = JAVA_PACKAGE.search(text)
    if java and language in ("Java", "Kotlin"):
        return {
            "missing": java.group(1),
            "installable": False,
            "message": (
                f"The package {java.group(1)} is not on the classpath. Add it as "
                f"a dependency in your build file (Maven or Gradle), or put its "
                f"jar next to the file."
            ),
        }
    return None


def _python_offer(imported: str) -> Optional[Dict[str, Any]]:
    """Python needs the alias table and a standard-library guard."""
    # "no module named a.b" — only the top-level name is ever installable.
    top = imported.split(".")[0]
    if top in PYTHON_STDLIB_HINT:
        return {"missing": top, "installable": False, "message": PYTHON_STDLIB_HINT[top]}
    if top in _stdlib_names():
        return {
            "missing": top,
            "installable": False,
            "message": (
                f"{top} is part of Python's standard library, so it should "
                f"already be there — this is more likely a broken install than "
                f"a missing package."
            ),
        }
    package = PYTHON_ALIASES.get(top, top)
    offer = _offer(package, "pip", "Python")
    if offer and package != top:
        # The single most useful sentence in the whole feature.
        offer["note"] = f"`import {top}` comes from the package `{package}`."
    return offer


def _stdlib_names() -> set:
    names = getattr(sys, "stdlib_module_names", None)
    return set(names) if names else set()


def _offer(name: str, manager_id: str, language: str) -> Optional[Dict[str, Any]]:
    """Build an install offer, or none at all if the name is not safe."""
    manager = MANAGERS.get(manager_id)
    if not manager or not SAFE_NAME.match(name or ""):
        return None
    argv = manager.argv(name)
    return {
        "missing": name,
        "package": name,
        "manager": manager_id,
        "manager_label": manager.label,
        "language": language,
        "installable": True,
        "available": _manager_available(manager),
        "command": " ".join(argv),
        "note": manager.note,
        "message": f"{name} is not installed. Install it with {manager.label}?",
    }


def _manager_available(manager: Manager) -> bool:
    if not manager.tool:
        return bool(shutil.which(python_executable()) or os.path.isabs(python_executable()))
    return shutil.which(manager.tool) is not None


# ===== Installation =====

def install(package: str, manager_id: str, cwd: str = "") -> Dict[str, Any]:
    """Run one install. The name is validated and passed as a single argument.

    No shell anywhere in this path, so a package name is a package name even
    when someone types one full of punctuation.
    """
    manager = MANAGERS.get(manager_id)
    if not manager:
        return {"ok": False, "output": f"Carrot does not know the '{manager_id}' installer."}
    if not SAFE_NAME.match(package or ""):
        return {"ok": False, "output": f"'{package}' is not a valid package name."}
    if not _manager_available(manager):
        return {
            "ok": False,
            "output": (f"{manager.label} is not installed on this computer, so "
                       f"Carrot cannot install {package} for you."),
        }

    argv = manager.argv(package)
    try:
        result = subprocess.run(
            argv,
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": f"Installing {package} took longer than "
                                       f"{INSTALL_TIMEOUT} seconds and was stopped."}
    except OSError as exc:
        return {"ok": False, "output": f"Could not start {argv[0]}: {exc}"}

    output = (result.stdout or "") + (("\n" + result.stderr) if result.stderr else "")
    ok = result.returncode == 0
    return {
        "ok": ok,
        "package": package,
        "manager": manager_id,
        "exit_code": result.returncode,
        "command": " ".join(argv),
        "output": (output[:MAX_OUTPUT_CHARS] or ("installed" if ok else "failed")),
    }
