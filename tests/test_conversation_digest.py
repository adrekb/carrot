"""A conversation as a document you can hand to another conversation.

The feature exists because carrying Tuesday's chat into today's meant pasting
several hundred lines of transcript into the box — which spends the context
window on the *shape* of a conversation rather than on what it concluded.

Three things here are worth holding in a test, and they are the three that were
got wrong or nearly wrong while building it:

* **The digest and the rolling summary share a row and are not the same
  thing.** `save_summary` used `INSERT OR REPLACE`, which is a delete followed
  by an insert — so every rolling summarisation pass would have silently thrown
  the digest away. That is invisible until somebody presses the button and gets
  back an older document than the one they read yesterday.

* **Stale is the load-bearing field.** A summary written forty turns ago that
  does not say so is worse than none, because it is the one you would attach.

* **No model is a real code path, not an error.** This runs on machines where
  the local model is exactly what is unavailable, and a button that answers
  "Ollama is not running" is a button that gets pressed once.
"""
import re
from pathlib import Path

import pytest

from carrot import conversation as conv_mod, summarize

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "carrot" / "web"


@pytest.fixture
def conversation(isolated_db):
    conv = conv_mod.create_conversation(title="Group pills and the ProseMirror layer")
    conv_mod.add_message(conv["id"], "user", "Why does the chip vanish 120ms after it is drawn?")
    conv_mod.add_message(conv["id"], "assistant",
                         "ProseMirror rebuilds its DOM from its own state and discards "
                         "anything it did not put there. Draw it as a decoration.")
    conv_mod.add_message(conv["id"], "user", "Make it a decoration then.")
    return conv


class FakeRoute:
    model = "phi4:14b"


def fake_complete(_route, messages):
    assert messages[0]["role"] == "system"
    return ("## What this was about\nThe group chip.\n\n"
            "## What was decided\n- Draw it as a ProseMirror decoration.\n")


class TestTheDocument:

    def test_it_is_markdown_with_a_title_and_its_provenance(self, conversation):
        state = summarize.build_digest(conversation["id"], title=conversation["title"],
                                       routes=[FakeRoute()], complete=fake_complete)
        assert state["markdown"].splitlines()[0] == "# Group pills and the ProseMirror layer"
        # Who wrote it and over how much. A file that says it summarises three
        # messages is one the reader can decide whether to trust.
        assert "Summary of 3 messages" in state["markdown"]
        assert "phi4:14b" in state["markdown"]

    def test_the_filename_is_the_conversation_not_its_id(self, conversation):
        state = summarize.build_digest(conversation["id"], title=conversation["title"])
        assert state["filename"] == "group-pills-and-the-prosemirror-layer.md"

    def test_an_untitled_conversation_still_gets_a_usable_filename(self, isolated_db):
        assert summarize.digest_filename("") == "conversation.md"
        assert summarize.digest_filename("!!! ???") == "conversation.md"

    def test_a_title_the_model_wrote_anyway_is_dropped(self, conversation):
        def titled(_route, _messages):
            return "# A title it was told not to write\n\n## What this was about\nThe chip."

        state = summarize.build_digest(conversation["id"], title="Real title",
                                       routes=[FakeRoute()], complete=titled)
        assert state["markdown"].splitlines()[0] == "# Real title"
        assert "A title it was told not to write" not in state["markdown"]

    def test_it_is_capped(self, conversation):
        state = summarize.build_digest(conversation["id"], title="x", routes=[FakeRoute()],
                                       complete=lambda *_: "z" * 99999)
        assert len(state["markdown"]) <= summarize.MAX_DIGEST_CHARS


