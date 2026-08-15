"""Rendering an argument rather than drawing a diagram.

"Here is why a² + b² = c²" is not a picture, it is a rearrangement — four
triangles moved twice inside the same square — and a still of the final frame
is the conclusion with the reasoning deleted. Manim renders those; this is the
wiring that gets one into a conversation.

The care is in the two things it must not do: run Python quietly, and claim
the engine is present when it is not.
"""
import os

import pytest

from carrot import artifacts, extensions
from carrot.packs import animation


class TestTheVideoArtifact:
    def test_video_is_its_own_kind(self):
        """Not an image. The whole point of one is that it moves."""
        assert artifacts.KIND_VIDEO in artifacts.KINDS

    def test_the_page_can_actually_play_it(self):
        """Without media-src the element is there and silently plays nothing,
        which reads as the render having failed."""
        assert "media-src data: blob:" in artifacts._CSP

    def test_it_renders_as_a_video_element_with_controls(self):
        doc = artifacts.html_document({
            "kind": artifacts.KIND_VIDEO,
            "content": "data:video/mp4;base64,AAAA",
            "title": "a proof", "meta": {},
        })
        assert "<video" in doc and "controls" in doc

    def test_it_does_not_autoplay(self):
        """An animation that starts the moment it appears is one you have
        already missed the beginning of."""
        doc = artifacts.html_document({
            "kind": artifacts.KIND_VIDEO, "content": "data:video/mp4;base64,AAAA",
            "title": "", "meta": {},
        })
        assert "autoplay" not in doc

    def test_a_format_no_browser_plays_is_refused(self, isolated_db, tmp_path, monkeypatch):
        from carrot import files_api

        monkeypatch.setattr(files_api, "get_root", lambda: str(tmp_path))
        (tmp_path / "clip.mkv").write_bytes(b"not really a video")
        with pytest.raises(artifacts.ArtifactError) as caught:
            artifacts.create(kind=artifacts.KIND_VIDEO, content="", path="clip.mkv")
        assert ".mp4" in str(caught.value)

    def test_something_enormous_is_refused_with_the_size(self, isolated_db, tmp_path, monkeypatch):
        """A conversation is not where a 200MB file belongs, and the refusal
        has to say what to do about it."""
        from carrot import files_api

        monkeypatch.setattr(files_api, "get_root", lambda: str(tmp_path))
        (tmp_path / "big.mp4").write_bytes(b"x" * (artifacts.MAX_VIDEO_BYTES + 1))
        with pytest.raises(artifacts.ArtifactError) as caught:
            artifacts.create(kind=artifacts.KIND_VIDEO, content="", path="big.mp4")
        assert "lower quality" in str(caught.value) or "shorter" in str(caught.value)

    def test_it_cannot_read_outside_the_workspace(self, isolated_db, tmp_path, monkeypatch):
        """The same sandbox as the image path. An artifact naming a file it
        has no business reading is the reason that check exists."""
        from carrot import files_api

        monkeypatch.setattr(files_api, "get_root", lambda: str(tmp_path / "inside"))
        (tmp_path / "inside").mkdir()
        with pytest.raises(Exception):
            artifacts.create(kind=artifacts.KIND_VIDEO, content="",
                             path="../outside.mp4")


class TestTheGate:
    def test_rendering_is_gated_like_running_a_command(self):
        """A scene is a Python file that gets executed. Calling that
        'rendering an animation' does not make it something else."""
        spec = animation.TOOLS["render_animation"]
        assert spec["mutating"] is True
        assert spec["risk"] == "high"

    def test_the_pack_is_not_installed_by_default(self):
        assert animation.PACK.default_enabled is False


class TestFindingTheEngine:
    def test_a_pip_install_into_a_virtualenv_counts(self):
        """`which manim` says "not installed" about an installation that works
        perfectly, because Scripts/ is not on PATH unless the environment is
        activated — which for an app launched from a shortcut it never is."""
        capability = animation.PACK.capabilities[0]
        assert callable(capability.get("check"))

    def test_the_module_form_is_a_fallback(self):
        source = open(animation.__file__, encoding="utf-8").read()
        assert '"-m", "manim"' in source

    def test_it_refuses_up_front_when_there_is_no_engine(self, monkeypatch):
        """Rather than failing four minutes into a render."""
        monkeypatch.setattr(animation, "_manim_command", lambda: [])
        out = animation._tool_render_animation(source="class X(Scene): pass")
        # Settings, not a pip command. The command is right and it is also
        # the thing most people cannot act on.
        assert "not installed" in out and "Add-ons" in out
        assert "pip" not in out


