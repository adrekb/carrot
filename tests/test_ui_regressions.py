"""The bugs where the app looked fine and did nothing.

Each of these was reported as a different symptom — "I can no longer type in
textboxes", "clicking New York doesn't work", "why no model picker in agent" —
and each is a static property of the shipped assets, so it can be pinned here
rather than rediscovered by a person clicking around.
"""
import re
from pathlib import Path

import pytest


WEB = Path(__file__).resolve().parents[1] / "carrot" / "web"


def read(*parts):
    return WEB.joinpath(*parts).read_text(encoding="utf-8")


def read_py(rel):
    root = Path(__file__).resolve().parents[1]
    return root.joinpath(rel).read_text(encoding="utf-8")


class TestTheDragOverlayCannotEatTheApp:
    """One stuck CSS class killed every input and every button in the app.

    `body.dropping::after` is a decoration covering the whole window. It is
    switched on by a class, and a drag cancelled with Escape or released
    outside the window fires no drop — and, in Chromium, no dragleave with a
    null relatedTarget either. The class stayed, the invisible overlay stayed,
    and from then on text boxes would not focus and buttons would not click,
    with nothing on screen to explain it. Only a reload cleared it.
    """

    def overlay_block(self):
        css = read("css", "style.css")
        return css.split("body.dropping::after {")[1].split("}")[0]

    def test_the_overlay_never_intercepts_input(self):
        # The safety property. However the class got there, a decoration must
        # not be able to swallow a click or a keystroke.
        assert "pointer-events: none" in self.overlay_block()

    def test_it_still_covers_the_window_when_it_is_wanted(self):
        assert "position: fixed" in self.overlay_block()

    def test_a_cancelled_drag_clears_it(self):
        # dragend is the one event that always arrives.
        assert "'dragend', stopDropping" in read("js", "app.js")

    def test_leaving_the_window_clears_it(self):
        assert "window.addEventListener('blur', stopDropping)" in read("js", "app.js")

    def test_dropping_a_non_file_clears_it(self):
        """The early return used to come first, so dropping a text selection
        or a link left the overlay up over the whole app."""
        js = read("js", "app.js")
        body = js.split("document.addEventListener('drop'")[1].split("});")[0]
        assert body.index("stopDropping()") < body.index("dataTransfer.files.length")


class TestTheCityListIsClickable:
    """Clicking "New York" did nothing, silently.

    The search results were stashed on the DOM node as `res._results`. Any
    re-render of the dashboard — a widget saving, the GitHub poller finishing —
    replaced that node and took the results with it, while the list stayed on
    screen looking perfectly clickable.
    """

    def test_results_are_not_stored_on_the_element(self):
        js = read("js", "dashboard.js")
        # The comment explaining the bug may name the old expando; the code
        # must not assign or read it.
        code = "\n".join(line for line in js.splitlines()
                         if not line.strip().startswith("//"))
        assert "._results" not in code

    def test_they_are_held_where_a_re_render_cannot_reach(self):
        js = read("js", "dashboard.js")
        assert "let wxCityResults" in js
        assert "wxCityResults = results" in js

    def test_a_stale_list_says_so_instead_of_ignoring_the_click(self):
        # The original failure mode was silence. A button that does nothing and
        # says nothing is the worst way for this to break.
        js = read("js", "dashboard.js")
        pick = js.split("function wxPickCity(")[1].split("\n}")[0]
        assert "stale" in pick


