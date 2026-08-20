"""The optional parts of Carrot, installable without a terminal.

Half of what Carrot can do arrives as an optional Python package. Browser
control needs Playwright, animations need manim, reading the screen needs a
capture library, charts need matplotlib. Until now the app knew all of that
and said it in the only place it could: a hint on a card reading
`pip install carrot[browser]`, followed for Playwright by a second command,
`python -m playwright install chromium`, which is not a pip install at all and
which nothing but the docs would ever tell you about.

That is a fine instruction for somebody who has a terminal open and knows
which Python it should point at. For everybody else it is the end of the road,
and it is the wrong end, because the app is *already running in* the
interpreter that needs the package. It knows the answer to the only hard part
of the question.

So this module is a list of the optional pieces, what each one unlocks in
plain language, and how to put it there — and Settings draws it as rows with
a button. Three things it is careful about:

**The interpreter is the one that will do the importing.** `packages.py`
already solved this for the Code tab: `-m pip` against `sys.executable`, so
the package lands where the import will look for it rather than in whichever
Python happens to be first on PATH. That is the classic "but I did install
it" failure and it is invisible when it happens.

**Some things are not finished when pip exits.** Playwright installs a
library and then needs a browser downloaded, which is a second command and a
few hundred more megabytes. A component declares that step and Carrot runs it
too, because "installed" has to mean "works now" or the button is a lie told
politely.

**Nothing installs itself.** Every row is a button somebody presses. The
detection is honest about what is already there, and a component that cannot
work on this operating system says so instead of offering to try.
"""
from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from . import packages

# A post-install step gets longer than a pip install: Playwright's browser
# download is a few hundred megabytes and it is the second half of one button
# press, so timing out at pip's five minutes would leave the component
# half-installed and reporting failure.
POST_STEP_TIMEOUT = 900

# A check that starts a subprocess cannot run on every poll of a page that
# polls every second. Short enough that pressing Install and watching the row
# still feels live.
PROBE_TTL = 20
# A probe that hangs must not hang the status endpoint with it.
PROBE_TIMEOUT = 20

_probe_cache: Dict[str, Any] = {}


def _cached(key: str, build: Callable[[], bool]) -> bool:
    hit = _probe_cache.get(key)
    if hit and time.time() - hit[0] < PROBE_TTL:
        return hit[1]
    value = build()
    _probe_cache[key] = (time.time(), value)
    return value


def forget_probes():
    """Drop the cache. Called when an install finishes, so the row it changed
    does not keep reporting what was true twenty seconds ago."""
    _probe_cache.clear()


def _importable(module: str) -> Callable[[], bool]:
    def check() -> bool:
        try:
            importlib.import_module(module)
            return True
        except Exception:
            return False
    return check


def _off_the_event_loop(probe: Callable[[], bool]) -> bool:
    """Run a check in a thread that has no asyncio loop in it.

    `/api/components` is an `async def`, so everything it calls runs on the
    event loop thread — and Playwright's **sync** API refuses to start there:
    "It looks like you are using Playwright Sync API inside the asyncio loop."
    The refusal is an exception, the exception was caught, and the caught
    exception became `installed: False`.

    So the Add-ons page reported that browser control was not installed on a
    machine where the agent was, at that moment, driving a browser. It is the
    worst shape a status can have: confidently wrong about something the user
    can see working, on the one page whose entire job is to say what is here.

    A worker thread has no running loop, so the sync API starts normally.
    """
    result: List[bool] = []

    def run():
        try:
            result.append(bool(probe()))
        except Exception:
            result.append(False)

    thread = threading.Thread(target=run, name="component-check", daemon=True)
    thread.start()
    thread.join(PROBE_TIMEOUT)
    return bool(result and result[0])


def _probe_playwright() -> bool:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as play:
        return bool(play.chromium.executable_path)


def _playwright_ready() -> bool:
    """The library *and* a browser. Either alone is a component that fails at
    the moment it is used rather than at the moment it is installed.

    Cached, because answering it starts Playwright's driver process — and this
    is called for every component on every poll of the Add-ons page, which is
    once a second while that page is open.
    """
    try:
        importlib.import_module("playwright.sync_api")
    except Exception:
        return False
    return _cached("playwright", lambda: _off_the_event_loop(_probe_playwright))


# Windows ships OCR in the operating system and needs only the projection
# library; everywhere else it is Tesseract, which is a system package rather
# than a wheel and so cannot be a button here.
def _ocr_ready() -> bool:
    if sys.platform == "win32":
        return _importable("winsdk")()
    return bool(shutil.which("tesseract")) or _importable("pytesseract")()


