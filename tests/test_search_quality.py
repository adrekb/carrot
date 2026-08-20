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
            if "QUESTION:" in messages[-1]["content"]:
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
            if "QUESTION:" in messages[-1]["content"]:
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


class TestNoResponseIsUnreachable:
    """The reported bug, twice. The first fix re-asked with the same history —
    which is exactly what had already overrun the model's context — so it
    failed identically. This is the version that cannot fail."""

    class Route:
        model = "gemma4:e4b"

        def as_dict(self):
            return {}

    def silent_run(self, second_pass_text=""):
        """A model that reads pages, then says nothing. Twice."""
        calls = {"n": 0}

        def fake(resolved, messages, tools=None):
            calls["n"] += 1
            if tools:
                yield {"type": "tool_calls", "calls": tool_call(
                    "carrot__read_url", url="https://apnews.com/politics")}
                return
            if second_pass_text:
                yield {"type": "text", "text": second_pass_text}
            # Otherwise: nothing at all, the way an out-of-context model behaves.

        return fake, calls

    def run(self, fake, tool_result="Reuters: the story text"):
        with patch.object(A.router_mod, "stream_events", fake), \
             patch.object(A, "_run_tool", lambda n, a, c: iter([{"_tool_result": tool_result}])), \
             patch.object(A, "_available_tools", lambda m: [{"name": "x"}]):
            return list(A._agentic_chat_events(
                [{"role": "user", "content": "recent american political news"}],
                self.Route(), mode=A.SEARCH_MULTI))

    def final_of(self, events):
        return next(e["_final_text"] for e in events if "_final_text" in e)

    def test_a_silent_model_still_produces_an_answer(self):
        fake, _ = self.silent_run()
        assert self.final_of(self.run(fake)).strip() != ""

    def test_the_retry_does_not_resend_the_bloated_history(self):
        # The whole reason the first fix failed: a model that ran out of room
        # reading six pages has no more room when asked again with all six.
        seen = {}

        def fake(resolved, messages, tools=None):
            if not tools:
                seen["count"] = len(messages)
                seen["chars"] = sum(len(str(m.get("content", ""))) for m in messages)
                return
                yield  # pragma: no cover
            yield {"type": "tool_calls", "calls": tool_call("carrot__read_url", url="https://x")}

        self.run(fake, tool_result="y" * 6000)
        # One message, and bounded by the digest constants no matter how many
        # rounds ran or how large each page was.
        assert seen["count"] == 1
        ceiling = A.EVIDENCE_CHARS * A.MAX_EVIDENCE_SOURCES + 2000
        assert seen["chars"] < ceiling

    def test_the_digest_carries_what_was_actually_read(self):
        seen = {}

        def fake(resolved, messages, tools=None):
            if not tools:
                seen["prompt"] = messages[0]["content"]
                return
                yield  # pragma: no cover
            yield {"type": "tool_calls", "calls": tool_call(
                "carrot__read_url", url="https://apnews.com/politics")}

        self.run(fake, tool_result="AP reported the vote failed 51-49.")
        assert "51-49" in seen["prompt"]
        assert "apnews.com/politics" in seen["prompt"]

    def test_a_second_pass_answer_is_used_when_there_is_one(self):
        fake, _ = self.silent_run("Here is the summary you asked for.")
        assert "summary you asked for" in self.final_of(self.run(fake))

    def test_the_answer_reaches_the_browser_not_just_the_store(self):
        fake, _ = self.silent_run()
        chunks = "".join(e["chunk"] for e in self.run(fake) if "chunk" in e)
        assert chunks.strip() != ""

    def test_the_fallback_names_the_pages_that_were_read(self):
        fake, _ = self.silent_run()
        assert "apnews.com" in self.final_of(self.run(fake))

    def test_the_fallback_shows_what_was_found(self):
        fake, _ = self.silent_run()
        assert "vote failed" in self.final_of(self.run(fake, tool_result="the vote failed 51-49"))

    def test_a_crashing_retry_still_answers(self):
        # A retry that raises must not become an exception the user sees
        # instead of an answer.
        def fake(resolved, messages, tools=None):
            if not tools:
                raise RuntimeError("context window exceeded")
            yield {"type": "tool_calls", "calls": tool_call("carrot__read_url", url="https://x")}

        assert self.final_of(self.run(fake)).strip() != ""

    def test_a_turn_that_gathered_nothing_says_so_usefully(self):
        answer = A._evidence_answer("anything", [])
        assert "could not gather" in answer and "Research" in answer

    def test_the_fallback_never_pretends_it_worked(self):
        # Being honest that the write-up failed is the point; claiming an
        # answer would be worse than "(no response)".
        answer = A._evidence_answer("x", [
            {"tool": "read_url", "source": "https://x", "text": "some text"}])
        assert "could not write it up" in answer


