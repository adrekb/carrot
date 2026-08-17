"""Whether anything is leaving this computer, without going to look for it.

The application's whole argument is that it does not have to send your work
anywhere. That claim was only visible in two places: a sentence on the empty
chat screen, and Settings — so the moment you had said anything at all, there
was nothing on screen saying which model was answering or what else was
switched on.

The rail now ends in a dot, where it runs, and which model. The detail is one
click deeper, because "Ollama server 127.0.0.1 unavailable" is not a sentence a
non-technical person can act on and "Local · Ready" is.
"""
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "carrot" / "web"
HTML = (WEB / "index.html").read_text(encoding="utf-8")
JS = (WEB / "js" / "app.js").read_text(encoding="utf-8")
CSS = (WEB / "css" / "style.css").read_text(encoding="utf-8")


def block(name):
    start = JS.index(f"function {name}")
    return JS[start:JS.index("\n}", start)]


def code(name):
    """One function's body with its comments removed.

    The comments in these functions quote the wording they replaced — that is
    what makes them worth reading — so a test grepping the raw source matches
    its own explanation and passes or fails for the wrong reason.
    """
    import re

    return re.sub(r"//[^\n]*", "", block(name))


class TestItIsThere:
    def test_the_chip_is_in_the_rail(self):
        assert 'id="privacy-chip"' in HTML
        assert 'class="nav-foot"' in HTML

    def test_it_sits_at_the_foot(self):
        """`margin-top: auto` rather than absolute positioning, so it still
        reaches in a short window instead of overlapping the last tab."""
        rule = CSS[CSS.index(".nav-foot {"):]
        assert "margin-top: auto" in rule[:rule.index("}")]

    def test_the_dot_survives_a_collapsed_rail(self):
        """Collapsed, the words go and the dot stays — it is still the fastest
        answer to "is anything leaving", which is the point of it."""
        assert "body.nav-collapsed .privacy-text" in CSS

    def test_the_panel_escapes_the_rail(self):
        """`.app-nav` scrolls, which makes it a clipping context — so a 268px
        panel positioned inside a 210px column had its right-hand side cut
        away and the headline read "Nothing is leaving this compute".

        Fixed rather than absolute takes it out of that box, which means
        nothing places it any more and something has to.
        """
        rule = CSS[CSS.index(".privacy-panel {"):]
        rule = rule[:rule.index("}")]
        assert "position: fixed" in rule
        assert "function placePrivacyPanel" in JS
        assert "placePrivacyPanel()" in block("togglePrivacyPanel")

    def test_it_is_placed_again_once_the_rows_arrive(self):
        """The rows load after the first placement and are taller than the
        "Checking…" line they replace, so without this it opens in the right
        spot and then grows off the bottom of the window."""
        assert "placePrivacyPanel()" in block("fillPrivacyPanel")

    def test_every_piece_it_builds_is_styled(self):
        for cls in (".privacy-chip", ".privacy-dot", ".privacy-panel",
                    ".privacy-row", ".privacy-head", ".privacy-more"):
            assert cls in CSS, f"{cls} is built by the chip but never styled"


class TestOneSourceOfTruth:
    def test_the_chip_and_the_empty_state_ask_the_same_question(self):
        """Two renderings of one fact. Worked out twice, they can disagree —
        and a privacy indicator that contradicts the sentence three inches away
        is worse than neither."""
        assert "function answersStayLocal" in JS
        assert "answersStayLocal()" in block("renderEmptyStateLine")
        assert "answersStayLocal()" in block("renderPrivacyChip")

    def test_auto_is_judged_on_what_it_could_reach(self):
        """Under Auto the promise holds only if none of the tasks it can reach
        escalates, so the claim comes from that rather than from whichever
        model the last turn happened to use."""
        body = block("answersStayLocal")
        assert "autoIsLocal" in body

    def test_the_chip_repaints_when_the_model_changes(self):
        assert "renderPrivacyChip()" in block("renderEmptyStateLine")