COMPONENTS: List[Dict[str, Any]] = [
    {
        "id": "charts",
        "label": "Charts and plots",
        "unlocks": "Drawing graphs, plots and figures from your data.",
        "detail": ("Carrot writes a small script and runs it. Without this the "
                   "script fails on its first line with a missing-module error."),
        "pip": ["matplotlib"],
        "check": _importable("matplotlib"),
        "size_hint": "~50 MB",
    },
    {
        "id": "browser",
        "label": "Browsing the web for you",
        "unlocks": "Carrot Agent opening pages, clicking and filling forms.",
        "detail": ("Two parts: the Playwright library, and a copy of Chromium "
                   "for it to drive. Carrot installs both."),
        "pip": ["playwright"],
        # The step that was a second terminal command nobody was told about.
        # Built when the button is pressed, not when this module is imported.
        # `sys.executable` inside the frozen desktop build is carrot-backend
        # itself, and running that with `-m playwright` re-launches the app:
        # a second backend that tries to bind the port the first is already
        # holding, fails with "only one usage of each socket address", and
        # downloads no browser. `packages.python_executable` finds a real one.
        "post": {
            "argv": lambda: [packages.python_executable(), "-m", "playwright",
                             "install", "chromium"],
            "label": "Downloading Chromium",
        },
        "check": _playwright_ready,
        "size_hint": "~400 MB",
    },
    {
        "id": "vectors",
        "label": "Faster document search",
        "unlocks": "An index that stays quick past a hundred thousand notes.",
        "detail": ("Optional. Without it search falls back to scanning, which "
                   "is fine well past most people's libraries."),
        "pip": ["sqlite-vec"],
        "check": _importable("sqlite_vec"),
        "size_hint": "~2 MB",
    },
    {
        "id": "cloud",
        "label": "Sending hard questions to a frontier model",
        "unlocks": "The router escalating work your machine cannot do well.",
        "detail": "Needs an API key in Settings afterwards. Nothing leaves this machine without one.",
        "pip": ["anthropic"],
        "check": _importable("anthropic"),
        "size_hint": "~5 MB",
    },
    {
        "id": "speech",
        "label": "Reading answers aloud",
        "unlocks": "Spoken replies, generated on this machine.",
        "pip": ["kokoro-onnx", "sounddevice"],
        "check": _importable("kokoro_onnx"),
        "size_hint": "~120 MB",
    },
    {
        "id": "ambient",
        "label": "Reading what is on your screen",
        "unlocks": "Ambient Recall — remembering what you were looking at.",
        "detail": ("Off until you switch it on in the Ambient pack, even once "
                   "installed."),
        # `winsdk` is the Windows OCR projection; elsewhere the engine is
        # Tesseract, a system package, and the row says so rather than
        # offering a button that cannot work.
        "pip": (["mss", "pillow", "winsdk"] if sys.platform == "win32"
                else ["mss", "pillow", "pytesseract"]),
        "check": lambda: _importable("mss")() and _importable("PIL")() and _ocr_ready(),
        "size_hint": "~30 MB",
        "note": (None if sys.platform == "win32" else
                 "Also needs Tesseract, which is a system package — install it "
                 "with your package manager."),
    },
    {
        "id": "animation",
        "label": "Animated explanations",
        "unlocks": "The Animation pack rendering a proof as something that moves.",
        "detail": ("Large — manim brings a full rendering stack. `imageio-ffmpeg` "
                   "comes with it because manim encodes through ffmpeg and will "
                   "not find one on Windows otherwise."),
        "pip": ["manim", "imageio-ffmpeg"],
        "check": _importable("manim"),
        "size_hint": "~200 MB",
    },
    {
        "id": "desktop",
        "label": "Controlling the mouse and keyboard",
        "unlocks": "Carrot acting on applications that have no other way in.",
        "detail": ("Still off after installing: the policy kernel refuses it "
                   "until you switch it on, because it is the most powerful "
                   "thing Carrot can be given."),
        "pip": ["pyautogui"],
        "check": _importable("pyautogui"),
        "size_hint": "~10 MB",
    },
]

# What each install is doing right now, so a button can show progress and a
# reload can find the run still going. In memory rather than the database: an
# install does not outlive the process doing it, and a row saying "installing"
# left behind by a crash would be a button that never comes back.
_runs: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()


def _find(component_id: str) -> Optional[Dict[str, Any]]:
    return next((c for c in COMPONENTS if c["id"] == component_id), None)


def _installed(component: Dict[str, Any]) -> bool:
    try:
        return bool(component["check"]())
    except Exception:
        return False


def status() -> List[Dict[str, Any]]:
    """Every optional component, whether it is here, and what it is doing."""
    out = []
    for component in COMPONENTS:
        with _lock:
            run = dict(_runs.get(component["id"]) or {})
        installed = _installed(component)
        # A failure from an earlier attempt does not survive the thing working.
        #
        # `_runs` is memory that lives until the process restarts, so a row
        # that failed once kept its red line for the rest of the session —
        # under a component that had since been installed, by a retry, by a
        # restart, or by the user doing it in a terminal. The reader is told
        # something is broken while the badge beside it says Installed, and the
        # only honest reading of that pair is that the page does not know.
        if installed and run.get("state") == "failed":
            run = {}
        out.append({
            "id": component["id"],
            "label": component["label"],
            "unlocks": component["unlocks"],
            "detail": component.get("detail", ""),
            "note": component.get("note"),
            "size_hint": component.get("size_hint", ""),
            "packages": list(component["pip"]),
            "installed": installed,
            "state": run.get("state", "idle"),
            "message": run.get("message", ""),
            "error": run.get("error", ""),
        })
    return out


