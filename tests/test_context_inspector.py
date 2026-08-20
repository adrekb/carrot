"""Context · what the model is actually receiving.

The system half of a prompt is assembled from eight independent switches —
answer style, the search directive, a skill, a document, the workspace's rules,
the calendar, the screen roster, memory, the rolling summary — and the only way
to find out which of them fired on a given turn was to read `_prepare_history`.
That is the wrong place for it. "Why does it know about my calendar" and "why
did it not use what it remembers" are questions asked at the composer, about
the turn being typed.

The load-bearing decision is that there is no second implementation. The
preview is the real builder with the model call left off, so a source that
stops being included stops being reported in the same commit. A preview
written separately would eventually describe a prompt the model never got,
which is worse than no preview at all.
"""
import re
from pathlib import Path

import pytest

from carrot import app as app_mod, config

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "carrot" / "web"


def build(message="hello", conv=None, **kwargs):
    manifest = []
    history, _ = app_mod._prepare_history(conv, message, "", manifest=manifest,
                                          **kwargs)
    return history, manifest


def included(manifest):
    return [row["source"] for row in manifest if row["included"]]


class TestTheManifestDescribesTheRealPrompt:
    def test_every_system_block_is_accounted_for(self, isolated_db):
        history, manifest = build()
        blocks = sum(1 for turn in history if turn["role"] == "system")
        assert blocks == len(included(manifest))

    def test_switching_one_off_removes_it_from_the_prompt(self, isolated_db):
        _, before = build()
        assert "style" in included(before)
        config.set_config(app_mod.CONTEXT_OFF_KEY, ["style"])
        history, after = build()
        assert "style" not in included(after)
        assert not any("style" in str(t.get("content", "")).lower()[:40]
                       for t in history if t["role"] == "system")

    def test_a_disabled_source_is_still_reported(self, isolated_db):
        """Switched off is a thing to show, not a thing to hide — the row is
        how you switch it back on."""
        config.set_config(app_mod.CONTEXT_OFF_KEY, ["style"])
        _, manifest = build()
        row = next(r for r in manifest if r["source"] == "style")
        assert row["included"] is False
        assert row["chars"] > 0

    def test_the_builder_is_the_only_builder(self):
        """The endpoint calls `_prepare_history`. If it ever stops, this whole
        file is testing a fiction."""
        source = (ROOT / "carrot" / "app.py").read_text(encoding="utf-8")
        preview = re.search(r"async def context_preview\(.*?\n\n\n", source, re.DOTALL)
        assert preview, "context_preview not found"
        assert "_prepare_history(" in preview.group(0)


class TestWhatMayBeSwitchedOff:
    def test_the_conversation_may_not(self, isolated_db):
        """Offering to drop the conversation from the prompt is offering to
        ignore what was just asked."""
        assert "recent" not in app_mod.CONTEXT_TOGGLEABLE

    @pytest.mark.parametrize("source", ["search", "skill", "document"])
    def test_an_explicit_choice_may_not(self, source):
        """These are already a choice you made this turn — the search picker,
        the skill you invoked, the document you sent. A second switch that
        countermands them is two controls disagreeing."""
        assert source not in app_mod.CONTEXT_TOGGLEABLE

    @pytest.mark.parametrize("source", ["memory", "calendar", "screen", "coder", "summary"])
    def test_a_standing_setting_may(self, source):
        assert source in app_mod.CONTEXT_TOGGLEABLE

    def test_a_stale_config_cannot_drop_a_fixed_source(self, isolated_db):
        """A config naming a source that is no longer optional would silently
        take the conversation out of every prompt."""
        config.set_config(app_mod.CONTEXT_OFF_KEY, ["recent", "memory"])
        assert app_mod.context_disabled() == {"memory"}

    def test_every_source_has_a_label_and_a_reason(self):
        for source, label, detail, _ in app_mod.CONTEXT_SOURCES:
            assert label and detail, source
            assert label[0].isupper(), label


class TestTheEndpoint:
    def test_it_reports_every_source(self, client):
        payload = client.get("/api/context", params={"message": "hi"}).json()
        ids = [s["id"] for s in payload["sources"]]
        assert ids == [s for s, _, _, _ in app_mod.CONTEXT_SOURCES]

    def test_present_is_not_the_same_as_enabled(self, client):
        """The calendar can be switched on and contribute nothing on a day with
        nothing in it. Reporting that as "on" with no qualifier is how someone
        concludes the feature is broken."""
        payload = client.get("/api/context", params={"message": "hi"}).json()
        calendar = next(s for s in payload["sources"] if s["id"] == "calendar")
        assert calendar["enabled"] is True
        assert calendar["present"] is False

    def test_the_count_is_of_what_is_actually_going(self, client):
        payload = client.get("/api/context", params={"message": "hi"}).json()
        expected = sum(1 for s in payload["sources"] if s["present"] and s["enabled"])
        assert payload["items"] == expected

    def test_a_present_source_carries_the_text_itself(self, client):
        """The row said "Answer style · 408 chars" and nothing about what those
        408 characters instruct. The size answers "is it there"; the question
        people actually have is what it says."""
        payload = client.get("/api/context", params={"message": "hi"}).json()
        style = next(s for s in payload["sources"] if s["id"] == "style")
        assert style["present"] is True
        assert style["preview"].strip()

    def test_an_absent_source_previews_nothing(self, client):
        payload = client.get("/api/context", params={"message": "hi"}).json()
        calendar = next(s for s in payload["sources"] if s["id"] == "calendar")
        assert calendar["preview"] == ""
        assert calendar["truncated"] is False

    def test_a_long_block_is_cut_and_says_so(self, client, isolated_db):
        """A preview that silently stops is one that gets trusted about the
        wrong things — so the cut is reported and the UI prints both numbers."""
        long_style = "Write plainly. " * 400
        config.set_config("answer_style_custom", long_style)
        payload = client.get("/api/context", params={"message": "hi"}).json()
        style = next(s for s in payload["sources"] if s["id"] == "style")
        if style["chars"] > app_mod.CONTEXT_PREVIEW_CHARS:
            assert style["truncated"] is True
            assert len(style["preview"]) <= app_mod.CONTEXT_PREVIEW_CHARS

    def test_a_toggle_persists(self, client):
        client.post("/api/context/toggle", json={"source": "memory", "enabled": False})
        payload = client.get("/api/context").json()
        memory = next(s for s in payload["sources"] if s["id"] == "memory")
        assert memory["enabled"] is False

    def test_a_fixed_source_is_refused(self, client):
        answer = client.post("/api/context/toggle",
                             json={"source": "recent", "enabled": False})
        assert answer.status_code == 400

    def test_it_works_before_a_conversation_exists(self, client):
        """The first message of a new chat, which is exactly when someone opens
        this to see what Carrot already knows about them."""
        payload = client.get("/api/context", params={"message": "hi"}).json()
        assert payload["sources"]
        assert payload["chars"] > 0


