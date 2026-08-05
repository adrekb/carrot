"""Local webhooks: letting the rest of your house talk to Carrot.

Home Assistant, a Stream Deck macro, a shell script on a cron, a Shortcut on
your phone — all of them can already make an HTTP request. What they could not
do is reach Carrot, because every endpoint requires a session token that only
the app's own window has. So a smart-home automation that wanted to say "tell
me if anything needs attention before I leave" had nowhere to send it.

This is that door, and the whole design is about it being a *narrow* one.

**Nothing is on by default.** No hook exists until the user creates one, and
the feature as a whole has an off switch.

**A hook does one named thing.** A hook is not a way to run arbitrary
instructions: it is bound to one action from a fixed list, chosen when the hook
is made. A token that leaks can do exactly the thing it was made to do and
nothing else — it cannot be repurposed into "run this shell command".

**Each hook carries its own secret**, compared in constant time, and can be
revoked on its own without disturbing the others.

**Rate limited per hook.** A misconfigured automation that fires every second
should be refused rather than left to drive a model in a loop.

Outbound hooks are the mirror image: Carrot POSTs a small JSON payload to a URL
on your own network when something happens. Those are restricted to private
addresses precisely because everything else in Carrot is restricted *from*
them — this is the one place where the private network is the intended
destination rather than an SSRF target.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

from .config import get_config, set_config

# What a hook is allowed to do. Deliberately a closed set: the point of a
# webhook token is that it cannot become a shell.
ACTION_NOTIFY = "notify"        # raise a notification in Carrot
ACTION_ASK = "ask"              # ask the assistant a question, return the answer
ACTION_BRIEF = "brief"          # today's status: calendar, reminders, notifications
ACTION_NOTE = "note"            # file a note
ACTION_REMINDER = "reminder"    # create a reminder

ACTIONS = {
    ACTION_NOTIFY: "Raise a notification in Carrot",
    ACTION_ASK: "Ask the assistant a question and return its answer",
    ACTION_BRIEF: "Return today's calendar, reminders and unread notifications",
    ACTION_NOTE: "File a note",
    ACTION_REMINDER: "Create a reminder",
}

# A hook firing more often than this is a misconfigured automation, not a user.
RATE_LIMIT_PER_MINUTE = 20
MAX_HOOKS = 25
MAX_BODY_CHARS = 4000
OUTBOUND_TIMEOUT = 10

ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")

_fired: Dict[str, List[float]] = {}
_fired_lock = threading.Lock()


class WebhookError(RuntimeError):
    """Something the user needs to fix, phrased for them."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ===== Storage =====

def enabled() -> bool:
    return bool(get_config().get("webhooks_enabled", False))


def set_enabled(value: bool) -> bool:
    set_config("webhooks_enabled", bool(value))
    return bool(value)


def _all() -> List[Dict[str, Any]]:
    raw = get_config().get("webhooks", [])
    return [h for h in raw if isinstance(h, dict) and h.get("id")]


def list_hooks(reveal: bool = False) -> List[Dict[str, Any]]:
    """Every hook. Tokens are withheld unless explicitly asked for.

    The listing endpoint never reveals them: a token is shown once, when the
    hook is created, the same way every other system does it — because a list
    view that renders secrets is a screenshot away from being a leak.
    """
    hooks = []
    for hook in _all():
        entry = dict(hook)
        if not reveal:
            entry.pop("token", None)
        entry["url"] = url_for(hook["id"])
        hooks.append(entry)
    return hooks


def get_hook(hook_id: str) -> Optional[Dict[str, Any]]:
    for hook in _all():
        if hook["id"] == hook_id:
            return hook
    return None


def url_for(hook_id: str) -> str:
    cfg = get_config()
    host = cfg.get("server_host", "127.0.0.1")
    port = cfg.get("server_port", 8181)
    return f"http://{host}:{port}/api/hooks/{hook_id}"


