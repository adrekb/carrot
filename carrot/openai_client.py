"""The OpenAI chat-completions wire protocol.

This is deliberately not the ``openai`` SDK. The format is the lingua franca of
hosted inference — OpenAI, OpenRouter, Groq, Together, DeepSeek, Mistral, vLLM,
LM Studio and llama.cpp all speak it — and talking to it over plain ``requests``
means a user can point Carrot at any of them without installing another package
per vendor.

Everything here emits the same typed events as ``OllamaClient.chat_stream_events``
(``{'type': 'thinking'|'content'|'tool_calls'}``) so the agentic chat loop stays
provider-agnostic.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Generator, List, Optional

import requests

from .ollama_client import ThinkTagStreamFilter

# Reasoning text arrives under different keys depending on who is serving it.
REASONING_KEYS = ("reasoning_content", "reasoning")


def text_of(value: Any) -> str:
    """Whatever a provider put in a content field, as a string.

    "chat-completions compatible" is a family resemblance, not a specification.
    The original format put a plain string in ``delta.content``; several
    providers now send the same structured content-part lists the request side
    has always accepted — ``[{"type": "text", "text": "..."}]`` — and Mistral's
    current API is one of them.

    That reached ``ThinkTagStreamFilter.feed``, whose first act is ``buf +=
    text``, and killed the turn with "can only concatenate str (not list) to
    str". It surfaced to the user as *provider stopped the turn*, which pointed
    the blame at Mistral for what was a type error two frames down in our own
    code.

    Unknown part types are skipped rather than guessed at: a future part that
    is an image or a citation has no string form, and inventing one would put
    a repr in the middle of somebody's answer.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(text_of(part) for part in value)
    if isinstance(value, dict):
        # A single content part. Only text carries text.
        if value.get("type") in (None, "text"):
            return str(value.get("text") or "")
        return ""
    return ""


class OpenAICompatibleClient:
    """One provider endpoint that speaks chat-completions."""

    def __init__(self, base_url: str, api_key: str = "", timeout: int = 300):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _request(self, payload: Dict[str, Any], stream: bool):
        response = requests.post(
            f"{self.base_url}/chat/completions",
            headers=self._headers(),
            json=payload,
            stream=stream,
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            raise RuntimeError(_error_message(response))
        return response

    def chat_stream_events(
        self,
        messages: List[Dict[str, Any]],
        model: str,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Generator[Dict[str, Any], None, None]:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": to_openai_messages(messages),
            "stream": True,
        }
        if tools:
            payload["tools"] = to_openai_tools(tools)

        think = ThinkTagStreamFilter()
        calls = _ToolCallAccumulator()

        with self._request(payload, stream=True) as response:
            # Server-sent events are UTF-8 by specification, always. But the
            # header is usually a bare `text/event-stream` with no charset,
            # and `requests` maps any charset-less `text/*` to ISO-8859-1 —
            # so every hosted provider's stream was being decoded as Latin-1.
            # An em dash arrived as "â", "Łask" as "Åask", and the mojibake
            # went into the answer, the stored message and the memory.
            #
            # Never guessed: SSE has no other legal encoding, so this is the
            # right value rather than a better default.
            response.encoding = "utf-8"
            for line in response.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except json.JSONDecodeError:
                    continue

                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}

                for key in REASONING_KEYS:
                    reasoning = text_of(delta.get(key))
                    if reasoning:
                        yield {"type": "thinking", "text": reasoning}

                # Normalized at the boundary, once. Everything downstream —
                # the think-tag filter, the accumulators, the chat loop — is
                # entitled to assume it is holding a string.
                content = text_of(delta.get("content"))
                if content:
                    # Some models inline their reasoning in <think> tags instead.
                    for event in think.feed(content):
                        if event["text"]:
                            yield event

                calls.feed(delta.get("tool_calls"))

        for event in think.flush():
            if event["text"]:
                yield event
        resolved = calls.result()
        if resolved:
            yield {"type": "tool_calls", "calls": resolved}

    def chat(self, messages: List[Dict[str, Any]], model: str, max_tokens: int = 16000) -> str:
        return self.chat_raw(to_openai_messages(messages), model, max_tokens)

    def chat_raw(self, messages: List[Dict[str, Any]], model: str,
                 max_tokens: int = 16000) -> str:
        """Post messages that are already in wire format.

        Multimodal turns carry a list of content parts rather than a string, and
        translating those back and forth through Carrot's internal shape would
        only lose information — so callers that build them pass them straight in.
        """
        payload = {
            "model": model,
            "messages": messages,
            "max_completion_tokens": max_tokens,
        }
        response = self._request(payload, stream=False)
        choices = response.json().get("choices") or []
        if not choices:
            return ""
        return text_of(choices[0].get("message", {}).get("content"))