class TestTheComposerLeavesRoomToType:
    """The input the box exists for had about an inch of width.

    Eight controls and the text field shared one row, and the input was the
    only item that would give up space, so it did — all of it. Two fixes went
    in on top of that arrangement rather than at it: `min-width: 0` so the
    controls stopped being deformed instead (the send button rendered as a
    16x36 ellipse), then a container query hiding the labels one breakpoint at
    a time, which had to be re-pinned once because 720px sat *below* the bar's
    own 760px cap and so never fired.

    The row split ended the argument. The question gets its own line at full
    width, the controls get theirs, and the six you set once and forget moved
    behind the plus. Nothing competes, so nothing has to collapse — these
    tests pin that there is no width at which the old squeeze can come back.
    """

    def css(self):
        return read("css", "style.css")

    def test_the_bar_is_two_rows_by_construction(self):
        # Not a flex row that wraps under pressure — wrapping is a fallback,
        # and a fallback has a width at which it has not happened yet.
        block = self.css()[self.css().index("#cmdbar {"):]
        block = block[:block.index("}")]
        assert "flex-direction: column;" in block

    def test_the_question_takes_the_whole_width(self):
        block = self.css()[self.css().index("#cmd-input {"):]
        block = block[:block.index("}")]
        assert "width: 100%;" in block
        # The property that stops a flex item being floored at its content
        # width. Nothing competes with the input now, but leaving it correct
        # costs nothing and it is what the original bug turned on.
        assert "min-width: 0;" in block

    def test_the_send_button_holds_its_own_size(self):
        block = self.css()[self.css().index("#send-btn {"):]
        block = block[:block.index("}")]
        assert "flex-shrink: 0" in block
        # A circle is only a circle while the two are equal.
        assert "width: 36px; height: 36px;" in block

    def test_send_sits_at_the_far_end(self):
        assert ".cmd-gap { flex: 1 1 auto;" in self.css()

    def test_the_labels_degrade_by_ellipsis_not_by_breakpoint(self):
        """The container query is gone, and must not come back by reflex.

        It was mis-keyed twice — 720px sat below the bar's own 760px cap, so
        it never fired at the width that broke — because a breakpoint has to
        guess a number. Text does not need one: the pickers shrink and the
        labels ellipsis, continuously, at every width.
        """
        css = self.css()
        assert "@container (max-width" not in css
        assert "container-type: inline-size" not in css
        # The mechanism that replaced it.
        assert "#model-label, #search-label, #active-skill-name {" in css

    def test_the_icon_buttons_are_the_ones_that_stay_rigid(self):
        # A circle at 80% is not a smaller circle, it is a broken one. Text
        # has a graceful way to be too long; a 36px circle does not.
        css = self.css()
        block = css[css.index("#cmd-row > #send-btn,"):]
        assert "flex-shrink: 0;" in block[:block.index("}")]

    def test_phone_width_gives_up_the_recoverable_label(self):
        """The ellipsis has a floor — two icons, a chevron and the button
        padding are ~60px per picker before any label at all — so below phone
        width something still has to go. It is the search label, because the
        icon carries it; the model name stays, being the one piece of state
        you cannot read off an icon.

        The desktop app never reaches this: its window has a 1024px minimum.
        """
        css = self.css()
        narrow = css[css.index("@media (max-width: 560px)"):]
        narrow = narrow[:narrow.index("}")]
        assert "#search-label" in narrow
        assert "#model-label" not in narrow

    def test_the_model_name_still_cannot_run_away(self):
        # The one label that is user-supplied and arbitrarily long:
        # `mistral-small-3.2-24b-instruct-2506-q4_K_M` would push send off the
        # end on its own.
        block = self.css()[self.css().index("#model-label {"):]
        block = block[:block.index("}")]
        assert "text-overflow: ellipsis;" in block


