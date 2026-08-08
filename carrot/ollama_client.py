import json
import requests
from typing import Optional, Generator, List, Dict, Any

from carrot.config import get_config


class ThinkTagStreamFilter:
    """Splits a token stream into 'thinking' and 'content' parts.

    Models mark their reasoning in whatever convention their trainer picked,
    and getting it wrong is not cosmetic: an unrecognised marker means the
    whole chain of thought is printed as the answer. Reported from a local
    model that emitted `<|channel>thought` — the user got several screens of
    "Analyze the Request:", "Self-Correction during writing", and a list of
    the tools it had been given, with the actual reply at the bottom.

    So this is a set of conventions rather than one tag, matched with a regex
    that still holds back a tail for markers split across chunk boundaries.

    What it cannot do is catch a model that reasons in plain prose with no
    marker at all. That is not a parsing problem and pretending otherwise —
    guessing at "The user asked..." — would eventually eat a real answer that
    happened to open that way.
    """

    # Ordered longest-first so `<|channel|>analysis` is not half-matched by a
    # shorter opener that shares a prefix.
    OPENERS = [
        "<|channel|>analysis", "<|channel|>thought",
        "<|channel>analysis", "<|channel>thought",
        "<thinking>", "<reasoning>", "<think>",
    ]
    CLOSERS = [
        "<|message|>", "<|channel|>final", "<|channel>final",
        "</thinking>", "</reasoning>", "</think>",
    ]
    # The longest marker, which is how much tail has to be held back.
    _TAIL = max(len(m) for m in OPENERS + CLOSERS)

    OPEN, CLOSE = "<think>", "</think>"          # kept for callers that read them

    def __init__(self):
        self.in_think = False
        self.buf = ""

    def _find(self, markers):
        """The earliest marker in the buffer, as (index, length)."""
        best = (-1, 0)
        for marker in markers:
            idx = self.buf.find(marker)
            if idx != -1 and (best[0] == -1 or idx < best[0]):
                best = (idx, len(marker))
        return best

    def feed(self, text: str) -> List[Dict[str, str]]:
        self.buf += text
        out = []
        while True:
            idx, width = self._find(self.CLOSERS if self.in_think else self.OPENERS)
            kind = "thinking" if self.in_think else "content"
            if idx == -1:
                # Hold back the longest marker's worth, in case one spans chunks.
                safe = len(self.buf) - self._TAIL
                if safe > 0:
                    out.append({"type": kind, "text": self.buf[:safe]})
                    self.buf = self.buf[safe:]
                break
            if idx > 0:
                out.append({"type": kind, "text": self.buf[:idx]})
            self.buf = self.buf[idx + width:]
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

    def delete_model(self, model: str) -> bool:
        """Remove a locally installed model (frees its disk space)."""
        try:
            # Newer Ollama expects "model", older expects "name" — send both.
            resp = requests.delete(
                self._url("/api/delete"),
                json={"model": model, "name": model},
                timeout=30,
            )
            return resp.status_code == 200
        except Exception:
            return False

    # Connect, then idle. A read timeout on a streamed response is the gap
    # *between* chunks, not the total, so this does not cap how long a pull may
    # take — a 40 GB model over a slow line is fine as long as bytes keep
    # arriving. What it catches is the case `timeout=None` could not: the
    # connection going dead mid-download, where the generator blocked forever
    # and the progress bar sat at whatever percentage it had reached, with no
    # error and no way to tell it apart from a slow network.
    PULL_TIMEOUT = (10, 300)

    def pull_model(self, model: str) -> Generator[Dict[str, Any], None, None]:
        """Pull a model from the Ollama registry, yielding progress dicts."""
        resp = requests.post(
            self._url("/api/pull"),
            json={"model": model, "stream": True},
            stream=True,
            timeout=self.PULL_TIMEOUT,
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

    def capabilities(self, model: str) -> List[str]:
        """Capabilities the server reports for a model, e.g. vision, thinking."""
        if not hasattr(self, "_capabilities"):
            self._capabilities = {}
        if model in self._capabilities:
            return self._capabilities[model]
        caps: List[str] = []
        try:
            resp = requests.post(self._url("/api/show"), json={"model": model}, timeout=10)
            resp.raise_for_status()
            caps = list(resp.json().get("capabilities", []) or [])
        except Exception:
            caps = []
        self._capabilities[model] = caps
        return caps

    # ===== Context window =====
    #
    # Ollama defaults `num_ctx` to 4096 and we never set it, so every local
    # model ran in 4k however much it could actually hold — `gemma4:e4b`
    # advertises 131,072. Anything past 4k is silently dropped from the *front*
    # of the prompt, which is where the system directive, the plan and the tool
    # results all live.
    #
    # That is the cause of the whole family of local-model complaints in this
    # app: a turn that read three pages and then answered "the provided notes
    # do not contain that" was telling the truth — by the time it answered, the
    # notes had been truncated away. Proven directly: a marker 9k tokens into a
    # conversation is invisible at the default and recalled perfectly at 32k.
    #
    # Capped rather than maximised. The KV cache grows with this number, and a
    # 128k window on a laptop is how you turn a working setup into a swapping
    # one. 32k holds the directive, the tools, a plan and several read pages,
    # which is what a turn here actually needs.
    DEFAULT_NUM_CTX = 32768
    _context_length: Dict[str, int] = {}

    def context_length(self, model: str) -> int:
        """How much context to ask for, for this model.

        Only the model's own limit is cached — that needs a round trip and
        never changes. The configured value is read every time, so changing it
        in Settings takes effect on the next turn rather than at the next
        restart, which is what a setting is supposed to mean.
        """
        wanted = self.DEFAULT_NUM_CTX
        try:
            wanted = int(get_config().get("ollama_num_ctx", self.DEFAULT_NUM_CTX))
        except (TypeError, ValueError):
            pass

        if model not in self._context_length:
            limit = 0
            try:
                resp = requests.post(self._url("/api/show"), json={"model": model}, timeout=10)
                resp.raise_for_status()
                info = resp.json().get("model_info", {}) or {}
                # The key is namespaced by architecture (`gemma4.context_length`,
                # `llama.context_length`), so it is found by suffix rather than
                # by guessing the family.
                for key, value in info.items():
                    if key.endswith("context_length") and isinstance(value, int):
                        limit = max(limit, value)
            except Exception:
                limit = 0
            self._context_length[model] = limit

        limit = self._context_length[model]
        # Never ask for more than the model has — Ollama accepts it and then
        # behaves unpredictably. Never go below Ollama's own default.
        resolved = min(wanted, limit) if limit else wanted
        return max(4096, resolved)

    def _options(self, model: str) -> Dict[str, Any]:
        return {"num_ctx": self.context_length(model)}

    def supports_thinking(self, model: str) -> bool:
        """Check (and cache) whether a model advertises the thinking capability."""
        if model in self._thinking_support:
            return self._thinking_support[model]
        supported = "thinking" in self.capabilities(model)
        self._thinking_support[model] = supported
        return supported

    def supports_vision(self, model: str) -> bool:
        """Whether the model can accept images."""
        from carrot.attachments import model_supports_vision
        return model_supports_vision(model, self)

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
        body = {"model": model, "messages": messages, "stream": True,
                "options": self._options(model)}
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
            "options": self._options(model or self.default_model),
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
            "options": self._options(model or self.default_model),
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
            "options": self._options(model or self.default_model),
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