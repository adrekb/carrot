"""The turn ran out of room and nothing noticed.

Traced from a reported answer: asked "c8 zr1X specs", the model searched five
times, read six pages, and then wrote out its entire deliberation followed by a
table — including a list of constraints it had invented for itself ("Plain
text. No silent thinking. No apologies."). Nobody sent it that list. It was
reconstructing instructions it could no longer see.

Two faults, and the first one disabled the machinery that would have caught it.

**The meter measured a window the turn was not running in.** `context_limit` is
the model's ceiling — 262,144 for the Qwen3.8 9B distil. `context_length` is
what Carrot passes as `num_ctx`, the ceiling clamped by the configured window,
which defaults to 32,768 so a 256k KV cache does not turn a laptop into a swap
file. Ollama truncates at `num_ctx` and drops the *front* of the prompt, where
the system directive lives.

`_window_tokens` reported the ceiling. So the meter, the pruner, `rounds_left`
and the stop-and-answer gate all believed there were eight times more tokens
than the request would be allowed: six pages at up to 20,000 characters each is
nowhere near 90% of 262,144, so nothing ever pruned, and Ollama silently ate the
directive instead.

**And the trim kept the wrong four hundred characters.** It kept the head, which
identifies a directory listing or a search result and is exactly wrong for a web
page — the head of a page is its navigation. The reported read began
"Autocatalog Blog Login Register Car" with sixty more lines before any
specification. Cut to its head, that page contributes its own menu.
"""
import pytest

from carrot import app, pruning


class LocalRoute:
    local = True
    provider = "ollama"
    model = "some-local-model"


class CloudRoute:
    local = False
    provider = "openai"
    model = "gpt-x"


class TestTheMeterMeasuresTheTurnNotTheModel:

    def test_a_local_window_is_what_we_ask_for(self, monkeypatch):
        from carrot import ollama_client

        monkeypatch.setattr(ollama_client.OllamaClient, "context_length",
                            lambda self, model: 32768)
        monkeypatch.setattr(ollama_client.OllamaClient, "context_limit",
                            lambda self, model: 262144)
        assert app._window_tokens(LocalRoute()) == 32768

    def test_not_the_ceiling_the_model_could_hold(self, monkeypatch):
        # The exact reported mismatch: metering 262,144 while Ollama truncates
        # at 32,768 means the pruner never runs and the directive is evicted.
        from carrot import ollama_client

        monkeypatch.setattr(ollama_client.OllamaClient, "context_length",
                            lambda self, model: 32768)
        monkeypatch.setattr(ollama_client.OllamaClient, "context_limit",
                            lambda self, model: 262144)
        assert app._window_tokens(LocalRoute()) != 262144

    def test_a_bigger_configured_window_is_honoured(self, monkeypatch):
        # The setting is what moves this number, which is what a setting means.
        from carrot import ollama_client

        monkeypatch.setattr(ollama_client.OllamaClient, "context_length",
                            lambda self, model: 131072)
        assert app._window_tokens(LocalRoute()) == 131072

    def test_a_cloud_route_still_uses_the_table(self, monkeypatch):
        from carrot import context_windows as ctxwin

        monkeypatch.setattr(ctxwin, "window_for",
                            lambda provider, model, probed=0: {"tokens": 200000})
        assert app._window_tokens(CloudRoute()) == 200000

    def test_an_unreachable_ollama_does_not_stop_the_turn(self, monkeypatch):
        """Zero turns the context check off rather than inventing a ceiling."""
        from carrot import context_windows as ctxwin, ollama_client

        def boom(self, model):
            raise RuntimeError("connection refused")

        monkeypatch.setattr(ollama_client.OllamaClient, "context_length", boom)
        monkeypatch.setattr(ctxwin, "window_for",
                            lambda provider, model, probed=0: {"tokens": 0})
        assert app._window_tokens(LocalRoute()) == 0

    def test_the_ceiling_lookup_is_not_paid_per_turn(self):
        # The module-level cache this used to keep was removed; the client's
        # own cache is on the class, so the /api/show round trip happens once
        # per process however many instances a turn builds.
        from carrot.ollama_client import OllamaClient

        assert OllamaClient()._context_length is OllamaClient()._context_length


