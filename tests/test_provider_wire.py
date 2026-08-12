"""What providers actually put on the wire, versus what the format says.

"chat-completions compatible" is a family resemblance, not a specification.
Every vendor in the list — OpenAI, Mistral, Groq, Together, DeepSeek,
OpenRouter, vLLM, LM Studio — has its own reading of it, and Carrot talks to
all of them through one client. So the client has to be liberal about what it
accepts and strict about what it hands on.

From a reported turn on mistral-large: the run died with

    provider stopped the turn: can only concatenate str (not "list") to str

Mistral's current API sends `delta.content` as a list of content parts, the
same shape the *request* side has always accepted. That went straight into
`ThinkTagStreamFilter.feed`, whose first act is `buf += text`. It was a
TypeError two frames down in our own code, reported to the user as the
provider's fault — so they went and checked their rate limits.
"""
import pytest

from carrot import app as A
from carrot.openai_client import text_of


class TestTextOf:
    def test_a_plain_string_is_itself(self):
        assert text_of("hello") == "hello"

    def test_none_is_empty(self):
        assert text_of(None) == ""

    def test_the_mistral_shape(self):
        assert text_of([{"type": "text", "text": "Here is "},
                        {"type": "text", "text": "the answer."}]) == "Here is the answer."

    def test_a_bare_part_without_a_type(self):
        # Some servers omit `type` when there is only one kind of part.
        assert text_of([{"text": "hello"}]) == "hello"

    def test_a_list_of_plain_strings(self):
        assert text_of(["a", "b"]) == "ab"

    def test_a_single_part_not_in_a_list(self):
        assert text_of({"type": "text", "text": "hi"}) == "hi"

    def test_non_text_parts_are_skipped_not_guessed_at(self):
        # An image or a citation part has no string form, and inventing one
        # would drop a repr into the middle of somebody's answer.
        assert text_of([{"type": "text", "text": "see "},
                        {"type": "image_url", "image_url": {"url": "http://x"}},
                        {"type": "text", "text": "this"}]) == "see this"

    def test_an_empty_list_is_empty(self):
        assert text_of([]) == ""

    def test_it_never_raises_on_a_shape_nobody_predicted(self):
        for value in (42, 3.5, True, object(), [[{"type": "text", "text": "x"}]]):
            text_of(value)


class TestTheStreamSurvivesIt:
    def stream(self, chunks):
        """Drive the client's stream parser over raw SSE lines."""
        import json
        from unittest.mock import patch

        from carrot.openai_client import OpenAICompatibleClient

        lines = [f"data: {json.dumps(c)}" for c in chunks] + ["data: [DONE]"]

        class Response:
            def iter_lines(self, decode_unicode=True):
                return iter(lines)

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        client = OpenAICompatibleClient(base_url="http://x")
        with patch.object(OpenAICompatibleClient, "_request", lambda *a, **k: Response()):
            return list(client.chat_stream_events([{"role": "user", "content": "hi"}],
                                                  model="m"))

    def delta(self, content):
        return {"choices": [{"delta": {"content": content}}]}

    def test_the_reported_crash_does_not_happen(self):
        events = self.stream([self.delta([{"type": "text", "text": "Corvette ZR1X"}])])
        assert "".join(e["text"] for e in events if e["type"] == "content") == "Corvette ZR1X"

    def test_plain_string_deltas_still_work(self):
        events = self.stream([self.delta("Hello "), self.delta("world")])
        assert "".join(e["text"] for e in events if e["type"] == "content") == "Hello world"

    def test_think_tags_still_split_across_list_deltas(self):
        # The filter is stateful across chunks; normalizing at the boundary
        # must not break the case it exists for.
        events = self.stream([self.delta([{"type": "text", "text": "<think>why"}]),
                              self.delta("</think>answer")])
        assert "".join(e["text"] for e in events if e["type"] == "thinking") == "why"
        assert "".join(e["text"] for e in events if e["type"] == "content") == "answer"

    def test_structured_reasoning_is_handled_too(self):
        events = self.stream([{"choices": [{"delta": {
            "reasoning_content": [{"type": "text", "text": "thinking out loud"}]}}]}])
        assert any(e["type"] == "thinking" and e["text"] == "thinking out loud"
                   for e in events)


class TestWhoGetsTheBlame:
    """The message sends the user somewhere. It should be the right place."""

    def test_our_own_type_error_is_named_as_ours(self):
        blame = A._blame_for('can only concatenate str (not "list") to str')
        assert "fault in Carrot" in blame
        assert "provider" not in blame.split("not in the provider")[0]

    @pytest.mark.parametrize("failure", [
        "rate limit exceeded",
        "HTTP 429 Too Many Requests",
        "context length exceeded",
        "upstream connect error",
    ])
    def test_real_provider_failures_still_blame_the_provider(self, failure):
        assert "the provider stopped the turn" in A._blame_for(failure)

    @pytest.mark.parametrize("failure", [
        "'NoneType' object has no attribute 'get'",
        "list indices must be integers or slices, not str",
        "unsupported operand type(s) for +: 'int' and 'str'",
    ])
    def test_the_other_interpreter_errors_too(self, failure):
        assert "fault in Carrot" in A._blame_for(failure)

    def test_the_failure_text_is_always_included(self):
        # Whoever reads it needs the actual words to report or search.
        for failure in ("boom", "can only concatenate str"):
            assert failure in A._blame_for(failure)