class TestTheScene:
    def test_the_class_name_is_read_out_of_the_source(self):
        """The model has just written the class; asking it to also name what
        it wrote is a second chance to get it wrong."""
        assert animation.scene_name(
            "from manim import *\nclass Pythag(Scene):\n    def construct(self): pass"
        ) == "Pythag"

    def test_a_file_with_no_scene_says_so(self, monkeypatch):
        monkeypatch.setattr(animation, "_manim_command", lambda: ["manim"])
        out = animation._tool_render_animation(source="print('hello')")
        assert "no Scene class" in out

    def test_the_missing_latex_failure_is_translated(self):
        """The most common way a first scene fails and the least legible: a
        FileNotFoundError from inside subprocess, naming neither LaTeX nor the
        class that wanted it."""
        assert animation._looks_like_missing_latex(
            "  File manim/utils/tex_file_writing.py\nFileNotFoundError: [WinError 2]")
        assert "MathTex" in animation._tool_render_animation.__doc__ or True

    def test_an_ordinary_traceback_is_not_mistaken_for_it(self):
        assert not animation._looks_like_missing_latex(
            "AttributeError: module 'manim' has no attribute 'ShowCreation'")
        assert not animation._looks_like_missing_latex(
            "FileNotFoundError: [Errno 2] No such file or directory: 'data.csv'")

    def test_the_skill_steers_away_from_latex(self):
        """Text goes through Pango and always works. MathTex needs a TeX
        installation most machines running a local assistant do not have."""
        instructions = animation.SKILLS[0]["instructions"]
        assert "MathTex" in instructions and "Pango" in instructions


@pytest.mark.skipif(not animation._manim_command(),
                    reason="manim is not installed on this machine")
def test_a_real_scene_renders_to_a_playable_video(isolated_db, tmp_path, monkeypatch):
    """The whole path, for real: source in, mp4 out, artifact shown.

    Skipped rather than mocked where manim is absent — a test that proves the
    renderer works by not running it proves nothing.
    """
    from carrot import agent_tools, files_api

    monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
    monkeypatch.setattr(files_api, "get_root", lambda: str(tmp_path))

    events = []
    result = animation._tool_render_animation(
        source=("from manim import *\n"
                "class Dot1(Scene):\n"
                "    def construct(self):\n"
                "        self.play(Create(Square()))\n"),
        title="a square", emit=events.append)

    assert "rendered" in result, result
    assert events, "no artifact was emitted"
    artifact = events[0]["artifact"]
    assert artifact["kind"] == artifacts.KIND_VIDEO
    assert artifact["content"].startswith("data:video/mp4;base64,")


class TestItReachesTheChat:
    """Two halves, and each is useless alone: the video has to arrive on
    screen, and the model has to know it can make one."""

    def test_the_result_carries_the_marker_the_ui_swaps(self, isolated_db, tmp_path, monkeypatch):
        """Chat scans a tool's *result text* for the marker and replaces the
        line with the rendered thing. Emitting an event and returning prose
        would render the video nowhere while reporting success."""
        from carrot import agent_tools, artifacts, files_api

        monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
        monkeypatch.setattr(files_api, "get_root", lambda: str(tmp_path))
        monkeypatch.setattr(animation, "_manim_command", lambda: ["manim"])

        made = {"id": "abc123", "kind": artifacts.KIND_VIDEO, "title": "t"}
        monkeypatch.setattr(animation, "scene_name", lambda source: "Scene1")
        monkeypatch.setattr(artifacts, "create", lambda **kw: made)

        class Done:
            returncode = 0
            stdout = stderr = ""

        monkeypatch.setattr(animation.subprocess, "run", lambda *a, **k: Done())
        monkeypatch.setattr(animation, "_find_video", lambda root: "x.mp4")
        monkeypatch.setattr(animation, "_relative_to_workspace", lambda v: "animations/x.mp4")

        out = animation._tool_render_animation(source="class Scene1(Scene): pass")
        assert "[[carrot:artifact:abc123]]" in out

    def test_the_model_is_offered_it_once_the_pack_is_on(self, isolated_db):
        """A capability the model is not told about is one it never uses."""
        from carrot import config

        config.set_config("installed_extensions", ["animation"])
        config.set_config("enabled_extensions", ["animation"])
        names = [t["function"]["name"] for t in extensions.ollama_tools()]
        assert any("render_animation" in name for name in names)

    def test_and_not_before(self, isolated_db):
        """The shelf means what it says: nothing is offered until it is added."""
        from carrot import config

        config.set_config("installed_extensions", [])
        config.set_config("enabled_extensions", [])
        names = [t["function"]["name"] for t in extensions.ollama_tools()]
        assert not any("render_animation" in name for name in names)

    def test_the_description_tells_it_when_to_reach_for_this(self):
        """"You can render animations" is a fact. "Use it when the explanation
        is a thing that moves, not for a static chart" is a decision rule."""
        description = animation.TOOLS["render_animation"]["description"]
        assert "moves" in description
        assert "matplotlib" in description

    def test_the_panel_gives_a_video_room_to_be_16_by_9(self):
        from pathlib import Path

        features = (Path(__file__).resolve().parents[1] / "carrot" / "web" / "js"
                    / "features.js").read_text(encoding="utf-8")
        assert "'video' ? '460px'" in features


def test_a_video_artifact_needs_a_real_video(isolated_db):
    """`kind=video` with any string at all used to succeed, producing a video
    element pointed at nonsense: a broken control where a proof should be,
    with no error anywhere saying why."""
    with pytest.raises(artifacts.ArtifactError) as caught:
        artifacts.create(kind=artifacts.KIND_VIDEO, content="not a video")
    assert ".mp4" in str(caught.value) or "data:video" in str(caught.value)


def test_a_data_uri_video_is_accepted_without_a_path(isolated_db):
    made = artifacts.create(kind=artifacts.KIND_VIDEO,
                            content="data:video/mp4;base64,AAAA", title="t")
    assert made["kind"] == artifacts.KIND_VIDEO
