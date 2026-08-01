"""Acting on the machine itself, in two tiers that are not the same thing.

There is a real difference between *asking the operating system to open
something* and *taking the mouse*. The first is a bounded request the OS
validates — it opens a PDF in your PDF reader and nothing else can happen. The
second is unbounded: once an agent has the cursor it can click anything on the
screen, including windows it was never told about, and there is no layer left
underneath to say no.

So this module keeps them apart:

**Tier one — delegated (on by default, approval-gated).**
``open_path`` hands a file or folder to the system handler. ``launch_app``
starts a program, but only one that is on a list the user wrote by hand. Both
still ask before running; neither can be silently talked into something else,
because the target is checked against the filesystem and the allowlist before
the OS ever sees it.

**Tier two — direct control (off by default).**
Screenshots, clicks at coordinates, synthetic keystrokes. This is what makes
"fill out this form in a desktop app" possible, and it is switched off until
the user turns it on in Settings. Every action asks, every time — no
"don't ask again" is offered, because there is no bounded description of what
a click at (840, 512) is going to do.

The distinction is the point: the tasks people actually want ("open my
assignment", "pull up the syllabus") almost all live in tier one, and tier one
cannot run away with the machine.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from . import policy
from .config import get_config

SCREENSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "screenshots")

# Extensions the OS would execute rather than open. Handing one of these to the
# system handler is running it, which is not what "open this file" means to
# anybody who typed it.
EXECUTABLE_EXTENSIONS = {
    ".exe", ".msi", ".bat", ".cmd", ".ps1", ".vbs", ".vbe", ".js", ".jse",
    ".wsf", ".wsh", ".scr", ".com", ".pif", ".reg", ".hta", ".cpl",
    ".sh", ".bash", ".zsh", ".command", ".app", ".dmg", ".pkg", ".jar", ".run",
}


def system() -> str:
    return platform.system().lower()


# ===== Tier one: delegated =====

def open_roots() -> List[str]:
    """Directories a path is allowed to live under.

    Defaults to the ordinary places a person keeps their work, plus whatever
    they added to the document index — if Carrot may read a folder, opening a
    file from it is not an escalation.
    """
    settings = get_config()
    configured = settings.get("agent_open_roots")
    if configured:
        return [os.path.abspath(os.path.expanduser(root)) for root in configured]

    home = os.path.expanduser("~")
    roots = [
        os.path.join(home, name)
        for name in ("Documents", "Desktop", "Downloads", "Pictures", "workspace", "CarrotProjects")
    ]
    roots += [os.path.abspath(os.path.expanduser(d)) for d in settings.get("index_dirs", [])]
    workspace = settings.get("code_workspace_dir")
    if workspace:
        roots.append(os.path.abspath(os.path.expanduser(workspace)))
    return [root for root in roots if root]


def check_path(path: str) -> Dict[str, Any]:
    """Decide whether a path may be handed to the system handler."""
    if not path:
        return {"ok": False, "reason": "no path given"}

    full = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(full):
        return {"ok": False, "reason": f"nothing exists at {full}"}

    if os.path.islink(full):
        # Following a symlink out of an allowed root is the obvious way around
        # the check below, so resolve it and judge the destination.
        full = os.path.realpath(full)

    extension = os.path.splitext(full)[1].lower()
    if extension in EXECUTABLE_EXTENSIONS:
        return {
            "ok": False,
            "reason": f"{extension} files are programs. Opening one runs it — "
                      "add it to the app allowlist if you really want Carrot to launch it.",
        }

    roots = open_roots()
    if roots and not any(full == root or full.startswith(root + os.sep) for root in roots):
        return {
            "ok": False,
            "reason": f"{full} is outside the folders Carrot may open. Add its folder "
                      "to your indexed directories, or to agent_open_roots in Settings.",
        }
    return {"ok": True, "reason": "", "path": full}


def open_path(path: str) -> Dict[str, Any]:
    """Open a file or folder with the system's default handler."""
    check = check_path(path)
    if not check["ok"]:
        return {"success": False, "error": check["reason"]}

    full = check["path"]
    try:
        if system() == "windows":
            os.startfile(full)  # noqa: S606 — the OS chooses the handler, not us
        elif system() == "darwin":
            subprocess.run(["open", full], check=True, timeout=20)
        else:
            subprocess.run(["xdg-open", full], check=True, timeout=20)
    except Exception as exc:
        return {"success": False, "error": f"could not open {full}: {exc}"}
    return {"success": True, "path": full, "detail": f"opened {os.path.basename(full)}"}


def app_allowlist() -> List[str]:
    return list(get_config().get("agent_app_allowlist", []))


