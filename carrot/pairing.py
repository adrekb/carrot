"""Letting your phone in, without letting the network in.

Everything else in Carrot is protected by the fact that it is only listening on
loopback. Read `require_session_token` in app.py and it says so outright: the
bind stops the network, the token stops the rest of the machine. The token
itself is handed to anyone who asks for `/`, because until now the only thing
that could ask was a browser on this computer.

That arrangement has exactly one property that matters here, and it is the one
that has to go: **the app is unreachable from your phone.** Removing the bind
without replacing the boundary would not be "adding mobile support", it would
be publishing a terminal on the local network — anything that loads the page
gets the token out of the HTML, and the token runs shell commands.

So the boundary moves from *where the packet came from* to *which device is
asking*:

* **The shell stops handing out the session token off-machine.** A request
  from loopback gets it, exactly as before; the desktop app is untouched. A
  request from anywhere else gets the page with no token, and the page asks to
  be paired.

* **Pairing is a code you read off the screen of the machine you are letting
  it into.** Six characters, five minutes, one use, five wrong guesses and the
  window shuts. Somebody who can see that screen is somebody sitting at your
  computer, which is the level of trust being granted.

* **Every device gets its own token, and it is stored hashed.** So the file on
  disk is not a spare key, one phone's token cannot be inferred from another's,
  and losing a phone costs you one revocation rather than a rotation that signs
  out everything you own.

* **Revocation is immediate and by name**, with a last-seen beside it, because
  "which of these is the old phone" is unanswerable from a list of ids.

What this module deliberately does *not* do is decide how the packets arrive.
LAN, a WireGuard tailnet, a tunnel from a hosting provider — a paired device is
paired regardless, and the transport is a question about your network rather
than about your authentication. That separation is the reason turning on remote
access from a coffee shop later does not need any of this rewritten.
"""
from __future__ import annotations

import hashlib
import hmac
import io
import secrets
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .database import get_db

# Unambiguous characters only. This code is read off one screen and typed into
# another, usually badly, and `I1` / `O0` in a six-character code is a support
# question rather than a design detail.
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6

# Five minutes, because the window is open only while you are standing there
# doing it. A pairing code that lives for an hour is an hour in which the
# screen it is written on can be seen by somebody else.
CODE_TTL_SECONDS = 300

# Five wrong guesses closes the window rather than throttling it. 32^6 is a
# billion combinations and nobody is brute-forcing that in five minutes, but a
# window that never closes on repeated failure is one that tells an attacker
# their guessing is free.
MAX_ATTEMPTS = 5

# How stale a last-seen may get before it is worth a write. Every request from
# a phone would otherwise be a database write, and this column exists to answer
# "when did that thing last talk to me", which a minute's precision answers.
LAST_SEEN_RESOLUTION_SECONDS = 60


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso() -> str:
    return _now().isoformat(timespec="seconds")


# ===== Storage =====

