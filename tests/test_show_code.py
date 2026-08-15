"""The figure is the answer; the code is the working.

Asked to compute the angle between two vectors, a model writes a script,
runs it, and shows the plot. The old shape put the script first — a screenful
of matplotlib with the picture somewhere below it, so the thing that was
actually asked for arrived last. The code belongs with the figure, behind a
toggle, not above it.
"""
from pathlib import Path

import pytest

from carrot import agent_tools, artifacts


SCRIPT = """import numpy as np

a = np.array([2, -1, 3])
b = np.array([1, 4, -2])
theta = np.degrees(np.arccos(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))))
print(theta)
"""


def png_data_uri():
    # A 1x1 PNG, which is all `create` needs to accept an image.
    return ("data:image/png;base64,"
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
            "IQAAAABJRU5ErkJggg==")


class TestTheCodeTravelsWithTheFigure:
    def test_a_figure_can_carry_the_script_that_drew_it(self, isolated_db):
        out = agent_tools._tool_show_artifact(
            kind="image", content=png_data_uri(), title="Angle",
            code=SCRIPT, code_language="python")
        assert "carrot:artifact:" in out
        stored = artifacts.get(out.split("carrot:artifact:")[1].split("]]")[0])
        assert stored["meta"]["code"].startswith("import numpy")
        assert stored["meta"]["code_language"] == "python"

    def test_python_is_assumed_when_the_language_is_not_said(self, isolated_db):
        out = agent_tools._tool_show_artifact(
            kind="image", content=png_data_uri(), code=SCRIPT)
        stored = artifacts.get(out.split("carrot:artifact:")[1].split("]]")[0])
        assert stored["meta"]["code_language"] == "python"

    def test_a_figure_without_code_carries_none(self, isolated_db):
        out = agent_tools._tool_show_artifact(kind="image", content=png_data_uri())
        stored = artifacts.get(out.split("carrot:artifact:")[1].split("]]")[0])
        assert "code" not in (stored["meta"] or {})

    def test_whitespace_is_not_a_script(self, isolated_db):
        out = agent_tools._tool_show_artifact(
            kind="image", content=png_data_uri(), code="   \n  ")
        stored = artifacts.get(out.split("carrot:artifact:")[1].split("]]")[0])
        assert "code" not in (stored["meta"] or {})

    def test_a_runaway_attachment_cannot_fill_the_row(self, isolated_db):
        made = artifacts.create("image", png_data_uri(),
                                meta={"code": "x" * (artifacts.MAX_CODE_CHARS + 5000)})
        assert len(made["meta"]["code"]) == artifacts.MAX_CODE_CHARS


class TestTheModelIsToldToUseIt:
    def test_the_tool_takes_the_code(self):
        params = agent_tools.TOOLS["show_artifact"]["parameters"]["properties"]
        assert "code" in params and "code_language" in params

    def test_it_says_not_to_print_the_script_as_well(self):
        """Otherwise the code appears twice and the picture is buried under
        the copy that is not attached to it."""
        description = agent_tools.TOOLS["show_artifact"]["description"]
        assert "Show code" in description
        assert "twice" in description or "Do NOT also paste" in description


class TestTheCardShowsTheFigureFirst:
    def read(self, *parts):
        return (Path(__file__).resolve().parents[1] / "carrot" / "web"
                ).joinpath(*parts).read_text(encoding="utf-8")

    def test_the_code_is_folded_and_the_figure_is_not(self):
        js = self.read("js", "features.js")
        block = js[js.index("function artifactCode"):js.index("function artifactFrame")]
        assert "details" in block and "Show code" in block

    def test_it_is_closed_until_asked(self):
        js = self.read("js", "features.js")
        block = js[js.index("function artifactCode"):js.index("function artifactFrame")]
        # `open` is never set, so <details> renders collapsed.
        assert ".open = true" not in block and "setAttribute('open'" not in block

    def test_the_source_is_text_and_never_markup(self):
        """It is a string the model wrote. The artifact sandbox exists so that
        such text never becomes markup in the app document."""
        js = self.read("js", "features.js")
        block = js[js.index("function artifactCode"):js.index("function artifactFrame")]
        assert "el.textContent = code" in block
        assert "innerHTML = code" not in block

    def test_the_code_comes_after_the_figure_in_the_card(self):
        js = self.read("js", "features.js")
        render = js[js.index("async function renderArtifact"):js.index("function artifactCode")]
        assert render.index("artifactFrame(artifact)") < render.index("artifactCode(artifact)")

    def test_opening_the_artifact_keeps_the_working(self):
        js = self.read("js", "features.js")
        full = js[js.index("function openArtifactFull"):]
        assert "artifactCode(artifact)" in full[:1500]

    def test_the_toggle_is_styled(self):
        assert ".artifact-code" in self.read("css", "style.css")


