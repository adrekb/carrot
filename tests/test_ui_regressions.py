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

        The composer's popovers are children of `#cmdbar`, which has a
        `backdrop-filter` of its own. Nested backdrop-filters do not compose —
        the ancestor establishes the backdrop root, so the child's blur samples
        an already-flattened backdrop and does nothing, leaving the bare 82%
        alpha of the glass colour. Glass is for a layer over its own
        background; these float over arbitrary content.
        """
        css = read("css", "style.css")
        block = css[css.index("#cmdbar #tool-pop,"):]
        block = block[:block.index("}")]
        for pop in ("#cmdbar #model-pop", "#cmdbar #search-pop"):
            assert pop in block, f"{pop} has the same flaw and is not covered"
        assert "background: var(--card);" in block
        assert "backdrop-filter: none;" in block

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

    A morning recap and a deadline list are useful to some people and pure
    noise to others, and the home page is the worst place to guess on
    someone's behalf.
    """

    def test_every_panel_can_be_named(self):
        # The toggle finds cards by this attribute; a panel without one is
        # unreachable from the menu and silently permanent.
        html = read("index.html")
        rail = html.split('<aside id="ws-left">')[1].split("</aside>")[0]
        for panel in ("recap", "deadlines", "milestones", "engine"):
            assert f'data-panel="{panel}"' in rail

    def test_the_choice_survives_a_restart(self):
        js = read("js", "app.js")
        assert "localStorage.setItem(RAIL_KEY" in js
        assert "/api/config/ui_rail_hidden" in js

    def test_it_paints_before_the_network_answers(self):
        """Local-first for the same reason the theme is: a panel you switched
        off must not flash on and then vanish."""
        js = read("js", "app.js")
        assert "let railHidden = readRailPref();" in js
        # And the server copy must never override a local choice, which is the
        # more recent expression of intent and what already painted.
        sync = js.split("function syncRailFromServer(")[1].split("\n}")[0]
        assert "if (stored ||" in sync

    def test_an_unknown_panel_id_is_ignored(self):
        # A stored list from a later version, or a hand-edited one, must not
        # be able to hide something that no longer exists or was never a panel.
        js = read("js", "app.js")
        assert "filter(id => known.includes(id))" in js

    def test_a_hidden_panel_is_not_fetched_for(self):
        # Hiding it in CSS while still polling would keep the cost and lose
        # the point.
        js = read("js", "app.js")
        body = js.split("async function loadWorkspace()")[1].split("\n}")[0]
        assert "if (shown('recap')) loadRecapCard();" in body

    def test_hiding_everything_removes_the_column(self):
        """Switching them all off has to give the width back.

        A 320px column standing empty is not less clutter, it is the same
        clutter with the content taken out.
        """
        js = read("js", "app.js")
        assert "?.classList.toggle('hidden', railHidden.length >= RAIL_PANELS.length)" in js
        # The rail is a flex child with a fixed width, so `hidden` has to beat
        # both of those, not just `display: flex`.
        assert "#ws-left.hidden { display: none; }" in read("css", "style.css")

    def test_the_control_outlives_the_thing_it_controls(self):
        """It lives in the tabstrip, not the rail. A button that disappears
        along with what it switches off cannot switch it back on."""
        html = read("index.html")
        strip = html.split('<div id="ws-tabstrip">')[1].split("</div>\n      <div id=")[0]
        assert 'id="rail-menu"' in strip
        rail = html.split('<aside id="ws-left">')[1].split("</aside>")[0]
        assert 'id="rail-menu"' not in rail

    def test_the_engine_card_says_what_it_answers(self):
        """"Local Engine" named the implementation, not the question. After
        the conversation and message counts were added it had stopped being
        about the engine at all."""
        html = read("index.html")
        assert "This machine" in html
        # The comment above the card explains the rename and names the old
        # title, so compare against the markup rather than the file.
        markup = re.sub(r"<!--.*?-->", "", html, flags=re.S)
        assert "Local Engine" not in markup


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
