"""HTTP surface for ambient capture — the policy, and now the capture itself.

The governor shipped first on purpose, and that ordering held: the capture
endpoints below cannot reach a frame except through `ambient_capture.capture_once`,
which asks `ambient.should_capture` before it does anything. There is no route
here that takes a screenshot directly.

Note what is missing: nothing serves an image. Frames are text, because the
image is dropped the moment OCR has read it, so there is no endpoint that could
hand one back.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from carrot import ambient, ambient_capture

router = APIRouter(prefix="/api/ambient", tags=["ambient"])


class PolicyRequest(BaseModel):
    policy: Dict[str, Any]


class ExclusionRequest(BaseModel):
    kind: str          # app | title | url
    value: str


class PauseRequest(BaseModel):
    minutes: Optional[float] = 60


class CheckRequest(BaseModel):
    """A hypothetical moment, for the panel's "would this be captured?" test."""
    app: Optional[str] = ""
    title: Optional[str] = ""
    url: Optional[str] = ""
    private_window: Optional[bool] = False
    secure_input: Optional[bool] = False


@router.get("")
async def state():
    return ambient.status()


@router.put("/policy")
async def set_policy(req: PolicyRequest):
    return {"policy": ambient.set_policy(req.policy)}


@router.post("/exclusions")
async def add_exclusion(req: ExclusionRequest):
    try:
        return {"policy": ambient.add_exclusion(req.kind, req.value)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/exclusions/remove")
async def remove_exclusion(req: ExclusionRequest):
    try:
        return {"policy": ambient.remove_exclusion(req.kind, req.value)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/pause")
async def pause(req: PauseRequest):
    return {"policy": ambient.pause_for(req.minutes or 60)}


@router.post("/resume")
async def resume():
    return {"policy": ambient.resume()}


@router.post("/check")
async def check(req: CheckRequest):
    """Would this window be captured? The panel's honesty test.

    Being able to try "Chase — Google Chrome" and see it refused, before
    trusting the feature with a real day, is the difference between a promise
    and a demonstration.
    """
    context = {**req.model_dump(), **ambient.probe_resources()}
    return {
        "decision": ambient.should_capture(context).as_dict(),
        "privacy": ambient.check_privacy(context).as_dict(),
        "resources": ambient.check_resources(context).as_dict(),
        "schedule": ambient.check_schedule(context).as_dict(),
    }


# ===== Capture =====


class RecallRequest(BaseModel):
    query: str
    limit: Optional[int] = 20
    since: Optional[str] = ""
    app: Optional[str] = ""
    workspace_id: Optional[str] = ""


class ForgetRequest(BaseModel):
    """Deliberately requires a target. See `forget_range`."""
    since: Optional[str] = ""
    until: Optional[str] = ""
    app: Optional[str] = ""


@router.get("/capabilities")
async def capture_capabilities():
    """What this machine can do, and what to install if it cannot.

    Read before offering the switch: a start button on a machine with no OCR
    engine is a button that fails silently every eight seconds.
    """
    return ambient_capture.capabilities()


@router.get("/status")
async def capture_status():
    return {**ambient_capture.worker.status(),
            "capabilities": ambient_capture.capabilities(),
            "policy": ambient.policy(),
            "stats": ambient_capture.stats()}


@router.post("/start")
async def start_capture():
    ready = ambient_capture.capabilities()
    if not ready["ready"]:
        raise HTTPException(
            status_code=400,
            detail="; ".join(f"{m['what']} — {m['fix']}" for m in ready["missing"]))
    # The policy's own enabled flag is the master switch. Starting the worker
    # sets it, so a restart resumes what the user actually asked for rather
    # than starting a capture loop nobody turned on.
    ambient.set_policy({"enabled": True})
    started = ambient_capture.worker.start()
    return {"started": started, **ambient_capture.worker.status()}


@router.post("/stop")
async def stop_capture():
    ambient.set_policy({"enabled": False})
    return {"stopped": ambient_capture.worker.stop()}


@router.post("/capture")
async def capture_now():
    """One frame, on request.

    Skips the cadence checks — you pressed a button, the idle timer is not
    relevant — and never the privacy or resource ones. A button that captures
    a password field on request is the same bug as one that does it
    automatically.

    Off the event loop, because a screen grab plus an OCR pass is most of a
    second on a good machine and several on a busy one. Run inline it holds
    every other request in the process for that long — the UI freezing is the
    visible half; the SSE stream stalling is the half people report as the
    model having stopped.
    """
    return await asyncio.to_thread(ambient_capture.capture_once, True)


@router.post("/recall")
async def recall(req: RecallRequest):
    return {"results": ambient_capture.recall(
        req.query, limit=req.limit or 20, since=req.since or "",
        app=req.app or "", workspace_id=req.workspace_id or "")}


@router.get("/timeline")
async def timeline(limit: int = 100, since: str = "", app: str = ""):
    return {"frames": ambient_capture.timeline(limit=limit, since=since, app=app)}


@router.get("/frames/{frame_id}")
async def frame(frame_id: str):
    found = ambient_capture.get_frame(frame_id)
    if not found:
        raise HTTPException(status_code=404, detail="no such frame")
    return found


@router.delete("/frames/{frame_id}")
async def forget_frame(frame_id: str):
    return {"forgotten": ambient_capture.forget(frame_id)}


@router.post("/forget")
async def forget_range(req: ForgetRequest):
    try:
        return {"forgotten": ambient_capture.forget_range(
            since=req.since or "", until=req.until or "", app=req.app or "")}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/frames")
async def forget_everything():
    """Everything, in one call.

    As prominent as the start button. Anything that records has to make
    deletion at least as easy, or the record is not something the user
    controls — it is something that happens to them.
    """
    return {"forgotten": ambient_capture.forget_all()}