class _ToolCallAccumulator:
    """Reassembles streamed tool calls, which arrive as fragments keyed by index."""

    def __init__(self):
        self._by_index: Dict[int, Dict[str, Any]] = {}

    def feed(self, deltas: Optional[List[Dict[str, Any]]]):
        for delta in deltas or []:
            index = delta.get("index", 0)
            call = self._by_index.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if delta.get("id"):
                call["id"] = delta["id"]
            function = delta.get("function") or {}
            if function.get("name"):
                call["name"] = function["name"]
            if function.get("arguments"):
                call["arguments"] += function["arguments"]

    def result(self) -> List[Dict[str, Any]]:
        calls = []
        for index in sorted(self._by_index):
            call = self._by_index[index]
            if not call["name"]:
                continue
            try:
                arguments = json.loads(call["arguments"]) if call["arguments"] else {}
            except json.JSONDecodeError:
                arguments = {}
            calls.append({
                "id": call["id"] or f"call_{index}",
                "type": "function",
                "function": {"name": call["name"], "arguments": arguments},
            })
        return calls


def to_openai_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert Carrot's Ollama-shaped history into chat-completions messages.

    The shapes are close. The one real difference is tool-call arguments, which
    Ollama carries as a dict and this API wants as a JSON string.
    """
    converted: List[Dict[str, Any]] = []
    for message in messages:
        role = message.get("role")
        content = message.get("content") or ""

        if role == "tool":
            converted.append({
                "role": "tool",
                "tool_call_id": message.get("tool_call_id") or message.get("name") or "unknown",
                "content": str(content)[:20000],
            })
            continue

        if role == "assistant" and message.get("tool_calls"):
            calls = []
            for index, call in enumerate(message["tool_calls"]):
                function = call.get("function", {})
                arguments = function.get("arguments", {})
                if not isinstance(arguments, str):
                    arguments = json.dumps(arguments)
                calls.append({
                    "id": call.get("id") or f"call_{index}",
                    "type": "function",
                    "function": {"name": function.get("name", ""), "arguments": arguments},
                })
            converted.append({"role": "assistant", "content": content or None, "tool_calls": calls})
            continue

        if not content:
            continue
        converted.append({
            "role": role if role in ("user", "assistant", "system") else "user",
            "content": content,
        })
    return converted


def to_openai_tools(tools: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Normalize Carrot's tool schemas into chat-completions tool definitions."""
    converted = []
    for tool in tools or []:
        function = tool.get("function", tool)
        name = function.get("name")
        if not name:
            continue
        converted.append({
            "type": "function",
            "function": {
                "name": name,
                "description": function.get("description", ""),
                "parameters": function.get("parameters") or {"type": "object", "properties": {}},
            },
        })
    return converted


def _error_message(response) -> str:
    """Pull the provider's own error text out, so the UI can show something useful."""
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP {response.status_code}: {response.text[:300]}"
    error = payload.get("error")
    if isinstance(error, dict) and error.get("message"):
        return f"HTTP {response.status_code}: {error['message']}"
    if isinstance(error, str):
        return f"HTTP {response.status_code}: {error}"
    return f"HTTP {response.status_code}: {json.dumps(payload)[:300]}"
