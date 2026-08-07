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

    The first fix keyed the label collapse to a viewport media query, which is
    the wrong measurement: the bar is capped at 760px however wide the window
    is, so on a 1000px screen the labels stayed on, eight controls took ~700px,
    and the placeholder rendered as "Ask ar".
    """

    def test_the_collapse_is_keyed_to_the_bar_not_the_window(self):
        css = read("css", "style.css")
        assert "#cmdbar { container-type: inline-size; }" in css

    def test_the_collapse_fires_at_the_width_the_bar_actually_rests_at(self):
        """The breakpoint has to clear the bar's own cap, or it never fires.

        Keying to the bar is necessary but not sufficient. The first version
        asserted the literal string "@container (max-width: 720px)" and passed
        green while the bug was live, because 720px is *below* the 760px the bar
        rests at — so on a 1000px window the query never matched, the input
        stayed pinned to its 160px floor, and the placeholder still cut off
        mid-word. Compare the two numbers instead of spelling either one out.
        """
        css = read("css", "style.css")
        # Scoped to the #cmdbar rules on purpose: an unanchored min(Npx, calc(
        # 100vw ...)) also matches unrelated panels and drags their widths in.
        caps = [int(n) for n in re.findall(r"#cmdbar\s*{[^}]*?width:\s*min\((\d+)px", css)]
        resting = min(caps)          # the narrow bar, the one that broke
        widest = max(caps)           # the wide-screen bar, where labels still fit
        assert resting == 760 and widest == 920, f"bar widths moved: {sorted(set(caps))}"

        breakpoints = [int(n) for n in re.findall(r"@container \(max-width: (\d+)px\)", css)]
        collapse = max(breakpoints)
        assert collapse >= resting, (
            f"the label collapse fires at {collapse}px but the bar rests at "
            f"{resting}px, so it never fires when the input is squeezed"
        )
        assert collapse < widest, (
            f"the collapse at {collapse}px would also strip the labels off the "
            f"{widest}px wide-screen bar, where they fit fine"
        )

    def test_there_is_a_fallback_without_container_queries(self):
        css = read("css", "style.css")
        assert "@supports not (container-type: inline-size)" in css

    def test_the_input_has_a_floor(self):
        assert "#cmd-input { min-width: 160px; }" in read("css", "style.css")


class TestTheAgentSaysWhichModelItUses:
    """It silently borrowed whatever the chat composer was set to.

    A coding agent is where the model matters most — a 4B local model and a
    frontier model are not interchangeable at editing a file — and it was the
    one place you could neither see the choice nor make it.
    """

    def test_the_picker_is_in_the_panel(self):
        assert 'id="agent-model"' in read("index.html")

    def test_it_is_populated_from_the_models_the_machine_can_reach(self):
        js = read("js", "features.js")
        assert "loadAgentModelPicker" in js
        assert "loadAvailableModels" in js

    def test_following_the_chat_model_stays_the_default(self):
        # Silently changing which model runs someone's agent is not an upgrade.
        js = read("js", "features.js")
        assert "Same as chat" in js

    def test_the_choice_actually_reaches_the_request(self):
        js = read("js", "features.js")
        body = js.split("await fetch('/api/chat/stream'")[1].split("});")[0]
        assert "agentModel" in body

    def test_the_choice_survives_a_restart(self):
        assert "carrot-agent-model" in read("js", "features.js")


class TestTheComposerKeepsItsShape:
    """Reported with a screenshot: the send button as a tall orange sliver.

    `#cmd-input` is a flex item, and `min-width: auto` floors a text input at
    its content width — about 160px. The bar had to find that space somewhere
    and took it from the only items that would give: every fixed-size control
    beside it. In a narrow window the send button was squeezed to 16px wide
    while staying 36 tall, and a `border-radius: 50%` circle rendered at those
    dimensions is an ellipse.

    Refusing to deform them is only half the fix. Nine controls and a text
    field do not fit on one line in a narrow window, so without wrapping the
    row simply overflows and the send button is clipped by the edge instead of
    squashed by its neighbours — which is the other thing in the screenshot.
    """

    def css(self):
        return read("css", "style.css")

    def test_the_input_is_what_gives_up_space(self):
        block = self.css()[self.css().index("#cmd-input {"):]
        assert "min-width: 0;" in block[:block.index("}")]

    def test_the_controls_do_not_shrink(self):
        assert "#cmdbar > button," in self.css()
        assert "flex-shrink: 0;" in self.css()

    def test_the_send_button_holds_its_own_size(self):
        block = self.css()[self.css().index("#send-btn {"):]
        block = block[:block.index("}")]
        assert "flex-shrink: 0" in block
        # A circle is only a circle while the two are equal.
        assert "width: 36px; height: 36px;" in block

    def test_a_narrow_window_wraps_instead_of_overflowing(self):
        css = self.css()
        assert "@media (max-width: 620px)" in css
        wrapped = css[css.index("@media (max-width: 620px)"):]
        assert "flex-wrap: wrap;" in wrapped
        # The text field goes on top, where it is the thing being used.
        assert "order: -1;" in wrapped
        assert "flex: 1 0 100%;" in wrapped
