"""HTTP surface for the coding agent: modes, checkpoints, recipes, git.

Kept apart from ``app.py`` for the same reason ``files_api`` is: the Code tab's
concerns are self-contained, and a 3000-line module is not improved by another
two hundred lines of it.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from carrot import coder as coder_mod, gitops
from carrot.agent_tools import workspace_root
from carrot.config import get_config, set_config

router = APIRouter(prefix="/api/coder", tags=["coder"])


class ModeRequest(BaseModel):
    mode: str
    # Switching to Act from a planning conversation compacts that conversation
    # into an implementation brief instead of dragging the transcript along.
    conversation_id: Optional[str] = None
    compact: Optional[bool] = True


class CheckpointRequest(BaseModel):
    label: Optional[str] = ""
    conversation_id: Optional[str] = None


class RecipeRequest(BaseModel):
    id: str
    title: Optional[str] = ""
    prompt: str
    description: Optional[str] = ""
    parameters: Optional[List[Dict[str, Any]]] = None
    mode: Optional[str] = coder_mod.MODE_PLAN
    tools: Optional[List[str]] = None


class RunRecipeRequest(BaseModel):
    values: Optional[Dict[str, Any]] = None


class CommitRequest(BaseModel):
    message: str
    paths: Optional[List[str]] = None


class BranchRequest(BaseModel):
    name: str
    checkout: Optional[bool] = True


# ===== Mode =====

@router.get("/state")
async def state():
    """Everything the Code tab needs to draw its agent header in one call."""
    root = workspace_root()
    mode = coder_mod.normalize_mode(get_config().get("coder_mode"))
    rules = coder_mod.load_rules(root)
    payload: Dict[str, Any] = {
        "root": root,
        "mode": mode,
        "modes": list(coder_mod.MODES),
        "guidance": coder_mod.MODE_PREAMBLE[mode],
        "has_rules": bool(rules),
        "rules_chars": len(rules),
        "recipes": coder_mod.recipes(),
        "checkpoints": coder_mod.list_checkpoints(10),
        "git": {"available": gitops.git_available(), "repo": False},
    }
    if payload["git"]["available"] and gitops.is_repo(root):
        try:
            payload["git"] = {"available": True, "repo": True, **gitops.status(root)}
        except gitops.GitError as exc:
            payload["git"] = {"available": True, "repo": True, "error": str(exc)}
    return payload


@router.put("/mode")
async def set_mode(req: ModeRequest):
    mode = coder_mod.normalize_mode(req.mode)
    if mode != (req.mode or "").strip().lower():
        raise HTTPException(status_code=400, detail=f"unknown mode: {req.mode}")
    previous = coder_mod.normalize_mode(get_config().get("coder_mode"))
    set_config("coder_mode", mode)

    compacted = ""
    if previous == coder_mod.MODE_PLAN and mode == coder_mod.MODE_ACT and req.compact:
        compacted = _compact_for(req.conversation_id or "")
    # Going back to Plan discards the brief: it described a plan that is now
    # being reconsidered, and carrying it into the new plan would anchor it.
    if mode == coder_mod.MODE_PLAN:
        coder_mod.clear_snapshot(req.conversation_id or "")

    return {
        "mode": mode,
        "guidance": coder_mod.MODE_PREAMBLE[mode],
        "compacted": bool(compacted),
        "brief": compacted,
    }


def _compact_for(conversation_id: str) -> str:
    """Compress the planning conversation into an implementation brief.

    Never raises: a failed compaction falls back to the old behaviour of
    carrying the history, which is worse but not broken. Being stuck in Plan
    mode because a summariser hiccuped would be far worse than either.
    """
    if not conversation_id:
        return ""
    try:
        from carrot import conversation as conv_mod, router as router_mod

        conversation = conv_mod.get_conversation(conversation_id) or {}
        history = conversation.get("messages") or []
        if len(coder_mod.plan_messages(history)) < 2:
            return ""  # Nothing was planned; there is nothing to compact.
        resolved = router_mod.route(task="summarize")
        brief = coder_mod.compact_plan(history, resolved, router_mod.stream_events)
    except Exception:
        return ""
    if brief:
        coder_mod.store_snapshot(conversation_id, brief)
    return brief


@router.get("/brief/{conversation_id}")
async def get_brief(conversation_id: str):
    return {"brief": coder_mod.snapshot_for(conversation_id)}


@router.get("/rules")
async def rules():
    text = coder_mod.load_rules(workspace_root())
    return {"rules": text, "files": list(coder_mod.RULE_FILES)}


# ===== Worktrees =====
#
# "Try this refactor" and "keep working" are the same directory otherwise, so
# the agent's edits land on top of whatever you had open and undoing them
# means undoing yours too.

class WorktreeRequest(BaseModel):
    branch: str
    path: str = ""
    # Switching is the point — a worktree you have to go and open by hand is a
    # directory, not a feature — but it is still a separate decision from
    # making one, and a caller scripting this may not want it.
    switch: bool = True


@router.get("/worktrees")
async def list_worktrees():
    from carrot import gitops

    root = workspace_root()
    if not gitops.is_repo(root):
        return {"worktrees": [], "repo": False, "current": root}
    try:
        return {"worktrees": gitops.worktrees(root), "repo": True, "current": root}
    except gitops.GitError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/worktrees")
async def create_worktree(req: WorktreeRequest):
    from carrot import gitops
    from carrot.files_api import set_files_root, RootRequest

    try:
        made = gitops.add_worktree(workspace_root(), req.branch, req.path)
    except gitops.GitError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if req.switch:
        await set_files_root(RootRequest(root=made["path"]))
    return {**made, "switched": req.switch}


@router.delete("/worktrees")
async def drop_worktree(path: str):
    from carrot import gitops

    try:
        return gitops.remove_worktree(workspace_root(), path)
    except gitops.GitError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ===== Tasks that run on a schedule =====
#
# In the coder API because the work they do is coding work — "what changed in
# the repo yesterday" is a question about a workspace — and because the Code
# tab is where they are listed and switched off.

class ScheduledTaskRequest(BaseModel):
    prompt: str
    schedule: str = "daily"
    at: str = "09:00"
    weekday: str = "monday"


class ScheduledTaskPatch(BaseModel):
    prompt: Optional[str] = None
    schedule: Optional[str] = None
    at: Optional[str] = None
    weekday: Optional[str] = None
    enabled: Optional[bool] = None


@router.get("/scheduled")
async def list_scheduled():
    from carrot import scheduled as scheduled_mod

    return {"tasks": scheduled_mod.list_tasks()}


@router.post("/scheduled")
async def create_scheduled(req: ScheduledTaskRequest):
    from carrot import scheduled as scheduled_mod

    try:
        return scheduled_mod.create(req.prompt, req.schedule, req.at, req.weekday)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.patch("/scheduled/{task_id}")
async def patch_scheduled(task_id: str, req: ScheduledTaskPatch):
    from carrot import scheduled as scheduled_mod

    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    task = scheduled_mod.update(task_id, **fields)
    if not task:
        raise HTTPException(status_code=404, detail="no such scheduled task")
    return task


@router.delete("/scheduled/{task_id}")
async def delete_scheduled(task_id: str):
    from carrot import scheduled as scheduled_mod

    if not scheduled_mod.delete(task_id):
        raise HTTPException(status_code=404, detail="no such scheduled task")
    return {"deleted": task_id}


@router.post("/scheduled/{task_id}/run")
async def run_scheduled_now(task_id: str):
    """Run it this second, without waiting for its slot.

    The only way to find out whether a task you have written does what you
    meant is to run it, and waiting until 09:00 tomorrow to discover it was
    phrased badly is not a feedback loop anyone will use.
    """
    from carrot import scheduled as scheduled_mod

    task = scheduled_mod.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="no such scheduled task")
    return scheduled_mod.run_task(task)


# ===== Servers the agent left running =====
#
# The panel needs to be able to answer "what is running right now" without
# having watched the stream that started it. A server outlives the turn that
# started it, and frequently the conversation too — reloading the page must
# not lose the user's only handle on a process holding one of their ports.

@router.get("/servers")
async def list_servers():
    from carrot import servers as servers_mod

    return {"servers": servers_mod.list_servers()}


@router.get("/servers/{server_id}/logs")
async def server_logs(server_id: str, lines: int = 200):
    from carrot import servers as servers_mod

    result = servers_mod.logs(server_id, lines=lines)
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.post("/servers/{server_id}/stop")
async def stop_server(server_id: str):
    from carrot import servers as servers_mod

    result = servers_mod.stop(server_id)
    if result.get("error"):
        raise HTTPException(status_code=404, detail=result["error"])
    return result


# ===== Checkpoints =====

@router.get("/checkpoints")
async def checkpoints():
    return {"checkpoints": coder_mod.list_checkpoints()}


@router.post("/checkpoints")
async def create_checkpoint(req: CheckpointRequest):
    return coder_mod.create_checkpoint(workspace_root(), req.label or "", req.conversation_id)


@router.post("/checkpoints/{checkpoint_id}/restore")
async def restore_checkpoint(checkpoint_id: str):
    try:
        return coder_mod.restore_checkpoint(checkpoint_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.delete("/checkpoints/{checkpoint_id}")
async def delete_checkpoint(checkpoint_id: str):
    if not coder_mod.delete_checkpoint(checkpoint_id):
        raise HTTPException(status_code=404, detail="no such checkpoint")
    return {"deleted": checkpoint_id}


# ===== Recipes =====

@router.get("/recipes")
async def list_recipes():
    return {"recipes": coder_mod.recipes()}


@router.put("/recipes")
async def save_recipe(req: RecipeRequest):
    try:
        return coder_mod.save_recipe(
            req.id, req.title or req.id, req.prompt, req.description or "",
            req.parameters, req.mode or coder_mod.MODE_PLAN, req.tools,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/recipes/{recipe_id}")
async def delete_recipe(recipe_id: str):
    if not coder_mod.delete_recipe(recipe_id):
        raise HTTPException(status_code=404, detail="no such recipe")
    return {"deleted": recipe_id}


@router.post("/recipes/{recipe_id}/render")
async def render_recipe(recipe_id: str, req: RunRecipeRequest):
    """Fill in a recipe's parameters and hand back the prompt to send."""
    try:
        prompt = coder_mod.render_recipe(recipe_id, req.values or {})
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    recipe = coder_mod.get_recipe(recipe_id) or {}
    return {"prompt": prompt, "mode": recipe.get("mode", coder_mod.MODE_PLAN)}


# ===== Git =====

@router.get("/git/status")
async def git_status():
    try:
        return gitops.status(workspace_root())
    except gitops.GitError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/git/diff")
async def git_diff(path: str = "", staged: bool = False):
    try:
        return {"diff": gitops.diff(workspace_root(), path, staged)}
    except gitops.GitError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/git/log")
async def git_log(limit: int = 15):
    try:
        return {"commits": gitops.log(workspace_root(), limit)}
    except gitops.GitError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/git/branches")
async def git_branches():
    try:
        return gitops.branches(workspace_root())
    except gitops.GitError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/git/commit")
async def git_commit(req: CommitRequest):
    try:
        return gitops.commit(workspace_root(), req.message, req.paths)
    except gitops.GitError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/git/branch")
async def git_branch(req: BranchRequest):
    try:
        return {"message": gitops.create_branch(workspace_root(), req.name, bool(req.checkout))}
    except gitops.GitError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/git/checkout")
async def git_checkout(req: BranchRequest):
    try:
        return {"message": gitops.checkout(workspace_root(), req.name)}
    except gitops.GitError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