class TestBlockedAndIndexPages:
    """From the reported trace: Politico and Cook Political returned 403 while
    AP and NYT worked, and the pages that *did* load were section fronts with
    no story text on them. Neither was Carrot refusing a source — one was the
    site refusing Carrot, and the other was Carrot reading the wrong page."""

    def test_the_fetcher_no_longer_announces_itself_as_a_bot(self):
        # "Mozilla/5.0 (compatible; Carrot/1.0; local assistant)" is the exact
        # shape bot management 403s.
        assert "compatible; Carrot" not in websearch.USER_AGENT

    def test_a_real_browser_header_set_is_sent(self):
        # A request carrying only User-Agent is trivially fingerprinted.
        for header in ("Accept", "Accept-Language", "User-Agent"):
            assert header in websearch.BROWSER_HEADERS

    def test_a_403_tells_the_model_not_to_retry(self):
        # "HTTP 403" reads like a transient failure, and the model duly tried
        # the same URL twice in one turn.
        message = websearch._status_message(403, "https://www.politico.com/politics")
        assert "Do not retry" in message and "politico.com" in message

    def test_a_429_points_elsewhere_rather_than_at_a_retry(self):
        assert "different source" in websearch._status_message(429, "https://x.com/a")

    def test_a_404_is_not_described_as_a_block(self):
        assert "does not exist" in websearch._status_message(404, "https://x.com/a")

    def test_a_blocked_response_is_flagged_as_such(self):
        assert websearch._status_message(451, "https://x.com/a").startswith("HTTP 451")


class TestSectionFronts:
    def index_links(self, count=40):
        return [{"text": f"Senate passes the bill on measure {i}",
                 "url": f"https://apnews.com/article/story-{i}"} for i in range(count)]

    def test_a_page_of_links_with_no_prose_is_an_index(self):
        assert websearch.looks_like_an_index("Politics\nMore\nSubscribe",
                                              self.index_links()) is True

    def test_an_article_is_not_an_index(self):
        # Real prose, few links.
        article = " ".join(["word"] * 900)
        assert websearch.looks_like_an_index(article, self.index_links(5)) is False

    def test_a_long_article_with_many_links_is_still_an_article(self):
        article = " ".join(["word"] * 3000)
        assert websearch.looks_like_an_index(article, self.index_links(40)) is False

    def test_headlines_are_separated_from_navigation(self):
        links = [
            {"text": "Subscribe", "url": "https://apnews.com/subscribe"},
            {"text": "Home", "url": "https://apnews.com/"},
            {"text": "Senate rejects the funding bill in a late vote",
             "url": "https://apnews.com/article/senate-funding-vote"},
        ]
        picked = websearch.headline_links(links, "https://apnews.com/politics")
        assert len(picked) == 1 and "Senate rejects" in picked[0]["text"]

    def test_offsite_links_are_dropped(self):
        links = [{"text": "Read this excellent thing elsewhere entirely",
                  "url": "https://example.com/a/b/c"}]
        assert websearch.headline_links(links, "https://apnews.com/politics") == []

    def test_shallow_links_are_dropped(self):
        # A story lives below the section, not beside it.
        links = [{"text": "Some fairly long section name here", "url": "https://apnews.com/world"}]
        assert websearch.headline_links(links, "https://apnews.com/politics") == []

    def test_duplicates_are_collapsed(self):
        link = {"text": "Senate rejects the funding bill today",
                "url": "https://apnews.com/article/x"}
        assert len(websearch.headline_links([link, link], "https://apnews.com/politics")) == 1

    def test_reading_an_index_returns_headlines_not_nav_furniture(self):
        from carrot import agent_tools

        page = {
            "error": "", "final_url": "https://apnews.com/politics",
            "text": "Politics\nMore\nSubscribe",
            "links": self.index_links(),
            "screening": {"tainted": False, "signals": []},
        }
        with patch.object(websearch, "fetch", return_value=page):
            out = agent_tools._tool_read_url("https://apnews.com/politics")
        assert "section index" in out
        assert "apnews.com/article/story-0" in out

    def test_reading_a_real_article_is_unchanged(self):
        from carrot import agent_tools

        page = {
            "error": "", "final_url": "https://apnews.com/article/x",
            "text": " ".join(["the vote failed"] * 400),
            "links": [],
            "screening": {"tainted": False, "signals": []},
        }
        with patch.object(websearch, "fetch", return_value=page):
            out = agent_tools._tool_read_url("https://apnews.com/article/x")
        assert "section index" not in out and "the vote failed" in out


