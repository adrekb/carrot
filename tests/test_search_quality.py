"""Knowing the date, preferring real sources, and never answering with nothing.

All three come from one reported turn: asked for "recent american political
news", the assistant searched without knowing the year, got back content farms
and a 2020 satire page, read one of them, deliberated at length in its
thinking, and produced "(no response)".
"""
from unittest.mock import patch

import pytest

from carrot import agent_tools, app as A, websearch


def tool_call(name, **args):
    return [{"id": name, "function": {"name": name, "arguments": args}}]


class TestNeverAnswerWithNothing:
    """A model can spend its whole reply in `thinking`, which is not content.
    The gate then ran out of nudges and accepted the empty string."""

    def test_an_empty_answer_triggers_one_more_ask(self):
        asked = {}

        def fake(resolved, messages, tools=None):
            if "Stop searching and answer now" in messages[-1]["content"]:
                asked["yes"] = True
                asked["tools"] = tools
                yield {"type": "text", "text": "Here is what I found."}
                return
            yield {"type": "thinking", "text": "thinking silently..."}

        class Route:
            def as_dict(self):
                return {}

        with patch.object(A.router_mod, "stream_events", fake), \
             patch.object(A, "_run_tool", lambda n, a, c: iter([{"_tool_result": "R"}])), \
             patch.object(A, "_available_tools", lambda m: [{"name": "x"}]):
            events = list(A._agentic_chat_events(
                [{"role": "user", "content": "recent american political news"}],
                Route(), None, None, A.SEARCH_MULTI))

        final = next(e["_final_text"] for e in events if "_final_text" in e)
        assert asked.get("yes"), "the model was never asked to write an answer"
        assert asked["tools"] is None, "the final ask must not offer more tools"
        assert final == "Here is what I found."

    def test_the_answer_reaches_the_user(self):
        def fake(resolved, messages, tools=None):
            if "Stop searching and answer now" in messages[-1]["content"]:
                yield {"type": "text", "text": "The answer."}
                return
            yield {"type": "thinking", "text": "..."}

        class Route:
            def as_dict(self):
                return {}

        with patch.object(A.router_mod, "stream_events", fake), \
             patch.object(A, "_run_tool", lambda n, a, c: iter([{"_tool_result": "R"}])), \
             patch.object(A, "_available_tools", lambda m: [{"name": "x"}]):
            events = list(A._agentic_chat_events(
                [{"role": "user", "content": "q"}], Route(), None, None, A.SEARCH_MULTI))
        assert "".join(e["chunk"] for e in events if "chunk" in e) == "The answer."


class TestTheDateTool:
    def test_it_exists_and_reports_today(self):
        import datetime

        out = agent_tools.TOOLS["current_datetime"]["handler"]()
        assert datetime.datetime.now().strftime("%Y-%m-%d") in out

    def test_it_needs_no_arguments(self):
        assert agent_tools.TOOLS["current_datetime"]["parameters"]["properties"] == {}

    def test_it_reads_nothing_and_changes_nothing(self):
        assert agent_tools.TOOLS["current_datetime"]["mutating"] is False

    def test_the_search_directives_tell_the_model_to_call_it(self):
        for mode in (A.SEARCH_SINGLE, A.SEARCH_MULTI):
            assert "current_datetime" in A.search_directive(mode)

    def test_no_search_mode_does_not_get_web_guidance(self):
        assert "current_datetime" not in A.search_directive(A.SEARCH_OFF)

    def test_it_is_available_even_without_web_access(self):
        """Knowing the date needs no network, and a turn that cannot search
        still benefits from not guessing the year."""
        # Built-ins are offered under a carrot__ prefix.
        names = {t["function"]["name"] for t in A._available_tools(A.SEARCH_OFF)}
        assert any(n.endswith("current_datetime") for n in names), sorted(names)


