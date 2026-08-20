"""The search page — results as rows you can scan, not cards you have to read.

The page was seven cards on a screen. Each spent seventy-five pixels on a date,
a role, a title and four hundred characters of body text, and none of them
marked the words that had matched — so finding the thing you searched for meant
reading every result in full, which is the work the search was supposed to do.

What is held here is the handful of decisions that make the difference, each of
which is a thing the cards did not do:

* the matched words are marked, and marked *after* escaping;
* the line shown is the one the match is in, not the first line of the message;
* a result goes somewhere — the cards were inert;
* the foot says which mode answered, because a hybrid run and one that fell
  back to keywords find different things and looked identical from here.

Source-text assertions, because this is browser code and there is no browser in
the suite. That is the same bargain the rest of the front-end tests make, and
it catches the class of mistake that actually happens: a control wired to
nothing, two definitions of one function, a style nothing draws.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "carrot" / "web"


@pytest.fixture(scope="module")
def search_js():
    return (WEB / "js" / "search.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def app_js():
    return (WEB / "js" / "app.js").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index_html():
    return (WEB / "index.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def style_css():
    return (WEB / "css" / "style.css").read_text(encoding="utf-8")


def body_of(source, name):
    start = source.index(f"function {name}(")
    rest = source[start:]
    end = rest.find("\nfunction ", 1)
    tail = rest.find("\nasync function ", 1)
    if tail != -1 and (end == -1 or tail < end):
        end = tail
    return rest if end == -1 else rest[:end]


class TestThereIsOneOfIt:
    """It was defined in app.js while the file named after it was a stub whose
    comment said so — one function in the wrong file and one file with no
    reason to exist, from the same decision. Two definitions of a global is
    also a bug waiting for someone to edit the one that loses."""

    def test_it_lives_in_the_file_named_after_it(self, search_js):
        assert "async function doSearch()" in search_js

    def test_app_js_no_longer_defines_it(self, app_js):
        assert "function doSearch(" not in app_js

    def test_the_places_that_call_it_still_reach_it(self, index_html, app_js):
        assert "onclick=\"doSearch()\"" in index_html
        # Called through a typeof guard from the composer's tool menu and from
        # the menubar's own box; both are still callers, not definitions.
        assert "typeof doSearch === 'function'" in app_js


class TestARowIsOneLine:

    def test_the_results_host_is_a_list_not_a_stack_of_cards(self, index_html, style_css):
        assert 'id="search-results" class="s-list"' in index_html
        assert ".s-list {" in style_css
        # One container with hairlines, rather than seven bordered boxes with
        # gaps between them: a list of things of one kind should read as one
        # object, and the gaps were most of the height.
        assert ".s-row + .s-row { border-top:" in style_css

    def test_the_whole_line_is_the_control(self, search_js, style_css):
        # A row where only the chevron is clickable is a row people click and
        # nothing happens.
        assert 'class="s-line" data-toggle=' in search_js
        assert ".s-line {" in style_css

    def test_the_snippet_is_the_one_line_that_holds_it(self, style_css):
        rules = style_css[style_css.index(".s-snip {"):]
        rules = rules[:rules.index("}")]
        assert "white-space: nowrap" in rules
        assert "text-overflow: ellipsis" in rules

    def test_an_empty_list_draws_nothing(self, style_css):
        # Before the first search there is nothing in it, and a bordered box
        # with nothing in it is a line under the input that looks like a bug.
        assert ".s-list:empty { display: none; }" in style_css


class TestWhyItMatched:
    """The cards showed a four-hundred-character prefix with nothing marked, so
    a result whose match was three paragraphs down showed a passage with none
    of the searched words in it at all."""

    def test_the_line_shown_is_the_one_the_match_is_in(self, search_js):
        line = body_of(search_js, "matchedLine")
        assert "indexOf(term)" in line
        assert "at - 40" in line, "the snippet does not back up to before the match"

    def test_a_message_with_no_match_in_it_still_shows_something(self, search_js):
        line = body_of(search_js, "matchedLine")
        assert "if (at === -1) return flat.slice(0, 200);" in line

    def test_marking_happens_after_escaping(self, search_js):
        # The other order puts the `<mark>` through the escaper and prints it
        # at the reader.
        mark = body_of(search_js, "markTerms")
        assert mark.index("escHtml(") < mark.index("<mark>")

    def test_the_pattern_is_escaped_before_it_becomes_a_regex(self, search_js):
        # A query with a bracket in it would otherwise throw, and the page
        # would go blank on a perfectly ordinary search.
        assert "escapeForRegex" in body_of(search_js, "markTerms")
        assert "function escapeForRegex" in search_js

    def test_short_words_are_not_marked(self, search_js):
        # Marking every "a" and "of" paints the whole line and says nothing
        # about why the result is there.
        assert "word.length > 2" in body_of(search_js, "searchTerms")


class TestAResultGoesSomewhere:

    def test_there_is_a_way_into_the_conversation(self, search_js):
        assert "function openSearchResult" in search_js
        opener = body_of(search_js, "openSearchResult")
        assert "openConversation(result.conversation_id)" in opener
        assert "switchTab('workspace')" in opener

    def test_it_is_offered_on_the_row_that_is_open(self, search_js):
        assert 'data-open="${index}"' in search_js
        assert "Open this conversation" in search_js

    def test_the_list_can_be_driven_from_the_keyboard(self, search_js):
        assert "'ArrowDown'" in search_js and "'ArrowUp'" in search_js
        # Only on the search page, and never while the query box has focus.
        assert "view.classList.contains('active')" in search_js
        assert "id === 'search-input'" in search_js


class TestTheFoot:

    def test_it_says_which_mode_answered(self, search_js):
        assert "SEARCH_MODE_WORDS" in search_js
        # Every mode `_format_results` can report has a word here; a mode that
        # falls through prints its raw database value at the reader.
        for mode in ("hybrid", "semantic", "fts", "fts_fallback", "keyword"):
            assert f"{mode}:" in search_js, f"no word for search mode '{mode}'"

    def test_it_counts_conversations_as_well_as_hits(self, search_js):
        # Twenty hits in one thread and twenty across twenty are different
        # answers, and the count alone cannot tell them apart.
        render = body_of(search_js, "renderSearch")
        assert "new Set(results.map(r => r.conversation_id)).size" in render

    def test_it_exists_in_the_markup(self, index_html, style_css):
        assert 'id="search-foot"' in index_html
        assert ".s-foot {" in style_css


class TestTheModesAreTheOnesTheServerSends:
    """The word list is only honest if it matches what `_format_results` can
    actually put in that field."""

    def test_every_mode_the_server_can_report_has_a_word(self, search_js):
        source = (ROOT / "carrot" / "search.py").read_text(encoding="utf-8")
        # Two shapes: handed to `_format_results`, and written straight into a
        # returned dict on the paths that answer without ranking anything. The
        # second is how `fts_only` was missed the first time this was written.
        sent = set(re.findall(r'_format_results\([^)]*?["\'](\w+)["\']\s*\)', source, re.DOTALL))
        sent |= set(re.findall(r'"mode":\s*"(\w+)"', source))
        assert sent, "the mode literals moved; this test now checks nothing"
        for mode in sent:
            assert f"{mode}:" in search_js, f"the server can report '{mode}' and the page cannot name it"
# ===== What a person types is not an FTS5 expression =====
#
# Found by typing one search into the running app. `MATCH ?` parses its
# argument as a query language — parentheses group, a colon is a column filter,
# a hyphen and an apostrophe are syntax — so five perfectly ordinary searches
# reached the browser as a 500:
#
#     routes (independent   fts5: syntax error near ""
#     don't                 fts5: syntax error near "'"
#     foo:bar               no such column: foo
#     C:/path/to/file       no such column: C
#     a-b                   no such column: b
#
# `don't` is the one that says how broken this was: an apostrophe in a search
# box, answered with a stack trace.


class TestAnOrdinarySearchCannotBeASyntaxError:

    @pytest.fixture
    def searchable(self, isolated_db):
        from carrot import conversation as conv_mod

        conv = conv_mod.create_conversation(title="Why the batch stops")
        conv_mod.add_message(conv["id"], "user",
                             "groups are independent routes rather than steps; don't stop")
        conv_mod.add_message(conv["id"], "assistant",
                             "the file is at C:/path/to/file and the flag is a-b")
        return conv

    @pytest.mark.parametrize("query", [
        "routes (independent",
        "don't",
        "foo:bar",
        "C:/path/to/file",
        "a-b",
        'a "quoted" thing',
        '"""',
        "NOT AND OR",
        "*",
        "^leading",
        "trailing-",
    ])
    def test_it_answers_rather_than_raising(self, searchable, query):
        from carrot import search as search_mod

        answer = search_mod.search_conversations(query)
        assert "results" in answer

    def test_the_words_still_find_the_message(self, searchable):
        from carrot import search as search_mod

        # The punctuation is dropped, not the search: these are the same query
        # with and without a character that used to be fatal.
        assert search_mod.search_conversations("independent routes")["count"] == 1
        assert search_mod.search_conversations("routes (independent")["count"] == 1

    def test_an_apostrophe_is_part_of_the_word(self, searchable):
        from carrot import search as search_mod

        assert search_mod.search_conversations("don't")["count"] == 1

    def test_a_path_is_a_search_for_the_path(self, searchable):
        from carrot import search as search_mod

        assert search_mod.search_conversations("C:/path/to/file")["count"] == 1