class TestTheComposerToolMenu:
    """Settings behind the plus; actions and live state on the row.

    Attaching a file and dictating are things you do in the middle of writing
    a question, so they keep their own buttons — two clicks for either is one
    too many. Temporary, Memory, Council and read-aloud are set once and then
    stopped looking at, so they moved. Search mode and model stay because they
    are state you need to read at a glance.
    """

    def row(self):
        html = read("index.html")
        return html.split('<div id="cmd-row">')[1].split('id="send-btn"')[0]

    def menu(self):
        html = read("index.html")
        return html.split('<div id="tool-pop"')[1].split('id="attach-btn"')[0]

    def test_the_settings_moved_into_the_menu(self):
        # These handlers find their buttons by id and toggle classes on them.
        # The menu is where they are drawn, not what they do.
        for control in ("temp-btn", "memory-btn", "debate-btn", "speak-toggle"):
            assert f'id="{control}"' in self.menu(), f"{control} is not in the menu"

    def test_attaching_and_dictating_keep_their_own_buttons(self):
        row = self.row()
        for control in ("attach-btn", "mic-btn"):
            assert f'id="{control}"' in row, f"{control} should be on the row"
        assert f'id="{control}"' not in self.menu()

    def test_the_lit_states_survived_the_move(self):
        # `.composer-chip.on` styled the old chips. The handlers still toggle
        # `on`, `recording` and `needs-setup`; without rules for them on the
        # menu items the state would be set and invisible.
        css = read("css", "style.css")
        for rule in (".tool-item.on", ".tool-item.recording", ".tool-item.needs-setup"):
            assert rule in css

    def test_the_menu_is_the_same_glass_as_every_other_popover(self):
        """What made it read as a foreign widget.

        It rolled its own background, border and radius instead of joining the
        shared floating-layer rule — and the radius it asked for, `--r-md`, is
        not one of the five that exist, so it fell back to square corners next
        to a bar with 14px ones. The surface is stated per theme, so joining
        the selector is the only way to be right in all three.
        """
        css = read("css", "style.css")
        surfaces = [line for line in css.splitlines()
                    if "#model-pop" in line and "#tool-pop" in line]
        assert len(surfaces) >= 4, "tool-pop is not on every themed popover surface"
        block = css[css.index("\n#tool-pop {"):]
        block = block[:block.index("}")]
        assert "border-radius" not in block, "it is rolling its own radius again"

    def test_a_menu_over_the_conversation_is_opaque(self):
        """You could read the dashboard card and the placeholder straight
        through it.

        Originally this was one rule's job: the composer's popovers are
        children of `#cmdbar`, which had a `backdrop-filter` of its own, and
        nested backdrop-filters do not compose — the ancestor establishes the
        backdrop root, so the child's blur sampled an already-flattened
        backdrop and did nothing, leaving the bare 82% alpha of the glass
        colour showing whatever was underneath.

        The glass is gone from the whole app now, so the fix is no longer a
        local override; it is that there is nothing left to override. The
        assertion follows: the popovers take an opaque fill, and nothing
        anywhere reintroduces a blur that would put them back over a
        translucent ancestor.
        """
        css = read("css", "style.css")
        block = css[css.index("#cmdbar #tool-pop,"):]
        block = block[:block.index("}")]
        for pop in ("#cmdbar #model-pop", "#cmdbar #search-pop"):
            assert pop in block, f"{pop} has the same flaw and is not covered"
        assert "background: var(--card);" in block

    def test_nothing_reintroduces_glass(self):
        """The de-glassing has to stay done.

        One `backdrop-filter` added back to an ancestor recreates the exact
        bug above — and it would do it silently, because the popover's own
        rule would still look correct.
        """
        import re
        css = read("css", "style.css")
        # Comments still discuss the old behaviour, and should: the reasoning
        # is why the rules look the way they do. Only declarations count.
        declarations = re.findall(r"^[^/*\n]*backdrop-filter\s*:", css, re.M)
        assert declarations == [], declarations

    def test_surfaces_are_opaque(self):
        """A translucent fill with no blur behind it is not glass, it is a
        card you can see the page through."""
        import re
        css = read("css", "style.css")
        translucent = re.findall(
            r"--surface-(?:bar|nav|card|pop|card-hi)\s*:\s*rgba\([^)]*\)", css)
        assert translucent == [], translucent

    def test_the_opaque_rule_does_not_depend_on_source_order(self):
        # The themed glass rules are plain id selectors appearing later in the
        # file. Matching their specificity would make this a coin flip.
        assert "#cmdbar #tool-pop," in read("css", "style.css")

    def test_no_rule_asks_for_a_variable_that_does_not_exist(self):
        """The general form of the bug that made the menu look wrong.

        `#tool-pop` asked for `var(--r-md)`, which is not one of the five
        radius tokens, so it got square corners next to a 14px bar. The same
        slip then put `var(--hover)` on the menu rows, where nothing is
        defined either and the hover state simply did nothing. Neither fails
        loudly — CSS drops the declaration and moves on.

        Only bare `var(--x)` counts. `var(--panel, var(--card))` is a
        deliberate hook with a fallback, which is valid and works.
        """
        css = read("css", "style.css")
        defined = set(re.findall(r"(--[a-z0-9-]+)\s*:", css))
        # Some are set inline by JS on the element that uses them.
        defined |= set(re.findall(r"(--[a-z0-9-]+)\s*:", read("js", "theme.js")))
        bare = set(re.findall(r"var\(\s*(--[a-z0-9-]+)\s*\)", css))
        missing = sorted(bare - defined)
        assert not missing, f"CSS asks for variables nothing defines: {missing}"

    def test_it_only_uses_radius_tokens_that_exist(self):
        css = read("css", "style.css")
        defined = set(re.findall(r"--r-([a-z]+):", css))
        used = set(re.findall(r"var\(--r-([a-z]+)\)", css))
        assert used <= defined, f"undefined radius tokens: {sorted(used - defined)}"

    def test_the_menu_closes_when_you_click_away(self):
        assert "closeToolMenu()" in read("js", "app.js")

    def test_the_search_menu_closes_when_you_click_away_too(self):
        """It never did. It closed by picking a mode or by hitting the same
        button again, so clicking anywhere else left it over the conversation."""
        js = read("js", "app.js")
        handler = js.split("// Click outside closes the popovers")[1].split("});")[0]
        assert "search-pop" in handler


class TestImagesAreNotOfferedToAModelThatCannotSeeThem:
    """The refusal existed, but only on send, as a 400.

    You found the file, attached it, wrote the question, and only then were
    told the model cannot read images. The composer knows beforehand now.
    """

    def test_the_server_says_whether_chat_can_see(self):
        assert '"chat_vision": chat_vision' in read_py("carrot/app.py")

    def test_a_failure_to_tell_does_not_remove_the_feature(self):
        # Claiming vision it may not have is the safe direction: the send-time
        # check still refuses, so the cost of being wrong is the old behaviour
        # rather than a vision model that silently will not take pictures.
        src = read_py("carrot/app.py")
        block = src.split("chat_vision = (ollama_mod")[1].split("return {")[0]
        assert "chat_vision = True" in block

    def test_the_picker_stops_listing_images(self):
        assert "input.accept = canSee ? 'image/*,' + docs : docs;" in read("js", "app.js")

    def test_a_pasted_or_dropped_image_is_refused_too(self):
        """`accept` only filters the picker. A screenshot pasted in, or a photo
        dropped on the window, never sees it."""
        js = read("js", "app.js")
        block = js.split("async function addAttachments(")[1].split("renderAttachTray")[0]
        assert "modelCanSeeImages === false" in block

    def test_auto_is_not_treated_as_blind(self):
        # Under Auto the model is picked per message, so no single answer
        # exists yet. Assume it can see and let send-time be the refusal.
        assert "renderAttachAffordance(autoModel || data.chat_vision !== false)"             in read("js", "app.js")


