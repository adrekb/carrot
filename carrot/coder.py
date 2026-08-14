"""The coding agent — the parts of Cline, Continue and Goose worth having.

Carrot could already read, write and run files. What it could not do is the
thing that separates a chat window with file access from a coding agent: work
in a disciplined loop that a person can follow, interrupt, and undo. Each of
the three open-source agents solves one piece of that well, and none of them
solves all of it. This module takes the piece each does best.

**Plan then act** (Cline). A coding turn has two phases. In *plan* the agent
may only read — list, grep, open files, ask questions — and its output is a
proposal. Nothing on disk moves until a person says go. In *act* the write
tools unlock. The split is enforced by which tools are offered, not by asking
the model to behave, because a model that can write will eventually write.

**Search/replace edits** (Cline). Rewriting a whole file to change one line
burns tokens proportional to file size and loses everything the model did not
bother to reproduce. An edit is a set of exact-match blocks instead, applied
with a whitespace-tolerant fallback, and it fails loudly rather than guessing
when a block does not match.

**Checkpoints** (Cline). Before the agent acts, the workspace's text files are
snapshotted. Any checkpoint can be restored whole. The existing file journal
already reverses one write at a time; a checkpoint reverses a whole train of
thought, which is what you actually want when an agent goes wrong on step 9.

**Project rules** (all three, incompatibly). Cline reads `.clinerules`,
Continue reads its rules files, Goose reads `.goosehints`, and the industry has
mostly converged on `AGENTS.md`. Carrot reads all of them, so a repo that is
already set up for any of those tools is already set up for Carrot.

**Recipes** (Goose). A task worth doing twice is worth saving: a named prompt
with typed parameters, its mode, and the tools it is allowed. Goose calls these
recipes and they are the most portable idea in the project.
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .config import get_config, set_config
from .database import get_db

# ===== Modes =====

MODE_PLAN = "plan"
MODE_ACT = "act"
MODES = (MODE_PLAN, MODE_ACT)

# Tools that change something. In plan mode none of these are offered at all.
WRITE_TOOLS = frozenset({
    "write_file", "edit_file", "run_command", "create_file", "delete_file",
    "move_file", "git_commit", "git_checkout", "restore_checkpoint",
    # Starting a server is running a command that then keeps running, so if
    # plan mode refuses one it has to refuse the other — otherwise "read-only"
    # is a mode in which the agent can execute anything it can phrase as a
    # server. Stopping is here too: killing the user's dev server is not a
    # read-only act on a machine they are working on.
    "start_server", "stop_server",
    # Writing a skill is writing a file, and one whose contents this agent
    # will obey later. Plan mode proposes; it does not leave standing orders
    # behind for itself.
    "save_skill",
})


def normalize_mode(value: Optional[str]) -> str:
    """Anything unrecognized means plan. The cautious default is the safe one."""
    mode = (value or "").strip().lower()
    return mode if mode in MODES else MODE_PLAN


def tools_for_mode(names, mode: str) -> List[str]:
    """Filter a tool list down to what this mode may call."""
    allowed = list(names)
    if normalize_mode(mode) == MODE_ACT:
        return allowed
    return [n for n in allowed if _bare(n) not in WRITE_TOOLS]


def _bare(name: str) -> str:
    """Strip the ``carrot__`` namespace the chat loop adds when offering tools."""
    return name.split("__", 1)[1] if "__" in name else name


MODE_PREAMBLE = {
    MODE_PLAN: (
        "You are in PLAN mode. You may read the workspace but you cannot change "
        "it — the write tools are not available to you and asking for them will "
        "not produce them.\n\n"
        # It planned a magnetic field simulator straight into a folder holding
        # a Pong game, because it never looked. Everything lands at the top
        # level next to whatever was there before, and the first the user sees
        # of it is an approval prompt for a filename in the wrong place.
        "Look before you plan. Call list_dir first, and read anything that "
        "looks related. Open by saying what is actually there — 'this workspace "
        "holds a Pong game in pong.py and game.py' — so the user can see you "
        "are working from what exists rather than from nothing.\n\n"
        "Then decide whether the request belongs in it. If it is unrelated to "
        "what is already there, do not scatter new files alongside it: say so, "
        "and make 'put it in a new folder' one of your questions, with a "
        "sensible name as the first option. write_file creates missing folders "
        "on the way, so in ACT mode you make one by writing the first file to "
        "'that-folder/main.py'. Do it as part of the work — do not stop and ask "
        "again once it has been agreed.\n\n"
        "Then propose: say which files you would change, what the change is, "
        "and what could go wrong. Ask about anything genuinely ambiguous "
        "instead of guessing. The user switches you to ACT mode when the plan "
        "is right.\n\n"
        "If you need answers before the plan is safe to carry out, you must "
        "ALSO end the reply with a carrot-questions block. Questions written "
        "only as prose cannot be answered — the user gets buttons, and prose "
        "questions produce no buttons, so they are ignored and you will be "
        "made to guess. Copy this shape exactly:\n\n"
        "```carrot-questions\n"
        "[\n"
        '  {"question": "How should the scoreboard look?",\n'
        '   "options": ["Just the numbers", "Labelled for each player"]},\n'
        '  {"question": "When does a game end?",\n'
        '   "options": ["First to 10", "Play until quit"]}\n'
        "]\n"
        "```\n\n"
        "At most four questions, two to four options each, and every option a "
        "concrete choice that stands on its own — \"a running score in the "
        "corner\", never \"option A\". Put the one you would pick first: "
        "skipping the form accepts it. Ask only about things that change what "
        "you would write. Keep the questions in your prose as well, for anyone "
        "reading the plan on its own.\n\n"
        # The block used to arrive under a finished plan built on assumed
        # answers, so the buttons were a vote on a decision already taken.
        # Everything after the block is now discarded before it is shown, so
        # this paragraph describes what happens rather than requesting it.
        "The block ENDS your reply. Nothing you write after it will be shown "
        "to the user — it is cut off at the marker — so do not answer your own "
        "questions, do not say which way you are leaning, and do not carry on "
        "planning underneath them. If a decision genuinely blocks the plan, "
        "ask and stop; the answers come back as the next turn and you write "
        "the plan then. If it does not block anything, do not ask at all: "
        "decide it yourself and say what you decided."
    ),
    MODE_ACT: (
        "You are in ACT mode, and you have the tools to change the workspace: "
        "write_file, edit_file and run_command are available to you right now.\n\n"
        "Use them. Do not print a file into the chat and describe how to save "
        "it — write it. Do not tell the user to run a command — run it and read "
        "the output. Pasting code the user then has to copy is the one thing "
        "ACT mode exists to avoid; if you were going to show a file, call "
        "write_file with that exact content instead, then say what you wrote "
        "and what it does.\n\n"
        "Prefer edit_file with exact search/replace blocks over rewriting whole "
        "files. After a change that should be runnable, run it and read the "
        "output. If you discover the plan was wrong, stop and say so rather "
        "than improvising something the user did not agree to.\n\n"
        "You can search the web. Use it for the two things it is good for: an "
        "API or library you are unsure of, and an error message you did not "
        "expect. Look it up rather than guessing at a signature. Do not go "
        "reading around the subject — one look, then back to the work."
    ),
}

# ===== Clarifying questions as a form =====
#
# A plan that ends "1. minimal or fancy? 2. pygame or tkinter?" is a dead end
# in a panel: the answer has to be typed back as prose, so most people retype
# the whole request instead, or give up and let it guess. The questions are
# lifted out into a form with buttons, which can also be skipped — and skipping
# is not silence, it accepts the model's own first option for each, because
# "just pick something sensible" is what skipping means.
# Three live runs of gemma4:e4b produced three different wrappings, none of
# them the one asked for:
#
#     ```carrot-questions        ```                    `carrot-questions`
#     [ ... ]                    carrot-questions
#     (no closing fence)         [ ... ]                ```json
#                                ```                    [ ... ]
#                                                       ```
#
# The JSON was well-formed every time; only the packaging moved. So nothing
# here matches on fences at all. It finds the marker, then takes the next
# JSON array after it, wherever that is. Matching the decoration was always
# going to be a losing game against a model that decorates differently each
# run, and a form that appears only when the fence is exactly right is a form
# that mostly does not appear.
QUESTIONS_MARKER = re.compile(r"carrot-questions", re.I)

MAX_QUESTIONS = 4
MAX_OPTIONS = 4


def _next_json_array(text: str, start: int):
    """The first balanced ``[...]`` at or after ``start``, and where it ends.

    Bracket counting rather than a regex, because the options are prose and
    contain brackets; and string-aware, so a `"]"` inside an option does not
    close the array early.
    """
    open_at = text.find("[", start)
    if open_at < 0:
        return None, -1
    depth, in_string, escaped = 0, False, False
    for i in range(open_at, len(text)):
        char = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
            if depth == 0:
                return text[open_at:i + 1], i + 1
    # Unterminated: the reply stopped mid-block. Close it and let json decide.
    return text[open_at:] + "]" * depth, len(text)


# A backslash before two or more letters is a LaTeX command, not a JSON escape.
# Asked for a magnetic field simulation, the model offered "Magnetostatics
# ($\nabla \cdot B = 0$)" — where `\c` is invalid JSON and kills the parse, and
# `\n` is *valid* JSON, so a stricter reading silently turns `\nabla` into a
# newline followed by "abla". Both are wrong, and the second is worse for being
# quiet. Physics, maths and Windows paths all put backslashes in prose.
#
# One pass, not two. Escaping LaTeX commands and then invalid escapes in
# sequence re-reads the backslashes the first pass inserted: `\cdot` became
# `\\cdot`, whose second backslash then looked invalid again and turned into
# `\\\cdot`, which parses no better than what it started with.
_STRAY_BACKSLASH = re.compile(r'\\(?:(?=[a-zA-Z]{2,})|(?!["\\/bfnrtu]))')


def _loads_forgiving(body: str):
    """``json.loads``, retried once with stray backslashes escaped."""
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        pass
    try:
        return json.loads(_STRAY_BACKSLASH.sub(r"\\\\", body))
    except (ValueError, TypeError):
        return None


def _one_line(value: Any) -> str:
    """Collapse whitespace: these are button labels, never paragraphs."""
    return " ".join(str(value or "").split())


def parse_questions(text: str) -> List[Dict[str, Any]]:
    """The clarifying questions in a plan, as a form. ``[]`` if there are none.

    Anything malformed returns ``[]`` rather than raising: a model that writes
    a broken block should cost the user a form, not the plan it is attached to.
    """
    marker = QUESTIONS_MARKER.search(text or "")
    if not marker:
        return []
    body, _ = _next_json_array(text, marker.end())
    if not body:
        return []
    raw = _loads_forgiving(body)
    if not isinstance(raw, list):
        return []

    questions = []
    for item in raw[:MAX_QUESTIONS]:
        if not isinstance(item, dict):
            continue
        prompt = _one_line(item.get("question"))
        options = [_one_line(o) for o in (item.get("options") or [])
                   if _one_line(o)]
        # A question with nothing to pick between is prose, not a form field.
        if not prompt or len(options) < 2:
            continue
        questions.append({"question": prompt, "options": options[:MAX_OPTIONS]})
    return questions


# A line that is a question the model is putting to the user, rather than one
# it is about to answer itself. Numbered, bulleted or bold — the three shapes
# "Key Decisions Needed:" is followed by in practice.
_PROSE_QUESTION = re.compile(
    r"^\s*(?:[-*•]|\d+[.)]|\*\*\d+[.)]?\*\*)?\s*(.{8,200}\?)\s*$", re.M)

# Headings that introduce a list of things the model wants decided. Their
# presence is not required — a bare question still counts — but they raise the
# confidence enough to report a single question, which on its own is often
# rhetorical.
_ASKING_HEADING = re.compile(
    r"^\s*#*\s*\**\s*(key\s+)?(decisions?|questions?|clarifications?|choices?)"
    r"\b.{0,40}(needed|required|for you|to make|before)?\s*:?\s*\**\s*$", re.I | re.M)


def prose_questions(text: str) -> List[str]:
    """Questions the model asked in prose when it should have sent a block.

    The reason this exists is a turn that ended "Key Decisions Needed:" and
    then stopped. The plan prompt says plainly that prose questions produce no
    buttons and will be ignored — so the model was ignored, silently, and the
    panel said Done over the top of a model waiting for an answer.

    Deliberately a detector and not a repair. The obvious alternative is a
    second call asking the model to reformat, but the models that miss this
    format are small local ones, and asking the same model to get the same
    format right on a second attempt fails in the same place it just failed.
    A detector cannot make the turn worse, costs nothing, and restores the
    part that was actually lost: knowing it is waiting.
    """
    text = text or ""
    if QUESTIONS_MARKER.search(text):
        return []
    found = [_one_line(m.group(1)) for m in _PROSE_QUESTION.finditer(text)]
    # Its own rhetorical questions are not questions for the user. "What could
    # go wrong?" as a heading over the answer is the common one.
    found = [q for q in found if not q.lower().startswith(("what could go wrong",
                                                           "what next", "why"))]
    if not found:
        return []
    if len(found) == 1 and not _ASKING_HEADING.search(text):
        return []
    return found[:MAX_QUESTIONS]


def strip_questions(text: str) -> str:
    """The plan without the machine-readable block, for display.

    Cuts from the start of the line carrying the marker to the end of the JSON
    array, then eats a trailing fence if the array was inside one. Showing the
    user raw JSON as well as the form is worse than not asking.
    """
    text = text or ""
    marker = QUESTIONS_MARKER.search(text)
    if not marker:
        return text.rstrip()

    body, end = _next_json_array(text, marker.end())
    if not body:
        return text.rstrip()

    # Back up over any fence or backticks opening the block, to the line start.
    start = text.rfind("\n", 0, marker.start()) + 1
    head = text[:start].rstrip()
    while True:
        previous = head.rstrip()
        stripped = previous.rsplit("\n", 1)[-1].strip()
        if stripped.startswith("```") and stripped.strip("`").strip() == "":
            head = previous[:previous.rfind("\n")] if "\n" in previous else ""
            continue
        break

    tail = text[end:]
    tail = re.sub(r"^\s*```[a-zA-Z]*\s*", "", tail)
    return (head.rstrip() + "\n" + tail.lstrip()).strip()


# ===== Asking is a turn-ending act =====
#
# The form worked and the questions still did nothing, because of *when* they
# were read. `parse_questions` ran on the finished reply, which meant the model
# had already written past its own question:
#
#     ...so I'll assume you want the numbers only, and here is the plan.
#     ```carrot-questions
#     [{"question": "How should the scoreboard look?", ...}]
#     ```
#
# Buttons under an answer that was written without them. Answering re-ran a
# turn whose conclusion was already on screen, and the model — now seeing its
# own confident reply in the history — mostly restated it. The user was being
# asked to ratify a guess, and it read as being consulted.
#
# The fix is not a firmer instruction. It is that the reply *stops* at the
# marker: everything after it is discarded before it is ever streamed, so
# asking a question costs the model the ability to answer it. An instruction
# is a request; a cut is a guarantee. This is the same reasoning the search
# gate and the host-concentration check are built on.
#
# What is *not* discarded is the prose before the marker, and where that prose
# sits decides what kind of question this is:
#
#   * Almost nothing before it — the model asked before working. The turn is
#     **blocking**: there is no answer yet and the form is the way forward.
#   * A substantial reply before it — the model answered and then thought of
#     something. Cutting that would throw away work the user can see is good.
#     The answer stands and the form is a **refinement**.
#
# Both cases lose everything after the marker, which is the part where the
# model was talking itself out of having asked.

# Enough prose to be an answer rather than a preamble. Tuned to the shape of
# the failure: "Sure — a couple of things first." is 40 characters and is not
# an answer; anything that has actually said something clears this easily.
BLOCKING_PROSE_CHARS = 240

# How much text has to be held back while streaming so a marker split across
# chunk boundaries is still caught. The marker plus the longest fence that can
# precede it, with room to spare — cheap insurance, and the cost is that the
# last few characters of a reply arrive with the final flush.
_HOLD_BACK = 64


class QuestionGate:
    """Streaming filter that ends a reply at its clarifying questions.

    Fed the model's content chunks, it returns only what is safe to show. Once
    the ``carrot-questions`` marker appears, it emits nothing further: the
    caller sees ``tripped`` go true and stops the turn.

    Holding text back is what makes this work on a stream. The marker can
    straddle a chunk boundary, and a filter that only inspected each chunk in
    isolation would miss ``carrot-que`` + ``stions`` and let the whole
    self-answer through — which is the exact bug, just harder to reproduce.
    """

    def __init__(self):
        self._raw: List[str] = []
        self._emitted = 0
        self.tripped = False

    @property
    def raw(self) -> str:
        return "".join(self._raw)

    def feed(self, text: str) -> str:
        """Accept a chunk; return the part of it that may be shown now."""
        if self.tripped:
            # Still accumulated, so the questions can be parsed out of the full
            # text afterwards — but nothing more reaches the user.
            self._raw.append(text)
            return ""
        self._raw.append(text)
        whole = self.raw

        marker = QUESTIONS_MARKER.search(whole)
        if marker:
            self.tripped = True
            visible = self._cut_at(whole, marker)
            out = visible[self._emitted:]
            self._emitted = len(visible)
            return out

        # No marker yet. Emit everything except a tail that could be the start
        # of one.
        safe_to = max(0, len(whole) - _HOLD_BACK)
        if safe_to <= self._emitted:
            return ""
        out = whole[self._emitted:safe_to]
        self._emitted = safe_to
        return out

    def flush(self) -> str:
        """Whatever was held back, once the stream is over."""
        if self.tripped:
            return ""
        whole = self.raw
        out = whole[self._emitted:]
        self._emitted = len(whole)
        return out

    @staticmethod
    def _cut_at(whole: str, marker) -> str:
        """The prose up to the line the marker sits on, fences included."""
        start = whole.rfind("\n", 0, marker.start()) + 1
        head = whole[:start].rstrip()
        # A fence line opening the block is part of the block, not the prose.
        while True:
            previous = head.rstrip()
            last = previous.rsplit("\n", 1)[-1].strip()
            if last.startswith("```") and last.strip("`").strip() == "":
                head = previous[:previous.rfind("\n")] if "\n" in previous else ""
                continue
            break
        return head.rstrip()

    # --- what the caller does with it ---

    def prose(self) -> str:
        """The reply, with the question block and everything after it removed."""
        if not self.tripped:
            return self.raw.rstrip()
        marker = QUESTIONS_MARKER.search(self.raw)
        return self._cut_at(self.raw, marker) if marker else self.raw.rstrip()

    def questions(self) -> List[Dict[str, Any]]:
        return parse_questions(self.raw)

    def blocking(self) -> bool:
        """Is the form the way forward, or a refinement of an answer already given?

        Judged on how much was said before the question, because that is the
        thing that actually differs. A model that asks first has nothing to
        show yet; a model that asks last has already committed.
        """
        if not self.tripped:
            return False
        return len(self.prose().strip()) < BLOCKING_PROSE_CHARS


def answers_message(pairs: List[Dict[str, str]]) -> str:
    """The follow-up turn a filled-in form becomes."""
    lines = [f"{p.get('question', '').strip()} — {p.get('answer', '').strip()}"
             for p in pairs if p.get("answer")]
    if not lines:
        return ""
    return ("Answers to your questions:\n"
            + "\n".join(f"- {line}" for line in lines)
            + "\n\nGo ahead on that basis.")


# What a turn looks like when the model ignored all of that: a fenced block
# long enough to be a file, and not one call to a write tool.
ACT_NOT_ACTING = (
    "You are in ACT mode and you just printed a file into the chat instead of "
    "writing it. The user cannot run that. Call write_file with the exact "
    "content you produced — pick a sensible path if none was given — and then "
    "say what you wrote. If a tool call failed, say which one and why; do not "
    "silently fall back to pasting."
)
# Long enough that a three-line illustration does not trip it, short enough
# that a real file always does.
ACT_CODE_BLOCK_CHARS = 400


def looks_like_a_pasted_file(text: str) -> bool:
    """Did this answer hand the user a file instead of writing one?

    Deliberately crude: one fenced block over a few hundred characters. The
    cost of a false positive is one extra round; the cost of a false negative
    is the user copying code out of a chat window in a tool whose entire
    purpose is that they should not have to.
    """
    if "```" not in (text or ""):
        return False
    parts = text.split("```")
    # Odd indices are the insides of fences.
    return any(len(parts[i]) >= ACT_CODE_BLOCK_CHARS for i in range(1, len(parts), 2))


# ===== Project rules =====
#
# Order matters: the more Carrot-specific and the more standard files come
# first, so a repo carrying several of them reads sensibly top to bottom.
# Ordered by authority, most authoritative first. A file written *for Carrot*
# outranks one written for another tool, and the local repo's own convention
# outranks a vendor default. This order is the conflict-resolution rule: when
# two files say incompatible things about the same topic, the earlier one wins
# and the later one is dropped rather than both being shipped to confuse the
# model.
RULE_FILES = (
    "CARROT.md",
    ".carrotrules",
    "AGENTS.md",
    "CLAUDE.md",
    ".clinerules",
    ".continuerules",
    ".goosehints",
    ".cursorrules",
    ".github/copilot-instructions.md",
)
RULES_DIRS = (".carrot/rules", ".clinerules", ".continue/rules")
MAX_RULES_CHARS = 24000

# Topics a rule can be *about*. Two rules that hit the same topic with opposite
# instructions cannot both be followed, so the lower-authority one is dropped.
# Deliberately narrow: these are the conventions that actually collide in
# practice, and inventing more would start discarding rules that do not clash.
CONFLICT_TOPICS = (
    ("indent", ("tab", "space", "indent")),
    ("quotes", ("single quote", "double quote")),
    ("semicolons", ("semicolon",)),
    ("test-runner", ("pytest", "unittest", "jest", "vitest", "mocha")),
    ("package-manager", ("npm", "yarn", "pnpm", "bun")),
    ("line-length", ("line length", "columns", "char limit", "characters per line")),
    ("comments", ("comment",)),
    ("commit-style", ("commit message", "conventional commit")),
)


def load_rules(root: str) -> str:
    """Compile every rules file this repo carries into one optimized block.

    Concatenating five vendors' rule files was the obvious first version and
    the wrong one: a small model given eight hundred lines of overlapping
    instruction attends to none of them. So the files are *compiled* —
    deduplicated line by line, conflicting instructions resolved by the
    authority order above, and flattened into a single directive list with no
    per-file headers to spend attention on.

    A repo already configured for Cline, Continue, Goose, Cursor or Copilot
    still needs no extra file to be configured for Carrot.
    """
    return compile_rules(collect_rules(root))


def collect_rules(root: str) -> List[Dict[str, str]]:
    """Read every rules file present, in authority order."""
    if not root or not os.path.isdir(root):
        return []
    found: List[Dict[str, str]] = []
    seen: set = set()

    def take(path: str, label: str) -> None:
        real = os.path.realpath(path)
        if real in seen or not os.path.isfile(real):
            return
        seen.add(real)
        try:
            with open(real, "r", encoding="utf-8", errors="replace") as handle:
                body = handle.read().strip()
        except OSError:
            return
        if body:
            found.append({"source": label, "body": body})

    for name in RULE_FILES:
        take(os.path.join(root, name), name)
    # A rules *directory* is how Cline and Continue handle multiple rule sets.
    for folder in RULES_DIRS:
        full = os.path.join(root, folder)
        if not os.path.isdir(full):
            continue
        for name in sorted(os.listdir(full)):
            if name.endswith((".md", ".txt", ".mdc")):
                take(os.path.join(full, name), f"{folder}/{name}")
    return found


def _directives(body: str) -> List[str]:
    """Split a rules file into individual instructions.

    Headings and horizontal rules are structure, not instruction, and carrying
    them into a flattened list is pure token cost.
    """
    lines = []
    for raw in body.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#") or set(line) <= set("-=*_"):
            continue
        line = re.sub(r"^[-*+]\s+|^\d+[.)]\s+", "", line)
        if line:
            lines.append(line)
    return lines


def _normalize_rule(text: str) -> str:
    """A comparison key: punctuation and casing are not the instruction."""
    return re.sub(r"[^a-z0-9 ]+", " ", text.lower()).strip()


def _topic_of(text: str) -> Optional[str]:
    lowered = text.lower()
    for topic, markers in CONFLICT_TOPICS:
        if any(marker in lowered for marker in markers):
            return topic
    return None


def compile_rules(collected: List[Dict[str, str]]) -> str:
    """Deduplicate, resolve conflicts by authority, and flatten."""
    kept: List[Dict[str, str]] = []
    seen_text: set = set()
    claimed_topics: Dict[str, str] = {}
    dropped: List[str] = []

    for entry in collected:
        for directive in _directives(entry["body"]):
            key = _normalize_rule(directive)
            if not key or key in seen_text:
                continue  # The same instruction from five vendors is one rule.
            topic = _topic_of(directive)
            if topic and topic in claimed_topics:
                # A higher-authority file already ruled on this. Shipping both
                # is how a model ends up choosing at random.
                if claimed_topics[topic] != entry["source"]:
                    dropped.append(f"{topic} ({entry['source']})")
                    continue
            seen_text.add(key)
            if topic:
                claimed_topics.setdefault(topic, entry["source"])
            kept.append({"text": directive, "source": entry["source"]})

    if not kept:
        return ""

    body = "\n".join(f"- {rule['text']}" for rule in kept)
    if len(body) > MAX_RULES_CHARS:
        body = body[:MAX_RULES_CHARS] + "\n- [rules truncated]"
    sources = ", ".join(dict.fromkeys(e["source"] for e in collected))
    header = (
        "Project rules, compiled from this repository's own instruction files "
        f"({sources}). They outrank your general habits. Duplicates have been "
        "merged and conflicts resolved in favour of the most specific file, so "
        "every line below applies.\n\n"
    )
    footer = (
        f"\n\n[{len(dropped)} lower-priority rule(s) dropped as conflicting: "
        f"{', '.join(dict.fromkeys(dropped))}]" if dropped else ""
    )
    return header + body + footer


# ===== Search/replace edits =====
#
# The block format is Cline's, which is also aider's and roughly everyone's:
# a search section, a divider, a replace section. It is parsed strictly — a
# malformed block is an error, never a partial application.

SEARCH_OPEN = re.compile(r"^<{5,9} SEARCH\s*$|^-{5,9} SEARCH\s*$", re.M)
DIVIDER = re.compile(r"^={5,9}\s*$", re.M)
REPLACE_CLOSE = re.compile(r"^>{5,9} REPLACE\s*$|^\+{5,9} REPLACE\s*$", re.M)


class EditError(ValueError):
    """A block was malformed, or matched zero or many times.

    Carries a structured payload as well as a message. A generic "edit failed"
    makes a small open-weights model panic and rewrite the whole file from
    scratch; compiler-grade coordinates — which line, what was expected, what
    is actually there — make it fix the block instead. That difference is the
    whole reason this class exists rather than a bare ValueError.
    """

    def __init__(self, message: str, **payload):
        super().__init__(message)
        self.payload = {"status": "REJECTED", "reason": message, **payload}


def parse_edit_blocks(text: str) -> List[Tuple[str, str]]:
    """Pull ``(search, replace)`` pairs out of a diff payload.

    Accepts both the ``<<<<<<< SEARCH`` and ``------- SEARCH`` spellings, since
    models trained on different agents emit different ones and rejecting the
    other spelling is a pointless failure.
    """
    blocks: List[Tuple[str, str]] = []
    pos = 0
    while True:
        opened = SEARCH_OPEN.search(text, pos)
        if not opened:
            break
        divider = DIVIDER.search(text, opened.end())
        if not divider:
            raise EditError("a SEARCH block has no ======= divider")
        closed = REPLACE_CLOSE.search(text, divider.end())
        if not closed:
            raise EditError("a SEARCH block has no REPLACE terminator")
        search = _strip_edges(text[opened.end():divider.start()])
        replace = _strip_edges(text[divider.end():closed.start()])
        blocks.append((search, replace))
        pos = closed.end()
    if not blocks:
        raise EditError(
            "no search/replace blocks found. Use:\n"
            "------- SEARCH\n<exact existing text>\n=======\n<new text>\n+++++++ REPLACE"
        )
    return blocks


def _strip_edges(section: str) -> str:
    """Drop the single newline the delimiters contribute, and nothing else.

    Indentation inside a block is meaningful, so nothing is stripped from the
    line starts — that was the bug that made every edit to indented code fail.
    """
    if section.startswith("\n"):
        section = section[1:]
    if section.endswith("\n"):
        section = section[:-1]
    return section


def apply_edits(content: str, blocks: List[Tuple[str, str]]) -> str:
    """Apply every block in order, or raise and change nothing.

    Exact match first. Failing that, a match ignoring trailing whitespace and
    line-ending differences — the two ways a model's copy of a file drifts from
    what is on disk without the code being different. Anything looser than that
    would be guessing, and a coding agent that guesses at edits is worse than
    one that refuses.
    """
    result = content
    for index, (search, replace) in enumerate(blocks, start=1):
        if search == "":
            # An empty search is "append", which is well-defined and useful.
            result = result + ("" if result.endswith("\n") or not result else "\n") + replace
            continue
        count = result.count(search)
        if count == 1:
            result = result.replace(search, replace, 1)
            continue
        if count > 1:
            raise EditError(
                f"block {index} matches {count} places — include more surrounding "
                f"context so it identifies exactly one",
                block=index, matches=count, fix="add_context",
            )
        loosened = _loose_replace(result, search, replace)
        if loosened is None:
            raise EditError(**_rejection(result, search, index))
        result = loosened
    return result


def _rejection(content: str, search: str, index: int) -> Dict[str, Any]:
    """Say exactly where the block stopped matching, and against what.

    "Does not match" tells the model nothing it did not already know. Naming
    the line, what it expected there, and what is actually there turns a
    guess-again into a one-token correction.
    """
    lines = content.replace("\r\n", "\n").split("\n")
    wanted = search.replace("\r\n", "\n").split("\n")
    first = wanted[0].strip()

    # Find where the block *starts* to match, then the first line that diverges.
    for start, line in enumerate(lines):
        if line.strip() != first:
            continue
        for offset, expected in enumerate(wanted):
            actual = lines[start + offset] if start + offset < len(lines) else None
            if actual is None:
                return {
                    "message": (
                        f"block {index} ran past the end of the file at line "
                        f"{start + offset + 1}"
                    ),
                    "block": index, "line": start + offset + 1,
                    "expected": expected, "found": None, "fix": "reread_file",
                }
            if actual.rstrip() != expected.rstrip():
                return {
                    "message": (
                        f"block {index} rejected: line {start + offset + 1} "
                        f"expected {expected.strip()!r}, found {actual.strip()!r}"
                    ),
                    "block": index, "line": start + offset + 1,
                    "expected": expected, "found": actual, "fix": "correct_block",
                }
    return {
        "message": (
            f"block {index} rejected: no line in the file matches its first line "
            f"{first!r}. Read the file again and copy the exact text."
        ),
        "block": index, "line": None, "expected": wanted[0], "found": None,
        "fix": "reread_file",
    }


def _normalize(text: str) -> str:
    return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n"))


def _loose_replace(content: str, search: str, replace: str) -> Optional[str]:
    """Match on normalized lines, but splice using the file's real text."""
    lines = content.replace("\r\n", "\n").split("\n")
    target = _normalize(search).split("\n")
    if not target:
        return None
    stripped = [line.rstrip() for line in lines]
    hits = [
        i for i in range(len(lines) - len(target) + 1)
        if stripped[i:i + len(target)] == target
    ]
    if len(hits) != 1:
        return None
    start = hits[0]
    spliced = lines[:start] + replace.split("\n") + lines[start + len(target):]
    return "\n".join(spliced)


