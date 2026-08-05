"""HTTP surface for local webhooks.

Two audiences, and they need opposite things. The management endpoints are for
the app's own settings page and are session-authenticated like everything else.
The *firing* endpoint is for Home Assistant and a Stream Deck, which have no
session and cannot be given one — it authenticates with the hook's own token
instead, and is deliberately the only unauthenticated route in the app besides
the OAuth callback.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from carrot import webhooks

# Management: session-authenticated with the rest of the API.
router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])
# Firing: token-authenticated, reachable from your own network.
public_router = APIRouter(prefix="/api/hooks", tags=["webhooks"])


class EnableRequest(BaseModel):
    enabled: bool


class CreateRequest(BaseModel):
    id: str
    action: str
    label: Optional[str] = ""
    defaults: Optional[Dict[str, Any]] = None


class TargetRequest(BaseModel):
    url: str
    events: Optional[List[str]] = None
    label: Optional[str] = ""


@router.get("")
async def state():
    return {
        "enabled": webhooks.enabled(),
        "hooks": webhooks.list_hooks(),
        "actions": [{"id": k, "description": v} for k, v in webhooks.ACTIONS.items()],
        "targets": webhooks.outbound_targets(),
        "rate_limit": webhooks.RATE_LIMIT_PER_MINUTE,
    }


@router.put("/enabled")
async def set_enabled(req: EnableRequest):
    return {"enabled": webhooks.set_enabled(req.enabled)}


@router.post("/hooks")
async def create(req: CreateRequest):
    """Create a hook. This is the only response that ever carries the token."""
    try:
        return webhooks.create_hook(req.id, req.action, req.label or "", req.defaults)
    except webhooks.WebhookError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/hooks/{hook_id}")
async def delete(hook_id: str):
    if not webhooks.delete_hook(hook_id):
        raise HTTPException(status_code=404, detail="no such hook")
    return {"deleted": hook_id}


@router.post("/hooks/{hook_id}/rotate")
async def rotate(hook_id: str):
    try:
        return webhooks.rotate_token(hook_id)
    except webhooks.WebhookError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/targets")
async def add_target(req: TargetRequest):
    try:
        return webhooks.add_target(req.url, req.events, req.label or "")
    except webhooks.WebhookError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/targets/{target_id}")
async def remove_target(target_id: str):
    if not webhooks.remove_target(target_id):
        raise HTTPException(status_code=404, detail="no such target")
    return {"deleted": target_id}


# ===== Firing =====

@public_router.post("/{hook_id}")
async def fire(hook_id: str, request: Request,
               authorization: str = Header(default=""),
               x_carrot_token: str = Header(default="")):
    """Run a hook. Authenticated by the hook's own token, not a session.

    The token may arrive as a Bearer header, an X-Carrot-Token header, or a
    `token` field in the body — Home Assistant, curl and Shortcuts each find a
    different one of those easiest, and refusing two of the three would just
    make the feature look broken.
    """
    body: Dict[str, Any] = {}
    try:
        parsed = await request.json()
        if isinstance(parsed, dict):
            body = parsed
    except Exception:
        body = {}

    # All three, not the first non-empty one: the app's own session header can
    # be on the same request, and it would otherwise shadow a token in the body.
    candidates = [
        authorization[7:].strip() if authorization.lower().startswith("bearer ") else "",
        x_carrot_token.strip(),
        str(body.pop("token", "") or "").strip(),
    ]

    try:
        hook = webhooks.authenticate(hook_id, *candidates)
        webhooks.check_rate(hook_id)
    except webhooks.WebhookError as exc:
        message = str(exc)
        # A wrong token and an unknown hook give the same 401 and the same
        # words: telling a caller that the hook exists is telling them what to
        # keep guessing at.
        if "fired" in message:
            raise HTTPException(status_code=429, detail=message)
        raise HTTPException(status_code=401, detail=message)

    try:
        return webhooks.fire(hook, body)
    except webhooks.WebhookError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@public_router.get("/{hook_id}")
async def fire_via_get(hook_id: str, token: str = "",
                       authorization: str = Header(default="")):
    """Some automation tools can only make a GET. This is that door.

    Kept deliberately minimal: no body, so it suits `brief` and hooks that
    carry their whole configuration in their defaults.
    """
    candidates = [
        token,
        authorization[7:].strip() if authorization.lower().startswith("bearer ") else "",
    ]
    try:
        hook = webhooks.authenticate(hook_id, *candidates)
        webhooks.check_rate(hook_id)
    except webhooks.WebhookError as exc:
        message = str(exc)
        if "fired" in message:
            raise HTTPException(status_code=429, detail=message)
        raise HTTPException(status_code=401, detail=message)
    try:
        return webhooks.fire(hook, {})
    except webhooks.WebhookError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
