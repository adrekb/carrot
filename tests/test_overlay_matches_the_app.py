"""The Alt-Space panel wears the app's palette, not an older copy of it.

The overlay is a `file://` page. It opens on a global shortcut, often before
the backend has finished starting, so it can neither import style.css nor read
the app's stored theme — the desktop shell relays the theme and stamps
`data-theme`/`data-accent` on `<html>`, and the colours are copied into the
file by hand. A copy drifts. These tests are what makes the duplication
survivable: change a colour in one file and the other fails until it is
changed too.

**The app's palette is not what the top of its stylesheet says.** style.css
declares `--card` and `--accent` in more than one `:root` block, and the later
one wins — read top-down it looks like a cool slate theme (#1e2027 cards,
#eceef4 text) that the app has never actually rendered. What it renders is the
black theme (#151517, #f4f4f6). So the expectations here come from resolving the cascade,
and `test_the_resolver_agrees_with_a_running_window` pins the resolver against
values read out of a live one — a resolver that quietly went wrong would
otherwise make every test below agree with it and mean nothing.

What had actually drifted was small and all one way: the panel a shade darker
than a card, the stroke heavier, `--sheen` left as `transparent` (so no hover
in the panel did anything), and ember's fill still #df3d17 — the value that sat
in the gap between the two inks, 4.25 against `--on-accent` and 4.36 against
white, failing AA by a little on every filled ember surface.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_CSS = ROOT / "carrot" / "web" / "css" / "style.css"
OVERLAY = ROOT / "gui" / "public" / "overlay.html"

# Read out of a running window (data-theme + data-accent stamped, computed
# style on :root). The resolver below has to reproduce these exactly.
MEASURED_DARK = {
    "--card": "#151517",
    "--card2": "#1f1f23",
    "--border": "rgba(255, 255, 255, 0.08)",
    "--border-hi": "rgba(255, 255, 255, 0.15)",
    "--text": "#f4f4f6",
    "--muted": "#a0a0a8",
    "--accent": "#ff7a2b",
    "--accent-fill": "#e0620f",
    "--on-accent": "#1a1208",
}
MEASURED_LIGHT = {
    "--card": "#fffdf8",
    "--card2": "#efe9db",
    "--border": "rgba(40, 34, 22, 0.11)",
    "--border-hi": "rgba(40, 34, 22, 0.18)",
    "--text": "#1e1b14",
    "--muted": "#5f584a",
}
MEASURED_ACCENTS = {
    "ember":  {"--accent": "#ff4f28", "--accent-fill": "#e8471f", "--on-accent": "#1a1208"},
    "amber":  {"--accent": "#ffab1a", "--accent-fill": "#e09000", "--on-accent": "#1a1208"},
    "orchid": {"--accent": "#b95cf5", "--accent-fill": "#8e2fd0", "--on-accent": "#ffffff"},
    "teal":   {"--accent": "#10c9a6", "--accent-fill": "#0a9b80", "--on-accent": "#0a1a16"},
    "indigo": {"--accent": "#2f6bff", "--accent-fill": "#1f55dd", "--on-accent": "#ffffff"},
}

BLOCK = re.compile(r"([^{}]+)\{([^{}]*)\}", re.DOTALL)
DECL = re.compile(r"(--[a-z0-9-]+)\s*:\s*([^;]+);")
COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)


def stylesheet(source):
    """Just the CSS, with comments gone.

    Two things would otherwise be read as selectors: a comment sitting above a
    block (the chunk between `}` and `{` is everything, comment included — and
    this file comments heavily), and, in the overlay, the script at the end,
    whose function bodies are braces as far as a regex is concerned.
    """
    styles = re.findall(r"<style>(.*?)</style>", source, re.DOTALL)
    return COMMENT.sub("", "\n".join(styles) if styles else source)


def declarations(source, matches):
    """Custom properties for an element the `matches` predicate accepts.

    Last-wins across the whole file rather than a specificity sort, which is
    the same answer here: style.css puts each override after the block it
    overrides. Anything more would be reimplementing a cascade to test a
    stylesheet, and the measured values keep this honest.
    """
    resolved = {}
    for selectors, body in BLOCK.findall(stylesheet(source)):
        parts = [part.strip() for part in selectors.split(",")]
        if not any(matches(part) for part in parts):
            continue
        for name, value in DECL.findall(body):
            resolved[name] = value.strip()
    return resolved


def app_palette(source, theme="dark", accent="carrot"):
    def matches(selector):
        if not selector.startswith(":root"):
            return False
        rest = selector[len(":root"):]
        for attr, wanted in (("data-theme", theme), ("data-accent", accent)):
            for found in re.findall(r'\[%s="([^"]+)"\]' % attr, rest):
                if found != wanted:
                    return False
        # A `[data-theme]` block never applies to the default dark root.
        if theme == "dark" and "data-theme" in rest:
            return False
        return not re.sub(r'\[[a-z-]+="[^"]+"\]', "", rest).strip()
    return declarations(source, matches)


def overlay_palette(source, theme="dark", accent=None):
    def matches(selector):
        if selector == ":root":
            return True
        if theme == "light" and selector == ':root[data-theme="light"]':
            return True
        return bool(accent) and selector == ':root[data-accent="%s"]' % accent
    return declarations(source, matches)


@pytest.fixture(scope="module")
def app():
    return APP_CSS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def overlay():
    return OVERLAY.read_text(encoding="utf-8")


class TestTheResolverIsTrustworthy:
    """Everything below compares two files. If the thing that reads the app's
    file is wrong, the comparisons still pass and say nothing."""

    @pytest.mark.parametrize("name,expected", sorted(MEASURED_DARK.items()))
    def test_it_agrees_with_a_running_window_in_dark(self, app, name, expected):
        assert app_palette(app, "dark")[name] == expected

    @pytest.mark.parametrize("name,expected", sorted(MEASURED_LIGHT.items()))
    def test_it_agrees_with_a_running_window_in_light(self, app, name, expected):
        assert app_palette(app, "light")[name] == expected

    def test_the_top_of_the_stylesheet_is_not_what_renders(self, app):
        """The trap this resolver exists for: the first `:root` says #1e2027
        and the window draws #151517."""
        first = re.search(r":root\s*\{(.*?)\n\}", app, re.DOTALL).group(1)
        assert "--card: #1e2027;" in first
        assert app_palette(app, "dark")["--card"] == "#151517"