class TestTheFilterCannotEmptyAPage:
    """It looked like a source blacklist, and the user asked whether it was.

    It is not — there is no reputation block list anywhere, only a ranking.
    But the lexical relevance filter could drop every result on a page, and an
    empty page for a query that plainly worked is indistinguishable from a
    blocked source. The filter exists to catch a *broken backend*, so it may
    thin a page. It may not empty one.
    """

    def raw(self, *titles):
        return [{"title": t, "href": f"https://example.com/{i}", "body": ""}
                for i, t in enumerate(titles)]

    def test_results_survive_when_none_share_the_query_wording(self):
        from unittest.mock import patch

        from carrot import websearch

        with patch.object(websearch, "_raw_search",
                          lambda q, n, r: self.raw("Primary results roundup",
                                                   "Live vote tallies")):
            results = websearch.search("American political news August 2026")
        assert results, "a working search came back empty because of the filter"

    def test_a_genuinely_broken_backend_is_still_thinned(self):
        from unittest.mock import patch

        from carrot import websearch

        with patch.object(websearch, "_raw_search",
                          lambda q, n, r: self.raw(
                              "RTX 4090 benchmark results deep dive",
                              "Centimetre to feet converter")):
            results = websearch.search("RTX 4090 benchmark results")
        assert len(results) == 1
        assert "4090" in results[0]["title"]


class TestAnIndexPageIsNotTheStory:
    """Asked for "recent us politics news", six of six results were section
    fronts, the model read nytimes.com/section/politics, and answered with the
    site's navigation: "The New York Times (covering US, World News, etc.)".

    A front is not a bad source, it is the wrong kind of page to answer from,
    and nothing in a result said which was which. Adding the month to the same
    query returns four dated articles out of six, so the information was always
    reachable — the search just could not tell the difference.
    """

    ARTICLES = [
        "https://www.theguardian.com/us-news/2026/aug/05/michigan-senate-primary-results",
        "https://reuters.com/world/china/china-purges-third-politburo-member-2026-07-14",
        "https://www.newyorker.com/magazine/2026/08/10/the-future-made-in-china",
    ]
    FRONTS = [
        "https://apnews.com/",
        "https://www.nytimes.com/section/politics",
        "https://www.nbcnews.com/politics",
        "https://www.bbc.com/news/world/asia/china",
    ]

    @pytest.mark.parametrize("url", ARTICLES)
    def test_articles_are_recognised(self, url):
        assert websearch.page_kind(url) == "article"

    @pytest.mark.parametrize("url", FRONTS)
    def test_fronts_are_recognised(self, url):
        assert websearch.page_kind(url) == "front"

    def test_articles_outrank_fronts_within_a_tier(self):
        mixed = [{"title": "", "url": u, "snippet": ""}
                 for u in self.FRONTS + self.ARTICLES]
        ordered = [r["url"] for r in websearch.rank_results(mixed)]
        assert all(websearch.page_kind(u) == "article" for u in ordered[:3])

    def test_source_quality_still_wins(self):
        # An article on a content farm must not outrank a real outlet's front:
        # ordering by page shape alone would be a worse bug than the one this
        # fixes.
        results = [{"title": "", "url": "https://apnews.com/", "snippet": ""},
                   {"title": "", "url": "https://freemovies123.example/a-b-c-d", "snippet": ""}]
        assert websearch.rank_results(results)[0]["url"] == "https://apnews.com/"

    @pytest.mark.parametrize("url,expected", [
        ("https://www.theguardian.com/us-news/2026/aug/05/michigan", "2026-08-05"),
        ("https://reuters.com/world/china/purge-2026-07-14", "2026-07-14"),
        ("https://www.newyorker.com/magazine/2026/08/10/china", "2026-08-10"),
        ("https://apnews.com/", ""),
        ("https://example.com/2026/99/99/nope", ""),
    ])
    def test_the_date_comes_off_the_url(self, url, expected):
        # Free, right far more often than not, and the difference between a
        # card that says "5 Aug 2026" and one that says nothing.
        assert websearch.date_from_url(url) == expected

    def test_the_site_is_named_as_a_person_would(self):
        assert websearch.site_name("https://www.theguardian.com/x") == "The Guardian"
        assert websearch.site_name("https://apnews.com/") == "AP News"
        # Unknown domains still get something printable rather than a blank.
        assert websearch.site_name("https://some-local-paper.co/x") == "Some Local Paper"

    def test_the_model_is_told_which_results_are_indexes(self):
        # It cannot prefer an article if the results all look alike.
        with patch.object(websearch, "search", return_value=[
            {"title": "Politics", "url": "https://apnews.com/", "snippet": "s",
             "site": "AP News", "date": "", "kind": "front"},
        ]):
            shown = agent_tools._tool_web_search("us politics")
        assert "index page" in shown
        assert "AP News" in shown

    def test_the_browser_is_sent_the_structured_sources(self):
        # The trace listed URLs as debug output nobody reads. A card needs the
        # outlet and the date, and re-deriving them in JavaScript would put the
        # rules in two places.
        events = []
        with patch.object(websearch, "search", return_value=[
            {"title": "El-Sayed wins", "url": "https://www.theguardian.com/us-news/2026/aug/05/x",
             "snippet": "s", "site": "The Guardian", "date": "2026-08-05", "kind": "article"},
        ]):
            agent_tools._tool_web_search("michigan primary", emit=events.append)
        sources = [e["sources"] for e in events if "sources" in e]
        assert sources, "nothing was emitted for the browser to render"
        assert sources[0][0]["site"] == "The Guardian"
        assert sources[0][0]["date"] == "2026-08-05"

    def test_the_directive_asks_for_inline_links(self):
        # "Cite the URL" produced answers containing no links at all.
        assert "markdown links" in A.SEARCH_PREAMBLE
        assert "index page" in A.SEARCH_PREAMBLE


