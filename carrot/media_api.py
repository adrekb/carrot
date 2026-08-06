"""HTTP surface for image/video generation and for dual-authentication.

Two routers, one file: both are about *how Carrot reaches a provider* rather
than about what it says to one, and both are small.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from carrot import dualauth, media
from carrot.config import get_config, set_config

router = APIRouter(prefix="/api/media", tags=["media"])
auth_router = APIRouter(prefix="/api/auth", tags=["auth"])


# ===== Media =====

class GenerateRequest(BaseModel):
    prompt: str
    kind: Optional[str] = media.KIND_IMAGE
    backend: Optional[str] = ""
    model: Optional[str] = ""
    conversation_id: Optional[str] = ""
    title: Optional[str] = ""
    negative: Optional[str] = ""
    width: Optional[int] = None
    height: Optional[int] = None
    steps: Optional[int] = None
    count: Optional[int] = 1
    size: Optional[str] = ""


class BackendKeyRequest(BaseModel):
    # Required, with no default: a field-name typo must not silently arrive as
    # an empty string and wipe a key the user already pasted.
    api_key: str


class BackendEndpointRequest(BaseModel):
    base_url: str


class DefaultBackendRequest(BaseModel):
    backend: str
    kind: Optional[str] = media.KIND_IMAGE


@router.get("")
async def list_backends(kind: str = ""):
    cfg = get_config()
    return {
        "backends": media.backends(kind),
        "default_image": cfg.get("media_backend_image", ""),
        "default_video": cfg.get("media_backend_video", ""),
        "kinds": [media.KIND_IMAGE, media.KIND_VIDEO],
    }


@router.post("/generate")
async def generate(req: GenerateRequest):
    options: Dict[str, Any] = {
        "model": req.model or "",
        "negative": req.negative or "",
        "count": req.count or 1,
    }
    for name in ("width", "height", "steps"):
        value = getattr(req, name)
        if value:
            options[name] = value
    if req.size:
        options["size"] = req.size
    try:
        return media.generate(
            req.prompt,
            kind=req.kind or media.KIND_IMAGE,
            backend=req.backend or "",
            conversation_id=req.conversation_id or "",
            title=req.title or "",
            **options,
        )
    except media.MediaError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.put("/backends/{backend_id}/key")
async def set_backend_key(backend_id: str, req: BackendKeyRequest):
    try:
        media.set_api_key(backend_id, req.api_key.strip())
    except media.MediaError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"backend": backend_id, "key_set": bool(req.api_key.strip())}


@router.put("/backends/{backend_id}/endpoint")
async def set_backend_endpoint(backend_id: str, req: BackendEndpointRequest):
    try:
        media.set_endpoint(backend_id, req.base_url.strip())
    except media.MediaError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"backend": backend_id, "base_url": media.base_url(backend_id)}


@router.put("/default")
async def set_default_backend(req: DefaultBackendRequest):
    kind = req.kind or media.KIND_IMAGE
    if req.backend and req.backend not in media.BACKENDS:
        raise HTTPException(status_code=404, detail=f"unknown backend: {req.backend}")
    if req.backend and kind not in media.BACKENDS[req.backend]["kinds"]:
        raise HTTPException(
            status_code=400,
            detail=f"{media.BACKENDS[req.backend]['label']} cannot generate {kind}",
        )
    set_config(
        "media_backend_video" if kind == media.KIND_VIDEO else "media_backend_image",
        req.backend,
    )
    return {"kind": kind, "backend": req.backend}


# ===== Dual authentication =====

class ModeRequest(BaseModel):
    mode: str


class OAuthClientRequest(BaseModel):
    client_id: Optional[str] = None
    authorize_url: Optional[str] = None
    token_url: Optional[str] = None
    scopes: Optional[str] = None
    redirect_uri: Optional[str] = None


@auth_router.get("/status")
async def auth_status():
    return dualauth.all_status()


@auth_router.get("/status/{provider_id}")
async def provider_auth_status(provider_id: str):
    try:
        return dualauth.status(provider_id)
    except dualauth.AuthError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@auth_router.put("/mode/{provider_id}")
async def set_auth_mode(provider_id: str, req: ModeRequest):
    try:
        dualauth.set_mode(provider_id, req.mode)
    except dualauth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return dualauth.status(provider_id)


@auth_router.put("/oauth/{provider_id}")
async def set_oauth_client(provider_id: str, req: OAuthClientRequest):
    try:
        dualauth.set_oauth_config(provider_id, **req.model_dump(exclude_none=True))
    except dualauth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return dualauth.status(provider_id)


@auth_router.post("/login/{provider_id}")
async def begin_login(provider_id: str):
    """Hand back the URL to open. The shell opens it in the system browser."""
    try:
        return dualauth.begin_login(provider_id)
    except dualauth.AuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@auth_router.get("/callback", response_class=HTMLResponse)
async def oauth_callback(code: str = "", state: str = "", error: str = "",
                         error_description: str = ""):
    """Where the provider sends the browser back to.

    This renders a page rather than JSON because a human is looking at it: the
    tab they were sent to has to say whether it worked and that they can close
    it. Reachable without a session token, but useless without a `state` this
    process is holding.
    """
    if error:
        return _callback_page(False, error_description or error)
    try:
        dualauth.complete_login(state, code)
    except dualauth.AuthError as exc:
        return _callback_page(False, str(exc))
    return _callback_page(True, "You're signed in. You can close this tab.")


def _callback_page(ok: bool, message: str) -> HTMLResponse:
    from html import escape

    colour = "#57d1a0" if ok else "#f2736b"
    return HTMLResponse(
        f"""<!doctype html><meta charset="utf-8"><title>Carrot</title>
<body style="margin:0;display:grid;place-items:center;height:100vh;
background:#131419;color:#eceef4;font-family:system-ui,sans-serif">
<div style="text-align:center;max-width:32rem;padding:2rem">
<div style="font-size:2rem;color:{colour}">{'&#10003;' if ok else '&#10007;'}</div>
<h1 style="font-size:1.1rem;font-weight:600">{'Signed in' if ok else 'Sign-in failed'}</h1>
<p style="color:#99a0ae;line-height:1.6">{escape(message)}</p>
</div></body>""",
        status_code=200 if ok else 400,
    )


@auth_router.post("/logout/{provider_id}")
async def sign_out(provider_id: str):
    return {"signed_out": dualauth.sign_out(provider_id)}