def _ensure_table(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS paired_devices (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            token_hash TEXT NOT NULL,
            created_at TEXT NOT NULL,
            last_seen TEXT DEFAULT '',
            user_agent TEXT DEFAULT ''
        )""")


def _hash(token: str) -> str:
    """SHA-256, and deliberately nothing slower.

    A password gets a slow KDF because it is short and human-chosen. This is 32
    bytes from `secrets`, so there is no dictionary to run and no amount of
    hardware that makes guessing it a strategy; the hash is here so that the
    database is not a keyring, not to buy time against a cracker.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ===== The pairing window =====
#
# In memory on purpose. A pairing code is a thing you are doing right now, and
# one that survived a restart would be a code still live on a screen nobody is
# looking at any more.
_window: Dict[str, Any] = {"code": "", "opened": 0.0, "attempts": 0}


def open_window() -> Dict[str, Any]:
    """Start pairing. Replaces any code already showing."""
    _window["code"] = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    _window["opened"] = time.monotonic()
    _window["attempts"] = 0
    return window_state()


def close_window() -> Dict[str, Any]:
    _window["code"] = ""
    _window["opened"] = 0.0
    _window["attempts"] = 0
    return window_state()


def _seconds_left() -> int:
    if not _window["code"]:
        return 0
    left = CODE_TTL_SECONDS - (time.monotonic() - _window["opened"])
    return int(left) if left > 0 else 0


def window_state() -> Dict[str, Any]:
    """What the desktop shows while it is waiting to be paired with."""
    left = _seconds_left()
    if not left:
        _window["code"] = ""
    return {
        "open": bool(_window["code"]),
        "code": _window["code"],
        "seconds_left": left,
        "attempts_left": max(0, MAX_ATTEMPTS - _window["attempts"]) if _window["code"] else 0,
    }


class PairingRefused(Exception):
    """Said out loud rather than returned as a false, because the three ways
    this fails need three different things from the person holding the phone."""


def claim(code: str, name: str = "", user_agent: str = "",
          username: str = "", password: str = "") -> Dict[str, Any]:
    """Trade a pairing code for this device's own token.

    The token is returned here and never again: it is stored hashed, so a
    device that loses it pairs again rather than looking it up.
    """
    if not _seconds_left():
        _window["code"] = ""
        raise PairingRefused("Pairing is not open. Start it on the computer running Carrot.")

    # The sign-in is checked before the code, and a failure costs an attempt
    # just as a wrong code does. Otherwise the code could be brute-forced with
    # the password left blank, and the five-guess limit would be protecting
    # only half the door.
    if not credentials_match(username, password):
        _window["attempts"] += 1
        if MAX_ATTEMPTS - _window["attempts"] <= 0:
            close_window()
            raise PairingRefused("Too many failed attempts. Start pairing again "
                                 "on the computer.")
        raise PairingRefused("That name and password do not match the ones set "
                             "on the computer.")

    # Constant-time, even though the code is short-lived and rate-limited:
    # a comparison that returns early is a comparison that can be measured,
    # and there is no reason to be the exception.
    if not hmac.compare_digest((code or "").strip().upper(), _window["code"]):
        _window["attempts"] += 1
        left = MAX_ATTEMPTS - _window["attempts"]
        if left <= 0:
            close_window()
            raise PairingRefused("Too many wrong codes. Start pairing again on the computer.")
        raise PairingRefused(f"That code is not right. {left} tries left.")

    token = secrets.token_urlsafe(32)
    device = {
        "id": str(uuid.uuid4())[:12],
        "name": (name or "").strip()[:60] or "A phone",
        "token_hash": _hash(token),
        "created_at": _iso(),
        "last_seen": _iso(),
        "user_agent": (user_agent or "")[:200],
    }
    conn = get_db()
    _ensure_table(conn)
    conn.execute(
        """INSERT INTO paired_devices
           (id, name, token_hash, created_at, last_seen, user_agent)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (device["id"], device["name"], device["token_hash"],
         device["created_at"], device["last_seen"], device["user_agent"]),
    )
    conn.commit()
    conn.close()

    # One use. The code on screen has been spent, and leaving it live would
    # mean a second device pairing off the same glance at the same screen.
    close_window()
    return {"token": token, "device": public(device)}


# ===== Devices =====

def public(device: Dict[str, Any]) -> Dict[str, Any]:
    """A device as the UI may see it — which is everything but the hash."""
    return {k: v for k, v in device.items() if k != "token_hash"}


def list_devices() -> List[Dict[str, Any]]:
    conn = get_db()
    _ensure_table(conn)
    rows = conn.execute(
        "SELECT * FROM paired_devices ORDER BY created_at DESC").fetchall()
    conn.close()
    return [public(dict(row)) for row in rows]


def revoke(device_id: str) -> bool:
    conn = get_db()
    _ensure_table(conn)
    cursor = conn.execute("DELETE FROM paired_devices WHERE id = ?", (device_id,))
    conn.commit()
    gone = cursor.rowcount > 0
    conn.close()
    return gone


def revoke_all() -> int:
    conn = get_db()
    _ensure_table(conn)
    cursor = conn.execute("DELETE FROM paired_devices")
    conn.commit()
    count = cursor.rowcount
    conn.close()
    return count


def device_token_valid(candidate: Optional[str]) -> bool:
    """Whether this is a live device's token, and note that it was used.

    Every row is compared even after a match, so the work does not depend on
    which device is asking or on how many are registered.
    """
    if not candidate:
        return False
    presented = _hash(candidate)
    conn = get_db()
    _ensure_table(conn)
    rows = conn.execute("SELECT id, token_hash, last_seen FROM paired_devices").fetchall()
    matched = None
    for row in rows:
        if hmac.compare_digest(presented, row["token_hash"]):
            matched = dict(row)
    if matched is None:
        conn.close()
        return False
    if _stale(matched.get("last_seen")):
        conn.execute("UPDATE paired_devices SET last_seen = ? WHERE id = ?",
                     (_iso(), matched["id"]))
        conn.commit()
    conn.close()
    return True


# ===== The shared credential =====
#
# Tailscale says the two machines are on the same private network. The pairing
# code says somebody is standing at the host. Neither says *who* — and both are
# properties of a place rather than of a person.
#
# So a name and a password, set on the host and typed again on the device, and
# the two have to agree before a device is let in. It is the one part of this
# that is a secret you carry rather than a network you are on: a tailnet device
# you no longer fully control, or a screen glanced at over a shoulder, gets a
# stranger to the pairing screen and no further.
#
# Hashed with PBKDF2 rather than the bare SHA-256 the device tokens get, and
# the difference is not inconsistency. A device token is 32 bytes from
# `secrets` — there is no dictionary to run against it. A password is short and
# chosen by a person, so the stored form has to be expensive to attack, which
# is what an iteration count buys.
PBKDF2_ROUNDS = 240_000
CREDENTIAL_KEY = "remote_credential"


def _derive(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS).hex()


def set_credentials(username: str, password: str) -> Dict[str, Any]:
    """Set — or with two empty strings, clear — the shared sign-in."""
    from .config import set_config

    username = (username or "").strip()
    password = password or ""
    if not username and not password:
        set_config(CREDENTIAL_KEY, {})
        return credential_state()
    if not username or not password:
        raise ValueError("A sign-in needs both a name and a password.")
    if len(password) < 6:
        raise ValueError("Use at least six characters.")
    salt = secrets.token_bytes(16)
    set_config(CREDENTIAL_KEY, {
        "username": username,
        "salt": salt.hex(),
        "hash": _derive(password, salt),
        "set_at": _iso(),
    })
    return credential_state()


def _stored_credential() -> Dict[str, Any]:
    from .config import get_config
    stored = get_config().get(CREDENTIAL_KEY) or {}
    return stored if isinstance(stored, dict) else {}


def credential_state() -> Dict[str, Any]:
    """Whether a sign-in is required, and under what name. Never the hash."""
    stored = _stored_credential()
    return {"required": bool(stored.get("hash")),
            "username": stored.get("username", ""),
            "set_at": stored.get("set_at", "")}


def credentials_match(username: str, password: str) -> bool:
    """Constant-time on both halves, and the work is done either way.

    Returning early on an unknown username would make it possible to learn
    which name is right by timing, and the name is half the credential.
    """
    stored = _stored_credential()
    if not stored.get("hash"):
        return True     # none set: nothing to match against
    try:
        salt = bytes.fromhex(stored.get("salt", ""))
    except ValueError:
        return False
    presented = _derive(password or "", salt) if salt else ""
    name_ok = hmac.compare_digest((username or "").strip(), stored.get("username", ""))
    hash_ok = hmac.compare_digest(presented, stored.get("hash", ""))
    return name_ok and hash_ok


# ===== The address, as something you can point a camera at =====
#
# Typing `http://100.87.14.3:8181` into a phone keyboard is four opportunities
# to get it wrong and no feedback about which one you took — and the address is
# the part nobody can be expected to know is even right. The camera reads it in
# one go.
#
# The code is deliberately *not* in the QR. Putting it there would make this one
# scan instead of a scan and six characters, at the cost of writing a live
# credential into the phone's URL bar and history — which is the exact thing
# this codebase already decided against for the SSE ticket. It also keeps the
# two halves honest: the QR says *where*, and the code says *you are the person
# sitting in front of this screen*. Only one of those is a secret.
def qr_svg(url: str) -> str:
    """An SVG QR for this URL, or an empty string if the encoder is missing.

    Never raises. A QR is a convenience on top of an address that is also
    printed underneath it in text; losing it must not lose pairing.
    """
    try:
        import segno
    except ImportError:
        return ""
    try:
        code = segno.make(url, error="m")
        # Bytes, not text: segno writes SVG as encoded bytes whatever the
        # stream, and a StringIO here raises "string argument expected".
        buffer = io.BytesIO()
        # Colours are set rather than inherited. An SVG with no colours is
        # black modules on a transparent field, which on this app's dark
        # background is a black square on a black square. Dark-on-white,
        # explicitly, because that is the contrast every scanner expects and a
        # QR is not a thing to be clever with — it either reads or it does not.
        # border=4 is the quiet zone the QR spec asks for, and it is not
        # decoration: scanners use it to find the symbol's edge, and a tighter
        # one reads fine on some phones and not at all on others. The CSS puts
        # white padding around this too, but padding measured in pixels and a
        # quiet zone measured in modules are not the same guarantee — the one
        # the spec cares about belongs in the encoder.
        code.save(buffer, kind="svg", scale=6, border=4,
                  dark="#111111", light="#ffffff", xmldecl=False, svgns=True)
        return buffer.getvalue().decode("utf-8")
    except Exception:
        return ""


def _stale(last_seen: Optional[str]) -> bool:
    if not last_seen:
        return True
    try:
        when = datetime.fromisoformat(last_seen)
    except (TypeError, ValueError):
        return True
    if not when.tzinfo:
        when = when.replace(tzinfo=timezone.utc)
    return (_now() - when).total_seconds() >= LAST_SEEN_RESOLUTION_SECONDS