class TestTheSourceCardsDoNotHideTheAnswer:
    """First attempt was a sideways-scrolling rail of six wide cards.

    Two things wrong with it. A horizontal scrollbar in the middle of a
    conversation reads as a control to operate rather than a list to glance
    at; and six cards wide enough to need one pushed the answer far enough
    down the page that it looked like there was no answer at all — which is
    exactly how it was reported.
    """

    def read(self, *parts):
        from pathlib import Path
        return Path(__file__).resolve().parents[1].joinpath(
            "carrot", "web", *parts).read_text(encoding="utf-8")

    def test_the_cards_do_not_scroll_sideways(self):
        css = self.read("css", "style.css")
        block = css.split(".source-cards {")[1].split("}")[0]
        assert "overflow-x" not in block, "the rail scrolls again"
        assert "grid" in block, "a fixed row of columns, not a flexible rail"

    def test_the_row_is_bounded_so_the_answer_stays_visible(self):
        # A search returns six; three is a glance, six is a wall.
        js = self.read("js", "app.js")
        body = js.split("function showSources")[1].split("\n}\n")[0]
        assert "MAX_CARDS = 3" in body
        assert "slice(0, MAX_CARDS)" in body, (
            "the cap is declared but never applied to what gets drawn")

    def test_the_cards_sit_above_the_answer_not_inside_it(self):
        # insertBefore(rail, contentEl): appending would bury them under a
        # streaming answer that grows past them.
        js = self.read("js", "app.js")
        assert "insertBefore(rail, contentEl)" in js

    def test_a_later_article_displaces_an_early_index_page(self):
        """The cards showed the wrong three.

        A multi-turn search opens broad — the first round came back as three
        section fronts — and finds the dated article two rounds later. Filling
        the row first-come put the fronts on screen and left the BBC piece the
        answer actually quoted off it. Every round's sources are kept and the
        three shown are chosen from all of them, articles first.
        """
        js = self.read("js", "app.js")
        body = js.split("function showSources")[1].split("\n}\n")[0]
        assert "rail._seen" in body, "sources from earlier rounds are discarded"
        assert "'front' ? 1 : 0" in body, "articles are not preferred over indexes"
        assert "rail.textContent = ''" in body, (
            "the row is appended to rather than redrawn, so an early index page "
            "can never be displaced")


# ===== Answering with the facts, not the names of the facts =====

class TestAnAnswerIsNotATableOfContents:
    """From a reported turn. Asked for "c8 zr1X specs" it searched well, found
    the right sources, and answered:

        Specs available include: 0-60 time, quarter mile times, lap times,
        top speed, price, engine specifications

    Every one of those is the *name* of a number. One of the snippets it had
    already been given said 1,250 combined hp and 1.89s to 60. It described the
    shape of an answer instead of giving one — and because the searching was
    genuinely good, it reads as competence, which is why it kept happening.
    """

    def preamble(self):
        from carrot import app as A

        return A.SEARCH_PREAMBLE

    def test_the_preamble_names_this_failure(self):
        text = self.preamble()
        assert "not with the names of the facts" in text
        assert "table of contents" in text

    def test_it_applies_to_every_mode_that_can_search(self):
        # It was only ever going to be a preamble rule: the mode that produced
        # this is the default one, and the detailed answer-shape guidance lived
        # entirely in multi-turn.
        from carrot import app as A

        for directive in (A.SINGLE_SEARCH_DIRECTIVE, A.MULTI_SEARCH_DIRECTIVE):
            assert "not with the names of the facts" in directive

    def test_a_snippet_counts_as_a_fact_it_has(self):
        # It had the numbers and did not use them, because it had been told to
        # answer from what it read and it had read nothing.
        assert "A snippet is a fact you have" in self.preamble()

    def test_single_pass_says_what_to_do_with_the_results(self):
        # "You may search and read a page when the question needs it" said
        # nothing about what to do once the results came back, in the mode
        # where most turns happen.
        from carrot import app as A

        assert "not an answer built from the result list" in A.SINGLE_SEARCH_DIRECTIVE
        assert "open the best result" in A.SINGLE_SEARCH_DIRECTIVE


