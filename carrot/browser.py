"""The browser Carrot drives, seen as a list of elements rather than pixels.

Most computer-use agents look at a screenshot and emit coordinates. That works,
and it fails in exactly the ways you would not want a form-filler to fail: a
click lands two pixels off and hits "Delete" instead of "Save", and nothing in
the transcript tells you it happened. This driver never emits a coordinate.

Instead every observation is a **numbered map of the interactive elements on
the page** — built from the DOM and the accessibility attributes, filtered to
what is actually visible — and every action names one of those numbers:

    12  button   "Submit assignment"
    13  input    "Comments"            value: ""
    14  input    "Password"            value: «hidden»

``click(13)`` either hits the element labelled 13 or fails loudly. That makes
three things possible that a coordinate agent cannot offer:

* **The approval prompt can tell the truth.** "Click *Submit assignment*" is
  derived from the element, so what the user approves is what gets clicked.
* **Risk can be judged before the click.** The policy kernel sees the button's
  label, which is how a checkout button gets caught while an ordinary one
  does not.
* **Credentials never pass through the model.** Password fields are marked, and
  their values are replaced with a placeholder in every observation. To fill
  one the agent asks for ``type_secret(ref, "canvas")``; the value is read from
  the vault inside this module and typed straight into the page.

Playwright is optional. Without it the rest of Carrot is unaffected and the
agent reports what to install rather than failing mid-task.
"""

from __future__ import annotations

import os
import queue
import threading
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from . import policy
from .config import get_config

SCREENSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "screenshots")
DEFAULT_TIMEOUT_MS = 20000
NAV_TIMEOUT_MS = 30000
MAX_ELEMENTS = 150
MAX_TEXT_CHARS = 6000

INSTALL_HINT = (
    "Browser control needs Playwright. Install it with:\n"
    "    pip install playwright\n"
    "    python -m playwright install chromium"
)

# The element sweep. Kept in one place because the agent's entire view of a
# page comes from it — if something is not in this list, the agent cannot touch
# it, which is the intended failure mode.
_SNAPSHOT_JS = r"""
() => {
  const SELECTOR = [
    'a[href]', 'button', 'input', 'select', 'textarea', 'summary',
    '[role=button]', '[role=link]', '[role=checkbox]', '[role=radio]',
    '[role=tab]', '[role=menuitem]', '[role=switch]', '[role=combobox]',
    '[role=searchbox]', '[role=textbox]',
    '[contenteditable=""]', '[contenteditable=true]', '[onclick]'
  ].join(',');

  const SENSITIVE = /pass(word|wd|phrase)|\bpin\b|\bcvv\b|\bcvc\b|security\s*code|card\s*number|\bssn\b|routing\s*number|account\s*number|\botp\b|one[-\s]?time|\b2fa\b|verification\s*code|api[-_\s]?key|secret|token/i;

  document.querySelectorAll('[data-carrot-ref]').forEach(el => el.removeAttribute('data-carrot-ref'));

  const label = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria;
    const labelledBy = el.getAttribute('aria-labelledby');
    if (labelledBy) {
      const target = document.getElementById(labelledBy);
      if (target && target.innerText) return target.innerText;
    }
    if (el.id) {
      const tag = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (tag && tag.innerText) return tag.innerText;
    }
    const wrapper = el.closest('label');
    if (wrapper && wrapper.innerText) return wrapper.innerText;
    return el.getAttribute('placeholder') || el.getAttribute('title')
        || (el.innerText || '').trim() || el.getAttribute('name')
        || el.getAttribute('alt') || el.value || '';
  };

  const out = [];
  let ref = 0;
  for (const el of document.querySelectorAll(SELECTOR)) {
    if (out.length >= %(max_elements)d) break;
    const rect = el.getBoundingClientRect();
    if (rect.width < 2 || rect.height < 2) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    if (parseFloat(style.opacity || '1') < 0.05) continue;

    ref += 1;
    el.setAttribute('data-carrot-ref', String(ref));

    const type = (el.getAttribute('type') || '').toLowerCase();
    const name = String(label(el)).replace(/\s+/g, ' ').trim().slice(0, 140);
    const secret = type === 'password' || SENSITIVE.test(name)
                || SENSITIVE.test(el.getAttribute('name') || '')
                || SENSITIVE.test(el.getAttribute('autocomplete') || '');

    let value = '';
    if ('value' in el && typeof el.value === 'string') {
      value = secret ? '«secret»' : el.value.slice(0, 100);
    }

    out.push({
      ref: ref,
      tag: el.tagName.toLowerCase(),
      type: type,
      role: el.getAttribute('role') || '',
      name: name,
      value: value,
      secret: secret,
      checked: el.checked === true,
      disabled: el.disabled === true || el.getAttribute('aria-disabled') === 'true',
      in_viewport: rect.top < window.innerHeight && rect.bottom > 0
    });
  }

  return {
    url: location.href,
    title: document.title,
    elements: out,
    scroll: { y: Math.round(window.scrollY), height: Math.round(document.body.scrollHeight) }
  };
}
""" % {"max_elements": MAX_ELEMENTS}

