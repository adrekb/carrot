"""Reading the screen, when `ambient.py` says it may.

`ambient.py` is the decision and this is the act. Nothing here runs without
`should_capture()` returning true first, and that is enforced in one place —
`capture_once` — so there is no path to a frame that skipped the gate.

**The image is never written to disk.** A frame is grabbed into memory, OCR'd,
and dropped; what is kept is the text, the window title and the time. This is
the single biggest departure from how tools like screenpipe work, and it is
deliberate. A rolling video of someone's screen is a catastrophic thing to
leave on a laptop — it survives the encryption you did not turn on, it is
readable by anything that can read your home directory, and it is exactly what
somebody would take if they took anything. Text costs a few hundred bytes a
frame, answers "what was I reading about pensions last Tuesday" just as well,
and is worth far less to anyone who steals it.

The cost of that choice is real and worth stating: you cannot look at what the
screen showed, only at what it said. A chart with no axis labels is invisible
to this. That is the trade, and it is the right way round for a tool whose
whole claim is that your data does not leave the machine.

**Everything is probed, nothing is assumed.** There is no bundled OCR engine
and no bundled screen grabber. Each is looked for, the best available one is
used, and when none is present the feature says so with the command to fix it
rather than failing quietly every eight seconds forever.
"""

from __future__ import annotations

import concurrent.futures
import ctypes
import logging
import platform
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import ambient
from .database import get_db

LOG = logging.getLogger(__name__)

# Below this a "frame" is a blank screen, a loading spinner or a desktop. Not
# worth a row, and worth even less in a search index.
MIN_TEXT_CHARS = 40

# Two consecutive frames of the same document differ by a scroll line or a
# blinking cursor. Storing both gives you four hundred near-identical rows an
# hour and a recall that returns the same moment nine times.
DEDUPE_SIMILARITY = 0.90

# The longest text kept from one frame. A full-screen terminal of logs can run
# to tens of thousands of characters and none of it is what anyone will search
# for.
MAX_TEXT_CHARS = 8000


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ===== What is on screen right now =====
#
# The privacy gate needs the app and the window title, and it needs them
# *before* anything is captured — that ordering is the whole point of the
# module. So this runs first and cheaply, and a probe that fails returns an
# empty context rather than raising, which the gate then treats as unknown
# rather than as permission.

def focused_window() -> Dict[str, Any]:
    """The foreground window's app and title, as best the platform will say."""
    system = platform.system()
    try:
        if system == "Windows":
            return _focused_windows()
        if system == "Darwin":
            return _focused_macos()
        return _focused_linux()
    except Exception:
        LOG.debug("could not read the foreground window", exc_info=True)
        return {}


def _focused_windows() -> Dict[str, Any]:
    """Win32 through ctypes — no dependency, and it is already installed."""
    user32 = ctypes.windll.user32
    handle = user32.GetForegroundWindow()
    if not handle:
        return {}
    length = user32.GetWindowTextLengthW(handle)
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(handle, buffer, length + 1)
    title = buffer.value or ""

    app = ""
    try:
        import psutil

        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(handle, ctypes.byref(pid))
        if pid.value:
            app = psutil.Process(pid.value).name()
    except Exception:
        pass
    return {"app": app, "title": title, "secure_input": _secure_input_windows()}


def _secure_input_windows() -> bool:
    """Is a password field focused?

    Windows does not expose this as directly as macOS. What it will say is
    whether the focused control has the ES_PASSWORD style, which covers native
    fields and misses browser ones — so this is a signal that helps when it
    fires and is never treated as an all-clear when it does not. The title
    checks in `ambient.py` are what cover the rest.
    """
    try:
        user32 = ctypes.windll.user32
        info = ctypes.create_string_buffer(48)
        ctypes.memset(info, 0, 48)
        ctypes.cast(info, ctypes.POINTER(ctypes.c_uint))[0] = 48
        if not user32.GetGUIThreadInfo(0, info):
            return False
        focus_handle = ctypes.cast(info, ctypes.POINTER(ctypes.c_void_p))[4]
        if not focus_handle:
            return False
        # EM_GETPASSWORDCHAR = 0x00D2. A non-zero answer means the control is
        # masking what is typed into it.
        return bool(user32.SendMessageW(focus_handle, 0x00D2, 0, 0))
    except Exception:
        return False


