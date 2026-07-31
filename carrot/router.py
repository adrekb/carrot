"""Task-aware model routing.

Local-first should not mean local-only. Every model call in Carrot names the
*task* it is performing, and the router maps that task to a model — a small
local model for classification, embedding and summarization, a larger one for
chat, and optionally a frontier model for the work a 4B model genuinely cannot
do. Which model actually ran is returned with every route so the UI can show it,
and nothing escalates to the cloud unless the user turned it on.

Local calls go to Ollama. Cloud calls go to the Anthropic API through the
official SDK, imported lazily so a local-only install never needs it.

The cloud path emits the same event shape as ``OllamaClient.chat_stream_events``
(``{'type': 'thinking'|'content'|'tool_calls'}``) so the agentic chat loop is
provider-agnostic and only has to be written once.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from typing import Any, Dict, Generator, List, Optional

from .config import get_config, set_config

PROVIDER_LOCAL = "ollama"
PROVIDER_CLOUD = "anthropic"

# Tasks the rest of Carrot routes by.
TASK_CHAT = "chat"
TASK_CODE = "code"
TASK_REASONING = "reasoning"
TASK_CLASSIFY = "classify"
TASK_SUMMARIZE = "summarize"
TASK_EXTRACT = "extract"
TASK_RECAP = "recap"

TASKS = (
    TASK_CHAT, TASK_CODE, TASK_REASONING, TASK_CLASSIFY,
    TASK_SUMMARIZE, TASK_EXTRACT, TASK_RECAP,
)

# Cheap, high-volume tasks that should never escalate — they run on every
# message and would make cloud routing expensive and slow for no quality gain.
LOCAL_ONLY_TASKS = frozenset({TASK_CLASSIFY, TASK_EXTRACT, TASK_SUMMARIZE})

DEFAULT_CLOUD_MODEL = "claude-opus-5"
DEFAULT_CLOUD_EFFORT = "high"

# Streaming gets a large ceiling (no HTTP timeout concern); non-streaming stays
# conservative so a slow response cannot outlive the request.
CLOUD_MAX_TOKENS_STREAM = 64000
CLOUD_MAX_TOKENS_SYNC = 16000

# Server-side refusal fallback: on a policy decline the API re-runs the request
# on Anthropic's recommended fallback model instead of returning the refusal.
CLOUD_BETAS = ["server-side-fallback-2026-07-01"]

# Local model suggestions by available RAM. Bigger weights need headroom beyond
# the file size for context, so the thresholds are deliberately generous.
HARDWARE_TIERS = [
    (64, "qwen3:32b", "64GB+ of RAM comfortably runs a 32B model"),
    (32, "qwen3:14b", "32GB of RAM fits a 14B model with room for context"),
    (16, "gemma3:12b", "16GB of RAM fits a 12B model"),
    (8, "llama3.2:3b", "8GB of RAM is best served by a 3B model"),
    (0, "llama3.2:1b", "under 8GB of RAM needs the smallest model"),
]


@dataclass
class Route:
    """The resolved destination for one model call."""

    task: str
    provider: str
    model: str
    reason: str
    effort: Optional[str] = None

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ===== Configuration =====

def cloud_api_key() -> str:
    """The Anthropic key, from config or the environment."""
    return get_config().get("cloud_api_key", "") or os.environ.get("ANTHROPIC_API_KEY", "")


def cloud_enabled() -> bool:
    config = get_config()
    return bool(config.get("cloud_enabled", False)) and bool(cloud_api_key())


def cloud_tasks() -> List[str]:
    """Tasks the user has opted into escalating."""
    configured = get_config().get("cloud_tasks", [TASK_REASONING, TASK_CODE])
    return [t for t in configured if t in TASKS and t not in LOCAL_ONLY_TASKS]


def local_model(task: str) -> str:
    """The local model for a task, falling back to the configured default."""
    config = get_config()
    routes = config.get("model_routes", {})
    if isinstance(routes, dict) and routes.get(task):
        return routes[task]
    return config.get("ollama_model", "gemma4:e4b")


def set_route(task: str, model: str) -> Dict[str, str]:
    if task not in TASKS:
        raise ValueError(f"unknown task: {task}")
    routes = dict(get_config().get("model_routes", {}) or {})
    routes[task] = model
    set_config("model_routes", routes)
    return routes


# ===== Hardware-aware auto-pick =====

def recommend_local_model() -> Dict[str, Any]:
    """Suggest a local model that fits this machine's memory."""
    try:
        from .leaderboard import get_hardware_profile

        profile = get_hardware_profile()
        ram_gb = float(profile.get("ram_gb") or 0)
    except Exception:
        return {"model": None, "reason": "hardware profile unavailable", "ram_gb": 0}

    for threshold, model, reason in HARDWARE_TIERS:
        if ram_gb >= threshold:
            return {"model": model, "reason": reason, "ram_gb": ram_gb}
    return {"model": HARDWARE_TIERS[-1][1], "reason": HARDWARE_TIERS[-1][2], "ram_gb": ram_gb}