class TestThePlanChecklist:
    """A long run gave no sense of what it was trying to find out.

    You watched searches go past with no way to tell whether any of them were
    the point, or how much was left. The plan is now a list that ticks, and it
    is the same component in chat and in Research so there is one thing to
    learn rather than three progress displays.
    """

    def test_the_component_exists_and_replaces_itself(self):
        source = read("js", "app.js")
        assert "function renderPlan(" in source
        # Re-sent every time it changes, so it must update in place rather than
        # stacking copies of itself down the transcript.
        assert "let box = host.querySelector('.plan-box');" in source

    def test_it_sits_above_the_answer(self):
        # It is what you watch while the answer does not exist yet.
        assert "host.insertBefore(box, content || null);" in read("js", "app.js")

    def test_done_items_are_marked_twice_over(self):
        # On a list of five, colour alone is a weak signal.
        css = read("css", "style.css")
        assert ".plan-item.done .plan-text { text-decoration: line-through; }" in css
        assert ".plan-item.done .plan-mark { color: var(--green); }" in css

    def test_research_uses_the_same_component(self):
        source = read("js", "agents.js")
        assert "renderPlan(document.getElementById('research-plan-host')" in source
        assert "event.plan_progress" in source

    def test_research_clears_its_plan_between_runs(self):
        # A plan left over from the last run would tick against this one.
        source = read("js", "agents.js")
        assert "researchGoals = [];" in source
        assert "document.getElementById('research-plan-host').innerHTML = '';" in source

    def test_it_folds_away(self):
        """Four questions nobody is waiting on any more, standing between the
        reader and the answer. On a re-read the plan is history, and history
        belongs behind the same disclosure the trace above it uses."""
        source = read("js", "app.js")
        assert "box = document.createElement('details');" in source
        assert "<summary class=\"plan-head\">" in source

    def test_it_is_open_while_the_run_is_going(self):
        """Watching what is left is the only reason it is on screen before the
        answer exists, so a live plan that starts shut is a plan nobody sees."""
        source = read("js", "app.js")
        assert "box.open = !collapsed;" in source
        # The live call site takes the default.
        assert "renderPlan(assistantEl, payload.plan);" in source

    def test_a_re_read_turn_gets_it_shut(self):
        source = read("js", "app.js")
        assert "renderPlan(messageEl, plan, { collapsed: true });" in source

    def test_the_rule_under_the_head_belongs_to_the_open_state(self):
        """Left on the head alone it underlines a closed card with nothing
        beneath it."""
        css = read("css", "style.css")
        assert ".plan-box[open] > .plan-head { border-bottom: 1px solid var(--border); }" in css

    def test_it_discloses_the_same_way_the_trace_does(self):
        """Two folds stacked on one message should not be two gestures."""
        css = read("css", "style.css")
        assert ".plan-box[open] > .plan-head::after { transform: rotate(90deg); }" in css
        assert "details.trace[open] > .trace-summary::before { transform: rotate(90deg); }" in css


class TestTheTerminalStartsOutOfTheWay:
    def test_it_is_collapsed_by_default(self):
        # Open by default it took 190px off the conversation for a tool most
        # turns never touch, and made the workspace read as a developer console.
        html = read("index.html")
        assert '<div id="terminal-panel" class="collapsed">' in html

    def test_it_is_still_one_click_away(self):
        assert 'onclick="toggleTerminal()"' in read("index.html")


class TestTheCodeTabWatchesItsPlanToo:
    """The checklist was built for chat and Research; the Code tab is where a
    plan matters most, because its steps are changes to your files."""

    def test_it_renders_the_same_component(self):
        source = read("js", "features.js")
        assert "renderPlan(wrap, payload.plan);" in source

    def test_the_component_finds_the_coder_bubble(self):
        # The Code tab's prose is `.agent-body`, not `.content`. Without this
        # the checklist appended below the answer, where the one thing it is
        # for — seeing what is left while you wait — cannot happen.
        assert "host.querySelector('.content, .agent-body')" in read("js", "app.js")

    def test_a_pushed_back_answer_is_dropped(self):
        """The ACT-mode "you pasted a file instead of writing it" nudge has
        been in the backend all along, and this panel had no handler for it.
        `answer` only ever grew, so the rejected file rendered with the real
        reply glued underneath, and nothing said why there were two."""
        source = read("js", "features.js")
        assert "if (payload.gate) {" in source
        assert "answer = '';" in source

    def test_it_says_what_sent_the_turn_back(self):
        # A turn that silently takes extra rounds looks stuck.
        source = read("js", "features.js")
        assert "step(s) from the plan not done yet" in source