def launch_app(app: str, arguments: Optional[List[str]] = None) -> Dict[str, Any]:
    """Start a program from the allowlist.

    The allowlist is matched before anything is resolved on disk, so a name the
    user never approved cannot be reached by pointing at a different binary
    with the same basename.
    """
    app = (app or "").strip()
    if not any(app.lower() == entry.lower() for entry in app_allowlist()):
        return {
            "success": False,
            "error": f"'{app}' is not on the list of apps Carrot may launch. Add it in Settings first.",
        }

    executable = shutil.which(app)
    if executable is None:
        return {"success": False, "error": f"'{app}' is allowed but is not installed, or is not on PATH"}

    try:
        subprocess.Popen(  # noqa: S603 — allowlisted name, resolved path, no shell
            [executable, *(arguments or [])],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        return {"success": False, "error": f"could not launch {app}: {exc}"}
    return {"success": True, "app": app, "detail": f"launched {app}"}


# ===== Tier two: direct control =====

def control_available() -> Dict[str, Any]:
    """Whether screen control could run, separate from whether it is allowed."""
    enabled = bool(get_config().get("agent_desktop_control_enabled", False))
    try:
        import pyautogui  # noqa: F401
    except Exception as exc:
        return {
            "available": False,
            "enabled": enabled,
            "reason": f"pyautogui is not usable here ({exc})",
            "hint": "pip install pyautogui",
        }
    return {"available": True, "enabled": enabled, "reason": "", "hint": ""}


def _require_control() -> Optional[str]:
    if not get_config().get("agent_desktop_control_enabled", False):
        return ("desktop control is turned off. Turn it on in Settings if you want "
                "Carrot to move the mouse and type on your behalf.")
    state = control_available()
    if not state["available"]:
        return f"{state['reason']}. {state['hint']}"
    return None


def screenshot() -> Dict[str, Any]:
    """Capture the screen.

    Gated on the same switch as clicking, because a screenshot of a whole
    desktop is a read of every window that happens to be open — mail, banking,
    someone else's message thread — and none of that was part of the task.
    """
    blocked = _require_control()
    if blocked:
        return {"success": False, "error": blocked}

    try:
        import pyautogui

        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        name = f"desktop_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.png"
        path = os.path.join(SCREENSHOT_DIR, name)
        pyautogui.screenshot().save(path)
    except Exception as exc:
        return {"success": False, "error": f"screenshot failed: {exc}"}
    return {"success": True, "path": path, "detail": f"captured the screen to {name}"}


def click_at(x: int, y: int, button: str = "left", clicks: int = 1) -> Dict[str, Any]:
    blocked = _require_control()
    if blocked:
        return {"success": False, "error": blocked}
    if button not in {"left", "right", "middle"}:
        return {"success": False, "error": f"unknown mouse button '{button}'"}

    try:
        import pyautogui

        width, height = pyautogui.size()
        if not (0 <= x < width and 0 <= y < height):
            return {"success": False, "error": f"({x}, {y}) is off screen ({width}x{height})"}
        pyautogui.click(x=x, y=y, button=button, clicks=min(clicks, 2))
    except Exception as exc:
        return {"success": False, "error": f"click failed: {exc}"}
    return {"success": True, "detail": f"clicked at ({x}, {y})"}


def type_text(text: str) -> Dict[str, Any]:
    """Type at the focused window — whatever that turns out to be.

    There is no way to know from here which window has focus, which is exactly
    why this is behind the switch and behind an approval every single time.
    """
    blocked = _require_control()
    if blocked:
        return {"success": False, "error": blocked}
    try:
        import pyautogui

        pyautogui.typewrite(text, interval=0.02)
    except Exception as exc:
        return {"success": False, "error": f"typing failed: {exc}"}
    return {"success": True, "detail": f"typed {len(text)} characters"}


def type_secret(name: str) -> Dict[str, Any]:
    """Type a vault secret at the focused window, never returning the value."""
    blocked = _require_control()
    if blocked:
        return {"success": False, "error": blocked}
    try:
        value = policy.reveal(name)
    except KeyError as exc:
        return {"success": False, "error": str(exc)}
    try:
        import pyautogui

        pyautogui.typewrite(value, interval=0.02)
    except Exception as exc:
        return {"success": False, "error": f"typing failed: {exc}"}
    return {"success": True, "detail": f"entered the stored secret '{name}'"}


def press_key(key: str) -> Dict[str, Any]:
    blocked = _require_control()
    if blocked:
        return {"success": False, "error": blocked}
    try:
        import pyautogui

        pyautogui.press(key)
    except Exception as exc:
        return {"success": False, "error": f"key press failed: {exc}"}
    return {"success": True, "detail": f"pressed {key}"}


def status() -> Dict[str, Any]:
    """What the Settings panel shows about desktop access."""
    control = control_available()
    return {
        "platform": system(),
        "open_roots": open_roots(),
        "app_allowlist": app_allowlist(),
        "control": control,
    }