class TestTheBuilderSurvivesNoConversation:
    def test_it_does_not_raise(self, isolated_db):
        """`build_history` reads `conversation["messages"]` and raised on None.
        The preview was swallowing that and reporting a manifest that stopped
        halfway — every source after the summary silently missing."""
        history, manifest = build(conv=None)
        assert history
        assert included(manifest)

    def test_the_manifest_reaches_the_end(self, isolated_db):
        """The specific symptom: `screen` comes after the point that raised."""
        _, manifest = build(conv=None)
        assert "screen" in [row["source"] for row in manifest]


class TestTheChip:
    def test_it_sits_with_the_model_picker(self):
        index = (WEB / "index.html").read_text(encoding="utf-8")
        assert 'id="context-picker"' in index
        assert index.index('id="context-picker"') < index.index('id="model-picker"')

    def test_it_counts_in_the_label(self):
        js = (WEB / "js" / "context.js").read_text(encoding="utf-8")
        assert "'Context · '" in js

    def test_it_reads_the_composer(self):
        """Memory recall is a search against what you are about to ask. An
        inspector that previewed the empty string would always report that
        Carrot remembers nothing about you."""
        js = (WEB / "js" / "context.js").read_text(encoding="utf-8")
        assert "cmd-input" in js
        assert "params.set('message'" in js

    def test_it_does_not_rebuild_a_prompt_per_keystroke(self):
        """This builds a real prompt server-side, memory recall included."""
        js = (WEB / "js" / "context.js").read_text(encoding="utf-8")
        assert "clearTimeout(contextTimer)" in js

    def test_an_idle_source_is_shown_rather_than_hidden(self):
        """A list that changes length as you type is a list you cannot learn,
        and "memory: nothing this turn" is information."""
        js = (WEB / "js" / "context.js").read_text(encoding="utf-8")
        assert "source.present ? '' : ' idle'" in js
        css = (WEB / "css" / "style.css").read_text(encoding="utf-8")
        # `.context-item` rather than `.context-row`: the row became a
        # container the moment it grew a second control, so the state that
        # describes the whole source moved up to it.
        assert ".context-item.idle" in css

    def test_off_is_marked_twice_over(self):
        """On a list of ten rows a colour change alone reads as "less
        important" rather than "not going"."""
        css = (WEB / "css" / "style.css").read_text(encoding="utf-8")
        assert "line-through" in re.search(r"\.context-item\.off[^}]*\}", css).group(0)

    def test_the_tick_and_the_eye_are_different_controls(self):
        """One button meant the only thing you could do to a source was silence
        it — you could never find out what it was you had silenced."""
        js = (WEB / "js" / "context.js").read_text(encoding="utf-8")
        assert "data-toggle=" in js and "data-open=" in js
        assert "openContextSource" in js

    def test_the_block_opens_on_the_reader_page_not_in_the_row(self):
        """Ten rows that can each grow four hundred words is a list that
        reorders itself under the hand every time you look at one — and a
        layer over the composer lands under the picker and the command bar,
        both of which sit higher in the stack. So it is a page you close."""
        index = (WEB / "index.html").read_text(encoding="utf-8")
        assert 'id="view-reader"' in index
        assert 'id="reader-text"' in index
        assert "openReaderPage" in (WEB / "js" / "context.js").read_text(encoding="utf-8")
        app_js = (WEB / "js" / "app.js").read_text(encoding="utf-8")
        assert "switchTab('reader')" in app_js
        # Closing returns you where you were, rather than to a tab the reader
        # picked on your behalf.
        assert "readerReturnTab" in app_js

    def test_the_reader_is_shared_with_the_scheduled_reports(self):
        """Two pages differing only in their heading is two things to maintain
        and one of them going stale."""
        assert "openReaderPage" in (WEB / "js" / "scheduled.js").read_text(encoding="utf-8")

    def test_the_script_is_loaded(self):
        index = (WEB / "index.html").read_text(encoding="utf-8")
        assert '<script src="/js/context.js"></script>' in index