class TestWithoutAModel:
    """The fallback is a feature, not a placeholder."""

    def test_no_model_still_produces_a_document(self, conversation):
        state = summarize.build_digest(conversation["id"], title=conversation["title"])
        assert state["exists"]
        assert "Why does the chip vanish" in state["markdown"]
        assert "without a model" in state["markdown"]

    def test_a_model_that_raises_falls_back_rather_than_failing(self, conversation):
        def broken(_route, _messages):
            raise RuntimeError("ollama is not running")

        state = summarize.build_digest(conversation["id"], title=conversation["title"],
                                       routes=[FakeRoute()], complete=broken)
        assert state["exists"]
        assert "without a model" in state["markdown"]

    def test_a_model_that_answers_with_nothing_falls_back_too(self, conversation):
        state = summarize.build_digest(conversation["id"], title="x",
                                       routes=[FakeRoute()], complete=lambda *_: "   ")
        assert "without a model" in state["markdown"]


class TestStaleness:

    def test_a_fresh_digest_is_not_stale(self, conversation):
        summarize.build_digest(conversation["id"], title=conversation["title"])
        assert summarize.digest_state(conversation["id"])["stale"] is False

    def test_one_more_turn_makes_it_stale(self, conversation):
        summarize.build_digest(conversation["id"], title=conversation["title"])
        conv_mod.add_message(conversation["id"], "user", "and one more thing")
        assert summarize.digest_state(conversation["id"])["stale"] is True

    def test_nothing_written_is_not_stale(self, conversation):
        state = summarize.digest_state(conversation["id"])
        assert state["exists"] is False
        # Not stale, because there is nothing to be out of date. A dot over an
        # absence is a button that lies.
        assert state["stale"] is False

    def test_an_empty_conversation_cannot_be_summarised(self, isolated_db):
        conv = conv_mod.create_conversation(title="nothing here")
        with pytest.raises(ValueError):
            summarize.build_digest(conv["id"], title="nothing here")


class TestItSharesARowWithTheRollingSummary:
    """They live in one table and are not the same artefact. This class exists
    for `INSERT OR REPLACE`, which deletes one while writing the other."""

    def test_the_rolling_summary_does_not_delete_the_digest(self, conversation):
        summarize.build_digest(conversation["id"], title=conversation["title"])
        summarize.save_summary(conversation["id"], "rolling prose about the earlier part",
                               covered_through=1, message_count=2)
        assert summarize.digest_state(conversation["id"])["markdown"]

    def test_the_digest_does_not_delete_the_rolling_summary(self, conversation):
        summarize.save_summary(conversation["id"], "rolling prose about the earlier part",
                               covered_through=1, message_count=2)
        summarize.build_digest(conversation["id"], title=conversation["title"])
        assert summarize.get_summary(conversation["id"])["summary"] == \
            "rolling prose about the earlier part"

    def test_a_long_thread_is_digested_through_its_rolling_summary(self, conversation):
        """What keeps a 500-turn conversation from being sent in full to
        describe itself."""
        summarize.save_summary(conversation["id"], "THE EARLIER PART, ALREADY CONDENSED",
                               covered_through=1, message_count=2)
        seen = {}

        def capture(_route, messages):
            seen["prompt"] = messages[-1]["content"]
            return "## What this was about\nx"

        summarize.build_digest(conversation["id"], title="x",
                               routes=[FakeRoute()], complete=capture)
        assert "THE EARLIER PART, ALREADY CONDENSED" in seen["prompt"]


