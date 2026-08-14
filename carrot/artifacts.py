"""Artifacts: things the assistant makes that are better looked at than read.

A chart, a diagram, a small interactive page, a generated image. These used to
have nowhere to go — a matplotlib figure became a file path in a tool result,
and the user had to go find it.

The security shape matters more than the feature. An artifact is, by
definition, markup the *model* wrote, and some of it (HTML, SVG) can carry
script. Rendering that inside the app's own page would hand model-authored
script the session token, the conversation history and every API route — a
prompt-injected page could quietly exfiltrate all of it.

So artifacts are never inlined into the app document. They are stored, then
served from a dedicated endpoint with a restrictive CSP and displayed in a
sandboxed iframe with `allow-scripts` but deliberately *not*
`allow-same-origin`, which puts them in an opaque origin: script inside can
animate a chart but cannot read the parent document, the session token, or
anything in storage. That is the whole reason this is a module and not three
lines of innerHTML.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from typing import Any, Dict, List, Optional

from .config import get_config

# What an artifact can be. Kept deliberately small: every kind is a rendering
# path in the UI and a decision about what script may run.
KIND_HTML = "html"          # a self-contained page; scripts allowed, sandboxed
KIND_SVG = "svg"            # vector image; sanitised, no script
KIND_MARKDOWN = "markdown"  # rendered by the existing markdown pipeline
KIND_MERMAID = "mermaid"    # diagram source
KIND_IMAGE = "image"        # a raster the model produced or generated
# A rendered animation. Its own kind rather than an image because the whole
# point of one is that it moves: a manim proof shown as its final frame is a
# diagram of the answer with the argument removed.
KIND_VIDEO = "video"
KIND_CODE = "code"          # a file worth showing whole, syntax highlighted

KINDS = {KIND_HTML, KIND_SVG, KIND_MARKDOWN, KIND_MERMAID, KIND_IMAGE,
         KIND_CODE, KIND_VIDEO}

# Big enough for a real chart or a page with inline data, small enough that a
# runaway generation cannot fill the database.
MAX_CONTENT_BYTES = 2 * 1024 * 1024
MAX_ARTIFACTS_PER_CONVERSATION = 50

# Only raster formats a browser renders natively.
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"}
# What a browser will actually play without a plugin. Manim writes mp4 by
# default and webm on request; anything else would be a file the panel shows
# a broken control for.
VIDEO_EXTENSIONS = {".mp4", ".webm"}
# Video is bigger than everything else here by an order of magnitude, and it
# is embedded as a data URI like the rest — a thirty-second animation at
# manim's default quality lands well inside this, and something that does not
# is a file to open rather than a thing to inline in a conversation.
MAX_VIDEO_BYTES = 24 * 1024 * 1024

# Stripped from SVG before it is shown. SVG is not a passive image format —
# it can carry <script>, event handlers and external references — and unlike
# HTML it is rendered inline rather than in the sandbox, so it is cleaned
# rather than isolated.
_SVG_SCRIPT = re.compile(r"<\s*script\b.*?<\s*/\s*script\s*>", re.I | re.S)
_SVG_EVENT_ATTR = re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.I)
_SVG_HREF_JS = re.compile(r"(xlink:href|href)\s*=\s*(\"|')?\s*javascript:[^\"'>]*(\"|')?", re.I)
_SVG_FOREIGN = re.compile(r"<\s*(foreignObject|iframe|embed|object)\b.*?<\s*/\s*\1\s*>", re.I | re.S)


class ArtifactError(ValueError):
    pass


def _db():
    from . import database

    return database.get_db()


def sanitize_svg(markup: str) -> str:
    """Remove the parts of SVG that are code rather than picture."""
    cleaned = _SVG_SCRIPT.sub("", markup or "")
    cleaned = _SVG_FOREIGN.sub("", cleaned)
    cleaned = _SVG_EVENT_ATTR.sub("", cleaned)
    cleaned = _SVG_HREF_JS.sub("", cleaned)
    return cleaned


def _read_workspace_image(path: str) -> Dict[str, Any]:
    """Turn a workspace-relative image path into a data URI.

    Going through the same sandbox the Code tab uses, so an artifact cannot
    name /etc/shadow and have the UI render it back.
    """
    import base64
    import mimetypes

    from .files_api import resolve

    full = resolve(path, must_exist=True)
    ext = os.path.splitext(full)[1].lower()
    if ext not in IMAGE_EXTENSIONS:
        raise ArtifactError(f"{ext or 'that file'} is not an image format the UI can show")
    size = os.path.getsize(full)
    if size > MAX_CONTENT_BYTES:
        raise ArtifactError("image is too large to show inline")
    with open(full, "rb") as handle:
        raw = handle.read()
    mime = mimetypes.guess_type(full)[0] or "application/octet-stream"
    if ext == ".svg":
        return {"content": sanitize_svg(raw.decode("utf-8", "replace")), "kind": KIND_SVG}
    encoded = base64.b64encode(raw).decode("ascii")
    return {"content": f"data:{mime};base64,{encoded}", "kind": KIND_IMAGE}


def _read_workspace_video(path: str) -> str:
    """A workspace-relative video file as a data URI.

    Through the same sandbox as the image path, for the same reason: an
    artifact must not be able to name a file outside the workspace and have
    the UI hand it back.
    """
    import base64
    import mimetypes

    from .files_api import resolve

    full = resolve(path, must_exist=True)
    ext = os.path.splitext(full)[1].lower()
    if ext not in VIDEO_EXTENSIONS:
        raise ArtifactError(
            f"{ext or 'that file'} is not a video format a browser will play — "
            "render to .mp4 or .webm")
    size = os.path.getsize(full)
    if size > MAX_VIDEO_BYTES:
        raise ArtifactError(
            f"that video is {size // (1024 * 1024)}MB, over the "
            f"{MAX_VIDEO_BYTES // (1024 * 1024)}MB limit for something shown inline. "
            "Render it at a lower quality, or make it shorter.")
    with open(full, "rb") as handle:
        raw = handle.read()
    mime = mimetypes.guess_type(full)[0] or "video/mp4"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def create(kind: str, content: str, *, title: str = "", conversation_id: str = "",
           message_id: str = "", meta: Optional[Dict[str, Any]] = None,
           path: str = "") -> Dict[str, Any]:
    """Store an artifact and return it."""
    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        raise ArtifactError(f"unknown artifact kind '{kind}' — expected one of {sorted(KINDS)}")

    # An image may arrive as a workspace path rather than inline data; that is
    # how a matplotlib figure gets here after run_command writes the PNG.
    if kind == KIND_IMAGE and path:
        loaded = _read_workspace_image(path)
        content = loaded["content"]
        kind = loaded["kind"]
    elif kind == KIND_VIDEO:
        if path:
            content = _read_workspace_video(path)
        elif not str(content).startswith("data:video/"):
            # A video arrives as a rendered file or as a data URI, and nothing
            # else. Without this, `kind=video` with any string at all produced
            # an artifact that rendered a video element pointed at nonsense —
            # a broken control where a proof should be, with no error anywhere
            # to say why.
            raise ArtifactError(
                "a video artifact needs `path` set to a rendered .mp4 or .webm in the "
                "workspace, or `content` set to a data:video/… URI")
    elif kind == KIND_SVG:
        content = sanitize_svg(content)

    if not (content or "").strip():
        raise ArtifactError("an artifact needs content")
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES:
        raise ArtifactError("artifact content is too large")

    artifact = {
        "id": uuid.uuid4().hex[:16],
        "conversation_id": conversation_id or "",
        "message_id": message_id or "",
        "kind": kind,
        "title": (title or "").strip()[:160],
        "content": content,
        "meta": meta or {},
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    conn = _db()
    conn.execute(
        "INSERT INTO artifacts (id, conversation_id, message_id, kind, title, content, meta, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (artifact["id"], artifact["conversation_id"], artifact["message_id"], artifact["kind"],
         artifact["title"], artifact["content"], json.dumps(artifact["meta"]), artifact["created_at"]),
    )
    conn.commit()
    _trim(conversation_id)
    return artifact


def _trim(conversation_id: str):
    """Keep a long conversation from accumulating megabytes of dead charts."""
    if not conversation_id:
        return
    conn = _db()
    rows = conn.execute(
        "SELECT id FROM artifacts WHERE conversation_id = ? ORDER BY created_at DESC",
        (conversation_id,),
    ).fetchall()
    stale = [row["id"] for row in rows[MAX_ARTIFACTS_PER_CONVERSATION:]]
    for artifact_id in stale:
        conn.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
    if stale:
        conn.commit()


def _row_to_dict(row) -> Dict[str, Any]:
    try:
        meta = json.loads(row["meta"] or "{}")
    except (json.JSONDecodeError, TypeError):
        meta = {}
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "message_id": row["message_id"],
        "kind": row["kind"],
        "title": row["title"],
        "content": row["content"],
        "meta": meta,
        "created_at": row["created_at"],
    }


def get(artifact_id: str) -> Optional[Dict[str, Any]]:
    row = _db().execute("SELECT * FROM artifacts WHERE id = ?", (artifact_id,)).fetchone()
    return _row_to_dict(row) if row else None


def for_conversation(conversation_id: str) -> List[Dict[str, Any]]:
    rows = _db().execute(
        "SELECT * FROM artifacts WHERE conversation_id = ? ORDER BY created_at",
        (conversation_id,),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def delete(artifact_id: str) -> bool:
    conn = _db()
    cursor = conn.execute("DELETE FROM artifacts WHERE id = ?", (artifact_id,))
    conn.commit()
    return cursor.rowcount > 0


# The page an HTML artifact is served as. The CSP is the second line of
# defence behind the iframe sandbox: even granted script, the artifact cannot
# call home, so a prompt-injected page cannot exfiltrate what it can see.
# 'unsafe-inline' and 'unsafe-eval' are present because charting libraries the
# model inlines need them, and the opaque origin is what makes that acceptable.
_CSP = (
    "default-src 'none'; "
    "script-src 'unsafe-inline' 'unsafe-eval' blob:; "
    "style-src 'unsafe-inline'; "
    "img-src data: blob:; "
    # Same shape as img-src: the video is a data URI in the document, so this
    # opens nothing to the network. Without it the element is present and
    # silently plays nothing, which reads as the render having failed.
    "media-src data: blob:; "
    "font-src data:; "
    "connect-src 'none'; "
    "form-action 'none'; "
    "base-uri 'none'"
)


def html_document(artifact: Dict[str, Any]) -> str:
    """Wrap artifact content in a standalone document for the iframe."""
    body = artifact["content"]
    if artifact["kind"] == KIND_SVG:
        body = f'<div class="svg-wrap">{body}</div>'
    elif artifact["kind"] == KIND_IMAGE:
        body = f'<img src="{artifact["content"]}" alt="{artifact.get("title") or "artifact"}">'
    elif artifact["kind"] == KIND_VIDEO:
        # Controls, and no autoplay. An animation that starts the moment it
        # appears is one you have already missed the beginning of, and a
        # proof is watched deliberately.
        body = (f'<video src="{artifact["content"]}" controls playsinline '
                f'preload="metadata"></video>')
    theme = (artifact.get("meta") or {}).get("theme", "dark")
    ink, ground = ("#1e1b14", "#faf6ed") if theme == "light" else ("#f2ece0", "#1b1a13")
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{_CSP}">
<style>
  html,body{{margin:0;padding:0;background:{ground};color:{ink};
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif}}
  body{{padding:12px;box-sizing:border-box}}
  img,svg{{max-width:100%;height:auto;display:block;margin:0 auto}}
  .svg-wrap{{display:flex;justify-content:center}}
  table{{border-collapse:collapse;width:100%}}
  th,td{{border:1px solid rgba(128,128,128,.35);padding:6px 8px;text-align:left}}
</style></head>
<body>{body}</body></html>"""


def csp_header() -> str:
    return _CSP