class TestTheSinglePassGapIsNotWiredInYet:
    """It exists, it is tested, and it is deliberately not called.

    Multi-turn can push an answer back because it buffers the prose until the
    gates pass. Single streams as it goes, so by the time the answer could be
    judged the user is already reading it — nudging means a second answer under
    the first, or holding the first token back and losing what single-pass is
    for. The docstring says so; this makes sure the claim stays true.
    """

    def test_it_flags_a_turn_that_searched_read_nothing_and_cited_nothing(self):
        from carrot import app as A

        assert A._single_pass_gap(1, 0, "Specs available include 0-60 and top speed.")

    def test_a_cited_snippet_answer_is_left_alone(self):
        from carrot import app as A

        assert A._single_pass_gap(
            1, 0, "It makes 1,250 hp ([Chevrolet](https://chevrolet.com/zr1x)).") is None

    def test_a_turn_that_read_a_page_is_left_alone(self):
        from carrot import app as A

        assert A._single_pass_gap(1, 1, "Anything at all.") is None

    def test_a_turn_that_never_searched_is_left_alone(self):
        from carrot import app as A

        assert A._single_pass_gap(0, 0, "I already knew this.") is None

    def test_the_loop_still_only_gates_multi_turn(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "carrot" / "app.py").read_text(
            encoding="utf-8")
        assert "gap = _search_gate_gap(searches, reads) if gated else None" in source
        assert "_single_pass_gap(searches, reads, content_str)" not in source


class TestTheOpeningSearchKeepsTheQuestionsModelNumbers:
    """From a reported turn. Asked for "c8 zr1X specs", the model searched
    "Toyota C-HR ZR1X 2026 specifications" — it did not know what a C8 was,
    replaced it with a car it had heard of, read eleven pages about a Toyota,
    and delivered a confident, well-formatted answer about the wrong vehicle.
    It never mentioned the substitution, so from outside it looked like a good
    search that simply did not answer the question.

    The drift check cannot see this. The query shares `zr1x` with the question,
    so by its definition it is on topic. What went wrong is not that the query
    left the subject — it is that the most specific term in the question was
    silently swapped for a guess.
    """

    def dropped(self, question, query):
        from carrot import app as A

        return A._dropped_identifiers(question, query)

    def test_the_reported_case(self):
        assert self.dropped("c8 zr1X specs",
                            "Toyota C-HR ZR1X 2026 specifications") == {"c8"}

    def test_a_query_that_keeps_it_is_fine(self):
        assert self.dropped("c8 zr1X specs", "Chevrolet Corvette C8 ZR1X specs") == set()

    def test_case_does_not_matter(self):
        assert self.dropped("c8 zr1X specs", "C8 ZR1X specifications") == set()

    def test_hyphenated_designators_count(self):
        assert self.dropped("what is the F-15EX program",
                            "US Air Force fighter jets") == {"f-15ex"}

    @pytest.mark.parametrize("question,query", [
        ("best pasta recipe", "authentic carbonara recipe"),
        ("who won the election", "2026 midterm results"),
        ("explain quantum entanglement", "quantum entanglement explained simply"),
    ])
    def test_ordinary_questions_never_trigger_it(self, question, query):
        # No identifiers in the question means nothing to drop. The check has
        # to be silent on the overwhelming majority of turns or it is just
        # another way to burn the round budget.
        assert self.dropped(question, query) == set()

    def test_a_bare_number_is_not_an_identifier(self):
        # "4090" alone is a number, not a model designator, and questions are
        # full of numbers. Requiring both a letter and a digit is what keeps
        # this from firing on "what happened in 2026".
        assert self.dropped("rtx 4090 vs 5090 benchmarks",
                            "rtx4090 rtx5090 gaming benchmark") == set()
        assert self.dropped("what happened in 2026", "news summary") == set()

    def test_the_correction_names_what_was_dropped(self):
        from carrot import app as A

        message = A.QUERY_IDENTIFIER_CORRECTION.format(dropped="'c8'", query="Toyota C-HR")
        assert "'c8'" in message
        assert "literally" in message

    def test_only_the_first_search_is_checked(self):
        # A later query narrowing to "1250 hp coupe" has legitimately moved
        # past the model number; the opening move has not earned that.
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "carrot" / "app.py").read_text(
            encoding="utf-8")
        assert 'if bare == "web_search" and searches == 0 else set()' in source

    def test_the_preamble_says_not_to_substitute_silently(self):
        from carrot import app as A

        assert "Search the user's words before your interpretation" in A.SEARCH_PREAMBLE
        assert "search it literally first" in A.SEARCH_PREAMBLE


