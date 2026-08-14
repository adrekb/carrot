"""Animation — explaining something by showing it move.

Some things are diagrams and some things are arguments. "Here is the proof
that a² + b² = c²" is an argument: squares are rearranged, and the
rearrangement *is* the proof. A still of the final frame is the conclusion
with the reasoning deleted.

Manim renders those. This pack is the wiring: a capability that is probed
rather than assumed, a tool that writes a scene, renders it, and hands back
the file, and a video artifact so the result appears in the conversation
instead of as a path the user has to go and find.

Two things it deliberately does not do.

It does not run arbitrary Python quietly. A manim scene is a Python file that
this tool executes, so the tool is high risk and goes through the same
approval gate as `run_command` — the same gate, worded to say what is about
to run. Rendering an animation is a friendly-sounding way to spell "execute
this code", and the prompt should not hide that.

And it does not pretend manim is installed when it is not. The engine is
several hundred megabytes with an ffmpeg dependency; most people running a
local assistant will not have it. The capability probe answers that question
on this machine, the Extensions card says so plainly, and the tool refuses up
front with the install command rather than failing four minutes into a render.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from typing import Any, Dict, List

from ... import extensions

# Manim's own CLI, plus the module form. The module form is what a pip install
# into a virtualenv leaves behind when the Scripts directory is not on PATH,
# which on Windows is most of the time.
MANIM_BINARIES = ["manim", "manimce"]

# Quality flags, smallest first. Default is the low one: an animation shown
# inline in a conversation is watched at a few hundred pixels, and 1080p60 of
# the same scene is twenty times the bytes for a picture nobody enlarges.
QUALITIES = {"low": "-ql", "medium": "-qm", "high": "-qh"}

# A render is the slowest thing in this app by a wide margin, and a scene with
# a mistake in it can loop. Four minutes is generous for anything worth
# watching inline and short enough that a mistake is not a lost afternoon.
RENDER_TIMEOUT = 240

_SCENE_NAME = re.compile(r"^class\s+([A-Za-z_]\w*)\s*\(\s*\w*Scene\w*\s*\)", re.M)


def _manim_command() -> List[str]:
    """How to invoke manim on this machine, or an empty list.

    The console script is preferred when it is on PATH; otherwise the module
    is run through the interpreter that is already running, which is the one
    that has manim installed if anything does.
    """
    import sys

    for binary in MANIM_BINARIES:
        found = shutil.which(binary)
        if found:
            return [found]
    try:
        import manim  # noqa: F401

        return [sys.executable, "-m", "manim"]
    except Exception:
        return []


def scene_name(source: str) -> str:
    """The first Scene subclass in the file — what manim is asked to render.

    Read out of the source rather than required as an argument: the model has
    just written the class, and asking it to also name the thing it wrote is a
    second chance to get it wrong.
    """
    match = _SCENE_NAME.search(source or "")
    return match.group(1) if match else ""


def _tool_render_animation(source: str = "", title: str = "",
                           quality: str = "low", emit=None, **_) -> str:
    """Render a manim scene and show the video in the conversation."""
    from ... import artifacts

    command = _manim_command()
    if not command:
        return ("error: manim is not installed on this machine. "
                "`pip install manim` (it needs ffmpeg as well), then try again. "
                "Everything else in this pack works without it.")

    scene = scene_name(source)
    if not scene:
        return ("error: no Scene class found. The file must define one, e.g. "
                "`class Proof(Scene):` with a `construct(self)` method.")

    workdir = tempfile.mkdtemp(prefix="carrot-manim-")
    script = os.path.join(workdir, "scene.py")
    with open(script, "w", encoding="utf-8") as handle:
        handle.write(source)

    # imageio-ffmpeg ships a binary; manim looks for `ffmpeg` on PATH. Pointing
    # at the bundled one means a pip install of this pack is enough on a
    # machine that has no system ffmpeg, which is most Windows machines.
    env = dict(os.environ)
    try:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        env["PATH"] = os.path.dirname(ffmpeg) + os.pathsep + env.get("PATH", "")
        env.setdefault("FFMPEG_BINARY", ffmpeg)
    except Exception:
        pass

    flag = QUALITIES.get(str(quality).lower(), QUALITIES["low"])
    started = time.time()
    try:
        result = subprocess.run(
            [*command, flag, "--media_dir", workdir, "--format", "mp4",
             script, scene],
            capture_output=True, text=True, timeout=RENDER_TIMEOUT,
            cwd=workdir, env=env,
        )
    except subprocess.TimeoutExpired:
        return (f"error: the render was still going after {RENDER_TIMEOUT}s and was "
                "stopped. Shorten the scene or lower the quality.")
    except Exception as exc:
        return f"error: could not run manim: {exc}"

    if result.returncode != 0:
        output = (result.stderr or result.stdout or "")
        # The one failure worth translating. MathTex and Tex typeset through a
        # real LaTeX installation, which manim shells out to — and on a machine
        # without one the error is `FileNotFoundError: [WinError 2]` from deep
        # inside subprocess, naming no file and mentioning neither LaTeX nor
        # the class that needed it. It is the most common way a first scene
        # fails and the least legible.
        if _looks_like_missing_latex(output):
            return ("error: this scene uses MathTex or Tex, which typeset through a "
                    "real LaTeX installation, and there is not one on this machine. "
                    "Either install TeX Live or MiKTeX, or rewrite the scene using "
                    "Text(\"a^2 + b^2 = c^2\") — Text renders through Pango and needs "
                    "no LaTeX. Everything else in manim works without it.")
        # Otherwise the traceback, trimmed to its end: manim's stack traces are
        # long and the line that says what is wrong is the last one.
        tail = output.strip().splitlines()[-12:]
        return "error: the scene did not render.\n" + "\n".join(tail)

    video = _find_video(workdir)
    if not video:
        return "error: manim finished but produced no video file."

    try:
        artifact = artifacts.create(
            kind=artifacts.KIND_VIDEO, content="", path=_relative_to_workspace(video),
            title=title or scene,
        )
    except Exception as exc:
        return (f"the scene rendered to {video}, but it could not be shown inline: {exc}")

    if emit:
        emit({"artifact": artifact})
    # The marker is the part that actually puts it on screen. Chat scans a
    # tool's *result text* for `[[carrot:artifact:id]]` and swaps the line for
    # the rendered thing — the event above is a convenience for panels that
    # listen, and returning only that would have rendered the video nowhere
    # while the tool cheerfully reported success. The prose after it is what
    # the model reads back in its own history.
    return (f"[[carrot:artifact:{artifact['id']}]] rendered {scene} in "
            f"{time.time() - started:.0f}s and showed the animation in the chat")


def _looks_like_missing_latex(output: str) -> bool:
    """Whether a failed render is the no-LaTeX one.

    Matched on the pair rather than on either half: a FileNotFoundError alone
    could be a missing asset the scene asked for, and a mention of Tex alone
    appears in tracebacks that rendered fine.
    """
    text = output or ""
    spawn_failed = ("FileNotFoundError" in text or "WinError 2" in text
                    or "No such file or directory" in text)
    typesetting = any(marker in text for marker in
                      ("MathTex", "Tex(", "latex", "dvisvgm", "tex_file_writing"))
    return spawn_failed and typesetting


def _find_video(root: str) -> str:
    newest = ""
    stamp = 0.0
    for base, _dirs, files in os.walk(root):
        for name in files:
            if not name.lower().endswith(".mp4"):
                continue
            full = os.path.join(base, name)
            when = os.path.getmtime(full)
            if when > stamp:
                newest, stamp = full, when
    return newest


def _relative_to_workspace(video: str) -> str:
    """Copy the render into the workspace and return its relative path.

    Artifacts read files through the workspace sandbox, which is the check
    that stops an artifact naming a file it has no business reading. A temp
    directory is outside it, so the video is brought inside rather than the
    sandbox being widened for this one caller.
    """
    from ...agent_tools import workspace_root

    target_dir = os.path.join(workspace_root(), "animations")
    os.makedirs(target_dir, exist_ok=True)
    name = f"{int(time.time())}-{os.path.basename(video)}"
    target = os.path.join(target_dir, name)
    shutil.copy2(video, target)
    return os.path.join("animations", name)


TOOLS: Dict[str, Dict[str, Any]] = {
    "render_animation": {
        "handler": _tool_render_animation,
        "mutating": True,
        "risk": "high",
        "wants_emit": True,
        "requires": "manim",
        "description": (
            "Render a manim scene and show the animation in the conversation. Write a "
            "complete Python file defining one Scene subclass with a construct method; "
            "it is executed. Use it when the explanation is a thing that moves — a "
            "proof by rearrangement, a limit being approached, a transformation — and "
            "not for a static chart, which is a matplotlib PNG and much faster."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "The complete scene file"},
                "title": {"type": "string", "description": "A short caption"},
                "quality": {"type": "string", "enum": ["low", "medium", "high"],
                            "description": "low is right for something watched inline"},
            },
            "required": ["source"],
        },
    },
}


SKILLS = [
    {
        "slug": "manim-scenes",
        "name": "Writing manim scenes",
        "description": "How to write a scene that renders first time",
        "instructions": (
            "Write one file defining a single Scene subclass with a construct "
            "method, and nothing else at module level that runs.\n\n"
            "Import from manim directly: `from manim import *`. Do not import "
            "manimlib or manimgl — those are a different library with the same "
            "shape, and their names fail at import.\n\n"
            "Keep it short. Every second of animation is seconds of rendering, "
            "and something watched inline in a conversation wants to be ten to "
            "thirty seconds, not two minutes.\n\n"
            "Use Text for labels, not MathTex or Tex, unless you know this "
            "machine has a LaTeX installation. MathTex typesets by shelling out "
            "to real LaTeX, and without one the render dies with a "
            "FileNotFoundError that names neither LaTeX nor the class that "
            "wanted it. Text goes through Pango and always works, and "
            "Text(\"a² + b² = c²\") reads the same as the typeset version at "
            "the size anybody watches this.\n\n"
            "Prefer Create, Transform, FadeIn and Write over anything exotic: "
            "they exist in every recent version. A scene that fails to render "
            "teaches the reader nothing, and the failure will be an "
            "AttributeError on a class that was renamed.\n\n"
            "Label what is on screen. An animation of unlabelled shapes moving "
            "is a screensaver — the point is that the viewer can follow the "
            "argument, so put the algebra next to the geometry."
        ),
    },
]


PACK = extensions.register(extensions.Pack(
    pack_id="animation",
    name="Animation",
    description=("Render manim animations and show them in the conversation. For "
                 "explanations that are arguments rather than diagrams — a proof by "
                 "rearrangement is the rearrangement, and a still of the last frame "
                 "is the conclusion with the reasoning deleted."),
    version="1.0",
    tools=TOOLS,
    skills=SKILLS,
    capabilities=[
        {
            "id": "manim",
            "label": "Manim",
            "binaries": MANIM_BINARIES,
            # A `check` rather than the default PATH lookup, because the usual
            # way manim arrives is `pip install` into a virtualenv — which
            # puts manim.exe in Scripts/, a directory that is not on PATH
            # unless the environment is activated. `which` therefore says
            # "not installed" about an installation that works perfectly, and
            # the card would tell the user to install what they already have.
            "check": lambda: bool(_manim_command()),
            "purpose": "Rendering animations. Nothing else in this pack needs it.",
            "install_hint": ("pip install manim — it needs ffmpeg too, and is a few "
                             "hundred megabytes with its dependencies."),
        },
    ],
    tutorial=[
        {"step": "Check the engine is there",
         "detail": "The capability line above says whether manim is installed on this "
                   "machine. If it is not, the tool refuses with the install command "
                   "rather than failing part-way through a render."},
        {"step": "Ask for something that moves",
         "detail": "\"Show me a visual proof of the Pythagorean theorem in manim.\" The "
                   "model writes the scene, it is rendered, and the video appears in "
                   "the conversation."},
        {"step": "Expect to approve it",
         "detail": "A scene is a Python file that gets executed, so rendering asks first "
                   "— the same gate as running any other command, worded to say so."},
    ],
))