def _focused_macos() -> Dict[str, Any]:
    script = (
        'tell application "System Events" to tell (first process whose frontmost is true) '
        'to return name & "\\n" & (name of front window)'
    )
    try:
        out = subprocess.check_output(["osascript", "-e", script],
                                      stderr=subprocess.DEVNULL, timeout=2)
        lines = out.decode("utf-8", "replace").strip().split("\n")
    except Exception:
        return {}
    return {"app": lines[0] if lines else "",
            "title": lines[1] if len(lines) > 1 else "",
            "secure_input": _secure_input_macos()}


def _secure_input_macos() -> bool:
    """macOS says this outright, and it is the strongest signal available.

    `IsSecureEventInputEnabled` is set whenever anything on the system has
    taken secure input — a password field, a terminal reading a passphrase,
    a password manager. It is exactly the question being asked.
    """
    try:
        from ctypes import cdll, util

        carbon = cdll.LoadLibrary(util.find_library("Carbon"))
        return bool(carbon.IsSecureEventInputEnabled())
    except Exception:
        return False


def _focused_linux() -> Dict[str, Any]:
    for command in (["xdotool", "getactivewindow", "getwindowname"],
                    ["xprop", "-root", "_NET_ACTIVE_WINDOW"]):
        try:
            out = subprocess.check_output(command, stderr=subprocess.DEVNULL, timeout=2)
            title = out.decode("utf-8", "replace").strip()
            if title and command[0] == "xdotool":
                return {"app": "", "title": title}
        except Exception:
            continue
    return {}


# A URL in a browser title bar, which is how the url exclusion gets something
# to match on without reading the browser's own state.
_URL_IN_TITLE = re.compile(r"\b((?:https?://)?(?:[\w-]+\.)+[a-z]{2,}(?:/\S*)?)", re.I)


def window_context() -> Dict[str, Any]:
    """Everything the privacy gate wants, in one call."""
    context = focused_window()
    title = str(context.get("title") or "")
    found = _URL_IN_TITLE.search(title)
    if found:
        context["url"] = found.group(1)
    return context


# ===== Grabbing the screen =====
#
# Probed rather than depended on. Ordered by how much each costs: mss takes a
# frame without touching the clipboard or the compositor and is the fastest of
# the three; Pillow is usually already present for something else; pyautogui is
# last because it drags in a mouse-control stack this feature does not want.

GRABBERS = (
    ("mss", "mss", "pip install mss"),
    ("pillow", "PIL", "pip install pillow"),
    ("pyautogui", "pyautogui", "pip install pyautogui"),
)


def available_grabber() -> Optional[str]:
    for name, module, _ in GRABBERS:
        try:
            __import__(module)
            return name
        except ImportError:
            continue
    return None


def grab_screen():
    """One frame as a PIL image, or None. Never written to disk."""
    grabber = available_grabber()
    if grabber == "mss":
        import mss
        import mss.tools
        from PIL import Image

        with mss.mss() as sct:
            shot = sct.grab(sct.monitors[0])
            return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    if grabber == "pillow":
        from PIL import ImageGrab

        return ImageGrab.grab()
    if grabber == "pyautogui":
        import pyautogui

        return pyautogui.screenshot()
    return None


# ===== Reading it =====
#
# Windows first, and not out of platform favouritism: `Windows.Media.Ocr` is
# already in the OS, needs no model download, runs on the hardware OCR block
# rather than the GPU, and therefore does not compete with Ollama for VRAM —
# which is precisely what `ambient.py`'s resource gate spends its time
# protecting. A VLM-based OCR would be more accurate on dense tables and would
# take the memory the thing you are actually waiting on needs.

