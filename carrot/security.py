"""Local API authentication and destructive-command screening.

Binding to 127.0.0.1 keeps Carrot off the network, but it does not keep it away
from the machine: any page open in the browser, and any process on the box, can
reach ``http://127.0.0.1:8181`` — including ``/api/terminal/execute``, which runs
shell commands. Two defences:

* **A session token.** Minted at startup and injected into the app's own HTML.
  Every ``/api`` call must present it. A cross-origin page cannot read the HTML
  to obtain it (the same-origin policy stops that), so it cannot forge a call.
* **A destructive-command gate.** Commands matching known-destructive patterns
  need an explicit confirmation flag, so an unattended agent or a stray click
  cannot wipe a directory in one step.

Both are on by default and can be turned off in config for debugging.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import re
import secrets
import time
from typing import Any, Dict, List, Optional, Tuple

from .config import get_config

LOG = logging.getLogger(__name__)

TOKEN_HEADER = "X-Carrot-Token"
TOKEN_QUERY_PARAM = "carrot_token"
# What an EventSource presents instead of the token itself.
TICKET_QUERY_PARAM = "ticket"
# The only paths a ticket opens. It is not a session token and must not
# work like one: minted for a stream, spent on that stream, nothing else.
TICKET_PATHS = ("/api/notifications/stream", "/api/bootstrap/stream")

# Where the session token lives.
#
# This used to be derived from `__file__`, i.e. inside the installed package.
# `config.py` goes out of its way *not* to do that — when the backend is
# frozen it puts everything under %APPDATA%/Carrot precisely because the
# install directory may be read-only — and this file ignored that decision.
# In a machine-wide install the `makedirs` below is a PermissionError that
# escapes `session_token()` outright, and in the milder case the write fails
# and the token is never persisted, which reinstates the exact failure
# persistence exists to prevent: restart the backend, and an already-open
# window is holding a token that no longer exists.
from .config import CARROT_DIR

CONFIG_DIR = os.path.join(CARROT_DIR, "config")
TOKEN_PATH = os.path.join(CONFIG_DIR, "session.json")

# Where it used to live. Read once, so upgrading does not log you out of a
# window that is already open.
LEGACY_TOKEN_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "config", "session.json")

# Paths reachable without a token: the shell that carries the token, the static
# assets it pulls in, and the health probe the Electron launcher polls before
# the UI exists.
# The OAuth callback is reached by the provider redirecting the system browser,
# which has no session token and cannot be given one. It is safe to leave open
# because it is useless without a `state` this process generated and is still
# holding in memory — an unknown state is rejected before anything happens.
# `/api/pair` is reachable without a token because a phone that has never
# been paired has no token to present — that is the entire point of it.
# What stands in for authentication there is the six-character code
# showing on the computer, which is open for five minutes, spent on first
# use, and shut by five wrong guesses. See pairing.py.
PUBLIC_PATHS = {"/", "/api/health", "/favicon.ico", "/api/auth/callback",
                "/api/pair", "/api/pair/requirements"}
PUBLIC_PREFIXES = (
    "/css/", "/js/", "/vendor/", "/assets/", "/docs", "/openapi.json", "/redoc",
    # Local webhooks carry their own per-hook token, checked in constant time,
    # and are refused outright unless the user turned the feature on. Home
    # Assistant has no session and cannot be given one.
    "/api/hooks/",
)

_token: Optional[str] = None


def _ensure_config_dir() -> bool:
    """Make the config directory, reporting whether it is usable.

    Returns rather than raises: a token that cannot be *stored* is still a
    perfectly good token for this process, and failing to make a directory
    must not take authentication down with it.
    """
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        return True
    except OSError:
        LOG.warning("cannot create %s — the session token will not survive a "
                    "backend restart", CONFIG_DIR)
        return False


def session_token() -> str:
    """The current session token, minted and persisted on first use.

    Persisting it means an Electron restart of the backend does not invalidate
    an already-open window.
    """
    global _token
    if _token:
        return _token

    writable = _ensure_config_dir()
    for path in (TOKEN_PATH, LEGACY_TOKEN_PATH):
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as handle:
                stored = json.load(handle).get("token")
            if stored:
                _token = stored
                # Carry a token found at the old location forward, so the
                # move happens once and quietly.
                if path is LEGACY_TOKEN_PATH and writable:
                    _write_token(_token)
                return _token
        except (OSError, json.JSONDecodeError):
            pass

    _token = secrets.token_urlsafe(32)
    if writable:
        _write_token(_token)
    return _token


def _write_token(token: str):
    try:
        with open(TOKEN_PATH, "w", encoding="utf-8") as handle:
            json.dump({"token": token}, handle)
    except OSError:
        LOG.warning("could not persist the session token to %s", TOKEN_PATH)
        return
    # A no-op on Windows, where the file inherits the parent directory's ACL.
    # %APPDATA%/Carrot is already per-user there, which is the protection this
    # is reaching for; on POSIX the mode is what provides it.
    if os.name == "posix":
        try:
            os.chmod(TOKEN_PATH, 0o600)
        except OSError:
            pass


def rotate_token() -> str:
    """Mint a fresh token, invalidating every existing client."""
    global _token
    _token = None
    # Both locations. Leaving the legacy file behind would mean "rotate" hands
    # back the very token it was asked to invalidate, because the read below
    # falls back to it — the one place where supporting the old path could
    # turn into a security hole rather than a convenience.
    for path in (TOKEN_PATH, LEGACY_TOKEN_PATH):
        try:
            os.remove(path)
        except OSError:
            pass
    return session_token()


# ===== Tickets for EventSource =====
#
# `EventSource` cannot set a request header, so the two SSE endpoints carried
# the session token in the query string — which put it in the server log, the
# browser's history, and anything sitting in between. It is a local app, so
# the exposure is small, but it was the one place the token left the header
# and it did so on every launch.
#
# A ticket is minted by an authenticated POST, spent by the SSE connection,
# and then gone: single use, short lived, and useless for anything else. What
# ends up in the log is a value that was already dead when it was written.

TICKET_TTL_SECONDS = 30
_tickets: Dict[str, float] = {}


def mint_ticket() -> str:
    """A one-shot credential for an EventSource connection."""
    _sweep_tickets()
    ticket = secrets.token_urlsafe(24)
    _tickets[ticket] = time.time() + TICKET_TTL_SECONDS
    return ticket


def spend_ticket(ticket: Optional[str]) -> bool:
    """Redeem a ticket. True at most once per ticket, and only before it expires."""
    if not ticket:
        return False
    _sweep_tickets()
    # `pop` is what makes it single use: a ticket replayed from a log or a
    # history entry finds nothing to redeem.
    expiry = _tickets.pop(ticket, None)
    return expiry is not None and expiry > time.time()


def _sweep_tickets():
    """Drop expired tickets, so an unused one cannot accumulate forever."""
    now = time.time()
    for key in [k for k, expiry in _tickets.items() if expiry <= now]:
        _tickets.pop(key, None)


def auth_enabled() -> bool:
    return bool(get_config().get("auth_enabled", True))


def is_public_path(path: str) -> bool:
    return path in PUBLIC_PATHS or path.startswith(PUBLIC_PREFIXES)


def token_valid(candidate: Optional[str]) -> bool:
    """Constant-time token comparison."""
    if not candidate:
        return False
    return hmac.compare_digest(candidate, session_token())


def inject_token(html: str) -> str:
    """Place the session token in the app shell for the frontend to pick up.

    This is the only place the token is handed out, and it is served from the
    same origin the UI runs on — which is exactly what stops another origin from
    reading it.
    """
    meta = f'<meta name="carrot-token" content="{session_token()}">'
    if "</head>" in html:
        return html.replace("</head>", f"  {meta}\n</head>", 1)
    return meta + html


# ===== Destructive command screening =====

# (pattern, human-readable reason). Ordered most-specific first so the reported
# reason is the most useful one.
DESTRUCTIVE_PATTERNS: List[Tuple[str, str]] = [
    (r"\brm\s+(-[a-zA-Z]*[rR][a-zA-Z]*f|-[a-zA-Z]*f[a-zA-Z]*[rR])\b", "recursive force delete"),
    (r"\brm\s+-[a-zA-Z]*r\b.*[/\\]\s*$", "recursive delete of a directory"),
    (r"\bmkfs(\.\w+)?\b", "filesystem format"),
    (r"\bdd\b[^|]*\bof=/dev/", "raw write to a block device"),
    (r"\b(shutdown|reboot|halt|poweroff)\b", "shuts down the machine"),
    (r"\bformat\s+[a-zA-Z]:", "drive format"),
    (r"Remove-Item\b.*-Recurse\b.*-Force\b", "recursive force delete"),
    (r">\s*/dev/(sd|nvme|hd)", "overwrites a disk device"),
    (r"\bgit\s+push\b.*(--force\b|-f\b)", "force push rewrites remote history"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-[a-zA-Z]*f)", "discards uncommitted work"),
    (r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|k)?sh\b", "pipes a downloaded script into a shell"),
    (r"\bchmod\s+-R\s+777\b", "makes files world-writable"),
    (r"\b(drop\s+database|truncate\s+table)\b", "destroys database contents"),
    (r":\(\)\s*\{.*\}\s*;\s*:", "fork bomb"),
    (r"\bsudo\s+rm\b", "privileged delete"),
]

_COMPILED = [(re.compile(pattern, re.IGNORECASE), reason) for pattern, reason in DESTRUCTIVE_PATTERNS]


def classify_command(command: str) -> Dict[str, Any]:
    """Screen a shell command for destructive intent.

    Pattern matching, not a sandbox — it catches the common ways a command
    destroys data by accident, and is deliberately biased toward warning.
    """
    command = (command or "").strip()
    if not command:
        return {"destructive": False, "reasons": []}
    reasons = [reason for pattern, reason in _COMPILED if pattern.search(command)]
    return {
        "destructive": bool(reasons),
        "reasons": reasons,
        "summary": reasons[0] if reasons else "",
    }


def require_confirmation() -> bool:
    return bool(get_config().get("terminal_confirm_destructive", True))


def check_command(command: str, confirmed: bool = False) -> Dict[str, Any]:
    """Decide whether a command may run. ``allowed`` False means ask the user."""
    verdict = classify_command(command)
    if not verdict["destructive"] or confirmed or not require_confirmation():
        return {"allowed": True, **verdict}
    return {
        "allowed": False,
        **verdict,
        "message": (
            f"This command {verdict['summary']}. Re-send with confirm=true to run it."
        ),
    }


# ===== Terminal working-directory containment =====

def allowed_cwd_roots() -> List[str]:
    """Directories the terminal may run in."""
    config = get_config()
    roots = [
        config.get("code_workspace_dir", "") or os.path.join(os.path.expanduser("~"), "CarrotProjects"),
        config.get("data_dir", ""),
    ]
    roots += config.get("terminal_extra_roots", []) or []
    return [os.path.abspath(os.path.expanduser(r)) for r in roots if r]


def resolve_cwd(cwd: Optional[str]) -> str:
    """Confine the terminal's working directory to the allowed roots.

    Containment is off unless ``terminal_restrict_cwd`` is set, because a
    general-purpose terminal that cannot leave one directory is not much of a
    terminal — but users who want the agent boxed in can have it.
    """
    roots = allowed_cwd_roots()
    default_root = roots[0] if roots else os.getcwd()
    if not cwd:
        return default_root

    resolved = os.path.abspath(os.path.expanduser(cwd))
    if not get_config().get("terminal_restrict_cwd", False):
        return resolved
    for root in roots:
        if resolved == root or resolved.startswith(root + os.sep):
            return resolved
    raise PermissionError(f"working directory is outside the allowed roots: {cwd}")


def status() -> Dict[str, Any]:
    return {
        "auth_enabled": auth_enabled(),
        "token_header": TOKEN_HEADER,
        "confirm_destructive": require_confirmation(),
        "restrict_cwd": bool(get_config().get("terminal_restrict_cwd", False)),
        "allowed_cwd_roots": allowed_cwd_roots(),
    }
