"""One keystroke to everything, without a fifth place to look.

Four tabs is the right number of destinations and the wrong number of ways in.
Everything the app can do is reachable — meaning "in a tab, behind a button,
once you remember which tab", which is fine when you know the app and is the
whole problem when you are trying to do one thing quickly.

The rule that keeps this a shortcut rather than another surface: nothing is
offered here that exists only here. Every entry is something you could already
click.
"""
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "carrot" / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "js" / "palette.js").read_text(encoding="utf-8")
APP_JS = (WEB / "js" / "app.js").read_text(encoding="utf-8")
CSS = (WEB / "css" / "style.css").read_text(encoding="utf-8")


def block(source, name):
    start = source.index(f"function {name}")
    return source[start:source.index("\n}", start)]


class TestTheKeystrokeIsNotStolen:
    def test_only_one_handler_owns_ctrl_k(self):
        """Both listening would have opened the palette and then moved the
        cursor into the composer behind it.

        Asserted on the binding itself rather than on `focusCmd` having no
        callers — it still has two legitimate ones, after clearing a chat and
        after voice transcription, and neither is a keyboard shortcut.
        """
        import re

        assert re.search(r"key\.toLowerCase\(\)\s*===\s*'k'", JS), \
            "the palette should own Ctrl+K"
        assert not re.search(r"key\.toLowerCase\(\)\s*===\s*'k'", APP_JS), \
            "app.js still binds Ctrl+K"

    def test_the_old_behaviour_survives(self):
        """Ctrl+K used to focus the composer. Typing into the palette and
        pressing Enter puts the text there instead — so the muscle memory still
        ends in asking a question rather than in nothing happening."""
        assert "function askFromPalette" in JS
        body = block(JS, "askFromPalette")
        assert "cmd-input" in body

    def test_it_does_not_send_what_you_typed(self):
        """A stray Enter firing a model call has no undo. The composer is one
        more keystroke and it is the keystroke that makes it deliberate."""
        body = block(JS, "askFromPalette")
        for send in ("sendMessage", "submitMessage", "sendChat"):
            assert send not in body, f"the palette calls {send} directly"

    def test_escape_closes_it(self):
        assert "'Escape'" in JS


class TestWhatItOffers:
    def test_nothing_exists_only_here(self):
        """Every action delegates to something the UI already has, which is
        what keeps this a shortcut rather than a fifth destination."""
        actions = JS[JS.index("const PALETTE_ACTIONS"):JS.index("function openPalette")]
        for call in ("switchTab(", "newChat(", "newNote(", "newDriveWorkspace("):
            assert call in actions, call

    def test_a_missing_feature_does_nothing_rather_than_throwing(self):
        """A pack that is switched off should leave its entry inert, not break
        the palette for everything else."""
        actions = JS[JS.index("const PALETTE_ACTIONS"):JS.index("function openPalette")]
        assert actions.count("typeof") >= 3

    def test_documents_come_from_the_drive_and_not_a_second_listing(self):
        """One place decides what "your work, most recent first" means, and it
        already merges documents with indexed files and sorts the whole set."""
        assert "/api/work/items" in JS

    def test_typed_text_that_matches_nothing_is_still_offered(self):
        """Asking is what the box is for. "No results" for a question you could
        simply ask would be the palette refusing its own purpose."""
        body = block(JS, "paletteGroups")
        assert "Ask Carrot about" in body

    def test_the_offer_to_ask_comes_last(self):
        """It used to be first, where it crowded out real matches — the thing
        you were looking for should be above the fallback for not finding it."""
        body = block(JS, "paletteGroups")
        ask_at = body.index("Ask Carrot about")
        results_at = body.index("for (const g of resultGroups)")
        assert results_at < ask_at

    def test_actions_shrink_once_you_type(self):
        """Six static actions took nearly half the height every time it opened,
        and once you have typed they are almost never what you want."""
        assert "PALETTE_ACTIONS_AT_REST" in JS
        body = block(JS, "paletteGroups")
        assert "slice(0, PALETTE_ACTIONS_AT_REST)" in body

    def test_results_are_ranked_rather_than_only_filtered(self):
        """A title starting with what you typed is what you meant; a
        subsequence hit buried in the middle is a guess."""
        assert "function paletteScore" in JS
        assert "score" in block(JS, "paletteGroups")

    def test_matching_is_loose_enough_for_how_people_type(self):
        """"sett" should find Go to Settings and "new doc" New document."""
        body = block(JS, "paletteMatches")
        assert "indexOf(ch, at)" in body, "no subsequence matching"
        assert "words.every" in body, "words in any order are not matched"

    def test_a_result_says_where_it_came_from(self):
        """Two documents called "notes" are indistinguishable without it."""
        assert "function paletteWhere" in JS
        assert "palette-where" in block(JS, "renderPalette")

    def test_the_same_thing_is_not_listed_twice(self):
        """A document in both the recents shelf and the search results is one
        document, and showing it twice makes the palette look like it cannot
        count."""
        body = block(JS, "paletteGroups")
        assert "seen.has(item.id)" in body

    def test_it_searches_past_the_recents_shelf(self):
        """The recents lists are what you had open, not what you have — a
        document from March is in the last eight of nothing."""
        assert "/api/search/all" in JS


