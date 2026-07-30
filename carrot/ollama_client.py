import json
import requests
from typing import Optional, Generator, List, Dict, Any

from carrot.config import get_config


class ThinkTagStreamFilter:
    """Splits a token stream into 'thinking' and 'content' parts.

    Some models embed reasoning inside <think>...</think> tags in the content
    stream. This stateful filter routes text to the right channel even when
    tags are split across chunk boundaries.
    """

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.in_think = False
        self.buf = ""

    def feed(self, text: str) -> List[Dict[str, str]]:
        self.buf += text
        out = []
        while True:
            tag = self.CLOSE if self.in_think else self.OPEN
            idx = self.buf.find(tag)
            kind = "thinking" if self.in_think else "content"
            if idx == -1:
                # Hold back a tag-length tail in case a tag spans chunks.
                safe = len(self.buf) - len(tag)
                if safe > 0:
                    out.append({"type": kind, "text": self.buf[:safe]})
                    self.buf = self.buf[safe:]
                break
            if idx > 0:
                out.append({"type": kind, "text": self.buf[:idx]})
            self.buf = self.buf[idx + len(tag):]
            self.in_think = not self.in_think
        return out

    def flush(self) -> List[Dict[str, str]]:
        out = []
        if self.buf:
            out.append({"type": "thinking" if self.in_think else "content", "text": self.buf})
            self.buf = ""
        return out