class TestTheConversationPageIsYours:
    """Four panels nobody chose, on the page you open most.

    This class used to check that each of them could be switched off and that
    the control to switch them back on outlived the column it emptied. The
    panels are gone now — recap, deadlines, milestones and machine stats, and
    the rail that held them — so the property to protect is the stronger one
    the old tests were circling: the conversation page holds the conversation
    and nothing else.

    Kept as assertions rather than deleted, because "there is no rail" is
    exactly the thing a later feature will quietly undo by adding a card
    beside the transcript because there is room for one.
    """

    def test_there_is_no_rail(self):
        assert 'id="ws-left"' not in read("index.html")

    def test_the_control_went_with_it(self):
        """A Panels button with no panels is a menu of nothing."""
        html = read("index.html")
        assert 'id="rail-menu"' not in html
        assert 'id="rail-btn"' not in html

    @pytest.mark.parametrize("card", ["card-recap", "card-deadlines",
                                      "card-milestones", "card-engine"])
    def test_no_panel_survives(self, card):
        """One left behind is a card rendering into a column that is not there
        — invisible, still fetched for, still costing a request a minute."""
        assert f'id="{card}"' not in read("index.html")

class TestTheTypefacesActuallyShip:
    """The stylesheet referenced ten font files and the logo; none was ever
    committed, so a fresh clone 404s all of them and the entire typography
    system falls back to Segoe UI while the brand mark renders as an empty
    box. The blanket `assets/` gitignore rule was swallowing them."""

    def test_every_referenced_font_exists(self):
        css = read("css", "style.css")
        for ref in set(re.findall(r'url\("(/assets/fonts/[^"]+)"\)', css)):
            path = WEB / ref.lstrip("/")
            assert path.exists(), f"{ref} is referenced but not shipped"
            assert path.stat().st_size > 1024, f"{ref} is present but empty"

    def test_the_logo_exists_where_the_css_looks_for_it(self):
        assert (WEB / "assets" / "logo.png").exists()

    def test_the_gitignore_no_longer_swallows_them(self):
        root = Path(__file__).resolve().parents[1]
        rules = (root / ".gitignore").read_text(encoding="utf-8").splitlines()
        # Anchored. Unanchored `assets/` matches at any depth, including
        # `carrot/web/assets/`, which is what hid these in the first place.
        assert "/assets/" in rules
        assert "assets/" not in rules

    def test_the_fonts_are_redistributable(self):
        # They ship inside a public installer, so the licence has to allow it.
        licences = list((WEB / "assets" / "fonts").glob("*-OFL.txt"))
        assert len(licences) >= 4, "a bundled family is missing its OFL text"

    def test_nothing_asks_for_a_font_that_was_removed(self):
        css = read("css", "style.css")
        rules = "\n".join(line for line in css.splitlines()
                          if not line.strip().startswith(("*", "/*")))
        for gone in ("InterVariable", "Vollkorn-", "PlayfairDisplay-"):
            assert gone not in rules, f"{gone} is still referenced but not shipped"

    def test_the_serif_role_is_gone_not_renamed_in_place(self):
        """A token called `--serif` holding a sans is a lie the next reader
        has to discover. The role is "things you read", so it is `--prose`."""
        css = read("css", "style.css")
        assert "--prose:" in css
        assert "var(--serif)" not in css


class TestTheCodeEditorIsAnEditor:
    """It was Monaco — the engine VS Code runs — created with almost every
    feature switched off, which is not a smaller editor but the same one with
    its lights out."""

    def js(self):
        return read("js", "features.js")

    def test_it_uses_the_app_mono_face(self):
        # So code in the editor, the terminal and a chat code block match.
        assert "fontFamily: 'DMMono" in self.js()

    def test_ligatures_are_off(self):
        # `!=` and `=>` should be the characters that are in the file.
        assert "fontLigatures: false" in self.js()

    def test_the_orientation_features_are_on(self):
        js = self.js()
        for opt in ("minimap: { enabled: true",
                    "stickyScroll: { enabled: true }",
                    "bracketPairColorization: { enabled: true }",
                    "indentation: true",
                    "folding: true"):
            assert opt in js, f"{opt} is not enabled"

    def test_the_jetbrains_bindings_are_aliases_not_replacements(self):
        """Monaco keeps its own defaults, so Ctrl+D duplicates for a JetBrains
        user and still adds a cursor for a VS Code one."""
        js = self.js()
        keymap = js.split("function jetbrainsKeymap(")[1].split("\n}")[0]
        for action in ("copyLinesDownAction", "deleteLines", "formatDocument",
                       "gotoLine", "smartSelect.expand", "rename"):
            assert action in keymap

    def test_an_unknown_action_is_skipped_not_invented(self):
        # `addAction` on an id Monaco lacks would create a dead menu entry.
        js = self.js()
        assert "if (!action) continue;" in js


