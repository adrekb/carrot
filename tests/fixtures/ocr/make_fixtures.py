"""Render the OCR fixtures, so the ground truth is known rather than transcribed.

Run this to regenerate:  python tests/fixtures/ocr/make_fixtures.py

Why rendered and not screenshotted
----------------------------------
A real screenshot needs its text typed out by hand to be a fixture, and a
hand transcription is itself a guess — you cannot tell a bad OCR from a typo in
the answer key. Rendering means the expected text is the input, exactly.

What this therefore does and does not prove
-------------------------------------------
It is a floor, not a benchmark. Rendered text is cleaner than a real window:
no subpixel smoothing from a different display scale, no JPEG-ish artefacts, no
overlapping chrome. An engine that fails these is broken; an engine that passes
them is not thereby proven good on a real screen.

What it catches is what actually goes wrong in the field: no engine installed,
no language pack, the async-in-a-running-loop failure that silently returned
nothing, a rewrite that drops punctuation or line order, and a Tesseract
fallback quietly replacing a much better native engine.

The cases are chosen to be the shapes a screen actually holds — a body of
prose, a UI with short labels, a code listing full of punctuation, dark mode,
and small text — rather than a paragraph of Lorem Ipsum five times.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
FONTS = Path("C:/Windows/Fonts")

# (name, font file, size, foreground, background, lines)
CASES = [
    (
        "prose_light",
        "segoeui.ttf", 22, (24, 24, 27), (255, 255, 255),
        [
            "The pelican is a genus of large water birds.",
            "They are characterised by a long beak and a large",
            "throat pouch used for catching prey and draining",
            "water from the scooped-up contents before",
            "swallowing.",
        ],
    ),
    (
        "prose_dark",
        "segoeui.ttf", 22, (233, 233, 235), (24, 24, 27),
        [
            "Dark mode is the same text on the other polarity.",
            "An engine that reads one and not the other will",
            "quietly index half of somebody's day.",
        ],
    ),
    (
        "ui_labels",
        "segoeui.ttf", 16, (32, 32, 36), (243, 243, 245),
        [
            "File  Edit  View  Insert  Format  Slide  Arrange",
            "Untitled presentation",
            "Click to add title",
            "Click to add subtitle",
            "Share      Present      Templates",
        ],
    ),
    (
        "code_punctuation",
        "consola.ttf", 18, (30, 30, 34), (250, 250, 252),
        [
            "def search_for_agent(query, limit=6):",
            '    if not agent_may_search():',
            '        return {"state": "off", "episodes": []}',
            "    rows = recall(query, limit=max(limit * 6, 40))",
            "    return {'state': 'ok'}",
        ],
    ),
    (
        "small_text",
        "arial.ttf", 13, (60, 60, 66), (255, 255, 255),
        [
            "Screenshots are never written to disk, only the text on them.",
            "Last indexed 16 August 2026 at 23:32. 1,911 characters read.",
        ],
    ),
]


def render(name, font_file, size, fg, bg, lines):
    font = ImageFont.truetype(str(FONTS / font_file), size)
    pad, leading = 28, int(size * 1.7)
    width = max(int(font.getlength(line)) for line in lines) + pad * 2
    height = leading * len(lines) + pad * 2
    image = Image.new("RGB", (width, height), bg)
    draw = ImageDraw.Draw(image)
    for index, line in enumerate(lines):
        draw.text((pad, pad + index * leading), line, font=font, fill=fg)
    image.save(HERE / f"{name}.png")
    return {"name": name, "file": f"{name}.png", "text": "\n".join(lines)}


def main():
    if not FONTS.exists():
        raise SystemExit("this generator needs the Windows font directory")
    truth = [render(*case) for case in CASES]
    (HERE / "expected.json").write_text(
        json.dumps(truth, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {len(truth)} fixtures to {HERE}")


if __name__ == "__main__":
    main()