class TestTheForcedAnswerAsksForFactsNotCoverage:
    """The last clause of this prompt was producing the reported answer.

    It read "if the notes do not answer it, say exactly what is missing and
    what they did cover" — and the model did exactly that: "Specs available
    include: 0-60 time, quarter mile times, lap times, top speed, price." In a
    turn whose notes also contained "1,250 combined hp" and "1.89s 0-60".
    Describing coverage was an option; giving the numbers was never made the
    requirement.
    """

    def prompt(self):
        from carrot import app as A

        return A.FORCED_ANSWER_PROMPT

    def test_it_asks_for_the_facts_themselves(self):
        assert "Give the facts themselves" in self.prompt()

    def test_it_forbids_describing_the_notes(self):
        text = self.prompt()
        assert "Never list what the notes are *about*" in text
        assert "table of contents" in text

    def test_partial_beats_a_summary_of_the_reading(self):
        # The old prompt offered "say what is missing" as an alternative to
        # answering. It is now only available when there is no fact at all.
        text = self.prompt()
        assert "even if it is partial" in text
        assert "Only if the notes contain no fact" in text


class TestAPageThatReadsAsNothingIsAFailure:
    """A 200 that yields no prose used to be reported as a success.

    That is worse than a refusal. A 403 tells the model to go and find another
    source; an empty success tells it nothing, so it carries on and writes the
    answer from search snippets — the exact failure the gates exist to catch,
    arriving through the one door they do not watch.

    It is not rare: imdb.com and espn.com both answer 202 with an interstitial
    and no article, and every JS-rendered page does the same.
    """

    def test_an_empty_page_is_reported_as_blocked(self):
        from carrot import websearch
        from unittest.mock import patch

        # A real shape: valid HTML, chrome only, no article.
        shell = "<html><head><title>Loading</title></head><body><div id='root'></div></body></html>"
        with patch.object(websearch, "_extract",
                          lambda html, url: {"text": "", "title": "Loading", "links": []}), \
             patch.object(websearch.httpx, "Client") as client:
            response = client.return_value.__enter__.return_value.get.return_value
            response.is_redirect = False
            response.status_code = 202
            response.headers = {"content-type": "text/html"}
            response.content = shell.encode()
            response.encoding = "utf-8"
            result = websearch.fetch("https://example.com/spa")

        assert result["error"], "an empty page came back as a success"
        assert "no readable text" in result["error"]
        assert result["blocked"] is True

    def test_the_threshold_leaves_room_for_a_genuinely_short_page(self):
        # A definition or a stub is still an answer. This only has to clear a
        # bot check and an empty shell.
        from carrot import websearch
        assert websearch.MIN_READABLE_CHARS <= 300


class TestTheReaderSpeaksTheProtocolItClaims:
    def test_http2_is_on(self):
        """These headers claim to be Chrome 131, and Chrome 131 does not speak
        HTTP/1.1 to a modern site. Wikipedia refuses on that mismatch alone:
        403 on HTTP/1.1, 200 on HTTP/2, same headers, same machine."""
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "carrot" / "websearch.py").read_text(encoding="utf-8")
        assert "http2=True" in source

    def test_the_dependency_is_declared(self):
        """`http2=True` raises without `h2`, and it was only ever installed as
        somebody else's transitive dependency."""
        from pathlib import Path
        pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        assert "httpx[http2]" in pyproject


