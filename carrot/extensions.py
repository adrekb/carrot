"""Extension packs — bundles of skills, tools and settings you turn on as a unit.

A pack is the unit a user actually thinks in. "I do academic writing" should be
one switch, not a hunt through a tool list for the eight things that serve it.
So a pack ships:

* **tools** the chat agent can call, on the same approval gate as the built-ins
* **skills** — instruction packs written to the ordinary skills directory, so
  they show up under ``/`` in the command bar like any other skill
* **settings** the pack's tools read (citation style, target venue, and so on)
* **capabilities** — the external programs its tools would like to have

That last one is the honest part. A pack can want LaTeX, MATLAB or a CAD kernel
without any of them being installed, and pretending otherwise produces a tool
that fails halfway through a task. Instead every capability is probed, the UI
says plainly what is present, and a tool whose program is missing refuses up
front with the reason and how to install it.

Packs are code-defined and ship with Carrot. Enabling one installs its skills;
disabling removes them and takes its tools out of the agent's reach.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import Any, Callable, Dict, List, Optional

from . import agent_tools, skills as skills_mod
from .config import get_config, set_config

LOG = logging.getLogger(__name__)

# Pack tools are namespaced so the chat loop can route a call by prefix alone,
# the same way it separates built-ins from MCP.
TOOL_PREFIX = "ext"

_PACKS: Dict[str, "Pack"] = {}


class Pack:
    """One installable bundle."""

    def __init__(
        self,
        pack_id: str,
        name: str,
        description: str,
        version: str = "1.0",
        tools: Optional[Dict[str, Dict[str, Any]]] = None,
        skills: Any = None,
        capabilities: Optional[List[Dict[str, Any]]] = None,
        settings: Optional[List[Dict[str, Any]]] = None,
        default_enabled: bool = False,
        tabs: Optional[List[str]] = None,
        tutorial: Optional[List[Dict[str, str]]] = None,
    ):
        self.id = pack_id
        self.name = name
        self.description = description
        self.version = version
        # Nav tabs this pack provides, hidden until it is enabled.
        #
        # Packs were tools, skills and settings — things the *model* reaches
        # for. A tab is the first thing a pack has contributed that the user
        # reaches for, and it is the same idea: a whole feature you either do
        # or do not want, as one switch. The Planner is the case that made
        # this necessary — a semester scheduler is not something most people
        # installing a local assistant are ever going to open, and it sat in
        # the sidebar between Research and Goals for all of them.
        self.tabs = tabs or []
        # How to actually use the thing, in steps.
        #
        # A pack's card lists its tools and its skills, which tells you what it
        # *has* and not what to do with it. Reading "latex_compile, bib_check,
        # matlab_run" and knowing where to begin is a different skill from
        # wanting the pack in the first place, and the gap was left to the
        # user: switch it on, then work out what changed.
        #
        # Deliberately steps rather than prose. A paragraph explaining a pack
        # is documentation; a numbered list of things to try is the shortest
        # path to the first moment it does something useful, which is the only
        # moment that decides whether the switch stays on.
        self.tutorial = tutorial or []
        self.tools = tools or {}
        # A pack whose skill text depends on its settings passes a callable, so
        # changing the target venue rewrites the instructions rather than
        # leaving text that was frozen at import time.
        self._skills = skills or []
        self.capabilities = capabilities or []
        self.settings = settings or []
        self.default_enabled = default_enabled

    @property
    def skills(self) -> List[Dict[str, str]]:
        return list(self._skills() if callable(self._skills) else self._skills)

    def as_dict(self, deep: bool = False) -> Dict[str, Any]:
        summary = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "enabled": is_enabled(self.id),
            "tool_count": len(self.tools),
            "skill_count": len(self.skills),
            "tabs": list(self.tabs),
            "has_tutorial": bool(self.tutorial),
        }
        if not deep:
            return summary
        return {
            **summary,
            "tools": [
                {"name": name, "description": spec["description"],
                 "mutating": spec.get("mutating", False),
                 "risk": spec.get("risk", "low"),
                 "requires": spec.get("requires", "")}
                for name, spec in self.tools.items()
            ],
            "skills": [
                {"slug": s["slug"], "name": s["name"], "description": s["description"]}
                for s in self.skills
            ],
            "tutorial": list(self.tutorial),
            "capabilities": [probe_capability(c) for c in self.capabilities],
            "settings": [
                {**spec, "value": pack_setting(self.id, spec["key"], spec.get("default"))}
                for spec in self.settings
            ],
        }


def register(pack: Pack) -> Pack:
    _PACKS[pack.id] = pack
    return pack


def list_packs(deep: bool = False) -> List[Dict[str, Any]]:
    return [pack.as_dict(deep=deep) for pack in _PACKS.values()]


def get_pack(pack_id: str) -> Optional[Pack]:
    return _PACKS.get(pack_id)


def require_pack(pack_id: str) -> Pack:
    pack = get_pack(pack_id)
    if pack is None:
        raise ValueError(f"unknown extension: {pack_id}")
    return pack


# ===== Enablement =====

def enabled_ids() -> List[str]:
    configured = get_config().get("enabled_extensions")
    if configured is None:
        return [pack.id for pack in _PACKS.values() if pack.default_enabled]
    return [pid for pid in configured if pid in _PACKS]


def is_enabled(pack_id: str) -> bool:
    return pack_id in enabled_ids()


def pack_tabs() -> Dict[str, Any]:
    """Which pack-provided tabs exist, and which are currently on.

    The browser needs both. It knows the full nav markup at load time and has
    to hide the tabs whose pack is off — so it needs the complete list of
    *managed* tabs as well as the enabled subset, or a tab belonging to a
    disabled pack would be indistinguishable from an ordinary one and would
    stay visible.
    """
    managed: Dict[str, str] = {}
    enabled: List[str] = []
    for pack in _PACKS.values():
        on = is_enabled(pack.id)
        for tab in pack.tabs:
            managed[tab] = pack.id
            if on:
                enabled.append(tab)
    return {"managed": managed, "enabled": enabled}


def set_enabled(pack_id: str, enabled: bool) -> Dict[str, Any]:
    """Turn a pack on or off, installing or removing its skills to match."""
    pack = require_pack(pack_id)
    current = set(enabled_ids())
    if enabled:
        current.add(pack_id)
        install_skills(pack)
    else:
        current.discard(pack_id)
        remove_skills(pack)
    set_config("enabled_extensions", sorted(current))
    return pack.as_dict(deep=True)


def install_skills(pack: Pack):
    """Write the pack's skills into the ordinary skills directory.

    They deliberately become normal skills rather than a parallel concept: the
    command bar, the chat pipeline and the skill editor already work, and a user
    who wants to tweak the pack's wording should just be able to.
    """
    for skill in pack.skills:
        skills_mod.save_skill(
            name=skill["name"],
            description=skill["description"],
            instructions=skill["instructions"],
            slug=skill["slug"],
        )


def remove_skills(pack: Pack):
    for skill in pack.skills:
        skills_mod.delete_skill(skill["slug"])


def sync_enabled_skills():
    """Re-install skills for every enabled pack. Safe to call on startup."""
    for pack_id in enabled_ids():
        pack = get_pack(pack_id)
        if pack:
            install_skills(pack)


# ===== Settings =====

def pack_setting(pack_id: str, key: str, default: Any = None) -> Any:
    stored = get_config().get("extension_settings", {})
    if not isinstance(stored, dict):
        return default
    return stored.get(pack_id, {}).get(key, default)


def set_pack_setting(pack_id: str, key: str, value: Any) -> Dict[str, Any]:
    pack = require_pack(pack_id)
    if not any(spec["key"] == key for spec in pack.settings):
        raise ValueError(f"{pack_id} has no setting called '{key}'")
    stored = dict(get_config().get("extension_settings", {}) or {})
    for_pack = dict(stored.get(pack_id, {}))
    for_pack[key] = value
    stored[pack_id] = for_pack
    set_config("extension_settings", stored)
    # Skills built from settings have to be rewritten, or the user changes the
    # venue and the instructions still name the old one.
    if is_enabled(pack_id):
        install_skills(pack)
    return for_pack


# ===== Capabilities =====

def probe_capability(capability: Dict[str, Any]) -> Dict[str, Any]:
    """Look for one capability and report what was found.

    Probing is a ``shutil.which`` by default. A capability may name several
    alternatives — any TeX engine will do — and the first one present wins.

    A capability may instead supply a ``check`` callable, for the ones that are
    not a program on PATH. Ambient Recall wants a Python import and, on
    Windows, an OCR engine that lives inside the operating system rather than
    in a binary anyone could point at — neither of which `which` can answer.
    The rest of the contract is unchanged, so the UI does not need to know
    which kind it is looking at.

    A check that raises is treated as absent rather than allowed to propagate.
    These run to render a settings card, and a probe that throws would take the
    whole Extensions tab down to report that a library is missing.
    """
    check = capability.get("check")
    if callable(check):
        try:
            available = bool(check())
        except Exception:
            LOG.debug("capability check failed for %s", capability.get("id"), exc_info=True)
            available = False
        return {
            "id": capability["id"],
            "label": capability.get("label") or capability.get("name", ""),
            "binaries": [],
            "available": available,
            "path": "",
            "purpose": capability.get("purpose") or capability.get("why", ""),
            "install_hint": capability.get("install_hint") or capability.get("install", ""),
        }

    binaries = capability.get("binaries") or [capability.get("binary", "")]
    found = next((shutil.which(b) for b in binaries if b and shutil.which(b)), None)
    return {
        "id": capability["id"],
        "label": capability["label"],
        "binaries": [b for b in binaries if b],
        "available": bool(found),
        "path": found or "",
        "purpose": capability.get("purpose", ""),
        "install_hint": capability.get("install_hint", ""),
    }


def capability_status(capability_id: str) -> Optional[Dict[str, Any]]:
    for pack in _PACKS.values():
        for capability in pack.capabilities:
            if capability["id"] == capability_id:
                return probe_capability(capability)
    return None


def require_capability(capability_id: str) -> Dict[str, Any]:
    """Resolve a capability or raise the reason it cannot be used, verbatim.

    Tools call this first so a missing program produces one clear sentence
    instead of a confusing failure part-way through the work.
    """
    status = capability_status(capability_id)
    if status is None:
        raise RuntimeError(f"unknown capability: {capability_id}")
    if not status["available"]:
        hint = f" {status['install_hint']}" if status["install_hint"] else ""
        raise RuntimeError(
            f"{status['label']} is not installed on this machine.{hint}"
        )
    return status


def run_capability(
    capability_id: str, args: List[str], cwd: Optional[str] = None, timeout: int = 120
) -> subprocess.CompletedProcess:
    """Run an external program a capability resolved to."""
    status = require_capability(capability_id)
    return subprocess.run(
        [status["path"], *args],
        cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False,
    )


# ===== Tools =====

def namespaced(pack_id: str, tool: str) -> str:
    return f"{TOOL_PREFIX}__{pack_id}__{tool}"


def is_extension_tool(name: str) -> bool:
    return name.startswith(f"{TOOL_PREFIX}__")


def _split(namespaced_name: str):
    parts = namespaced_name.split("__", 2)
    return (parts[1], parts[2]) if len(parts) == 3 else ("", "")


def ollama_tools() -> List[Dict[str, Any]]:
    """Every enabled pack's tools, in the agent's function-calling schema."""
    if not get_config().get("agent_tools_enabled", True):
        return []
    tools = []
    for pack_id in enabled_ids():
        pack = _PACKS[pack_id]
        for name, spec in pack.tools.items():
            tools.append({
                "type": "function",
                "function": {
                    "name": namespaced(pack_id, name),
                    "description": spec["description"],
                    "parameters": spec["parameters"],
                },
            })
    return tools


def call(
    namespaced_name: str,
    arguments: Dict[str, Any],
    conversation_id: Optional[str] = None,
    emit: Optional[Callable] = None,
) -> str:
    """Run one pack tool, on the same approval gate as a built-in."""
    pack_id, tool = _split(namespaced_name)
    pack = get_pack(pack_id)
    if pack is None or tool not in pack.tools:
        return f"error: unknown tool {namespaced_name}"
    if not is_enabled(pack_id):
        return f"error: the {pack.name} extension is not enabled"

    spec = pack.tools[tool]
    # Check for the external program *before* the approval prompt. Asking the
    # user to approve running MATLAB and only then telling them MATLAB is not
    # installed wastes the one interaction the gate is there to protect.
    if spec.get("requires"):
        try:
            require_capability(spec["requires"])
        except RuntimeError as exc:
            return f"error: {exc}"

    return agent_tools.run_tool(f"{pack_id}.{tool}", spec, arguments, conversation_id, emit)


# Registering the bundled packs at import time keeps discovery in one place.
from .packs import academia  # noqa: E402,F401  (registers itself)
from .packs import planner  # noqa: E402,F401  (registers itself)
from .packs import ambient  # noqa: E402,F401  (registers itself)