# The overlay's name for a colour, and the app's name for the same one.
SHARED = [
    ("--panel", "--card"),
    ("--sheen", "--card2"),
    ("--stroke", "--border"),
    ("--stroke-hi", "--border-hi"),
    ("--text", "--text"),
    ("--muted", "--muted"),
    ("--accent", "--accent"),
    ("--accent-fill", "--accent-fill"),
    ("--on-accent", "--on-accent"),
]


class TestTheDarkPaletteIsTheApps:
    @pytest.mark.parametrize("here,there", SHARED)
    def test_the_value_matches(self, app, overlay, here, there):
        mine = overlay_palette(overlay, "dark")
        theirs = app_palette(app, "dark")
        assert mine[here] == theirs[there], (
            f"overlay {here} is {mine[here]}, app {there} is {theirs[there]}"
        )

    def test_hovers_have_a_surface_to_land_on(self, overlay):
        """`--sheen: transparent` is why `.icon:hover`, `.chip:hover` and
        `.menu-item:hover` all did nothing."""
        assert overlay_palette(overlay, "dark")["--sheen"] != "transparent"


class TestTheLightPaletteIsTheApps:
    @pytest.mark.parametrize("here,there", [
        ("--panel", "--card"), ("--sheen", "--card2"),
        ("--stroke", "--border"), ("--stroke-hi", "--border-hi"),
        ("--text", "--text"), ("--muted", "--muted"),
    ])
    def test_the_value_matches(self, app, overlay, here, there):
        assert overlay_palette(overlay, "light")[here] == app_palette(app, "light")[there]


class TestEveryAccentAgrees:
    @pytest.mark.parametrize("accent", sorted(MEASURED_ACCENTS))
    @pytest.mark.parametrize("name", ["--accent", "--accent-fill", "--on-accent",
                                      "--accent-soft", "--accent-line"])
    def test_the_value_matches(self, app, overlay, accent, name):
        mine = overlay_palette(overlay, "dark", accent)
        theirs = app_palette(app, "dark", accent)
        assert mine[name] == theirs[name], f"{accent} {name}"

    def test_ember_carries_the_contrast_fix(self, overlay):
        """Named explicitly because the failure is invisible: the surface looks
        fine and the text on it is a little too dim to read."""
        assert overlay_palette(overlay, "dark", "ember")["--accent-fill"] != "#df3d17"