def create_hook(hook_id: str, action: str, label: str = "",
                defaults: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Make a hook and mint its token. The token is returned exactly once."""
    if not ID_PATTERN.match(hook_id or ""):
        raise WebhookError("a hook id is lowercase letters, digits, - and _")
    if action not in ACTIONS:
        raise WebhookError(f"unknown action '{action}' — expected one of {sorted(ACTIONS)}")
    if get_hook(hook_id):
        raise WebhookError(f"a hook called '{hook_id}' already exists")
    hooks = _all()
    if len(hooks) >= MAX_HOOKS:
        raise WebhookError(f"that is already {MAX_HOOKS} hooks — delete one first")

    hook = {
        "id": hook_id,
        "action": action,
        "label": label or hook_id,
        "token": secrets.token_urlsafe(32),
        "defaults": dict(defaults or {}),
        "created_at": _now(),
        "last_fired": "",
        "fires": 0,
    }
    set_config("webhooks", hooks + [hook])
    return {**hook, "url": url_for(hook_id)}


def delete_hook(hook_id: str) -> bool:
    hooks = _all()
    kept = [h for h in hooks if h["id"] != hook_id]
    if len(kept) == len(hooks):
        return False
    set_config("webhooks", kept)
    return True


def rotate_token(hook_id: str) -> Dict[str, Any]:
    """Replace a hook's secret without changing what it does."""
    hooks = _all()
    for hook in hooks:
        if hook["id"] == hook_id:
            hook["token"] = secrets.token_urlsafe(32)
            set_config("webhooks", hooks)
            return {**hook, "url": url_for(hook_id)}
    raise WebhookError(f"no hook called '{hook_id}'")


def _record_fire(hook_id: str) -> None:
    hooks = _all()
    for hook in hooks:
        if hook["id"] == hook_id:
            hook["last_fired"] = _now()
            hook["fires"] = int(hook.get("fires", 0)) + 1
            set_config("webhooks", hooks)
            return


# ===== Authenticating a call =====

def authenticate(hook_id: str, *presented: str) -> Dict[str, Any]:
    """Find the hook and check its token, in constant time.

    Several candidates may be offered, because the token can arrive three ways
    and the app's own ``X-Carrot-Token`` session header may be present on the
    same request — taking only the first non-empty one meant a hook token in
    the body was shadowed by the session header and never seen.

    ``hmac.compare_digest`` rather than ``==``: a check that returns early on
    the first wrong character is measurably guessable, and this endpoint is
    reachable without a session.
    """
    if not enabled():
        raise WebhookError("webhooks are turned off")
    hook = get_hook(hook_id)
    # Compare against a dummy of the same shape even when the hook does not
    # exist, so a wrong id and a wrong token take the same time to refuse.
    expected = str(hook["token"] if hook else secrets.token_urlsafe(32))
    # Every candidate is compared, not just until one matches: short-circuiting
    # would leak which position held the right one through timing.
    matched = False
    for candidate in presented or ("",):
        if hmac.compare_digest(expected, str(candidate or "")):
            matched = True
    if not hook or not matched:
        raise WebhookError("unknown hook or wrong token")
    return hook


def check_rate(hook_id: str) -> None:
    """Refuse a hook firing faster than any human intent could."""
    now = time.time()
    with _fired_lock:
        recent = [t for t in _fired.get(hook_id, []) if now - t < 60]
        if len(recent) >= RATE_LIMIT_PER_MINUTE:
            raise WebhookError(
                f"that hook has fired {RATE_LIMIT_PER_MINUTE} times in a minute — "
                f"it is probably misconfigured, so this call was refused"
            )
        recent.append(now)
        _fired[hook_id] = recent


def reset_rate_limits() -> None:
    with _fired_lock:
        _fired.clear()


# ===== Running an action =====

def fire(hook: Dict[str, Any], payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Do the one thing this hook was made to do."""
    body = {**(hook.get("defaults") or {}), **(payload or {})}
    action = hook["action"]
    try:
        result = _ACTIONS[action](body)
    except WebhookError:
        raise
    except Exception as exc:
        raise WebhookError(f"the hook failed: {exc}")
    _record_fire(hook["id"])
    return {"hook": hook["id"], "action": action, **result}


def _text(body: Dict[str, Any], *names: str, limit: int = MAX_BODY_CHARS) -> str:
    for name in names:
        value = body.get(name)
        if isinstance(value, str) and value.strip():
            return value.strip()[:limit]
    return ""


def _do_notify(body: Dict[str, Any]) -> Dict[str, Any]:
    from . import proactive

    title = _text(body, "title", "message", "text", limit=200)
    if not title:
        raise WebhookError("a notification needs a title or message")
    notification = proactive.create(
        kind="webhook",
        title=title,
        body=_text(body, "body", "detail"),
        severity=str(body.get("severity") or "info"),
    )
    return {"notification": notification.get("id", ""), "title": title}


def _do_ask(body: Dict[str, Any]) -> Dict[str, Any]:
    from . import router as router_mod

    question = _text(body, "question", "message", "text", "prompt")
    if not question:
        raise WebhookError("nothing was asked")
    resolved = router_mod.route(task="chat")
    answer = "".join(
        event.get("text", "")
        for event in router_mod.stream_events(
            resolved, [{"role": "user", "content": question}], tools=None)
        if event.get("type") in ("text", "content")
    )
    return {"question": question, "answer": answer.strip()}


def _do_brief(body: Dict[str, Any]) -> Dict[str, Any]:
    """Everything worth knowing before you walk out of the door."""
    brief: Dict[str, Any] = {"generated_at": _now()}
    try:
        from . import calfeed

        brief["events"] = [
            {"title": e.get("summary", ""), "start": e.get("start", "")}
            for e in (calfeed.upcoming_events(days=1) or [])[:10]
        ]
    except Exception:
        brief["events"] = []
    try:
        from . import reminders

        brief["reminders"] = [
            {"title": r.get("title", ""), "due_at": r.get("due_at", "")}
            for r in reminders.list_reminders(completed=False, limit=10)
        ]
    except Exception:
        brief["reminders"] = []
    try:
        from . import proactive

        brief["notifications"] = [
            {"title": n.get("title", ""), "severity": n.get("severity", "info")}
            for n in proactive.list_notifications(unread_only=True, limit=10)
        ]
    except Exception:
        brief["notifications"] = []
    return {"brief": brief}


def _do_note(body: Dict[str, Any]) -> Dict[str, Any]:
    from . import notes

    title = _text(body, "title", limit=200) or "Note from a webhook"
    note = notes.create_note(title=title, content=_text(body, "content", "body", "text"))
    return {"note": note.get("id", ""), "title": title}


def _do_reminder(body: Dict[str, Any]) -> Dict[str, Any]:
    from . import reminders

    title = _text(body, "title", "message", "text", limit=200)
    if not title:
        raise WebhookError("a reminder needs a title")
    reminder = reminders.create_reminder(
        title=title,
        description=_text(body, "description", "body"),
        due_at=_text(body, "due_at", "when", limit=64) or None,
    )
    return {"reminder": reminder.get("id", ""), "title": title}


_ACTIONS = {
    ACTION_NOTIFY: _do_notify,
    ACTION_ASK: _do_ask,
    ACTION_BRIEF: _do_brief,
    ACTION_NOTE: _do_note,
    ACTION_REMINDER: _do_reminder,
}


# ===== Outbound =====
#
# The mirror image, and the one place in Carrot where a private address is the
# intended destination rather than something to refuse. Everything else — the
# web fetcher, the research crawler — is barred from the local network for SSRF
# reasons; a smart-home webhook is the deliberate exception, so it is confined
# to private addresses rather than merely permitted to reach them.

def outbound_targets() -> List[Dict[str, Any]]:
    raw = get_config().get("webhook_targets", [])
    return [t for t in raw if isinstance(t, dict) and t.get("url")]


def add_target(url: str, events: Optional[List[str]] = None,
               label: str = "") -> Dict[str, Any]:
    check_outbound_url(url)
    target = {
        "id": uuid.uuid4().hex[:10],
        "url": url.strip(),
        "label": label or urlparse(url).netloc,
        "events": list(events or ["notification"]),
        "created_at": _now(),
    }
    set_config("webhook_targets", outbound_targets() + [target])
    return target


def remove_target(target_id: str) -> bool:
    targets = outbound_targets()
    kept = [t for t in targets if t["id"] != target_id]
    if len(kept) == len(targets):
        return False
    set_config("webhook_targets", kept)
    return True


def check_outbound_url(url: str) -> None:
    """Only http(s) to an address on your own network.

    Allowing an arbitrary public URL would turn Carrot's notifications into an
    exfiltration channel that looks like a feature.
    """
    parsed = urlparse((url or "").strip())
    if parsed.scheme not in ("http", "https"):
        raise WebhookError("a webhook target must be an http or https URL")
    host = (parsed.hostname or "").lower()
    if not host:
        raise WebhookError("that URL has no host")
    if host in ("localhost", "localhost.localdomain") or host.endswith(".local"):
        return
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise WebhookError(
            "a webhook target must be an address on your own network — a name "
            "that resolves elsewhere would send your notifications to a stranger"
        )
    if not (address.is_private or address.is_loopback or address.is_link_local):
        raise WebhookError(f"{host} is a public address, not one on your network")


def notify_targets(event: str, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """POST an event to every target subscribed to it. Failures never raise."""
    if not enabled():
        return []
    results = []
    for target in outbound_targets():
        if event not in target.get("events", []):
            continue
        try:
            check_outbound_url(target["url"])
            response = requests.post(
                target["url"],
                json={"event": event, "at": _now(), **payload},
                timeout=OUTBOUND_TIMEOUT,
            )
            results.append({"target": target["id"], "status": response.status_code})
        except Exception as exc:
            # A smart-home box being off is not a Carrot error.
            results.append({"target": target["id"], "error": str(exc)[:200]})
    return results