class TestFormControlsInheritTheType:
    """Buttons and inputs were rendering in Arial next to a page set in Plus
    Jakarta. No browser inherits `font-family` into form controls — they use
    their own UI font unless told otherwise — and it had been patched five
    times at individual rules instead of once at the reset."""

    def test_the_reset_covers_every_control(self):
        css = read("css", "style.css")
        assert "button, input, select, textarea, optgroup { font-family: inherit; }" in css

    def test_it_only_takes_the_family(self):
        # Plenty of controls set their own size and weight deliberately; only
        # the family was ever wrong, so `font: inherit` would be a regression.
        css = read("css", "style.css")
        block = css[css.index("button, input, select, textarea, optgroup"):]
        block = block[:block.index("}")]
        assert "font-size" not in block and "font-weight" not in block


class TestTheFileTreeMarksItsTypes:
    """A tree of identical grey rows makes you read every name to find one."""

    def test_common_languages_are_distinguishable(self):
        js = read("js", "features.js")
        marks = js.split("const FILE_MARKS = {")[1].split("\n};")[0]
        for ext in ("py", "js", "ts", "json", "html", "css", "md", "go", "rs", "cpp"):
            assert f"{ext}:" in marks, f"{ext} has no mark"

    def test_extensionless_names_are_recognised_whole(self):
        # `.gitignore` has no extension; `Dockerfile` would read as plain text.
        js = read("js", "features.js")
        by_name = js.split("const FILE_MARKS_BY_NAME = {")[1].split("\n};")[0]
        assert "'.gitignore'" in by_name and "'dockerfile'" in by_name

    def test_an_unknown_type_still_gets_a_mark(self):
        # Returning nothing would collapse the row and misalign the tree.
        js = read("js", "features.js")
        assert "return mark || ['\u2022', '#6b7280'];" in js

    def test_the_tree_and_the_tabs_use_the_same_mark(self):
        js = read("js", "features.js")
        assert js.count("fileMarkHtml(") >= 3   # definition + tree + tabs

    def test_there_is_a_fallback_without_color_mix(self):
        # Older WebKit would otherwise render every badge as an untinted box.
        css = read("css", "style.css")
        assert "@supports not (background: color-mix(in srgb, red 10%, transparent))" in css


class TestInlineCitationsAreChips:
    """A paragraph carrying five source links read as a paragraph with five
    interruptions in it — and when the model wrote the link flush against the
    last word you got "architectural interestsAl Jazeera", a sentence that
    appears to end in a proper noun."""

    def test_a_glued_citation_gets_its_space_back(self):
        from carrot import app
        assert app._tidy_answer("architectural interests[Al Jazeera](https://x.com).") == \
            "architectural interests [Al Jazeera](https://x.com)."
        assert app._tidy_answer("at 15%[Pew](https://p.com).") == "at 15% [Pew](https://p.com)."

    def test_it_does_not_touch_correct_markdown(self):
        from carrot import app
        for text in ("a normal [link](https://w.com) here", "**bold**[link](https://z.com)"):
            assert app._tidy_answer(text) == text

    def test_the_repair_is_only_whitespace(self):
        """Anything that rewrites the model's words belongs in the directive
        where it can be argued with, not in a regex that silently edits what
        the user is told."""
        from carrot import app
        text = "The Senate voted 86-11[Democrats](https://d.com)."
        assert app._tidy_answer(text).replace(" ", "") == text.replace(" ", "")

    def test_only_short_source_names_become_chips(self):
        # A link whose text is a sentence is the author linking a phrase, and
        # turning that into a chip would be rewriting their prose.
        js = read("js", "features.js")
        block = js.split("function markCitations(")[1].split("\n}")[0]
        assert "CITE_MAX_CHARS" in block
        assert "split(/\s+/).length > 4" in block

    def test_a_standalone_source_link_is_left_alone(self):
        # A link that is the whole of its own line is a sources list, which
        # already reads correctly.
        js = read("js", "features.js")
        block = js.split("function markCitations(")[1].split("\n}")[0]
        assert "childNodes.length === 1" in block

    def test_no_favicons_are_fetched(self):
        """Asking every cited domain for an image on every render tells those
        sites what you are reading — the one thing this app exists not to do."""
        css = read("css", "style.css")
        block = css[css.index(".md a.cite-chip::before"):]
        block = block[:block.index("}")]
        assert "url(" not in block


