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
    set_config("coder_mode", mode)
    return {"mode": mode, "guidance": coder_mod.MODE_PREAMBLE[mode]}


@router.get("/rules")
async def rules():
    text = coder_mod.load_rules(workspace_root())
    return {"rules": text, "files": list(coder_mod.RULE_FILES)}


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