class TestTheEndpoints:
    """Two, and not one. The GET runs on every conversation you open — it has
    to know whether a summary exists and whether it is current — so a GET that
    quietly ran a model would put a model call behind every click in the
    history list."""

    def test_reading_never_writes(self, client):
        conv = conv_mod.create_conversation(title="Reading is free")
        conv_mod.add_message(conv["id"], "user", "hello")
        body = client.get(f"/api/conversations/{conv['id']}/digest").json()
        assert body["exists"] is False
        assert body["messages"] == 1
        assert summarize.digest_state(conv["id"])["exists"] is False

    def test_writing_returns_the_document(self, client):
        conv = conv_mod.create_conversation(title="Writing costs a call")
        conv_mod.add_message(conv["id"], "user", "why does the chip vanish?")
        body = client.post(f"/api/conversations/{conv['id']}/digest").json()
        assert body["exists"] is True
        assert body["markdown"].startswith("# Writing costs a call")
        assert body["filename"].endswith(".md")

    def test_an_unknown_conversation_is_a_404_not_an_empty_document(self, client):
        assert client.get("/api/conversations/nope/digest").status_code == 404
        assert client.post("/api/conversations/nope/digest").status_code == 404

    def test_a_conversation_with_nothing_in_it_says_so(self, client):
        conv = conv_mod.create_conversation(title="empty")
        assert client.post(f"/api/conversations/{conv['id']}/digest").status_code == 400


class TestWhichModelWritesIt:
    """Summarising is local-only by default, and rightly so — the *rolling*
    summary runs on every message. This button is pressed once, by hand, and
    inherited that default: a user with a cloud key and no Ollama would have
    got a bullet list of their own sentences every time, with a working model
    one tab away."""

    def test_the_routes_are_tried_in_order(self, conversation):
        tried = []

        class First:
            model = "the-summarize-model"

        class Second:
            model = "the-chat-model"

        def only_the_second_answers(route, _messages):
            tried.append(route.model)
            if route.model == "the-summarize-model":
                raise RuntimeError("that provider is not configured")
            return "## What this was about\nx"

        state = summarize.build_digest(conversation["id"], title="x",
                                       routes=[First(), Second()],
                                       complete=only_the_second_answers)
        assert tried == ["the-summarize-model", "the-chat-model"]
        assert state["model"] == "the-chat-model"

    def test_the_first_that_answers_wins_and_the_rest_are_not_called(self, conversation):
        tried = []

        class R:
            def __init__(self, model):
                self.model = model

        def answer(route, _messages):
            tried.append(route.model)
            return "## What this was about\nx"

        summarize.build_digest(conversation["id"], title="x",
                               routes=[R("a"), R("b")], complete=answer)
        assert tried == ["a"]

    def test_the_model_that_wrote_it_is_reported(self, conversation):
        summarize.build_digest(conversation["id"], title="x",
                               routes=[FakeRoute()], complete=fake_complete)
        assert summarize.digest_state(conversation["id"])["model"] == "phi4:14b"

    def test_no_model_reports_no_model(self, conversation):
        # The field the UI reads to say "this lists what was said rather than
        # what it amounted to" — and to offer the way to fix it.
        summarize.build_digest(conversation["id"], title="x")
        assert summarize.digest_state(conversation["id"])["model"] == ""

    def test_a_model_that_fails_says_why_in_the_log(self, conversation, caplog):
        """The fallback is deliberate and produces a usable file either way,
        which is exactly what makes a silent `except` here expensive: "no model
        is running" and "the model timed out on a long thread" are the same
        screen, and only one of them is worth doing anything about. It cost an
        hour of guessing at a failure that had left no trace."""
        def broken(_route, _messages):
            raise TimeoutError("read timed out")

        with caplog.at_level("INFO", logger="carrot.summarize"):
            summarize.build_digest(conversation["id"], title="x",
                                   routes=[FakeRoute()], complete=broken)
        assert "phi4:14b" in caplog.text
        assert "read timed out" in caplog.text

    def test_a_model_that_answers_with_nothing_says_that_too(self, conversation, caplog):
        with caplog.at_level("INFO", logger="carrot.summarize"):
            summarize.build_digest(conversation["id"], title="x",
                                   routes=[FakeRoute()], complete=lambda *_: "")
        assert "answered with nothing" in caplog.text

    def test_the_chat_model_is_the_second_candidate(self, isolated_db):
        from carrot import app as carrot_app, router as router_mod

        routes = carrot_app._digest_routes()
        assert routes, "nothing may write a summary"
        # Deduplicated: on the common setup both tasks resolve to the same
        # on-device model, and trying it twice would only double the wait
        # before the transcript fallback.
        assert len({(r.provider, r.model) for r in routes}) == len(routes)
        assert routes[0].task == router_mod.TASK_SUMMARIZE

    def test_a_cloud_only_setup_still_reaches_a_model(self, isolated_db, monkeypatch):
        from carrot import app as carrot_app, router as router_mod

        # Summarize stays on-device (it is local-only and unpinned); chat is
        # assigned to a cloud provider. Without the chain this is the setup
        # that could never produce a written summary.
        monkeypatch.setattr(router_mod, "assignment",
                            lambda task: {"provider": "openai", "model": "gpt-x"}
                            if task == router_mod.TASK_CHAT else None)
        monkeypatch.setattr(router_mod.providers_mod, "usable", lambda provider: True)
        routes = carrot_app._digest_routes()
        assert "gpt-x" in [r.model for r in routes]