class TestTheModelGetsEnoughOfThePage:
    """Every page tested was cut, and the model was never told.

    6000 characters was sized for a 4k context window. Measured against real
    articles it meant 12% of a Wikipedia page, 35% of a magazine review and
    78% of a news story — and the facts people ask for (a spec table, a
    figures section) are exactly what lives past the intro. So the model read
    the opening, did not find them, and reported that the page does not
    contain them. That is indistinguishable from a page that genuinely does
    not, which is what made it dangerous rather than merely lossy.
    """

    def test_the_page_budget_matches_the_context_window(self):
        from carrot import websearch
        # Four pages at this size sits inside a 32k window alongside the
        # directive, the tools and the conversation.
        assert 12000 <= websearch.DEFAULT_MAX_CHARS <= 20000

    def test_a_cut_page_says_so(self):
        """Absence has to be distinguishable from a page that just stopped."""
        from unittest.mock import patch
        from carrot import agent_tools, websearch

        page = {"text": "x" * 500, "final_url": "https://example.com/a",
                "screening": {"tainted": False, "signals": []}, "links": [],
                "error": "", "truncated": True}
        with patch.object(websearch, "fetch", return_value=page), \
             patch.object(websearch, "looks_like_an_index", return_value=False):
            out = agent_tools._tool_read_url(url="https://example.com/a")
        assert "this page was longer than could be shown" in out

    def test_a_whole_page_does_not(self):
        from unittest.mock import patch
        from carrot import agent_tools, websearch

        page = {"text": "x" * 500, "final_url": "https://example.com/a",
                "screening": {"tainted": False, "signals": []}, "links": [],
                "error": "", "truncated": False}
        with patch.object(websearch, "fetch", return_value=page), \
             patch.object(websearch, "looks_like_an_index", return_value=False):
            out = agent_tools._tool_read_url(url="https://example.com/a")
        assert "longer than could be shown" not in out

    def test_the_notice_is_outside_the_untrusted_envelope(self):
        """It is Carrot speaking about the fetch, not something the page said —
        and a note inside the envelope is a note the page could have forged."""
        from pathlib import Path
        source = (Path(__file__).resolve().parents[1] / "carrot" / "agent_tools.py").read_text(encoding="utf-8")
        block = source.split("def _tool_read_url")[1][:2400]
        assert block.index("wrap_untrusted") < block.index("[Carrot: this page was longer")


class TestTheCheckerSeesWhatTheModelSaw:
    """It was grading an answer against a fifth of the source.

    Stored evidence was clipped to 1200 characters of a 6000-character page,
    and the checker then clipped that to 2500. In the reported case the
    sentence that *disproves* the claim sits at character 3085 — outside the
    window entirely. A checker that cannot see the contradiction is not
    lenient, it is guessing, and it strikes out true statements as readily as
    false ones.
    """

    def test_the_checker_window_is_not_smaller_than_the_stored_evidence(self):
        from carrot import app
        assert app.SUPPORT_EVIDENCE_CHARS >= app.EVIDENCE_CHARS

    def test_the_stored_evidence_is_not_smaller_than_the_page(self):
        from carrot import app, websearch
        assert app.EVIDENCE_CHARS >= websearch.DEFAULT_MAX_CHARS * 0.4

    def test_the_last_resort_digest_stays_small(self):
        """That retry exists because the transcript overran the window, so it
        is the one path that has to clip — everything else wants the page."""
        from carrot import app
        assert app.DIGEST_CHARS < app.EVIDENCE_CHARS
        source = __import__("pathlib").Path(
            app.__file__).read_text(encoding="utf-8")
        assert "item['text'][:DIGEST_CHARS]" in source


class TestTheAnswerIsNotTheWorking:
    """The same reported question, one failure later. Having found the right
    car this time, the model shipped its entire deliberation as the reply: a
    numbered "Thinking Process", the sources restated as bullet notes, a list
    of constraints it had invented for itself, three drafts of the answer and a
    closing "Let's go." — and then, underneath all of it, the answer.

    Then the answer itself was fifteen lines of

        2026 Chevrolet Corvette ZR1X Horsepower: 1250 hp @ 7000 RPM [url]

    with the full name of the car restated on every line, one field per line,
    and the two disagreeing torque figures listed as separate rows rather than
    as the disagreement it is. A spreadsheet typed out longhand.

    Both go in the preamble rather than in one mode's directive: it happened on
    a search turn and could happen on any of them.
    """

    def preamble(self):
        from carrot import app as A

        return A.SEARCH_PREAMBLE

    def test_the_deliberation_is_not_the_reply(self):
        text = self.preamble()
        assert "Deliberating is not answering" in text
        assert "The reply begins at the first word of the answer" in text

    def test_the_specific_shapes_it_took_are_named(self):
        # A rule stated only in the abstract is one a model reads past. These
        # are the exact artefacts that arrived on screen.
        text = self.preamble()
        assert "Thinking Process" in text
        assert "no draft followed by a revision" in text
        assert "restating your instructions back" in text

    def test_an_answer_is_sentences_rather_than_rows(self):
        text = self.preamble()
        assert "Write sentences, not rows" in text
        assert "Name the subject once" in text

    def test_disagreeing_figures_belong_in_one_sentence(self):
        # 973 lb-ft and 828 lb-ft arrived as two unrelated rows, which reads as
        # two facts rather than as one contradiction the reader has to resolve.
        assert "belong in a sentence that says so" in self.preamble()

    def test_every_search_mode_carries_it(self):
        from carrot import app as A

        for mode in (A.SEARCH_SINGLE, A.SEARCH_MULTI):
            directive = A.search_directive(mode)
            assert "Deliberating is not answering" in directive, mode
            assert "Write sentences, not rows" in directive, mode