OCR_ENGINES = (
    ("windows", "Windows.Media.Ocr — built into Windows, no download",
     "already present on Windows 10 and 11; pip install winsdk"),
    ("tesseract", "Tesseract — cross-platform",
     "install Tesseract, then pip install pytesseract"),
)


def available_ocr() -> Optional[str]:
    if platform.system() == "Windows":
        try:
            import winsdk.windows.media.ocr  # noqa: F401
            return "windows"
        except ImportError:
            pass
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        return "tesseract"
    except Exception:
        return None


def ocr_image(image) -> Tuple[str, str]:
    """Text from a frame, and which engine read it. ``("", "")`` if none can."""
    engine = available_ocr()
    if engine == "windows":
        try:
            return _ocr_windows(image), "windows"
        except Exception:
            LOG.debug("windows OCR failed, falling back", exc_info=True)
    try:
        import pytesseract

        return pytesseract.image_to_string(image), "tesseract"
    except Exception:
        LOG.debug("tesseract OCR failed", exc_info=True)
    return "", ""


def _ocr_windows(image) -> str:
    """Windows.Media.Ocr through winsdk.

    The API is async and bitmap-oriented, so the frame has to become a
    SoftwareBitmap first. Done here rather than in a helper because the
    conversion is the only genuinely fiddly part and it belongs next to the
    call it serves.
    """
    import asyncio

    from winsdk.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winsdk.windows.media.ocr import OcrEngine
    from winsdk.windows.security.cryptography import CryptographicBuffer

    rgba = image.convert("RGBA")
    buffer = CryptographicBuffer.create_from_byte_array(rgba.tobytes())
    bitmap = SoftwareBitmap.create_copy_from_buffer(
        buffer, BitmapPixelFormat.RGBA8, rgba.width, rgba.height)

    engine = OcrEngine.try_create_from_user_profile_languages()
    if engine is None:
        raise RuntimeError("no OCR language pack is installed")

    async def run():
        result = await engine.recognize_async(bitmap)
        return result.text or ""

    # `asyncio.run` refuses to start a loop inside a thread that already has
    # one, and raises rather than returning nothing — which the caller catches
    # and treats as "this engine did not work", falling through to Tesseract
    # and, on a machine without it, to no text at all.
    #
    # The background worker never hit this because it runs in its own thread.
    # Anything reached from a request handler does: `Capture now` was a button
    # that grabbed the screen, OCR'd nothing, and reported "nothing readable on
    # screen" on a screen that was full of words.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(run())

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(run())).result()


def capabilities() -> Dict[str, Any]:
    """What this machine can actually do, and what to install if it cannot.

    The same shape the extension packs use, for the same reason: a feature
    that fails every eight seconds in a log nobody reads is worse than one
    that says up front what is missing.
    """
    grabber = available_grabber()
    engine = available_ocr()
    return {
        "grabber": grabber,
        "ocr": engine,
        "ready": bool(grabber and engine),
        "missing": [
            *([] if grabber else [{
                "what": "a way to take a screenshot",
                "fix": "pip install mss",
            }]),
            *([] if engine else [{
                "what": "an OCR engine",
                "fix": ("pip install winsdk" if platform.system() == "Windows"
                        else "install Tesseract, then pip install pytesseract"),
            }]),
        ],
    }


# ===== Keeping it =====

def _similar(a: str, b: str) -> float:
    """Cheap similarity on word sets.

    A real edit distance over eight thousand characters, every eight seconds,
    would cost more than the OCR did. Word overlap is enough to answer the
    only question being asked: is this the same screen as a moment ago.
    """
    if not a or not b:
        return 0.0
    first, second = set(a.split()), set(b.split())
    if not first or not second:
        return 0.0
    return len(first & second) / max(len(first), len(second))