# ===== The corner icon =====
#
# The explanation is part of the feature, not decoration on it: an unlabelled
# glyph in a corner is a thing people learn by pressing, and this one costs a
# model call to press.


@pytest.fixture(scope="module")
def digest_js():
    return (WEB / "js" / "digest.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index_html():
    return (WEB / "index.html").read_text(encoding="utf-8")


class TestTheIcon:

    def test_it_is_loaded(self, index_html):
        assert '<script src="/js/digest.js"></script>' in index_html

    def test_it_sits_beside_the_conversation_it_summarises(self, index_html):
        assert index_html.index('id="chat-title"') < index_html.index('id="digest-btn"')
        assert 'id="digest-picker"' in index_html

    def test_the_i_explains_what_the_icon_means(self, index_html):
        block = re.search(r'<span class="digest-info"(.*?)</span>', index_html, re.DOTALL)
        assert block, "no info affordance beside the summary heading"
        assert "#i-info" in block.group(1)
        title = re.search(r'title="([^"]+)"', block.group(1))
        assert title and len(title.group(1)) > 60, "the info icon carries no explanation"

    def test_it_is_absent_until_there_is_something_to_summarise(self, digest_js, index_html):
        assert 'id="digest-btn" class="ws-tab-action hidden"' in index_html
        assert "if (!currentConversationId)" in digest_js

    def test_the_document_can_be_attached_rather_than_only_read(self, digest_js):
        # The entire point: it goes into the composer's tray as a .md, by the
        # same route a note takes, instead of being pasted.
        assert "stageDocument(name, text)" in digest_js
        assert 'data-act="new"' in digest_js
        assert 'data-act="attach"' in digest_js

    def test_a_fresh_chat_is_cleared_before_the_file_is_put_in_it(self, digest_js):
        # `newChat` empties the transcript and leaves the tray alone, so the
        # order matters: clear the room, then put the file on the table.
        body = digest_js[digest_js.index("function attachDigest"):]
        assert body.index("if (fresh) newChat();") < body.index("stageDocument(")

    def test_it_says_when_no_model_wrote_it(self, digest_js):
        # Otherwise the difference is a line of italics inside the document,
        # which reads like part of the summary rather than a fact about it.
        assert "!digestState.model" in digest_js
        assert "Settings" in digest_js and "Models" in digest_js

    def test_writing_one_is_the_only_action_that_costs_a_model_call(self, digest_js):
        posts = re.findall(r"api\([^)]*\{\s*method:\s*'POST'", digest_js, re.DOTALL)
        assert len(posts) == 1, "something other than the write button runs the model"

    def test_the_styles_it_needs_exist(self):
        css = (WEB / "css" / "style.css").read_text(encoding="utf-8")
        for selector in ("#digest-pop", ".digest-dot", ".digest-act", ".digest-md"):
            assert selector in css, f"{selector} is drawn by nothing"