_TEXT_JS = r"""
() => {
  const clone = document.body.cloneNode(true);
  clone.querySelectorAll('script, style, noscript, svg').forEach(el => el.remove());
  clone.querySelectorAll('[style*="display:none"], [style*="display: none"], [hidden]')
       .forEach(el => el.remove());
  return (clone.innerText || '').replace(/\n{3,}/g, '\n\n').trim();
}
"""


def is_available() -> Dict[str, Any]:
    """Whether browser control can run here, and what is missing if not."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return {"available": False, "reason": "playwright is not installed", "hint": INSTALL_HINT}
    return {"available": True, "reason": "", "hint": ""}


class BrowserError(RuntimeError):
    pass


class BrowserSession:
    """A Chromium window owned by one dedicated thread.

    Playwright's sync API must be created and used on a single thread, and the
    agent loop already runs on worker threads that come and go. So the session
    owns a thread of its own and every operation is a closure posted to it —
    which also serialises actions, meaning two agent steps can never interleave
    halfway through a click.
    """

    def __init__(self, headless: Optional[bool] = None):
        settings = get_config()
        self.headless = settings.get("agent_browser_headless", False) if headless is None else headless
        self._commands: "queue.Queue" = queue.Queue()
        self._ready = threading.Event()
        self._error: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._closed = False
        self.last_snapshot: Dict[str, Any] = {}

    # --- lifecycle ---

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True, name="carrot-browser")
        self._thread.start()
        self._ready.wait(90)
        if self._error:
            raise BrowserError(self._error)

    def _loop(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self._error = INSTALL_HINT
            self._ready.set()
            return

        playwright = None
        browser = None
        try:
            playwright = sync_playwright().start()
            launch: Dict[str, Any] = {"headless": self.headless}
            # Packaged environments preinstall Chromium at a known path rather
            # than in Playwright's cache; honour it when it is there.
            executable = os.environ.get("CARROT_CHROMIUM_PATH")
            if executable and os.path.exists(executable):
                launch["executable_path"] = executable
            browser = playwright.chromium.launch(**launch)
            self.context = browser.new_context(
                viewport={"width": 1280, "height": 900},
                accept_downloads=False,
            )
            self.page = self.context.new_page()
            self.page.set_default_timeout(DEFAULT_TIMEOUT_MS)
        except Exception as exc:
            self._error = f"could not start Chromium: {exc}"
            self._ready.set()
            return

        self._ready.set()
        while True:
            item = self._commands.get()
            if item is None:
                break
            work, result_queue = item
            try:
                result_queue.put(("ok", work(self.page)))
            except Exception as exc:
                result_queue.put(("error", exc))

        for closer in (self.context.close, browser.close, playwright.stop):
            try:
                closer()
            except Exception:
                pass

    def call(self, work: Callable, timeout: float = 120.0):
        """Run ``work(page)`` on the browser thread and return its result."""
        if self._closed:
            raise BrowserError("the browser session is closed")
        self.start()
        result_queue: "queue.Queue" = queue.Queue()
        self._commands.put((work, result_queue))
        try:
            status, payload = result_queue.get(timeout=timeout)
        except queue.Empty:
            raise BrowserError("the browser stopped responding")
        if status == "error":
            raise BrowserError(str(payload))
        return payload

    def close(self):
        if self._thread is None or self._closed:
            self._closed = True
            return
        self._closed = True
        self._commands.put(None)
        self._thread.join(timeout=15)
        self._thread = None

    # --- observation ---

    def snapshot(self, include_text: bool = True) -> Dict[str, Any]:
        """The agent's whole view of the page: elements, text, and taint.

        Page text is screened here rather than at the call site, so there is no
        path that reads a page without the injection check running.
        """
        def work(page):
            data = page.evaluate(_SNAPSHOT_JS)
            data["text"] = page.evaluate(_TEXT_JS)[:MAX_TEXT_CHARS] if include_text else ""
            return data

        data = self.call(work)
        screening = policy.screen_untrusted(
            f"{data.get('title', '')}\n{data.get('text', '')}", origin=data.get("url", "")
        )
        data["text"] = policy.redact(policy.sanitize_untrusted(data.get("text", "")))
        data["screening"] = screening
        data["tainted"] = screening["tainted"]
        self.last_snapshot = data
        return data

    def element(self, ref: int) -> Optional[Dict[str, Any]]:
        """The element behind a ref, from the most recent snapshot."""
        for item in self.last_snapshot.get("elements", []):
            if int(item["ref"]) == int(ref):
                return item
        return None

    def screenshot(self) -> str:
        """Capture the viewport and return the file path."""
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        name = f"agent_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}.png"
        path = os.path.join(SCREENSHOT_DIR, name)
        self.call(lambda page: page.screenshot(path=path, full_page=False))
        return path

    # --- actions ---
    #
    # Each returns a short human-readable outcome string; the agent loop pairs
    # it with a fresh snapshot. None of them check permission — that already
    # happened in the policy kernel before the call reached here.

    def navigate(self, url: str) -> str:
        def work(page):
            page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            return page.url

        return f"opened {self.call(work)}"

    def _locator(self, page, ref: int):
        locator = page.locator(f'[data-carrot-ref="{int(ref)}"]')
        if locator.count() == 0:
            raise BrowserError(
                f"element {ref} is no longer on the page — take a new snapshot before acting"
            )
        return locator.first

    def click(self, ref: int) -> str:
        def work(page):
            self._locator(page, ref).click(timeout=DEFAULT_TIMEOUT_MS)
            page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
            return page.url

        return f"clicked element {ref}; now on {self.call(work)}"

    def type_text(self, ref: int, text: str, submit: bool = False) -> str:
        def work(page):
            locator = self._locator(page, ref)
            locator.fill("")
            locator.type(text, delay=15)
            if submit:
                locator.press("Enter")
                page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
            return page.url

        self.call(work)
        return f"typed {len(text)} characters into element {ref}" + (" and pressed Enter" if submit else "")

    def type_secret(self, ref: int, name: str, submit: bool = False) -> str:
        """Fill a field from the vault without the value entering the transcript.

        ``policy.reveal`` is called inside the closure and the value is bound to
        nothing that outlives it: it is not returned, not stored on the session,
        and not part of the outcome string.
        """
        value = policy.reveal(name)

        def work(page):
            locator = self._locator(page, ref)
            locator.fill("")
            locator.type(value, delay=15)
            if submit:
                locator.press("Enter")
                page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
            return page.url

        self.call(work)
        return f"entered the stored secret '{name}' into element {ref}"

    def select_option(self, ref: int, value: str) -> str:
        def work(page):
            locator = self._locator(page, ref)
            try:
                locator.select_option(label=value, timeout=DEFAULT_TIMEOUT_MS)
            except Exception:
                locator.select_option(value=value, timeout=DEFAULT_TIMEOUT_MS)
            return True

        self.call(work)
        return f"selected '{value}' in element {ref}"

    def press(self, key: str, ref: Optional[int] = None) -> str:
        def work(page):
            if ref is None:
                page.keyboard.press(key)
            else:
                self._locator(page, ref).press(key)
            page.wait_for_load_state("domcontentloaded", timeout=NAV_TIMEOUT_MS)
            return page.url

        self.call(work)
        return f"pressed {key}"

    def submit(self, ref: int) -> str:
        """Click a control knowing it submits — same mechanics, different name.

        Kept separate from :meth:`click` because the policy kernel treats it as
        irreversible, and the agent naming it explicitly is a clearer signal
        than the kernel guessing from a button's caption.
        """
        return self.click(ref).replace("clicked", "submitted with", 1)

    def scroll(self, amount: int = 600) -> str:
        self.call(lambda page: page.mouse.wheel(0, amount))
        return f"scrolled {amount}px"

    def back(self) -> str:
        def work(page):
            page.go_back(timeout=NAV_TIMEOUT_MS)
            return page.url

        return f"went back to {self.call(work)}"

    def extract_text(self) -> str:
        text = self.call(lambda page: page.evaluate(_TEXT_JS))
        return policy.redact(policy.sanitize_untrusted(text[:MAX_TEXT_CHARS]))

    def current_url(self) -> str:
        try:
            return self.call(lambda page: page.url, timeout=10)
        except BrowserError:
            return ""


# ===== Session registry =====
#
# One session per agent run, so two runs never share a cookie jar or fight over
# the same tab.

_sessions: Dict[str, BrowserSession] = {}
_sessions_lock = threading.Lock()


def session_for(run_id: str, headless: Optional[bool] = None) -> BrowserSession:
    with _sessions_lock:
        existing = _sessions.get(run_id)
        if existing is not None:
            return existing
        created = BrowserSession(headless=headless)
        _sessions[run_id] = created
    created.start()
    return created


def close_session(run_id: str):
    with _sessions_lock:
        existing = _sessions.pop(run_id, None)
    if existing is not None:
        existing.close()


def close_all():
    with _sessions_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for existing in sessions:
        existing.close()


# ===== Rendering a snapshot for the model =====

def render_snapshot(snapshot: Dict[str, Any], include_text: bool = True) -> str:
    """Format a page observation as the text the model actually reads.

    Elements first, because they are what the agent can act on; page text after,
    inside the untrusted envelope. A password field shows as ``«secret»`` here
    exactly as it does everywhere else.
    """
    lines = [
        f"PAGE: {snapshot.get('title', '')}",
        f"URL: {snapshot.get('url', '')}",
        "",
        "INTERACTIVE ELEMENTS (act on these by ref number):",
    ]
    for item in snapshot.get("elements", []):
        parts = [f"  {item['ref']:>3}  {item.get('role') or item['tag']:<9}"]
        if item.get("type"):
            parts.append(f"[{item['type']}]")
        parts.append(f'"{item.get("name", "")}"')
        if item.get("value"):
            parts.append(f"= {item['value']}")
        if item.get("checked"):
            parts.append("(checked)")
        if item.get("disabled"):
            parts.append("(disabled)")
        if item.get("secret"):
            parts.append("(credential field — use type_secret)")
        lines.append(" ".join(parts))

    if not snapshot.get("elements"):
        lines.append("  (nothing interactive is visible — try scrolling)")

    if include_text and snapshot.get("text"):
        lines.append("")
        lines.append(policy.wrap_untrusted(
            snapshot["text"], origin=snapshot.get("url", ""),
            screening=snapshot.get("screening"),
        ))
    return "\n".join(lines)