class TestTheDropdownArrowSurvivesItsOwnStylesheet:
    """A row of filters rendered as a wall of tiled chevrons.

    `select` sets `appearance: none`, so the chevron drawn by the `select` rule
    is the *only* arrow a dropdown has. `background` is a shorthand: any later
    or more specific rule setting `background: <colour>` on something that
    reaches a `<select>` silently resets `background-image`, `background-repeat`
    and `background-position` together.

    That produced two different bugs from one cause. In dark, `.write-filter`
    won outright and the arrow disappeared — a dropdown with no affordance at
    all. In light, `:root[data-theme="light"] select` is more specific, so it
    put the *image* back while repeat stayed `repeat` and position stayed
    `0 0`, and the control filled edge to edge with chevrons.

    Four more selects were found in the same state once it was looked for:
    #worktree-picker, #agent-model, #prov-preset and #prov-kind.
    """

    # `select` as an element in the selector — not `user-select`, not `.selected`.
    REACHES_A_SELECT = re.compile(r"(^|[\s,>+~])select($|[\s,.:>+~\[])")

    def _rules(self):
        css = read("css", "style.css")
        # Flat enough for this: every `... { ... }` whose body has no nested
        # brace. Comments run together with the following selector, so they are
        # stripped rather than matched against.
        for match in re.finditer(r"([^{}]+)\{([^{}]*)\}", css):
            selector = re.sub(r"/\*.*?\*/", "", match.group(1), flags=re.S).strip()
            yield selector, match.group(2)

    def test_no_class_scoped_rule_clears_the_chevron_with_a_shorthand(self):
        """Scoped by class or id, because that is what makes a rule dangerous.

        A bare `input, select, textarea { background: ... }` has the same
        specificity as the `select` rule and loses to it on source order, which
        is why several of those are harmless and still in the file. A rule
        carrying a class or an id outranks `select` outright and wins wherever
        it sits — that is the shape all five bugs had.
        """
        offenders = []
        for selector, body in self._rules():
            if selector.startswith("@") or not self.REACHES_A_SELECT.search(selector):
                continue
            # The popup's own rows have no chevron to clear.
            if re.search(r"select\s+(option|optgroup)", selector):
                continue
            reaching = [p for p in selector.split(",")
                        if self.REACHES_A_SELECT.search(p.strip())]
            if not any(re.search(r"[.#]", p) for p in reaching):
                continue
            if re.search(r"(^|;|\s)background\s*:", body):
                offenders.append(selector.replace("\n", " ")[:70])
        assert not offenders, (
            "these outrank `select` and set the `background` shorthand, which "
            "clears the chevron that `appearance: none` makes the only arrow — "
            f"use background-color: {offenders}")

    def test_the_chevron_and_its_geometry_live_in_one_rule(self):
        """Splitting the image from its repeat/position is what let a theme
        override restore one without the other."""
        css = read("css", "style.css")
        block = css[css.index("\nselect {"):]
        block = block[:block.index("}")]
        for prop in ("background-image", "background-repeat", "background-position"):
            assert prop in block, f"{prop} belongs with the chevron, not elsewhere"

    def test_the_theme_swaps_a_token_rather_than_the_image(self):
        css = read("css", "style.css")
        # Found by selector prefix: the block carries `.paper` as well.
        light = css[css.index(':root[data-theme="light"]'):]
        light = light[light.index("{"):light.index("}")]
        assert "--select-chevron" in light
        # Comments stripped first — the one above the token explains why there
        # is no background-image here, and would otherwise match.
        declarations = re.sub(r"/\*.*?\*/", "", light, flags=re.S)
        assert "background-image" not in declarations


class TestThePhoneLayout:
    """The app is reachable from a phone now, which makes 375px a real width
    rather than a hypothetical one. It used to lay out at a 672px minimum and
    get scaled down by the browser — legible, in the way a photograph of a
    document is legible."""

    @property
    def phone_block(self):
        css = read("css", "style.css")
        start = css.index("@media (max-width: 640px)")
        return css[start:]

    def test_there_is_a_phone_breakpoint(self):
        assert "@media (max-width: 640px)" in read("css", "style.css")

    def test_the_rail_becomes_a_drawer(self):
        """216px of sidebar on a 375px screen is not a sidebar. It holds what
        is running and what failed, so it cannot simply be hidden either."""
        block = self.phone_block
        assert "--nav-w: 0px" in block
        assert "body.nav-open .app-nav" in block
        assert "translateX(-100%)" in block

    def test_the_composer_is_pinned_to_the_bottom_even_when_blank(self):
        """`body.chat-blank` centres it under the question, which is right for
        a window and wrong for a phone: `top: 50%` is measured against the
        layout viewport, so the keyboard opens over the box you are typing
        into."""
        assert "body.chat-blank #cmdbar" in self.phone_block

    def test_the_composer_controls_wrap_by_the_id_they_actually_have(self):
        """Written as `.cmd-row` first, which matches nothing: the element is
        `#cmd-row`. A selector for a class no element has is invisible — the
        rule is in the stylesheet, the row still overflows, and nothing
        anywhere reports a problem."""
        assert "#cmd-row { flex-wrap: wrap" in self.phone_block
        assert 'id="cmd-row"' in read("index.html")

    def test_the_drawer_closes_when_something_in_it_is_chosen(self):
        """A drawer left covering the thing you just asked for is the single
        most common way this gets built wrong."""
        app_js = read("js", "app.js")
        assert "closeNavDrawer" in app_js
        assert "nav-item, .nav-job, .nav-recent" in app_js