class TestTheStreamIsDecodedAsUTF8:
    """Every hosted provider's answer was arriving as mojibake.

    Server-sent events are UTF-8 by specification, always. But the header is
    usually a bare `text/event-stream` with no charset, and `requests` maps a
    charset-less `text/*` to ISO-8859-1 — so an em dash arrived as "â", and
    "Łask" as "Åask". It went into the answer, the stored message, and the
    memory written from it.

    It only bites when the provider puts raw UTF-8 on the wire. A provider
    that escapes non-ASCII to backslash-u form is pure ASCII and survives either way, which is
    why this hid for so long.
    """

    def sse(self, text):
        import io, json
        from requests.models import Response
        from urllib3 import HTTPResponse

        body = b"".join(
            b"data: " + json.dumps({"choices": [{"delta": {"content": ch}}]},
                                   ensure_ascii=False).encode("utf-8") + b"\n\n"
            for ch in text
        ) + b"data: [DONE]\n\n"
        response = Response()
        response.headers["Content-Type"] = "text/event-stream"   # as providers send it
        response.raw = HTTPResponse(body=io.BytesIO(body), preload_content=False, status=200)
        response.status_code = 200
        import requests.utils
        response.encoding = requests.utils.get_encoding_from_headers(response.headers)
        return response

    def read(self, response):
        import json
        out = []
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            if isinstance(line, bytes):
                line = line.decode("utf-8")
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            out.append(json.loads(payload)["choices"][0]["delta"]["content"])
        return "".join(out)

    def test_requests_would_mangle_it_without_the_fix(self):
        """Pins the cause, so nobody removes the one line as redundant."""
        from requests.utils import get_encoding_from_headers
        assert get_encoding_from_headers({"content-type": "text/event-stream"}) == "ISO-8859-1"

    def test_setting_utf8_recovers_the_text(self):
        text = "F-35 Status \u2014 August 2026, Lots 18\u201319, \u0141ask AB"
        broken = self.read(self.sse(text))
        assert broken != text, "the fixture no longer reproduces the bug"

        fixed_response = self.sse(text)
        fixed_response.encoding = "utf-8"
        assert self.read(fixed_response) == text

    def test_the_client_sets_it(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "carrot" / "openai_client.py").read_text(encoding="utf-8")
        block = source.split("with self._request(payload, stream=True) as response:")[1][:900]
        assert 'response.encoding = "utf-8"' in block
        assert block.index('response.encoding = "utf-8"') < block.index("iter_lines")


class TestLocalModelsGetTheirWholeContextWindow:
    """Every local model was running in 4k however much it could hold.

    Ollama defaults `num_ctx` to 4096 and nothing here ever set it, while
    `gemma4:e4b` advertises 131,072. Anything past 4k is dropped from the
    *front* of the prompt — where the system directive, the plan and the tool
    results live. A turn that read three pages and then said "the provided
    notes do not contain that" was telling the truth: by the time it answered,
    the notes had been truncated away.
    """

    def client(self, context_length=131072):
        from unittest.mock import patch
        from carrot.ollama_client import OllamaClient

        client = OllamaClient()
        OllamaClient._context_length = {}
        response = type("R", (), {
            "raise_for_status": lambda self: None,
            "json": lambda self: {"model_info": {"gemma4.context_length": context_length}},
        })()
        return client, patch("carrot.ollama_client.requests.post", return_value=response)

    def test_every_request_carries_it(self):
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "carrot" / "ollama_client.py").read_text(encoding="utf-8")
        # chat, chat_stream_events, generate and structured_chat. Missing one
        # means that path silently keeps the 4k window.
        assert source.count('"options": self._options') == 4

    def test_it_asks_for_the_configured_window(self):
        client, patched = self.client()
        with patched:
            assert client.context_length("gemma4:e4b") == client.DEFAULT_NUM_CTX

    def test_it_never_exceeds_what_the_model_has(self):
        """Ollama accepts a larger number and then behaves unpredictably."""
        client, patched = self.client(context_length=8192)
        with patched:
            assert client.context_length("small:1b") == 8192

    def test_it_never_drops_below_ollamas_own_default(self):
        client, patched = self.client(context_length=512)
        with patched:
            assert client.context_length("tiny:0.5b") == 4096

    def test_a_model_that_reports_nothing_still_gets_a_window(self):
        # Failing to read the limit must not put it back to 4k.
        from unittest.mock import patch
        from carrot.ollama_client import OllamaClient

        client = OllamaClient()
        OllamaClient._context_length = {}
        with patch("carrot.ollama_client.requests.post", side_effect=RuntimeError("offline")):
            assert client.context_length("unknown:latest") == client.DEFAULT_NUM_CTX

    def test_the_cap_is_deliberate(self):
        """The KV cache grows with this, and a 128k window on a laptop turns a
        working setup into a swapping one."""
        from carrot.ollama_client import OllamaClient
        assert 8192 <= OllamaClient.DEFAULT_NUM_CTX <= 65536