def last_frame() -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM ambient_frames ORDER BY captured_at DESC LIMIT 1").fetchone()
    conn.close()
    return dict(row) if row else None


def store_frame(text: str, context: Dict[str, Any], engine: str) -> Optional[str]:
    """Write one frame's text, unless it repeats the frame before it."""
    text = (text or "").strip()[:MAX_TEXT_CHARS]
    if len(text) < MIN_TEXT_CHARS:
        return None

    previous = last_frame()
    if previous and _similar(text, previous.get("text", "")) >= DEDUPE_SIMILARITY:
        # Same screen, still there. The row that exists already covers this
        # moment; extending its end time is more useful than a second copy,
        # because "how long was I on this" becomes answerable.
        conn = get_db()
        conn.execute("UPDATE ambient_frames SET ended_at = ?, seen = seen + 1 WHERE id = ?",
                     (_now(), previous["id"]))
        conn.commit()
        conn.close()
        return None

    frame_id = uuid.uuid4().hex[:12]
    now = _now()
    conn = get_db()
    conn.execute(
        """INSERT INTO ambient_frames
           (id, captured_at, ended_at, app, title, url, text, engine, seen, workspace_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?)""",
        (frame_id, now, now, str(context.get("app") or "")[:120],
         str(context.get("title") or "")[:300], str(context.get("url") or "")[:300],
         text, engine, _active_workspace()),
    )
    conn.commit()
    conn.close()

    # Embedded on the same background worker every other store uses, so recall
    # is semantic rather than only lexical and nothing blocks the capture loop
    # waiting for an embedding.
    try:
        from . import vectors

        vectors.enqueue("ambient", frame_id, text[:2000])
    except Exception:
        LOG.debug("could not queue an ambient embedding", exc_info=True)
    return frame_id


def _active_workspace() -> str:
    try:
        from . import workspaces

        return workspaces.active_id() or ""
    except Exception:
        return ""


# ===== One capture =====

def capture_once(force: bool = False) -> Dict[str, Any]:
    """Probe, ask the gate, and capture only if it says yes.

    The single path to a frame. `force` skips the *cadence* checks — the idle
    timer, the interval, the schedule — for a capture the user asked for by
    pressing something. It does not skip privacy or resources, and cannot: a
    button that captures a password field on request is the same bug as one
    that does it automatically.
    """
    context = window_context()
    context.update(ambient.probe_resources())

    privacy = ambient.check_privacy(context)
    if not privacy.allowed:
        return {"captured": False, **privacy.as_dict()}

    resources = ambient.check_resources(context)
    if not resources.allowed:
        return {"captured": False, **resources.as_dict()}

    if not force:
        decision = ambient.should_capture(context)
        if not decision.allowed:
            return {"captured": False, **decision.as_dict()}

    ready = capabilities()
    if not ready["ready"]:
        missing = ", ".join(m["what"] for m in ready["missing"])
        return {"captured": False, "allowed": False,
                "reason": f"this machine is missing {missing}",
                "rule": "not_installed", "retry_after": 300.0,
                "missing": ready["missing"]}

    image = grab_screen()
    if image is None:
        return {"captured": False, "allowed": False,
                "reason": "could not take a screenshot", "rule": "grab_failed",
                "retry_after": 60.0}

    text, engine = ocr_image(image)
    # Dropped here, explicitly, and before anything else can touch it. The
    # image exists for the duration of the OCR call and no longer.
    del image

    if not text.strip():
        return {"captured": False, "allowed": True,
                "reason": "nothing readable on screen", "rule": "no_text",
                "retry_after": 0.0}

    frame_id = store_frame(text, context, engine)
    return {
        "captured": bool(frame_id),
        "allowed": True,
        "frame_id": frame_id,
        "reason": "stored" if frame_id else "the screen has not changed",
        "rule": ALLOWED_RULE if frame_id else "unchanged",
        "chars": len(text),
        "app": context.get("app", ""),
        "title": context.get("title", ""),
    }


ALLOWED_RULE = "captured"


