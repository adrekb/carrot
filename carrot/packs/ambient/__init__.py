"""Ambient Recall — the screen, remembered as text.

Reading what is on your screen every few seconds is the most invasive thing
this app is capable of, so it is the pack most obviously worth being a pack:
off until you add it, and gone the moment you switch it off.

What it does: takes a frame, reads the text on it, keeps the text, and throws
the picture away. You can then ask what you were reading about last Tuesday and
get an answer, without a single byte having left the machine.

What it does not do, and this is the design rather than a limitation: it does
not keep the screenshots. Tools in this space usually record video and index it
afterwards, which gives you a searchable film of your own life sitting in your
home directory — readable by anything that can read your files, surviving the
disk encryption you did not switch on, and worth a great deal to anyone who
takes it. Text answers the same question, costs a few hundred bytes a frame,
and is worth almost nothing to a thief. The cost is that you can search what
the screen *said* and not what it *showed*: an unlabelled chart is invisible to
this, and that is the trade.

The exclusions were written before the capture was. See `carrot/ambient.py` —
private browsing windows, password fields, credential managers and anything you
add are refused by a gate that runs first and cannot be bypassed by the capture
path, because there is only one capture path and it asks.
"""

from __future__ import annotations

import platform

from ... import extensions


def _capabilities():
    """Probed at render time, so the card describes this machine.

    Written as a function rather than a constant because the answer changes
    when somebody installs the thing it asked for, and a card that still says
    "missing" after a successful pip install is a card nobody believes again.
    """
    windows = platform.system() == "Windows"
    return [
        {
            "id": "screen_grabber",
            "name": "A way to take a screenshot",
            "why": "Without one there is nothing to read.",
            "check": lambda: bool(_capture().available_grabber()),
            "install": "pip install mss",
        },
        {
            "id": "ocr",
            "name": ("Windows.Media.Ocr" if windows else "Tesseract OCR"),
            "why": (
                "Built into Windows: no download, no model, and it runs on the "
                "OS accessibility path rather than the GPU — so it does not "
                "compete with Ollama for the VRAM your answers need."
                if windows else
                "Reads the text off a frame. Carrot never sends a screenshot "
                "anywhere, so the OCR has to happen on this machine."
            ),
            "check": lambda: bool(_capture().available_ocr()),
            "install": ("pip install winsdk" if windows
                        else "install Tesseract, then: pip install pytesseract"),
        },
    ]


def _capture():
    from ... import ambient_capture

    return ambient_capture


PACK = extensions.Pack(
    pack_id="ambient",
    name="Ambient Recall",
    description=(
        "Remembers what was on your screen, as text — never as images. Ask "
        "what you were reading last Tuesday and get the answer. Private "
        "browsing, password fields and credential managers are refused before "
        "anything is captured, and nothing leaves this machine."
    ),
    version="1.0",
    tabs=["ambient"],
    capabilities=_capabilities(),
    tutorial=[
        {"title": "Check what this machine needs",
         "body": "The list above is probed, not assumed. On Windows the OCR is "
                 "already in the operating system and only needs its Python "
                 "binding; elsewhere it is Tesseract. Nothing starts until both "
                 "a screen grabber and an OCR engine are present, and the "
                 "start button will tell you which is missing rather than "
                 "failing quietly."},
        {"title": "Read the exclusions before you start",
         "body": "On the Ambient tab. Private browsing windows, password "
                 "fields, credential managers and a list of sensitive window "
                 "titles are all refused by default — you do not have to think "
                 "of incognito, because the whole point of incognito is that "
                 "you already said what you wanted. Add your own by app name, "
                 "window title or domain."},
        {"title": "Try one capture by hand",
         "body": "Press Capture now. It shows you exactly what was stored — "
                 "the app, the window title and the text — so you can see what "
                 "this feature keeps before you leave it running. If the "
                 "window is one it will not touch, it tells you which rule "
                 "stopped it."},
        {"title": "Start it, and let it pace itself",
         "body": "Capture yields to a model that is generating, slows down on "
                 "battery, stops below your battery floor and skips while you "
                 "are away from the machine. When it is not capturing it says "
                 "why, because a feature that silently stops looks exactly "
                 "like one that is broken."},
        {"title": "Ask it something",
         "body": "Use Recall on the Ambient tab: 'that pension article', 'the "
                 "error about a missing certificate'. Search is over the words "
                 "that were on screen, reranked by meaning — the same hybrid "
                 "search the rest of Carrot uses."},
        {"title": "Know how to forget",
         "body": "Any single moment, an app, a time range, or everything, from "
                 "the same tab. This is as prominent as the start button on "
                 "purpose: something that records and is hard to erase is not a "
                 "record you control."},
    ],
    settings=[
        {"key": "retention_days", "label": "Keep frames for", "type": "text",
         "default": "30",
         "help": "Days. Anything older is deleted. Text is small — a month is "
                 "typically a few megabytes — but a record with no end date is "
                 "a record nobody decided to keep."},
    ],
    default_enabled=False,
)

extensions.register(PACK)