class TestAnAnswerThatIsATableTypedOutLonghand:
    """The reply that prompted this was fifteen lines of

        2026 Chevrolet Corvette ZR1X Horsepower: 1250 hp @ 7000 RPM [url]
        2026 Chevrolet Corvette ZR1X Torque: 973 lb-ft [url]

    with the car's full name restated at the head of every line and nothing
    joined to anything. The two disagreeing torque figures sat as separate rows
    rather than as the contradiction a reader has to resolve.

    The preamble asks for sentences; a 9B model will still do it, so there is a
    gate as well as a rule. The signal is the **repeated opening**, not the
    colons: a list of "Displacement: 5.5L" lines is a perfectly good spec list
    and sometimes the right answer, but restating the subject on every line
    only happens when a model is walking a table row by row.
    """

    def rows(self, answer):
        from carrot import app as A

        return A._rows_not_prose(answer)

    def test_the_reported_answer(self):
        answer = "\n".join(
            f"2026 Chevrolet Corvette ZR1X {field}: {value} [https://carbuzz.com/x]"
            for field, value in [
                ("Drivetrain", "All-Wheel Drive"),
                ("Transmission", "8-speed dual-clutch automatic"),
                ("Horsepower", "1250 hp @ 7000 RPM"),
                ("Torque", "973 lb-ft"),
                ("Engine", "5.5-Liter twin-turbo LT7 V8 hybrid"),
            ])
        assert self.rows(answer) == "2026 Chevrolet Corvette"

    def test_an_honest_spec_list_is_left_alone(self):
        # Same colons, no repeated subject. This is a list because the content
        # is a list, which is allowed.
        assert self.rows("Displacement: 5.5 litres\nHorsepower: 1,250 hp\n"
                         "Torque: 828 lb-ft\nTransmission: 8-speed dual-clutch") is None

    def test_prose_is_left_alone(self):
        assert self.rows(
            "The 2026 Corvette ZR1X is the all-wheel-drive version of the ZR1.\n"
            "Its 5.5-litre V8 makes 1,064 hp and a front motor adds 186 more.\n"
            "Sources disagree on torque: Carbuzz gives 973 lb-ft, Car and\n"
            "Driver lists 828 at 6,000 rpm.") is None

    def test_a_claim_first_bullet_list_is_left_alone(self):
        # The shape the balanced answer style actually asks for.
        assert self.rows(
            "- **Power.** 1,250 hp combined.\n"
            "- **Torque.** Sources disagree: 973 or 828 lb-ft.\n"
            "- **Weight.** 3,914 lb for the coupe.\n"
            "- **Price.** From $207,395.") is None

    def test_headed_prose_is_left_alone(self):
        assert self.rows(
            "## Powertrain\n"
            "Powertrain figures are as follows and the engine is a 5.5 litre V8.\n"
            "## Performance\n"
            "Performance is quick, reaching 60 mph in about two seconds.") is None

    def test_three_rows_are_not_enough_to_call_it(self):
        # A short run of parallel lines is a coincidence; four is a habit.
        assert self.rows("Alpha One: a\nAlpha One: b\nAlpha One: c") is None

    def test_the_nudge_quotes_the_phrase_back(self):
        from carrot import app as A

        text = A.ROWS_NUDGE.format(prefix="2026 Chevrolet Corvette")
        # A rule the model can see itself having broken is one it can fix.
        assert "2026 Chevrolet Corvette" in text
        assert "Keep every figure and every source link" in text
        assert "say that in a sentence" in text

    def test_it_only_runs_where_a_gate_can_run(self):
        # Multi-turn buffers the prose until the checks pass, so nothing has
        # reached the screen and the answer can be sent back. Single-pass
        # streams as it goes — the same reason `_single_pass_gap` is unwired.
        source = read_app_source()
        block = source[source.index("if not stalled and gated and rows_nudges"):]
        assert "gated" in block[:80]

    def test_it_asks_once(self):
        from carrot import app as A

        assert A.MAX_ROWS_NUDGES == 1


def read_app_source():
    from pathlib import Path

    return (Path(__file__).resolve().parents[1] / "carrot" / "app.py").read_text(
        encoding="utf-8")