# ===== The loop =====

class AmbientWorker:
    """The background capture loop.

    Paced by `ambient.next_interval`, which already knows to slow down on
    battery. A worker that ignored that and slept a constant eight seconds
    would undo the whole resource gate.
    """

    def __init__(self):
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.last: Dict[str, Any] = {}
        self.started_at: float = 0.0
        self.captures = 0
        self.skips = 0

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> bool:
        if self.running:
            return False
        self._stop.clear()
        self.started_at = time.time()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="carrot-ambient")
        self._thread.start()
        return True

    def stop(self) -> bool:
        was = self.running
        self._stop.set()
        return was

    def _run(self):
        while not self._stop.is_set():
            try:
                result = capture_once()
                self.last = {**result, "at": time.time()}
                if result.get("captured"):
                    self.captures += 1
                else:
                    self.skips += 1
                # A refusal carries its own retry interval — a password field
                # is worth re-checking in two seconds, a low battery in five
                # minutes. Honouring it is what stops the loop spinning
                # against a condition that will not change.
                wait = float(result.get("retry_after") or 0) or \
                    ambient.next_interval(ambient.probe_resources())
                # Retention, applied by the thing that creates the rows. A
                # limit enforced only by a button nobody presses is not a
                # limit, and the cheapest moment to drop old frames is right
                # after adding one, while the loop is already awake.
                if result.get("captured"):
                    self._maybe_prune()
            except Exception as exc:
                LOG.exception("ambient capture failed")
                self.last = {"captured": False, "reason": str(exc),
                             "rule": "error", "at": time.time()}
                wait = 60.0
            self._stop.wait(max(1.0, float(wait)))

    # Every two hundred captures rather than every one: the delete is a table
    # scan and the answer changes by at most one frame between runs.
    _PRUNE_EVERY = 200

    def _maybe_prune(self):
        """Drop anything past the retention limit, occasionally."""
        if self.captures % self._PRUNE_EVERY:
            return
        try:
            from . import extensions

            days = int(extensions.pack_setting("ambient", "retention_days", 30) or 30)
        except Exception:
            days = 30
        try:
            dropped = prune(days)
            if dropped:
                LOG.info("ambient retention dropped %d frame(s) older than %d days",
                         dropped, days)
        except Exception:
            LOG.debug("ambient prune failed", exc_info=True)

    def status(self) -> Dict[str, Any]:
        return {
            "running": self.running,
            "started_at": self.started_at,
            "captures": self.captures,
            "skips": self.skips,
            "last": self.last,
        }


worker = AmbientWorker()


# ===== Recall =====
#
# The point of all of it. Hybrid, like every other search here: FTS5 finds the
# rows containing the words, embeddings reorder them by what they meant. Same
# shape as `search.py` and `indexer.py` so a result from the screen index sits
# alongside one from a document without needing its own vocabulary.


def _fts_query(text: str) -> str:
    """User text as an FTS5 MATCH expression.

    Quoted term by term. FTS5 treats bare punctuation as syntax, so a search
    for "cost: $40" is a syntax error rather than a search, and the user is
    told their query is malformed for typing a colon.
    """
    terms = re.findall(r"[\w][\w'-]*", text or "")
    return " OR ".join(f'"{t}"' for t in terms if len(t) > 1)