class OllamaClient:
    _thinking_support: Dict[str, bool] = {}

    def __init__(self):
        config = get_config()
        self.base_url = config.get("ollama_host", "http://localhost:11434")
        self.default_model = config.get("ollama_model", "gemma4:e4b")
        self.classifier_model = config.get("ollama_model_query", self.default_model)
        self.embedding_model = "nomic-embed-text"

    def _url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}/{path.lstrip('/')}"

    def is_available(self) -> bool:
        try:
            resp = requests.get(self._url("/api/tags"), timeout=5)
            return resp.status_code == 200
        except requests.ConnectionError:
            return False

    def list_models(self) -> List[Dict[str, Any]]:
        """Return locally installed models from Ollama (/api/tags)."""
        try:
            resp = requests.get(self._url("/api/tags"), timeout=5)
            resp.raise_for_status()
            models = resp.json().get("models", [])
            return [
                {
                    "name": m.get("name", ""),
                    "size": m.get("size", 0),
                    "modified_at": m.get("modified_at", ""),
                    "parameter_size": (m.get("details") or {}).get("parameter_size", ""),
                }
                for m in models
            ]
        except Exception:
            return []

    def pull_model(self, model: str) -> Generator[Dict[str, Any], None, None]:
        """Pull a model from the Ollama registry, yielding progress dicts."""
        resp = requests.post(
            self._url("/api/pull"),
            json={"model": model, "stream": True},
            stream=True,
            timeout=None,
        )
        resp.raise_for_status()
        for line in resp.iter_lines(decode_unicode=True):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            yield data
            if data.get("status") == "success" or data.get("error"):
                break

    def supports_thinking(self, model: str) -> bool:
        """Check (and cache) whether a model advertises the thinking capability."""
        if model in self._thinking_support:
            return self._thinking_support[model]
        supported = False
        try:
            resp = requests.post(self._url("/api/show"), json={"model": model}, timeout=10)
            resp.raise_for_status()
            supported = "thinking" in resp.json().get("capabilities", [])
        except Exception:
            supported = False
        self._thinking_support[model] = supported
        return supported

    def chat_stream_events(
        self,
        messages: list,
        model: Optional[str] = None,
        tools: Optional[list] = None,
    ) -> Generator[Dict[str, Any], None, None]:
        """Stream a chat response as typed events.

        Yields {'type': 'thinking'|'content', 'text': ...} and, when tools are
        provided and the model requests them, {'type': 'tool_calls', 'calls': [...]}.
        Uses Ollama's native thinking channel when the model supports it, and
        additionally splits inline <think> tags for models that embed reasoning
        in the content stream.
        """
        model = model or self.default_model
        body = {"model": model, "messages": messages, "stream": True}
        if tools:
            body["tools"] = tools
        if self.supports_thinking(model):
            body["think"] = True
        resp = requests.post(
            self._url("/api/chat"),
            json=body,
            timeout=120,
            stream=True,
        )
        resp.raise_for_status()
        tag_filter = ThinkTagStreamFilter()
        for line in resp.iter_lines(decode_unicode=True):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = data.get("message", {})
            if msg.get("thinking"):
                yield {"type": "thinking", "text": msg["thinking"]}
            if msg.get("tool_calls"):
                yield {"type": "tool_calls", "calls": msg["tool_calls"]}
            if msg.get("content"):
                yield from tag_filter.feed(msg["content"])
            if data.get("done", False):
                break
        yield from tag_filter.flush()

    def generate(
        self,
        prompt: str,
        model: Optional[str] = None,
        system: Optional[str] = None,
        stream: bool = False,
        context: Optional[list] = None,
    ) -> str:
        body = {
            "model": model or self.default_model,
            "prompt": prompt,
            "stream": stream,
        }
        if system:
            body["system"] = system
        if context:
            body["context"] = context
        resp = requests.post(
            self._url("/api/generate"),
            json=body,
            timeout=120,
            stream=stream,
        )
        resp.raise_for_status()
        if stream:
            return self._stream_response(resp)
        return resp.json().get("response", "")

    def chat(
        self,
        messages: list,
        model: Optional[str] = None,
        stream: bool = False,
    ) -> str:
        body = {
            "model": model or self.default_model,
            "messages": messages,
            "stream": stream,
        }
        resp = requests.post(
            self._url("/api/chat"),
            json=body,
            timeout=120,
            stream=stream,
        )
        resp.raise_for_status()
        if stream:
            return self._stream_chat_response(resp)
        return resp.json().get("message", {}).get("content", "")

    def classify_query(self, query: str) -> Dict[str, Any]:
        system = (
            "You are a query classifier for Carrot, a personal AI assistant. "
            "Extract structured metadata from the user's query for search and retrieval. "
            "Return ONLY a valid JSON object with no markdown formatting, no extra text, no quotes wrapping the JSON."
        )
        user = (
            f"Classify this query and extract search metadata as JSON:\n\n"
            f'Query: "{query}"\n\n'
            f'Return a JSON object with these fields:\n'
            f'- "search_keywords": the core search terms (2-8 words, lowercase)\n'
            f'- "time_cutoff_days": number of days to look back (0 means all time)\n'
            f'- "intent": one of "recall", "search", "code", "reminder", "goal", "general"\n'
            f'- "entities": any specific names, dates, files, or topics mentioned\n'
            f'Example output: {{"search_keywords": "bench press stats", "time_cutoff_days": 180, "intent": "recall", "entities": []}}'
        )
        response = self.chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            model=self.classifier_model,
        )
        try:
            cleaned = response.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                cleaned = "\n".join(line for line in lines if not line.strip().startswith("```"))
            cleaned = cleaned.strip()
            if cleaned.startswith("{") and cleaned.endswith("}"):
                return json.loads(cleaned)
            start = cleaned.index("{")
            end = cleaned.rindex("}") + 1
            return json.loads(cleaned[start:end])
        except (json.JSONDecodeError, ValueError):
            return {
                "search_keywords": query,
                "time_cutoff_days": 0,
                "intent": "general",
                "entities": [],
            }

    def structured_chat(
        self,
        messages: list,
        model: Optional[str] = None,
        tools: Optional[list] = None,
        response_format: Optional[dict] = None,
    ) -> str:
        body = {
            "model": model or self.default_model,
            "messages": messages,
            "stream": False,
        }
        if response_format:
            body["format"] = response_format
        resp = requests.post(
            self._url("/api/chat"),
            json=body,
            timeout=120,
        )
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "")

    def get_embedding(self, text: str, model: Optional[str] = None) -> Optional[List[float]]:
        if not self.is_available():
            return None
        try:
            resp = requests.post(
                self._url("/api/embeddings"),
                json={
                    "model": model or self.embedding_model,
                    "prompt": text,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("embedding")
        except Exception:
            return None

    def _stream_response(self, resp):
        for line in resp.iter_lines(decode_unicode=True):
            if line.strip():
                try:
                    data = json.loads(line)
                    if "response" in data and data["response"]:
                        yield data["response"]
                    if data.get("done", False):
                        break
                except json.JSONDecodeError:
                    continue

    def _stream_chat_response(self, resp):
        for line in resp.iter_lines(decode_unicode=True):
            if line.strip():
                try:
                    data = json.loads(line)
                    msg = data.get("message", {})
                    if msg.get("content"):
                        yield msg["content"]
                    if data.get("done", False):
                        break
                except json.JSONDecodeError:
                    continue