class TestReasoningIsNotPrintedAsTheAnswer:
    """Models mark reasoning in whatever convention their trainer picked, and
    an unrecognised marker means the whole chain of thought becomes the reply."""

    def split(self, raw):
        from carrot.ollama_client import ThinkTagStreamFilter
        f = ThinkTagStreamFilter()
        parts = f.feed(raw) + f.flush()
        return ("".join(p["text"] for p in parts if p["type"] == "content").strip(),
                "".join(p["text"] for p in parts if p["type"] == "thinking").strip())

    # The two channel cases below put the reasoning *before* `<|message|>`,
    # which is not the shape real harmony emits — there the header is
    # `<|channel|>analysis<|message|>` and the reasoning follows it. They are
    # kept because a malformed stream should still not print its reasoning,
    # but they are not the format: see test_thinking_recap.py, where the real
    # one is covered. Reading these as the spec is what produced the bug they
    # were meant to guard against — `<|message|>` treated as a closer, so
    # thinking ended one token after it began and the chain of thought was
    # emitted as the answer.
    @pytest.mark.parametrize("raw", [
        "<think>reasoning</think>The answer.",
        "<thinking>reasoning</thinking>The answer.",
        "<reasoning>reasoning</reasoning>The answer.",
        "<|channel>thought reasoning<|message|>The answer.",
        "<|channel|>analysis reasoning<|channel|>final The answer.",
    ])
    def test_each_convention_is_recognised(self, raw):
        content, thinking = self.split(raw)
        assert content == "The answer."
        assert "reasoning" in thinking

    def test_a_marker_split_across_chunks_still_works(self):
        from carrot.ollama_client import ThinkTagStreamFilter
        f = ThinkTagStreamFilter()
        parts = []
        for chunk in ["<|chan", "nel>thou", "ght reasoning", "<|mess", "age|>The answer."]:
            parts += f.feed(chunk)
        parts += f.flush()
        content = "".join(p["text"] for p in parts if p["type"] == "content").strip()
        assert content == "The answer."

    def test_an_answer_with_no_marker_is_untouched(self):
        assert self.split("The answer.")[0] == "The answer."


class TestTheContextWindowIsVisibleAndAdjustable:
    """The number that broke every local turn was invisible and unchangeable.

    Ollama's default of 4096 is what made a model lose its own instructions
    mid-turn, and nothing in the app showed it or let anyone move it.
    """

    def test_the_setting_takes_effect_without_a_restart(self):
        """Only the model's own limit is cached — that needs a round trip and
        never changes. Caching the resolved value meant changing the setting
        did nothing until the backend was restarted, which is not what a
        setting means."""
        from unittest.mock import patch
        from carrot import config
        from carrot.ollama_client import OllamaClient

        client = OllamaClient()
        OllamaClient._context_length = {}
        response = type("R", (), {
            "raise_for_status": lambda self: None,
            "json": lambda self: {"model_info": {"gemma4.context_length": 131072}},
        })()
        original = config.get_config().get("ollama_num_ctx")
        try:
            with patch("carrot.ollama_client.requests.post", return_value=response):
                config.set_config("ollama_num_ctx", 8192)
                assert client.context_length("gemma4:e4b") == 8192
                config.set_config("ollama_num_ctx", 65536)
                assert client.context_length("gemma4:e4b") == 65536
        finally:
            if original is not None:
                config.set_config("ollama_num_ctx", original)

    def test_the_models_endpoint_reports_it(self, client):
        body = client.get("/api/models").json()
        assert "context" in body

    def test_the_picker_shows_it(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "carrot" / "web" / "js" / "app.js").read_text(encoding="utf-8")
        assert "ctxLabel(data, m.name)" in js
        assert "function fmtCtx(" in js

    def test_it_is_offered_as_a_choice_not_a_token_box(self):
        """`num_ctx` is not something a person should have to know, and the
        real decision behind it is a trade against the memory in their
        machine."""
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "carrot" / "web" / "js" / "app.js").read_text(encoding="utf-8")
        block = js.split("const CTX_CHOICES = [")[1].split("];")[0]
        for word in ("Small", "Balanced", "Large"):
            assert word in block
        # The numbers are still shown; hiding them from someone who does know
        # would be its own kind of rude.
        assert "tokens" in js.split("function renderContextChoices(")[1][:900]

    def test_a_model_capped_below_the_setting_says_so(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "carrot" / "web" / "js" / "app.js").read_text(encoding="utf-8")
        assert "its own limit, which is lower than the setting" in js