class TestItBehavesLikeAList:
    def test_the_arrows_wrap(self):
        assert "% paletteItems.length" in JS

    def test_moving_the_cursor_does_not_rebuild_the_list(self):
        """Re-rendering every row to move one class would fight the mouse and
        lose the input's selection."""
        assert "function paintPaletteCursor" in JS
        body = block(JS, "paintPaletteCursor")
        assert "classList.toggle('on'" in body
        assert "innerHTML" not in body

    def test_the_mouse_moves_the_same_cursor(self):
        """Two independent highlights in a list you are arrowing through is the
        bug where Enter opens something you are not looking at."""
        body = block(JS, "renderPalette")
        assert "onmouseenter" in body
        assert "paletteCursor =" in body

    def test_it_uses_mousedown_rather_than_click(self):
        """The input has focus; a click that blurs it first can close the
        palette out from under itself."""
        body = block(JS, "renderPalette")
        assert "onmousedown" in body

    def test_the_selected_row_is_scrolled_into_view(self):
        assert "scrollIntoView" in block(JS, "paintPaletteCursor")


class TestItDoesNotThrashTheServer:
    def test_typing_is_debounced(self):
        """A keystroke is not a question, and eight of them are not eight
        questions — the same mistake already fixed in the drive's search."""
        assert "paletteTimer" in JS
        assert "setTimeout" in JS

    def test_recents_are_read_once_per_opening(self):
        """They do not change while you type."""
        assert "paletteRecents = null" in block(JS, "openPalette")
        body = block(JS, "renderPalette")
        assert "api(" not in body, "rendering should not fetch"

    def test_a_stale_reply_cannot_land(self):
        """A slow answer for "th" must not arrive after "thesis" and show
        results for a query nobody is looking at any more.

        Guarded on the query rather than a sequence number. The two fetches
        shared one counter and each invalidated the other, so good answers were
        discarded — and a counter is only a proxy for "is this still the
        question on screen", which the query answers directly.
        """
        body = block(JS, "searchFromPalette")
        assert "current !== query" in body

    def test_the_recents_load_still_has_its_own_guard(self):
        assert "paletteRecentsSeq" in JS

    def test_the_palette_does_not_wait_on_a_model_call(self):
        """`/api/search/all` embeds the query, and embedding is a model call —
        measured at 9.4s on the machine this was rewritten on. A palette that
        waits for that is a palette that hangs.

        This used to assert `Promise.race` and a timeout constant, which is the
        mechanism rather than the property, and the mechanism was wrong: the
        race rejected at 1500ms and *threw the response away*, so on any machine
        slower than the cap the semantic half of the palette was unreachable —
        results computed, paid for, and dropped on arrival, every keystroke.

        The property was never in danger. Nothing waits on this promise to draw
        the box: keystrokes render local matches synchronously and the server
        answer only ever adds to what is already up. So what is asserted now is
        the property — the box renders without the search — plus the specific
        regression, below.
        """
        assert "api(" not in block(JS, "renderPalette"), "rendering must not fetch"
        # The keystroke path renders before it schedules the search.
        typed = JS[JS.index("paletteTimer = setTimeout") - 600:JS.index("paletteTimer = setTimeout")]
        assert "renderPalette(input.value)" in typed

    def test_the_search_result_is_never_discarded_on_a_deadline(self):
        """The regression this file exists to prevent a second time.

        A deadline on the *answer* throws away work that has already been done
        and cannot be made correct by choosing a larger number: there is none
        both short enough to feel instant and long enough to cover a model call.
        The deadline belongs on the "Looking…" row instead, which is what
        PALETTE_SEARCH_HINT_MS is.
        """
        body = block(JS, "searchFromPalette")
        assert "Promise.race" not in body, "a raced search discards results it already paid for"
        assert "PALETTE_SEARCH_HINT_MS" in JS
        # The hint timer must only clear the hint, never the response.
        assert "paletteSearching = false" in body

    def test_a_failed_search_does_not_wipe_earlier_results(self):
        body = block(JS, "searchFromPalette")
        assert "if (found) paletteFound = found;" in body


class TestItIsWiredIn:
    def test_the_markup_exists(self):
        for hook in ('id="palette"', 'id="palette-input"', 'id="palette-list"'):
            assert hook in HTML, hook

    def test_the_script_is_loaded_after_what_it_calls(self):
        """It calls switchTab, newChat, newNote and the rest. Loading it last
        means its guards are for genuinely absent features rather than for load
        order."""
        assert HTML.index("/js/palette.js") > HTML.index("/js/app.js")

    def test_every_piece_is_styled(self):
        for cls in (".palette", ".palette-box", ".palette-item", ".palette-group",
                    ".palette-foot"):
            assert cls in CSS, f"{cls} is built by the palette but never styled"

    def test_it_says_how_to_drive_it(self):
        """A keyboard surface that does not say it is one gets used with the
        mouse and then feels slow."""
        assert "palette-foot" in HTML
        assert "<kbd>" in HTML