def recall(query: str, limit: int = 20, since: str = "", app: str = "",
           workspace_id: str = "") -> List[Dict[str, Any]]:
    """What was on screen, matching this."""
    match = _fts_query(query)
    if not match:
        return []

    sql = [
        "SELECT f.* FROM ambient_fts x JOIN ambient_frames f ON f.id = x.frame_id",
        "WHERE ambient_fts MATCH ?",
    ]
    params: List[Any] = [match]
    if since:
        sql.append("AND f.captured_at >= ?")
        params.append(since)
    if app:
        sql.append("AND lower(f.app) LIKE ?")
        params.append(f"%{app.lower()}%")
    if workspace_id:
        sql.append("AND f.workspace_id = ?")
        params.append(workspace_id)
    # Over-fetch so the embedding pass has something to reorder. Ranking by
    # FTS alone puts a page that says the word nine times above the page that
    # was about it.
    sql.append("ORDER BY rank LIMIT ?")
    params.append(max(limit * 4, 40))

    conn = get_db()
    try:
        rows = [dict(r) for r in conn.execute(" ".join(sql), params).fetchall()]
    except Exception:
        LOG.debug("ambient FTS query failed", exc_info=True)
        rows = []
    finally:
        conn.close()
    if not rows:
        return []

    try:
        from . import vectors

        # Checked before embedding, not after. `vectors.embed` is a network
        # call to Ollama that returns None when it cannot connect — but it
        # takes the socket timeout to find out, and a search that stalls four
        # seconds to discover there was nothing to rerank against is a search
        # that feels broken. A fresh install and an install with Ollama down
        # look the same from here, and the answer is the same in both: the FTS
        # ordering is what there is.
        embedding = vectors.embed(query) if vectors.count("ambient") else None
        if embedding:
            scored = dict(vectors.search("ambient", embedding,
                                         limit=len(rows),
                                         candidates=[r["id"] for r in rows]))
            rows.sort(key=lambda r: scored.get(r["id"], 0.0), reverse=True)
    except Exception:
        LOG.debug("ambient rerank unavailable", exc_info=True)

    for row in rows:
        row["snippet"] = _snippet(row["text"], query)
    return rows[:limit]


_SNIPPET_CHARS = 240