class TestDiagramsAndMathsAreDrawnNotPrinted:
    """A mermaid artifact was stored, listed, downloadable, editable — and
    rendered as a <pre> full of `graph TD; A-->B`. The one kind of artifact
    whose entire purpose is to be a picture was the only one shown as its
    source."""

    def test_mermaid_is_bundled_offline(self):
        """Not a CDN. The argument of this app is that it runs on your machine,
        and a diagram that only draws when the network is up would be the one
        part of a reply that depends on somebody else's server."""
        assert (WEB / "vendor" / "mermaid.js").exists()
        build = read_py("webvendor/build.mjs")
        assert "mermaid-entry.js" in build

    def test_it_is_loaded_only_when_something_needs_it(self):
        """3.3MB, and most conversations contain no diagram at all."""
        features = read("js", "features.js")
        assert "'/vendor/mermaid.js'" in features
        assert '<script src="/vendor/mermaid.js"' not in read("index.html")

    def test_a_mermaid_artifact_renders(self):
        features = read("js", "features.js")
        branch = features[features.index("artifact.kind === 'mermaid'"):][:400]
        assert "renderMermaid" in branch
        assert "textContent = artifact.content" not in branch

    def test_fenced_blocks_are_marked_for_rendering(self):
        features = read("js", "features.js")
        assert "data-render" in features
        assert "hydrateBlocks" in features and "hydrateBlocks" in read("js", "app.js")

    def test_a_latex_document_stays_source(self):
        r"""A \documentclass preamble is a file somebody wants to read and
        copy, not an expression to typeset — and KaTeX cannot render one."""
        features = read("js", "features.js")
        assert "isLatexDocument" in features
        assert "documentclass" in features

    def test_a_broken_diagram_shows_its_source_and_the_reason(self):
        """One line away from working, usually. Showing nothing hides both the
        mistake and the content."""
        features = read("js", "features.js")
        assert "mermaid-error" in features
        assert "artifact-mermaid-source" in features


class TestTheChatButtonStartsANewOne:
    def test_it_is_its_own_handler(self):
        """Pressing it from elsewhere navigates; pressing it while already in a
        conversation starts a fresh one, which is what every app with a compose
        button does and what people try here first."""
        index = read("index.html")
        assert 'data-tab="workspace" onclick="goToChat()"' in index
        app_js = read("js", "app.js")
        goto = app_js[app_js.index("function goToChat"):][:600]
        assert "newChat()" in goto
        # An empty new session is left alone: clearing a blank screen reads as
        # a dead button.
        assert "currentConversationId" in goto


class TestSeveralDocumentsOpenAtOnce:
    """Work held exactly one. Opening a second closed the first — not visibly,
    it just stopped being on screen — so anything needing two documents at the
    same time meant going back to the grid and losing your place in both."""

    def test_the_strip_exists_and_is_loaded(self):
        index = read("index.html")
        assert 'id="doc-tabs"' in index
        assert '<script src="/js/doctabs.js"></script>' in index

    def test_switching_saves_the_one_being_left_first(self):
        """Autosave is on an 800ms timer, so leaving a document within a second
        of typing in it would drop the last thing typed — which is exactly when
        somebody switches away."""
        tabs = read("js", "doctabs.js")
        switch = tabs[tabs.index("async function switchToDoc"):][:400]
        assert "await flushPendingNoteSave()" in switch
        assert switch.index("flushPendingNoteSave") < switch.index("openNote")
        assert "async function flushPendingNoteSave" in read("js", "features.js")

    def test_a_document_opens_once(self):
        """Two tabs on one file is two editors on one autosave, and the second
        one to save wins."""
        tabs = read("js", "doctabs.js")
        opened = tabs[tabs.index("function noteOpened"):][:600]
        assert "openDocs.find(d => d.id === note.id)" in opened

    def test_closing_the_last_one_goes_to_the_grid(self):
        """Not to an empty editor pointed at no document."""
        tabs = read("js", "doctabs.js")
        close = tabs[tabs.index("async function closeDoc"):][:900]
        assert "showWriteStart" in close

    def test_a_deleted_document_loses_its_tab(self):
        assert "forgetDoc" in read("js", "features.js")
        assert "function forgetDoc" in read("js", "doctabs.js")

    def test_one_document_shows_no_strip(self):
        """A single tab is chrome that explains itself and nothing else."""
        tabs = read("js", "doctabs.js")
        assert "openDocs.length < 2" in tabs


class TestTheWorkBadgeSaysWhatItCounts:
    def test_it_names_notifications_rather_than_documents(self):
        """A bare number beside the word "Work" reads as a count of the things
        Work contains, which is documents. It counts unread notifications."""
        index = read("index.html")
        badge = index[index.index('id="notification-badge"'):][:220]
        assert "Unread notifications" in badge
        agentops = read("js", "agentops.js")
        assert "unread notification" in agentops


class TestTheSendControlSaysOneThingOnce:
    def test_the_button_does_not_repeat_the_destination(self):
        """The picker immediately to its left already says Chat. Printing the
        same word in the button beside it reads as two controls that both do
        something with chat, rather than one control and the place it points."""
        docagent = read("js", "docagent.js")
        assert "`Send to ${spec.label}`" not in docagent
        assert "button.textContent = 'Send'" in docagent
        # Where it goes is still said, on the hover.
        assert "Send this note (or the selected text) to ${spec.label}" in docagent