class TestSourceRanking:
    @pytest.mark.parametrize("url", [
        "https://www.reuters.com/world/us/",
        "https://apnews.com/hub/politics",
        "https://www.congress.gov/bill/119th",
        "https://en.wikipedia.org/wiki/Thing",
        "https://docs.python.org/3/library/os.html",
        "https://某.edu/paper",
    ])
    def test_known_good_sources_rank_first(self, url):
        assert websearch.source_rank(url) == 0

    @pytest.mark.parametrize("url", [
        "https://242movietv.com/2026/02/20/american-news/",
        "https://streamingcasino.net/politics",
    ])
    def test_filler_shapes_rank_last(self, url):
        assert websearch.source_rank(url) == 2

    def test_an_unknown_site_is_neutral_not_blocked(self):
        """Blocking would lose the long tail of legitimate small sites, which
        is most of the web. This is a ranking."""
        assert websearch.source_rank("https://someones-blog.dev/post") == 1

    def test_ranking_puts_reputable_first_and_keeps_the_rest(self):
        results = [
            {"title": "", "url": "https://242movietv.com/x", "snippet": ""},
            {"title": "", "url": "https://unknown.example/y", "snippet": ""},
            {"title": "", "url": "https://reuters.com/z", "snippet": ""},
        ]
        ranked = websearch.rank_results(results)
        assert websearch.domain_of(ranked[0]["url"]) == "reuters.com"
        assert len(ranked) == 3, "ranking must not drop results"

    def test_order_within_a_tier_is_preserved(self):
        results = [{"title": "", "url": f"https://reuters.com/{i}", "snippet": ""}
                   for i in range(3)]
        assert [r["url"] for r in websearch.rank_results(results)] == \
               [r["url"] for r in results]

    def test_the_directive_warns_about_unrecognised_sources(self):
        assert "do not recognise" in A.search_directive(A.SEARCH_MULTI)


class TestToolchainOnCreate:
    def test_a_new_python_file_reports_python_is_present(self, client, tmp_path, isolated_db):
        from carrot import config

        config.set_config("code_workspace_dir", str(tmp_path))
        body = client.post("/api/files/create",
                           json={"path": "", "name": "a.py"}).json()
        assert body["toolchain"]["language"] == "Python"
        assert body["toolchain"]["available"] is True

    def test_a_missing_toolchain_names_a_download_page(self, client, tmp_path, isolated_db):
        """"python is not recognised as an internal or external command" is
        not something a non-technical user can act on."""
        from carrot import config, runner

        config.set_config("code_workspace_dir", str(tmp_path))
        with patch.object(runner.shutil, "which", lambda n: None):
            body = client.post("/api/files/create",
                               json={"path": "", "name": "b.cpp"}).json()
        assert body["toolchain"]["available"] is False
        assert body["toolchain"]["install"]
        assert body["toolchain"]["help_url"].startswith("https://")

    def test_a_file_with_no_language_reports_nothing(self, client, tmp_path, isolated_db):
        from carrot import config

        config.set_config("code_workspace_dir", str(tmp_path))
        body = client.post("/api/files/create",
                           json={"path": "", "name": "notes.txt"}).json()
        assert body["toolchain"] == {}

    def test_the_file_is_still_created_when_the_toolchain_is_missing(
            self, client, tmp_path, isolated_db):
        from carrot import config, runner

        config.set_config("code_workspace_dir", str(tmp_path))
        with patch.object(runner.shutil, "which", lambda n: None):
            client.post("/api/files/create", json={"path": "", "name": "c.cpp"})
        assert (tmp_path / "c.cpp").exists()


class TestOverlayVisibility:
    @property
    def overlay(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        return (root / "gui" / "public" / "overlay.html").read_text(encoding="utf-8")

    def test_the_panel_is_near_opaque(self):
        """It floats over an arbitrary desktop; at 0.82 the text competes with
        whatever is behind it."""
        import re

        for match in re.findall(r"--panel: rgba\([^)]*?([\d.]+)\);", self.overlay):
            assert float(match) >= 0.95, "the quick-ask panel is too transparent to read"

    def test_it_has_a_dictation_button(self):
        assert 'id="dictate"' in self.overlay

    def test_dictation_degrades_when_unavailable(self):
        """A button that does nothing on click is worse than no button."""
        assert "dictateBtn.style.display = 'none'" in self.overlay

    def test_dictation_appends_rather_than_replacing(self):
        assert "dictation appends rather than replaces" in self.overlay


class TestLogoVisibility:
    def test_the_mark_does_not_depend_on_the_accent(self):
        """Tinted mark on a tinted background washes out on some accents; the
        theme guarantees contrast for --text, not for --accent."""
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        css = (root / "carrot" / "web" / "css" / "style.css").read_text(encoding="utf-8")
        block = css[css.index("The rabbit, always visible"):]
        assert "background: var(--text)" in block

    def test_no_hard_coded_dark_only_logo_colour(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        css = (root / "carrot" / "web" / "css" / "style.css").read_text(encoding="utf-8")
        assert ".chat-empty .logo-mask.big { color: #232329; }" not in css
