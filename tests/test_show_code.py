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