def _snippet(text: str, query: str) -> str:
    """The part of the frame the query actually matched.

    A frame is up to eight thousand characters of whatever was on screen. The
    first 240 of them are usually a menu bar.
    """
    body = " ".join((text or "").split())
    terms = [t.lower() for t in re.findall(r"[\w][\w'-]*", query or "") if len(t) > 2]
    lowered = body.lower()
    for term in terms:
        at = lowered.find(term)
        if at >= 0:
            start = max(0, at - _SNIPPET_CHARS // 3)
            return ("…" if start else "") + body[start:start + _SNIPPET_CHARS] + "…"
    return body[:_SNIPPET_CHARS] + ("…" if len(body) > _SNIPPET_CHARS else "")


# ===== What the assistant is allowed to see =====
#
# The store already answers "what was on screen". Nothing could ask it: recall()
# was reachable from the Ambient UI and from nowhere else, so a question typed
# into chat — "that paper I was reading yesterday" — had no path to the frames
# that hold the answer.
#
# What follows is that path. It is deliberately thin: the retrieval, the
# ranking and the snippets are recall()'s, and this adds only the three things
# an assistant needs that a UI does not — permission, episodes instead of
# frames, and an honest answer when there is nothing to search.


def agent_may_search() -> bool:
    """Both switches, because they mean different things.

    Recording the screen and letting the assistant read the recording are two
    permissions. Someone who wants a searchable history but does not want it in
    conversation is asking for something coherent, and one flag cannot say it.
    """
    from . import ambient

    rules = ambient.policy()
    return bool(rules.get("enabled")) and bool(rules.get("agent_aware"))


def _as_episodes(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Consecutive frames of the same thing, as one result.

    store_frame() already merges a screen that has not changed, but a paper
    read over twenty minutes is dozens of rows: you scroll, the text changes,
    the dedupe correctly says it is a new frame. To a search those are one
    answer — the same document, at a range of times — and returning them
    separately would spend the whole reply on one PDF and crowd out the rest.
    """
    episodes: Dict[tuple, Dict[str, Any]] = {}
    for row in rows:
        key = (row.get("app") or "", row.get("title") or "", row.get("url") or "")
        found = episodes.get(key)
        if not found:
            episodes[key] = {
                "app": row.get("app") or "",
                "title": row.get("title") or "",
                "url": row.get("url") or "",
                "started_at": row.get("captured_at") or "",
                "ended_at": row.get("ended_at") or row.get("captured_at") or "",
                "frames": 1,
                # The best-ranked frame's snippet, because rows arrive ranked
                # and the first one to land here is the closest match.
                "snippet": row.get("snippet") or "",
                "frame_id": row.get("id") or "",
            }
            continue
        found["frames"] += 1
        found["started_at"] = min(found["started_at"], row.get("captured_at") or "")
        found["ended_at"] = max(found["ended_at"],
                                row.get("ended_at") or row.get("captured_at") or "")
    return list(episodes.values())


def search_for_agent(query: str, limit: int = 6, since: str = "",
                     app: str = "") -> Dict[str, Any]:
    """Episodes matching a question, with why they matched.

    Returns a state as well as results, because "I searched and found nothing"
    and "I cannot see your screen" are different answers and only one of them
    should send the user to Settings.
    """
    if not agent_may_search():
        from . import ambient

        return {"state": "off" if not ambient.policy().get("enabled") else "not_allowed",
                "episodes": []}
    # Over-fetch: several frames collapse into one episode, so asking for six
    # frames would routinely yield two answers.
    rows = recall(query, limit=max(limit * 6, 40), since=since, app=app)
    if not rows:
        return {"state": "empty", "episodes": []}
    return {"state": "ok", "episodes": _as_episodes(rows)[:limit]}


def agent_roster_line() -> str:
    """One line, every turn, saying whether this can be searched at all.

    Not a tool the model has to remember to call. A small model asked "am I
    free Friday" does not reliably think to check whether it has a calendar,
    and the same model asked "what was I reading" will invent a paper rather
    than notice it has no screen history. Silence is not a fact; the cost of
    saying so is one line.
    """
    from . import ambient

    rules = ambient.policy()
    if not rules.get("enabled"):
        return ("Screen history: not recording (Settings → Ambient). You cannot see "
                "what was on their screen; say so rather than guessing.")
    if not rules.get("agent_aware"):
        return ("Screen history: recorded, but not shared with you (Settings → Ambient, "
                "\"Let the assistant search my screen history\"). Say so rather than guessing.")
    return ("Screen history: available — use search_screen to find what was on "
            "their screen. An empty result means nothing matched, not that it is off.")


def timeline(limit: int = 100, since: str = "", app: str = "") -> List[Dict[str, Any]]:
    """Recent frames, newest first — what the last hour looked like."""
    sql = ["SELECT id, captured_at, ended_at, app, title, url, seen, engine,",
           "substr(text, 1, 400) AS preview FROM ambient_frames WHERE 1=1"]
    params: List[Any] = []
    if since:
        sql.append("AND captured_at >= ?")
        params.append(since)
    if app:
        sql.append("AND lower(app) LIKE ?")
        params.append(f"%{app.lower()}%")
    sql.append("ORDER BY captured_at DESC LIMIT ?")
    params.append(max(1, min(int(limit), 500)))
    conn = get_db()
    rows = [dict(r) for r in conn.execute(" ".join(sql), params).fetchall()]
    conn.close()
    return rows


def get_frame(frame_id: str) -> Optional[Dict[str, Any]]:
    conn = get_db()
    row = conn.execute("SELECT * FROM ambient_frames WHERE id = ?", (frame_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def stats() -> Dict[str, Any]:
    conn = get_db()
    row = conn.execute(
        """SELECT COUNT(*) AS frames, MIN(captured_at) AS oldest,
                  MAX(captured_at) AS newest, SUM(LENGTH(text)) AS chars
             FROM ambient_frames""").fetchone()
    apps = conn.execute(
        """SELECT app, COUNT(*) AS n FROM ambient_frames
            WHERE app != '' GROUP BY app ORDER BY n DESC LIMIT 8""").fetchall()
    conn.close()
    return {
        "frames": row["frames"] or 0,
        "oldest": row["oldest"] or "",
        "newest": row["newest"] or "",
        # Shown in the UI because "how much of my disk is this" is the first
        # question anyone sensible asks of a feature that watches the screen,
        # and text is small enough that the honest answer is reassuring.
        "kilobytes": round((row["chars"] or 0) / 1024, 1),
        "apps": [dict(a) for a in apps],
    }


# ===== Forgetting =====
#
# As prominent as capture, and deliberately so. Anything that records has to
# make deletion at least as easy, or the record is not something the user
# controls — it is something that happens to them.
#
# Which has to include the embedding. Every stored frame is queued for one, and
# deleting the row left it behind: a vector of what was on screen, in a table
# the user was just told they had emptied. It is not recoverable as text, but
# "not readable" is not "deleted", and a forget button that half-forgets is
# worse than none — it is the one people believe.


def _forget_vectors(frame_ids: List[str]) -> None:
    """Contained: a vector store that is unavailable must not stop a deletion.

    If this fails the row is still gone, which is the part the user can see and
    the part that matters most. Logged rather than raised so the failure is
    findable instead of silent.
    """
    if not frame_ids:
        return
    try:
        from . import vectors

        for frame_id in frame_ids:
            vectors.delete("ambient", frame_id)
    except Exception:
        LOG.warning("could not delete ambient embeddings for %d frame(s)",
                    len(frame_ids), exc_info=True)


def _frame_ids(where: str = "", params: Optional[List[Any]] = None) -> List[str]:
    conn = get_db()
    try:
        sql = "SELECT id FROM ambient_frames" + (f" WHERE {where}" if where else "")
        return [r["id"] for r in conn.execute(sql, params or []).fetchall()]
    finally:
        conn.close()


def forget(frame_id: str) -> bool:
    conn = get_db()
    cursor = conn.execute("DELETE FROM ambient_frames WHERE id = ?", (frame_id,))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    if deleted:
        _forget_vectors([frame_id])
    return deleted


def forget_range(since: str = "", until: str = "", app: str = "") -> int:
    """Delete a span, an app, or both. Refuses to delete everything by accident."""
    if not (since or until or app):
        raise ValueError("say what to forget — a time range, an app, or both")
    # Built once and used for both statements, so the rows whose embeddings are
    # dropped are exactly the rows that were deleted.
    where = ["1=1"]
    params: List[Any] = []
    if since:
        where.append("AND captured_at >= ?")
        params.append(since)
    if until:
        where.append("AND captured_at <= ?")
        params.append(until)
    if app:
        where.append("AND lower(app) LIKE ?")
        params.append(f"%{app.lower()}%")
    clause = " ".join(where)

    # Read first: after the DELETE there is nothing left to ask which
    # embeddings belonged to it.
    doomed = _frame_ids(clause, params)
    conn = get_db()
    cursor = conn.execute(f"DELETE FROM ambient_frames WHERE {clause}", params)
    conn.commit()
    count = cursor.rowcount
    conn.close()
    _forget_vectors(doomed)
    return count


def forget_all() -> int:
    conn = get_db()
    cursor = conn.execute("DELETE FROM ambient_frames")
    conn.commit()
    count = cursor.rowcount
    conn.close()
    # The whole namespace, rather than id by id: this is the button that says
    # everything, and it should not leave a residue proportional to how many
    # embeddings happened to be queued when it was pressed.
    try:
        from . import vectors

        vectors.delete_namespace("ambient")
    except Exception:
        LOG.warning("could not clear ambient embeddings", exc_info=True)
    return count


def prune(days: int) -> int:
    """Drop anything older than `days`. The retention limit, applied."""
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))).isoformat()
    doomed = _frame_ids("captured_at < ?", [cutoff])
    conn = get_db()
    cursor = conn.execute("DELETE FROM ambient_frames WHERE captured_at < ?", (cutoff,))
    conn.commit()
    count = cursor.rowcount
    conn.close()
    _forget_vectors(doomed)
    return count