# ===== Checkpoints =====

SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".mypy_cache", ".pytest_cache", ".next", "target", ".tox", ".idea",
}
TEXT_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".md", ".txt", ".html",
    ".css", ".scss", ".yml", ".yaml", ".toml", ".ini", ".cfg", ".sh", ".rs",
    ".go", ".java", ".c", ".h", ".cpp", ".hpp", ".rb", ".php", ".sql", ".swift",
    ".kt", ".lua", ".r", ".pl", ".cs", ".vue", ".svelte", ".xml", ".env.example",
}
MAX_FILE_BYTES = 512 * 1024
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_CHECKPOINTS = 50


def _is_text(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in TEXT_SUFFIXES


def snapshot(root: str) -> Dict[str, str]:
    """Every text file in the workspace, keyed by relative path.

    Binaries and build output are skipped: restoring them is not what anyone
    means by "undo what the agent did", and carrying them would make a
    checkpoint too big to keep fifty of.
    """
    files: Dict[str, str] = {}
    total = 0
    for base, dirs, names in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        for name in sorted(names):
            if not _is_text(name):
                continue
            full = os.path.join(base, name)
            try:
                if os.path.getsize(full) > MAX_FILE_BYTES:
                    continue
                with open(full, "r", encoding="utf-8") as handle:
                    body = handle.read()
            except (OSError, UnicodeDecodeError):
                continue
            total += len(body)
            if total > MAX_SNAPSHOT_BYTES:
                return files
            files[os.path.relpath(full, root).replace(os.sep, "/")] = body
    return files


def create_checkpoint(root: str, label: str = "", conversation_id: Optional[str] = None) -> Dict[str, Any]:
    """Snapshot the workspace so this point can be returned to.

    In a git repository the snapshot is a git tree object written through a
    private index — content-addressed, atomic, and effectively free, because
    git has already done the work. Outside one it falls back to copying the
    text files into the database, which is the only option but is slower and
    bounded. Either way the caller gets the same shape back.
    """
    entry_id = str(uuid.uuid4())[:12]
    tree, head, files = "", "", {}
    try:
        from . import gitops

        if gitops.is_repo(root):
            written = gitops.write_tree(root)
            tree, head = written["tree"], written["head"]
    except Exception:
        tree = ""  # Any git trouble at all falls back to the copy.
    if not tree:
        files = snapshot(root)

    conn = get_db()
    conn.execute(
        """INSERT INTO coder_checkpoints
           (id, label, root, files, conversation_id, created_at, tree, head)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (entry_id, label or "checkpoint", root, json.dumps(files),
         conversation_id, datetime.now(timezone.utc).isoformat(), tree, head),
    )
    # Keep the table bounded; the oldest checkpoint is the least useful one.
    conn.execute(
        """DELETE FROM coder_checkpoints WHERE id NOT IN (
               SELECT id FROM coder_checkpoints ORDER BY created_at DESC LIMIT ?)""",
        (MAX_CHECKPOINTS,),
    )
    conn.commit()
    conn.close()
    return {
        "id": entry_id,
        "label": label or "checkpoint",
        "files": len(files),
        "backend": "git" if tree else "snapshot",
        "tree": tree,
        "head": head,
    }


def list_checkpoints(limit: int = 30) -> List[Dict[str, Any]]:
    conn = get_db()
    rows = conn.execute(
        """SELECT id, label, root, conversation_id, created_at, tree, head,
                  length(files) AS size
           FROM coder_checkpoints ORDER BY created_at DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def restore_checkpoint(checkpoint_id: str) -> Dict[str, Any]:
    """Put every snapshotted file back, and report what moved.

    Files the agent *created* after the checkpoint are deleted, because leaving
    them behind would make "restore" mean "mostly restore" — the failure mode
    that makes people stop trusting undo.
    """
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM coder_checkpoints WHERE id = ?", (checkpoint_id,)
    ).fetchone()
    conn.close()
    if not row:
        raise KeyError(f"no such checkpoint: {checkpoint_id}")

    root = row["root"]
    tree = row["tree"] if "tree" in row.keys() else ""
    if tree:
        from . import gitops

        result = gitops.restore_tree(root, tree)
        return {
            "id": checkpoint_id,
            "backend": "git",
            # `clean -fd` is what wipes the ghost files a rabbit hole leaves;
            # the caller is told so rather than having to infer it.
            "restored": result["reverted"],
            "removed": [],
            "purged": True,
            # Files the OS would not let go of — an editor or another program
            # holding them open. Named rather than swallowed: a restore that
            # silently skipped a file is the one that costs someone their work.
            "blocked": result.get("blocked", []),
        }

    files = json.loads(row["files"])
    restored, removed = [], []
    for rel, body in files.items():
        full = os.path.join(root, rel.replace("/", os.sep))
        try:
            current = None
            if os.path.isfile(full):
                with open(full, "r", encoding="utf-8", errors="replace") as handle:
                    current = handle.read()
            if current == body:
                continue
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as handle:
                handle.write(body)
            restored.append(rel)
        except OSError:
            continue

    for rel in sorted(set(snapshot(root)) - set(files)):
        try:
            os.remove(os.path.join(root, rel.replace("/", os.sep)))
            removed.append(rel)
        except OSError:
            continue

    return {"id": checkpoint_id, "restored": restored, "removed": removed}


def delete_checkpoint(checkpoint_id: str) -> bool:
    conn = get_db()
    cursor = conn.execute("DELETE FROM coder_checkpoints WHERE id = ?", (checkpoint_id,))
    conn.commit()
    deleted = cursor.rowcount > 0
    conn.close()
    return deleted


# ===== Recipes =====
#
# Goose's best portable idea: a saved task, with parameters, a mode and a tool
# allowlist. Stored in config rather than the database so a recipe can be
# exported, checked into a repo, and shared.

RECIPE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


def recipes() -> List[Dict[str, Any]]:
    raw = get_config().get("coder_recipes", [])
    return [r for r in raw if isinstance(r, dict) and r.get("id")]


def get_recipe(recipe_id: str) -> Optional[Dict[str, Any]]:
    for recipe in recipes():
        if recipe["id"] == recipe_id:
            return recipe
    return None


def save_recipe(
    recipe_id: str,
    title: str,
    prompt: str,
    description: str = "",
    parameters: Optional[List[Dict[str, Any]]] = None,
    mode: str = MODE_PLAN,
    tools: Optional[List[str]] = None,
) -> Dict[str, Any]:
    if not RECIPE_ID.match(recipe_id or ""):
        raise ValueError("a recipe id is lowercase letters, digits, - and _")
    if not (prompt or "").strip():
        raise ValueError("a recipe needs a prompt")
    recipe = {
        "id": recipe_id,
        "title": title or recipe_id,
        "description": description,
        "prompt": prompt,
        "parameters": [p for p in (parameters or []) if isinstance(p, dict) and p.get("name")],
        "mode": normalize_mode(mode),
        "tools": list(tools or []),
    }
    kept = [r for r in recipes() if r["id"] != recipe_id]
    set_config("coder_recipes", kept + [recipe])
    return recipe


def delete_recipe(recipe_id: str) -> bool:
    kept = [r for r in recipes() if r["id"] != recipe_id]
    if len(kept) == len(recipes()):
        return False
    set_config("coder_recipes", kept)
    return True


PARAM_PATTERN = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")


def required_parameters(recipe_id: str) -> List[str]:
    """Every placeholder the prompt uses that has no default.

    Returned with the validation error so the model is told what to supply
    rather than having to guess from the failure.
    """
    recipe = get_recipe(recipe_id) or {}
    defaults = {
        p["name"] for p in recipe.get("parameters", [])
        if p.get("default") is not None
    }
    return sorted(set(PARAM_PATTERN.findall(recipe.get("prompt", ""))) - defaults)


def render_recipe(recipe_id: str, values: Optional[Dict[str, Any]] = None) -> str:
    """Substitute ``{{name}}`` placeholders, refusing to run half-filled.

    A recipe that silently ran with an unfilled parameter would send the model
    a prompt containing the literal text ``{{path}}``, which is worse than an
    error because it looks like it worked.
    """
    recipe = get_recipe(recipe_id)
    if not recipe:
        raise KeyError(f"no such recipe: {recipe_id}")
    supplied = dict(values or {})
    for param in recipe.get("parameters", []):
        name = param["name"]
        if name not in supplied and param.get("default") is not None:
            supplied[name] = param["default"]

    missing = sorted({
        name for name in PARAM_PATTERN.findall(recipe["prompt"])
        if str(supplied.get(name, "")).strip() == ""
    })
    if missing:
        raise ValueError("missing recipe parameters: " + ", ".join(missing))
    return PARAM_PATTERN.sub(lambda m: str(supplied[m.group(1)]), recipe["prompt"])


# ===== Plan -> Act handoff =====
#
# Switching to Act with the planning conversation still attached is the most
# expensive mistake in this whole flow. A plan is produced by reading files,
# grepping, and thinking out loud; by the time it is agreed, the history is
# mostly tool output that has already served its purpose. Carrying it forward
# spends the context window on transcript and buries the plan itself.
#
# So the transition compacts: the planning turns are compressed into a dense
# architectural snapshot, the verbose middle is dropped, and Act starts from
# the snapshot with the write tools restored.

COMPACTION_PROMPT = (
    "Compress the planning conversation above into an implementation brief for "
    "the engineer who will now carry it out. Write it as terse structured notes, "
    "not prose, and include exactly these sections:\n"
    "GOAL — one sentence.\n"
    "FILES — each file to change and what changes in it.\n"
    "APPROACH — the ordered steps.\n"
    "CONSTRAINTS — anything agreed that limits the implementation.\n"
    "OPEN — anything still undecided, or 'none'.\n"
    "Include no commentary, no restating of the conversation, and nothing you "
    "were not actually told. If the plan is incomplete, say so under OPEN "
    "rather than inventing the missing part."
)
MAX_COMPACTION_INPUT = 24000
SNAPSHOT_HEADER = "Implementation brief, compacted from the planning session:"


def plan_messages(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The parts of a conversation worth compacting.

    System messages are re-derived every turn, so they are not part of the plan
    and would only dilute the compaction input.
    """
    return [m for m in history if m.get("role") in ("user", "assistant") and m.get("content")]


def compact_plan(history: List[Dict[str, Any]], resolved: Any, stream_events) -> str:
    """Ask the model to compress its own plan into a brief.

    Returns an empty string on any failure. A failed compaction must never
    block the switch to Act — the worst case is the old behaviour, which is
    carrying the full history, not a user stuck in Plan mode.
    """
    turns = plan_messages(history)
    if not turns:
        return ""
    transcript = "\n\n".join(
        f"{m['role'].upper()}: {m['content']}" for m in turns
    )[-MAX_COMPACTION_INPUT:]
    try:
        parts = []
        for event in stream_events(
            resolved,
            [{"role": "user", "content": f"{transcript}\n\n---\n\n{COMPACTION_PROMPT}"}],
            tools=None,
        ):
            if event.get("type") in ("text", "content"):
                parts.append(event.get("text", ""))
        return "".join(parts).strip()
    except Exception:
        return ""


def store_snapshot(conversation_id: str, snapshot_text: str) -> bool:
    """Keep the brief for the Act turns that follow."""
    if not (conversation_id and snapshot_text.strip()):
        return False
    stored = dict(get_config().get("coder_snapshots", {}) or {})
    stored[conversation_id] = snapshot_text.strip()
    # One brief per conversation, and only the recent ones — an unbounded map
    # in config would grow forever for a feature nobody reads twice.
    if len(stored) > 20:
        stored = dict(list(stored.items())[-20:])
    set_config("coder_snapshots", stored)
    return True


def snapshot_for(conversation_id: str) -> str:
    return (get_config().get("coder_snapshots", {}) or {}).get(conversation_id or "", "")


def clear_snapshot(conversation_id: str) -> None:
    stored = dict(get_config().get("coder_snapshots", {}) or {})
    if stored.pop(conversation_id or "", None) is not None:
        set_config("coder_snapshots", stored)


# ===== Client-side tool rejection =====

def reject_tool(name: str, mode: str) -> Optional[Dict[str, Any]]:
    """Refuse a write tool called in plan mode, before it can run.

    Removing the declaration is the first line of defence, but a model can
    still emit a call for a tool it was never offered — small models do it
    constantly, having seen the name in their training data. The rejection is
    structured rather than prose so the model reads it as a protocol error and
    corrects, instead of apologising and trying again.
    """
    if normalize_mode(mode) != MODE_PLAN or _bare(name) not in WRITE_TOOLS:
        return None
    return {
        "status": "REJECTED",
        "reason": f"{_bare(name)} is not available in PLAN mode",
        "tool": name,
        "fix": (
            "Finish the plan instead. Say which files you would change and how; "
            "the user switches to ACT mode when they agree, and the write tools "
            "come back then."
        ),
    }