# ===== Routing =====

def route(task: str = TASK_CHAT, model: Optional[str] = None, prefer_cloud: bool = False) -> Route:
    """Resolve a task to a provider and model.

    An explicit ``model`` always wins — it is what the user picked in the UI.
    Otherwise the task escalates to the cloud only when the cloud is configured,
    the user opted this task in (or asked for it on this call), and the task is
    not one of the high-volume local-only ones.
    """
    task = task if task in TASKS else TASK_CHAT

    if model:
        provider = PROVIDER_CLOUD if model.startswith("claude") else PROVIDER_LOCAL
        return Route(task=task, provider=provider, model=model, reason="explicitly selected")

    if task not in LOCAL_ONLY_TASKS and (prefer_cloud or task in cloud_tasks()):
        if cloud_enabled():
            config = get_config()
            return Route(
                task=task,
                provider=PROVIDER_CLOUD,
                model=config.get("cloud_model", DEFAULT_CLOUD_MODEL),
                effort=config.get("cloud_effort", DEFAULT_CLOUD_EFFORT),
                reason=f"'{task}' is routed to the cloud",
            )
        if prefer_cloud:
            return Route(
                task=task,
                provider=PROVIDER_LOCAL,
                model=local_model(task),
                reason="cloud requested but not configured — staying local",
            )

    return Route(
        task=task,
        provider=PROVIDER_LOCAL,
        model=local_model(task),
        reason=f"'{task}' runs on-device",
    )


def status() -> Dict[str, Any]:
    config = get_config()
    return {
        "cloud_enabled": cloud_enabled(),
        "cloud_configured": bool(cloud_api_key()),
        "cloud_model": config.get("cloud_model", DEFAULT_CLOUD_MODEL),
        "cloud_effort": config.get("cloud_effort", DEFAULT_CLOUD_EFFORT),
        "cloud_tasks": cloud_tasks(),
        "sdk_installed": _sdk_available(),
        "routes": {task: route(task).as_dict() for task in TASKS},
        "recommendation": recommend_local_model(),
    }


# ===== Anthropic client =====

def _sdk_available() -> bool:
    try:
        import anthropic  # noqa: F401

        return True
    except ImportError:
        return False


def _client():
    """Build an Anthropic client, or raise a message the UI can show verbatim."""
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError(
            "the anthropic package is not installed — run: pip install 'carrot[cloud]'"
        ) from exc
    key = cloud_api_key()
    if not key:
        raise RuntimeError("no Anthropic API key configured")
    return anthropic.Anthropic(api_key=key)


# ===== Message/tool translation =====

def _split_system(messages: List[Dict[str, Any]]):
    """Anthropic takes the system prompt out of band; Ollama keeps it inline."""
    system_parts = [m["content"] for m in messages if m.get("role") == "system" and m.get("content")]
    rest = [m for m in messages if m.get("role") != "system"]
    return "\n\n".join(system_parts), rest