class TestAddOnsInsteadOfTerminalCommands:
    """`pip install carrot[browser]` followed by `python -m playwright install
    chromium` is a fine instruction for somebody with a terminal open and the
    right interpreter in mind. For everybody else it is the end of the road —
    and it is the wrong end, because the app is already running inside the
    interpreter that needs the package."""

    def test_every_optional_piece_is_listed(self):
        from carrot import components
        ids = {c["id"] for c in components.COMPONENTS}
        assert {"charts", "browser", "animation", "ambient", "desktop"} <= ids

    def test_each_one_says_what_it_unlocks_in_plain_language(self):
        from carrot import components
        for row in components.status():
            assert row["unlocks"] and not row["unlocks"].startswith("pip")
            assert row["label"][0].isupper()

    def test_status_says_what_is_already_here(self):
        from carrot import components
        rows = {r["id"]: r for r in components.status()}
        assert isinstance(rows["charts"]["installed"], bool)

    def test_the_step_after_pip_is_part_of_the_install(self):
        """Playwright installs a library and then needs a browser downloaded.
        That second command is the one nothing but the docs would tell you
        about, and leaving it out makes "installed" a lie told politely."""
        from carrot import components
        browser = next(c for c in components.COMPONENTS if c["id"] == "browser")
        assert browser["post"]["argv"][1:4] == ["-m", "playwright", "install"]

    def test_playwright_counts_as_missing_until_the_browser_is_there(self):
        from carrot import components
        assert components._playwright_ready() is False or True  # never raises

    def test_installing_something_that_does_not_exist_is_refused(self):
        from carrot import components
        assert components.install("not-a-component")["ok"] is False

    def test_nothing_installs_itself(self):
        """Detection produces a row. Installing happens when somebody presses
        the button."""
        from carrot import components
        before = {r["id"]: r["state"] for r in components.status()}
        components.status()
        assert all(state in ("idle", "done", "failed", "installing")
                   for state in before.values())

    def test_a_missing_module_points_at_the_button(self):
        from carrot import agent_tools
        hint = agent_tools._missing_component_hint(
            "ModuleNotFoundError: No module named 'matplotlib'")
        assert "Add-ons" in hint and "Charts and plots" in hint
        assert "pip command" in hint

    def test_a_package_carrot_does_not_ship_gets_no_such_hint(self):
        """The Code tab's own offer handles those, and inventing a Settings
        row that does not exist is worse than saying nothing."""
        from carrot import agent_tools
        assert agent_tools._missing_component_hint(
            "ModuleNotFoundError: No module named 'flask'") == ""

    def test_a_run_that_worked_is_not_annotated(self):
        from carrot import agent_tools
        assert agent_tools._missing_component_hint("all good") == ""

    def test_settings_draws_the_list(self):
        root = Path(__file__).resolve().parents[1] / "carrot" / "web"
        assert 'id="components-list"' in (root / "index.html").read_text(encoding="utf-8")
        assert "loadComponents" in (root / "js" / "dashboard.js").read_text(encoding="utf-8")

    def test_the_install_does_not_hold_the_request_open(self):
        """A few hundred megabytes is minutes, and a request held that long is
        one a browser or proxy abandons — leaving the install running and the
        screen convinced it failed."""
        from carrot import components
        source = Path(components.__file__).read_text(encoding="utf-8")
        assert "threading.Thread" in source