def page(url, fact, nav=60, footer=40):
    """A web page shaped like the ones in the report: menu, fact, boilerplate."""
    lines = ["Autocatalog", "Blog", "Login", "Register", "Car"]
    lines += [f"nav link number {i} in the sidebar menu listing here" for i in range(nav)]
    lines += [fact]
    lines += [f"more boilerplate footer text line {i} goes here" for i in range(footer)]
    return f'<untrusted_content origin="{url}">\n' + "\n".join(lines)


HORSEPOWER = "The 2026 Corvette ZR1X makes 1,250 horsepower combined from the LT7 V8."
TORQUE = "Peak torque is quoted at 828 lb-ft at 6,000 rpm for the ZR1X."


@pytest.fixture
def transcript():
    return [
        {"role": "user", "content": "c8 zr1X specs"},
        {"role": "tool", "content": page("https://carbuzz.com/x", HORSEPOWER)},
        {"role": "tool", "content": page("https://caranddriver.com/y", TORQUE)},
        {"role": "tool", "content": page("https://motor1.com/z", "Curb weight is 4,139 lb.")},
        {"role": "tool", "content": page("https://chevrolet.com/w", "It starts at $207,395.")},
    ]


class TestATrimmedPageStillAnswersTheQuestion:

    def terms(self):
        return pruning.terms_of("c8 zr1X specs horsepower torque")

    def test_the_question_becomes_terms_worth_matching(self):
        terms = self.terms()
        assert "zr1x" in terms and "horsepower" in terms and "torque" in terms
        # The words that match every page and mean nothing are dropped.
        assert "specs" not in terms and "the" not in terms

    def test_the_head_alone_would_have_kept_the_navigation(self, transcript):
        # What it used to do, pinned so the difference is not theoretical.
        out, _ = pruning.prune([dict(m) for m in transcript], 100000)
        assert "1,250 horsepower" not in out[1]["content"]
        assert "nav link number" in out[1]["content"]

    def test_the_fact_survives_the_trim(self, transcript):
        out, _ = pruning.prune([dict(m) for m in transcript], 100000, self.terms())
        assert "1,250 horsepower" in out[1]["content"]

    def test_it_is_not_paid_for_in_room(self, transcript):
        """Keeping the useful part is not a licence to keep more of it."""
        plain, plain_report = pruning.prune([dict(m) for m in transcript], 100000)
        smart, smart_report = pruning.prune([dict(m) for m in transcript], 100000,
                                            self.terms())
        assert smart_report["freed"] >= plain_report["freed"]
        assert len(smart[1]["content"]) <= len(plain[1]["content"])

    def test_the_source_is_still_identifiable(self, transcript):
        # The head is kept as well as the passages: a trimmed result the model
        # cannot attribute is one it will re-read.
        out, _ = pruning.prune([dict(m) for m in transcript], 100000, self.terms())
        assert "carbuzz.com" in out[1]["content"]

    def test_a_page_with_nothing_relevant_falls_back_to_its_head(self):
        content = "\n".join(f"unrelated paragraph number {i} about something else"
                            for i in range(200))
        kept = pruning._relevant(content, 400, ("zr1x",))
        assert kept == content[:400]

    def test_the_estimate_matches_what_the_trim_will_do(self, transcript):
        """`prunable_tokens` decides between pruning and giving up. Measured
        without the terms it promises room the trim will not deliver."""
        promised = pruning.prunable_tokens(transcript, self.terms())
        _, report = pruning.prune([dict(m) for m in transcript], 10 ** 9, self.terms())
        assert report["freed"] == promised

    def test_the_recent_results_are_still_untouched(self, transcript):
        # What the model is acting on this round is not context to save.
        out, _ = pruning.prune([dict(m) for m in transcript], 100000, self.terms())
        assert out[-1]["content"] == transcript[-1]["content"]
        assert out[-2]["content"] == transcript[-2]["content"]

    def test_the_user_is_never_trimmed(self, transcript):
        out, _ = pruning.prune([dict(m) for m in transcript], 10 ** 9, self.terms())
        assert out[0] == transcript[0]


class TestTheTermsComeFromTheQuestion:
    """Not from `working`, which by the time anything needs pruning ends with a
    nudge we wrote — trimming pages against our own prompts would keep the
    paragraphs about citations rather than the ones with the answer in them."""

    def test_the_loop_reads_the_last_user_turn_of_the_history(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "carrot" / "app.py").read_text(
            encoding="utf-8")
        assert 'for m in reversed(history) if m.get("role") == "user"' in source

    def test_an_empty_question_is_not_an_error(self):
        assert pruning.terms_of("") == ()
        assert pruning.terms_of(None) == ()