# The two halves of what a re-launched Carrot leaves in stderr. Both, because
# a genuine port clash in some other tool is not this, and mislabelling that
# would send the reader after a Python they do not need.
_RELAUNCH_SIGNS = ("only one usage of each socket address", "10048")


def _is_relaunch_failure(output: str) -> bool:
    lowered = (output or "").lower()
    return any(sign in lowered for sign in _RELAUNCH_SIGNS)


def _run_install(component: Dict[str, Any]):
    component_id = component["id"]

    def note(state: str, message: str, error: str = ""):
        with _lock:
            _runs[component_id] = {"state": state, "message": message, "error": error,
                                   "at": time.time()}

    for name in component["pip"]:
        note("installing", f"Installing {name}…")
        result = packages.install(name, "pip")
        if not result.get("ok"):
            # The tail, not the whole log. pip's failure output is mostly
            # resolver noise and the line that says what went wrong is at the
            # end — the same reasoning the animation pack uses for manim.
            tail = "\n".join((result.get("output") or "").strip().splitlines()[-8:])
            note("failed", f"Could not install {name}.", tail)
            return

    post = component.get("post")
    if post:
        note("installing", post["label"] + "…")
        argv = post["argv"]() if callable(post["argv"]) else post["argv"]
        # Said plainly rather than discovered as a failure to spawn. With no
        # interpreter anywhere, `python_executable` falls back to the bare name
        # "python3", which on Windows is nothing at all — and "could not start
        # downloading chromium" does not tell anybody what to go and fix.
        # `shutil.which` answers for an absolute path too.
        if not shutil.which(argv[0]):
            note("failed", f"{post['label']} needs Python, and there is no "
                           "Python on this computer for Carrot to use.")
            return
        try:
            done = subprocess.run(
                argv, capture_output=True, text=True,
                timeout=POST_STEP_TIMEOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except subprocess.TimeoutExpired:
            note("failed", f"{post['label']} took too long and was stopped.")
            return
        except OSError as exc:
            note("failed", f"Could not start {post['label'].lower()}.", str(exc))
            return
        if done.returncode != 0:
            tail = "\n".join(((done.stderr or done.stdout or "").strip()).splitlines()[-8:])
            # Carrot's own footprint, recognised. `-m playwright` run against
            # the frozen backend re-launches the app, which tries to bind the
            # port the first copy is holding and dies — and the output it
            # leaves behind is a socket error about an address, which explains
            # nothing to somebody who pressed a button labelled Install.
            if _is_relaunch_failure(tail):
                note("failed",
                     f"{post['label']} could not start — Carrot ran itself instead "
                     "of a Python.",
                     "The step needs a real Python interpreter and found only "
                     "Carrot's own executable, which re-launched the app and "
                     "failed on the port already in use. Installing Python from "
                     "python.org and pressing Install again is the fix.")
                return
            note("failed", f"{post['label']} did not finish.", tail)
            return

    # Checked rather than assumed. A pip that exits zero and an import that
    # still fails is the case worth catching here, because the alternative is
    # a green tick and a feature that breaks the first time it is used.
    #
    # The cache goes first: it is a few seconds old and was taken before any of
    # this ran, so trusting it here would report the state the component was in
    # before the install that just finished.
    forget_probes()
    if _installed(component):
        note("done", f"{component['label']} is ready.")
    else:
        note("failed",
             f"{component['label']} installed but is still not usable.",
             "Restarting Carrot sometimes finishes this — a package installed "
             "into a running interpreter is not always importable until then.")


def install(component_id: str) -> Dict[str, Any]:
    """Start an install in the background and return immediately.

    Not synchronous: a few hundred megabytes over a domestic connection is
    minutes, and a request that holds the connection open that long is one the
    browser or a proxy will give up on, leaving the install running and the UI
    convinced it failed.
    """
    component = _find(component_id)
    if not component:
        return {"ok": False, "error": f"There is no component called '{component_id}'."}
    with _lock:
        current = _runs.get(component_id) or {}
        if current.get("state") == "installing":
            return {"ok": True, "state": "installing", "message": current.get("message", "")}
        _runs[component_id] = {"state": "installing", "message": "Starting…",
                               "error": "", "at": time.time()}
    thread = threading.Thread(target=_run_install, args=(component,),
                              name=f"install-{component_id}", daemon=True)
    thread.start()
    return {"ok": True, "state": "installing", "message": "Starting…"}