def rule(source, selector):
    """The declarations of one rule, by exact selector."""
    for selectors, body in BLOCK.findall(stylesheet(source)):
        if [part.strip() for part in selectors.split(",")] == [selector]:
            return {name.strip(): value.strip() for name, value in
                    (d.split(":", 1) for d in body.split(";") if ":" in d)}
    raise AssertionError("no rule for " + selector)


class TestItIsTheAppsCommandBar:
    """Matching colours is not matching a design language, and this file did
    the first and not the second: the same orange on a panel that was a single
    wrapping row with a logo tile at the left end and a bordered `Enter` key
    cap at the right. The app's composer is none of those things, and its own
    stylesheet is explicit about the part that matters —

        Two rows, always: the question, then the controls. Not a flex row that
        wraps under pressure — the rows are the design, so there is no width at
        which the layout is a compromise.

    — so the panel is now that: question, then controls, with the app's send
    button on the end of them."""

    def test_the_shell_is_the_command_bar(self, app, overlay):
        theirs = rule(app, "#cmdbar")
        mine = rule(overlay, ".panel")
        assert mine["flex-direction"] == theirs["flex-direction"] == "column"
        assert mine["gap"] == theirs["gap"]
        assert mine["padding"] == theirs["padding"]
        assert mine["border-radius"] == theirs["border-radius"] == "var(--r-lg)"

    def test_the_surface_is_the_floating_one(self, app, overlay):
        """`--surface-pop`, not `--card`. A panel that floats over the page is
        drawn differently from one sitting in it, and this floats over the
        whole desktop."""
        assert app_palette(app, "dark")["--surface-pop"] == overlay_palette(overlay, "dark")["--pop"]
        assert (app_palette(app, "dark")["--surface-pop-edge"]
                == overlay_palette(overlay, "dark")["--pop-edge"])

    def test_the_send_button_is_the_apps(self, app, overlay):
        theirs = rule(app, "#send-btn")
        mine = rule(overlay, ".send")
        for name in ("width", "height", "border-radius"):
            assert mine[name] == theirs[name], name
        assert mine["background"] == theirs["background"] == "var(--accent)"

    def test_there_is_no_key_cap_where_the_button_goes(self, overlay):
        """A bordered `Enter` chip beside a send button is the same instruction
        twice, and the app shows neither."""
        assert "hint.textContent = 'Enter'" not in overlay
        hint = rule(overlay, ".hint")
        assert "border" not in hint

    def test_the_button_actually_sends(self, overlay):
        """It replaced the only thing that said how to submit, so a decorative
        one would leave the panel with no visible way to ask anything."""
        assert "getElementById('send').addEventListener('click'" in overlay

    def test_the_input_has_no_leading_tile(self, overlay):
        """A logo inside the input is a search box from another decade, and the
        app's composer has no leading mark at all."""
        assert 'class="mark"' not in overlay
        assert ".mark {" not in overlay

    def test_the_icon_buttons_are_the_apps_size(self, app, overlay):
        assert rule(app, ".icon-btn")["width"] == rule(overlay, ".icon")["width"] == "32px"


class TestItUsesAScaleRatherThanAHalfPixelRack:
    def test_no_half_pixel_sizes_remain(self, overlay):
        assert not re.findall(r"font-size:\s*[0-9]+\.5px", overlay)

    @pytest.mark.parametrize("token", ["--text-2xs", "--text-xs", "--text-sm",
                                       "--text-base", "--text-md"])
    def test_the_scale_matches_the_app(self, app, overlay, token):
        assert overlay_palette(overlay, "dark")[token] == app_palette(app, "dark")[token]

    @pytest.mark.parametrize("token", ["--r-xs", "--r-sm", "--r-lg", "--r-xl"])
    def test_the_radii_match_the_app(self, app, overlay, token):
        assert overlay_palette(overlay, "dark")[token] == app_palette(app, "dark")[token]

    def test_the_type_is_set_from_the_scale(self, overlay):
        """A token nothing uses is a token that will be wrong next time."""
        assert overlay.count("font-size: var(--text-") >= 8
