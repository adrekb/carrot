r"""Maths in chat, Research and the Code tab.

KaTeX's fonts have shipped since the notes editor landed — Milkdown pulls the
library in and esbuild emitted every glyph as a side effect — and the library
itself was reachable from exactly that one panel. Everywhere else goes through
`mdToHtml`, which is `marked` with no maths extension, so a model answering
with $\nabla \cdot B = 0$ printed the dollars and the backslashes.

Models write LaTeX constantly: any physics question, anything from the Academia
pack, most of a maths answer.
"""
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "carrot" / "web"


def read(*parts):
    return WEB.joinpath(*parts).read_text(encoding="utf-8")


class TestTheLibraryIsThere:
    def test_katex_is_vendored_not_fetched(self):
        """A local-first assistant that phones a CDN for its maths renderer is
        not local-first, and would show nothing offline."""
        assert (WEB / "vendor" / "katex.js").exists()
        assert (WEB / "vendor" / "katex.css").exists()

    def test_it_is_bundled_from_the_same_copy_the_editor_uses(self):
        """Two KaTeX versions would render the same expression differently in
        a note and in a chat reply."""
        build = (WEB.parents[1] / "webvendor" / "build.mjs").read_text(encoding="utf-8")
        assert "katex-entry.js" in build

    def test_the_page_loads_both(self):
        index = read("index.html")
        assert "/vendor/katex.js" in index
        assert "/vendor/katex.css" in index


class TestTheRenderer:
    def test_maths_is_extracted_before_markdown(self):
        """`marked` treats _ as emphasis and * as a list, so `$a_1 * b_2$`
        would reach KaTeX already mangled — and silently, because what comes
        out is still valid HTML."""
        js = read("js", "features.js")
        body = js[js.index("function mdToHtml"):]
        body = body[:body.index("\n}")]
        assert body.index("extractMath") < body.index("marked.parse")

    def test_maths_is_restored_after_sanitising(self):
        """Otherwise the attribute pass strips KaTeX's own markup."""
        js = read("js", "features.js")
        body = js[js.index("function mdToHtml"):]
        body = body[:body.index("\n}")]
        assert body.index("markCitations") < body.index("restoreMath")

    def test_all_four_delimiters_are_recognised(self):
        js = read("js", "features.js")
        block = js[js.index("const MATH_PATTERNS"):]
        block = block[:block.index("];")]
        assert r"\$\$" in block          # $$...$$
        assert r"\\[" in block          # \[...\]
        assert r"\\(" in block          # \(...\)
        assert block.count("re:") == 4

    def test_prices_are_not_treated_as_maths(self):
        """"$5 and $10" must survive. The guards are no space inside the
        delimiters and no digit after the closing one."""
        js = read("js", "features.js")
        inline = [line for line in js.splitlines() if r"(?<!\s)\$" in line]
        assert inline, "the inline-dollar guard is gone"
        assert r"(?!\d)" in inline[0]

    def test_broken_latex_costs_the_expression_not_the_answer(self):
        js = read("js", "features.js")
        assert "throwOnError: false" in js

    def test_model_authored_latex_cannot_inject_a_link(self):
        r"""`\href` in model-written LaTeX is a link the user did not ask for,
        in a place no sanitiser is looking."""
        js = read("js", "features.js")
        assert "trust: false" in js

    def test_the_placeholder_contains_nothing_markdown_reacts_to(self):
        """An underscore in the token would itself become emphasis."""
        js = read("js", "features.js")
        assert "KTXMATH" in js
        token = "KTXMATH${store.length}KTXEND"
        assert token in js


class TestEverySurfaceGetsIt:
    """One renderer, so this is about who calls it."""

    @pytest.mark.parametrize("path,needle", [
        (("js", "app.js"), "mdToHtml"),        # chat
        (("js", "agents.js"), "mdToHtml"),     # Research
        (("js", "features.js"), "mdToHtml"),   # Code
    ])
    def test_the_surface_renders_through_mdtohtml(self, path, needle):
        assert needle in read(*path)