def _to_anthropic_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert Ollama-shaped history into Anthropic content blocks.

    Ollama models tool results as a ``tool`` role message; Anthropic models them
    as a ``tool_result`` block inside a user turn keyed by ``tool_use_id``.
    Consecutive tool results are merged into one user turn, which is what the
    API expects when several tools ran in parallel.
    """
    converted: List[Dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content") or ""

        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id") or message.get("name") or "unknown",
                "content": str(content)[:20000],
            }
            if converted and converted[-1]["role"] == "user" and isinstance(converted[-1]["content"], list):
                converted[-1]["content"].append(block)
            else:
                converted.append({"role": "user", "content": [block]})
            continue

        if role == "assistant" and message.get("tool_calls"):
            blocks: List[Dict[str, Any]] = []
            if content:
                blocks.append({"type": "text", "text": content})
            for call in message["tool_calls"]:
                function = call.get("function", {})
                arguments = function.get("arguments", {})
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id") or f"call_{len(blocks)}",
                        "name": function.get("name", ""),
                        "input": arguments,
                    }
                )
            converted.append({"role": "assistant", "content": blocks})
            continue

        if not content:
            continue
        converted.append({"role": role if role in ("user", "assistant") else "user", "content": content})

    return converted


def _to_anthropic_tools(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Convert Ollama function-calling schemas into Anthropic tool definitions."""
    if not tools:
        return []
    converted = []
    for tool in tools:
        function = tool.get("function", tool)
        name = function.get("name")
        if not name:
            continue
        converted.append(
            {
                "name": name,
                "description": function.get("description", ""),
                "input_schema": function.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return converted


def _tool_calls_from_message(message) -> List[Dict[str, Any]]:
    """Extract tool_use blocks as Ollama-shaped calls, preserving the block id.

    The id round-trips back as ``tool_call_id`` on the tool result, which is how
    Anthropic pairs a result with its call.
    """
    calls = []
    for block in message.content:
        if getattr(block, "type", None) == "tool_use":
            calls.append(
                {
                    "id": block.id,
                    "type": "function",
                    "function": {"name": block.name, "arguments": block.input or {}},
                }
            )
    return calls


REFUSAL_MESSAGE = (
    "The model declined this request. Rephrasing it, or switching this task back "
    "to a local model in Settings, may work."
)


# ===== Unified calls =====

def stream_events(
    resolved: Route,
    messages: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
) -> Generator[Dict[str, Any], None, None]:
    """Stream one turn from whichever provider the route names.

    Yields the same typed events as ``OllamaClient.chat_stream_events`` so the
    agentic loop does not need to know which provider ran.
    """
    if resolved.provider == PROVIDER_LOCAL:
        from .ollama_client import OllamaClient

        yield from OllamaClient().chat_stream_events(messages, model=resolved.model, tools=tools)
        return

    client = _client()
    system, conversation = _split_system(messages)
    request: Dict[str, Any] = {
        "model": resolved.model,
        "max_tokens": CLOUD_MAX_TOKENS_STREAM,
        "messages": _to_anthropic_messages(conversation),
        "thinking": {"type": "adaptive", "display": "summarized"},
        "output_config": {"effort": resolved.effort or DEFAULT_CLOUD_EFFORT},
        "betas": CLOUD_BETAS,
        "fallbacks": "default",
    }
    if system:
        request["system"] = system
    anthropic_tools = _to_anthropic_tools(tools)
    if anthropic_tools:
        request["tools"] = anthropic_tools

    with client.beta.messages.stream(**request) as stream:
        for event in stream:
            if event.type != "content_block_delta":
                continue
            delta = event.delta
            if delta.type == "thinking_delta":
                yield {"type": "thinking", "text": delta.thinking}
            elif delta.type == "text_delta":
                yield {"type": "content", "text": delta.text}
        final = stream.get_final_message()

    if final.stop_reason == "refusal":
        yield {"type": "content", "text": REFUSAL_MESSAGE}
        return

    calls = _tool_calls_from_message(final)
    if calls:
        yield {"type": "tool_calls", "calls": calls}


def complete(resolved: Route, messages: List[Dict[str, Any]]) -> str:
    """Non-streaming completion through whichever provider the route names."""
    if resolved.provider == PROVIDER_LOCAL:
        from .ollama_client import OllamaClient

        return OllamaClient().chat(messages, model=resolved.model)

    client = _client()
    system, conversation = _split_system(messages)
    request: Dict[str, Any] = {
        "model": resolved.model,
        "max_tokens": CLOUD_MAX_TOKENS_SYNC,
        "messages": _to_anthropic_messages(conversation),
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": resolved.effort or DEFAULT_CLOUD_EFFORT},
        "betas": CLOUD_BETAS,
        "fallbacks": "default",
    }
    if system:
        request["system"] = system

    response = client.beta.messages.create(**request)
    if response.stop_reason == "refusal":
        return REFUSAL_MESSAGE
    return "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