class TestThePanelIsHonest:
    def test_it_reads_the_real_switches(self):
        """Not a second copy of the state. If this panel and the assistant
        disagree about what Carrot can see, this panel is the bug — so it asks
        the same endpoints the assistant is told about."""
        body = block("fillPrivacyPanel")
        for path in ("/api/calendar/status", "/api/ambient", "/api/config"):
            assert path in body, path

    def test_one_dead_endpoint_does_not_empty_the_panel(self):
        """Each row is asked for separately and contained separately: a
        service being unreachable should grey out its own line, not leave
        somebody unable to find out anything at all."""
        body = code("fillPrivacyPanel")
        assert "catch (err)" in body
        assert "return { on: false, unknown: true," in body

    def test_a_setting_it_could_not_read_says_so(self):
        """It printed "Unknown", which is the exact silence this panel exists
        to remove: at a glance it is indistinguishable from "off", and on a
        screen whose whole job is saying what is switched on, "off" is the
        reading that matters. The reason goes to the console, because a row
        cannot hold a stack trace and somebody debugging needs it.
        """
        body = code("fillPrivacyPanel")
        assert "Unknown" not in body
        assert "Could not check" in body
        assert "console.warn" in body

    def test_it_does_not_promise_quiet_over_a_gap(self):
        """"Nothing is leaving this computer" is a promise, and it cannot be
        made over a setting that failed to answer. A row that could not be read
        is a gap, not evidence of a quiet machine."""
        body = code("fillPrivacyPanel")
        assert "unchecked" in body
        assert "Nothing known to be leaving" in body

    def test_an_unread_row_does_not_look_like_a_no(self):
        """A hollow ring, not an empty dot: an empty dot reads as "no", and
        answering "no" to a question you never managed to ask is the one thing
        this panel must not do."""
        rule = CSS[CSS.index(".privacy-row.unchecked .privacy-row-dot {"):]
        rule = rule[:rule.index("}")]
        assert "background: none" in rule
        assert "inset" in rule

    def test_the_headline_wraps_rather_than_clips(self):
        """It grows with however many things are going out, and a truncated
        privacy claim is worse than a two-line one — "Leaving this computer:
        answers and web sea" names one of the two."""
        rule = CSS[CSS.index(".privacy-head {"):]
        assert "overflow-wrap" in rule[:rule.index("}")]

    def test_it_names_what_leaves_rather_than_counting_it(self):
        """Two amber dots tell you something is going out and not what."""
        body = code("fillPrivacyPanel")
        assert "Leaving this computer: " in body
        assert "Nothing is leaving this computer" in body

    def test_the_headline_does_not_have_to_conjugate(self):
        """The subject is sometimes plural ("answers") and sometimes not ("web
        search"), so a verb has to agree with something this cannot know.
        Agreeing with the length of the list gets it wrong as soon as one
        plural thing goes out alone — "Answers leaves this computer".
        """
        # Scoped to the headline. The rows are allowed their own verbs — "web
        # search" has a fixed subject and can say "Searches leave this
        # computer" safely; it is only the headline whose subject varies.
        body = block("fillPrivacyPanel")
        headline = body[body.index("const headline"):]
        headline = headline[:headline.index(";")]
        assert "Leaving this computer: " in headline
        assert "leaves" not in headline
        assert "leave " not in headline

    def test_a_hosted_model_is_not_dressed_as_a_fault(self):
        """Sending a question to a model you chose is not an error, and
        colouring it red would make the honest state look like a failure."""
        rule = CSS[CSS.index(".privacy-dot.cloud {"):]
        rule = rule[:rule.index("}")]
        assert "--red" not in rule
        assert "--accent" in rule

    def test_it_offers_the_way_to_change_it(self):
        """A status light you cannot act on is a decoration."""
        assert "privacy-more" in block("fillPrivacyPanel")
        assert "switchTab(\\'settings\\')" in block("fillPrivacyPanel") \
            or "switchTab('settings')" in block("fillPrivacyPanel")