class TestTheQuoting:
    """`fts_query` is the whole of why the above cannot raise."""

    def test_each_word_becomes_a_phrase_literal(self):
        from carrot.search import fts_query

        assert fts_query("independent routes") == '"independent" "routes"'

    def test_operators_become_words(self):
        # The documented trade: a typed OR searches for the word "or". Nothing
        # in the UI ever offered the query language, and a box that answers
        # punctuation with a server error is broken in a way that a box without
        # boolean operators is not.
        from carrot.search import fts_query

        assert fts_query("a OR b") == '"a" "OR" "b"'

    def test_a_quote_cannot_close_the_literal_it_is_inside(self):
        from carrot.search import fts_query

        # Every quote in the output is one this function put there, so the
        # expression is balanced and the user's cannot end a literal early.
        assert fts_query('say "hi"') == '"say" "hi"'

    def test_a_query_of_nothing_but_punctuation_is_empty(self):
        from carrot.search import fts_query

        assert fts_query('"""') == ""
        assert fts_query("   ") == ""

    def test_an_empty_expression_is_no_results_rather_than_a_match_on_nothing(self, isolated_db):
        from carrot import conversation as conv_mod, search as search_mod

        conv = conv_mod.create_conversation(title="t")
        conv_mod.add_message(conv["id"], "user", "something")
        answer = search_mod.search_conversations('"""')
        assert answer["count"] == 0


class TestTheBraces:
    """The quoting is what stops a syntax error being possible; the catch is
    what stops a future edit to the quoting from turning one back into a 500."""

    def test_an_unparseable_match_is_an_empty_result(self, isolated_db, monkeypatch):
        from carrot import conversation as conv_mod, search as search_mod

        conv = conv_mod.create_conversation(title="t")
        conv_mod.add_message(conv["id"], "user", "something")
        # A `fts_query` that has stopped quoting, which is the regression.
        monkeypatch.setattr(search_mod, "fts_query", lambda text: text)
        answer = search_mod.search_conversations("routes (independent")
        assert answer["count"] == 0
        assert answer["mode"] == "fts_only"
