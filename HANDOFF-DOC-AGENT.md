# Handoff: the document agent, and what is already built for it

Written at the end of a long session, for whoever picks this up. Two things
here: the **document agent** (the big one), and **Settings** (small, mostly
done). The document agent is described first because it is the thing that was
misunderstood, and the misunderstanding is worth inheriting.

---

## 1. The correction that matters

Carrot currently has a button called **Send to Chat** in the Write tab, and I
added two more like it to the deck, canvas and LaTeX editors. That framing is
wrong and the user said so plainly:

> "our not thinking about this right… it's not a send doc to chat… no…
> imagine Cursor, but for your docs! It's agent for your docs, for your LaTeX,
> for your code, for your slides."

**"Send to chat" treats the document as an attachment.** You lift text out of
the editor, carry it to another tab, ask a question, read an answer, and carry
the answer back by hand. Every one of those steps is the user doing clerical
work that the machine is better at.

**What is wanted is Cursor's shape, applied to documents.** In Cursor you do
not send your file to a chat. The chat is *in* the file's window, it already
knows what file you are in, what you have selected, and what else is open — and
its output is a **diff you accept or reject**, not a paragraph you copy.

So the object to build is:

> A chat pane docked beside whichever editor is open — prose, LaTeX, deck,
> canvas — that already has the document as context, and whose edits arrive as
> proposals against the document rather than as text in a transcript.

The three design questions were asked and answered by the user earlier in the
session. **These are decided; do not re-open them.**

1. **Edit in place, or propose?** → **Propose.** Reuse the Code tab's existing
   Plan/Act split rather than inventing a second convention. Plan proposes and
   nothing on disk moves; Act may write. `carrot/coder.py` already implements
   this — `WRITE_TOOLS`, `tools_for_mode`, `normalize_mode` — and enforces it
   by *subtracting tools from the list*, not by asking the model nicely. That
   enforcement is the part worth copying: a model that can write eventually
   will, however the prompt is worded.

2. **What is "context of everything"?** → Workspace, backlinks, open files, and
   the document itself — and this wants building **together with the Context
   Inspector**, which now exists (see below). The inspector is the surface that
   makes "what does it know about my document" answerable.

3. **One panel or four?** → **One**, that knows which editor it is docked in.
   Not four per-format panes.

---

## 2. What already exists that you should not rebuild

This session built most of the foundation. Read these before writing anything.

### `carrot/doctext.py` — every format as text a model can read

**This is the piece that unblocked the whole idea.** A deck is a JSON array of
positioned boxes; a canvas is an Excalidraw scene. Sending either to a model
means sending kilobytes of coordinates, which is why those editors had no route
into chat at all.

`doctext.as_text(body, format, title)` renders any of the four formats. Served
at `GET /api/notes/{id}/text`. Its four rules, which you should keep:

- **Content before geometry.** What a slide says is first on its line; where
  the box sits is a parenthetical. A model asked to rewrite the second bullet
  should not parse a layout to find it.
- **Every element is addressable.** `[0]`, `[1]`, `[2]` per slide. **This is
  the hook the whole editing story hangs on** — "element 3 on slide 2" is an
  edit that can be applied surgically, without re-serialising the document and
  hoping a diff lands where it was meant to.
- **Empty is empty.** A blank slide renders as one. A model that cannot see it
  will not notice it is there.
- **Lossy in one direction only.** Nothing parses back. A deck's source of
  truth is its JSON, and a round trip through prose would silently drop fills,
  rotations and z-order.

A deck reads like this:

```
Deck: Pipeline
3 slides

## Slide 1
[0] text: "Your deck" (at 120,250 · 1040×130)
[1] text: "A subtitle, maybe" (at 120,390 · 1040×70)
Notes: mention the budget

## Slide 2
[0] shape rect: "Ingest" (at 100,200 · 320×240)
[3] image: embedded image (at 0,0 · 10×10)

## Slide 3
(empty)
```

Tests: `tests/test_doctext.py`.

### The Context Inspector — `/api/context`, `carrot/web/js/context.js`

`Context · 8 items` beside the composer, listing every source in the prompt
with its size and a switch on the ones that are a standing preference.

**The load-bearing decision: there is no second implementation.** The preview
is the real builder (`_prepare_history`) with the model call left off. Every
system block goes through `_context_add(history, manifest, disabled, source,
content)`, which names it, measures it and can be told to leave it out. A test
asserts the endpoint calls `_prepare_history`, because if that stops being true
the whole feature describes a prompt the model never got.

**When you add document context, add it as a source here.** `CONTEXT_SOURCES`
in `app.py` is the registry: `(id, label, detail, toggleable)`. A `document`
source already exists for `extra_system`. You will likely want `workspace`,
`backlinks` and `open files` as new toggleable sources.

### Trajectory — `carrot/trajectory.py`, `/api/conversations/{id}/trajectory`

A run as turns rather than as a transcript. Assembled entirely from stored
traces, so it cannot disagree with the transcript. Available in the Code tab
and in chat's Agent mode; steps expand to show a tool's full arguments and
result, a plan's actual goals, the answer.

**Use it while building.** On its first real run it found a live bug in under a
minute (see §4). It is the fastest way to see what an agentic turn actually
did.

### The Code tab's session machinery

- Code sessions are conversations with `metadata.surface == "code"`, set
  server-side from the `coder` flag in `_open_conversation`.
- `openCodeSession()` in `features.js` replays a stored session **including its
  tool trace**, using the same three functions the live stream uses
  (`agentToolCard`, `agentToolCardResult`, `agentTrace`). Copy that pattern; do
  not write a second renderer.

### The prose editor's command access

`window.CarrotMilkdownKit` (built by `webvendor/src/milkdown-entry.js`) exposes
Milkdown's command keys and ctx slices, so the app can drive the editor
programmatically — `toggleStrongCommand`, `wrapInHeadingCommand`,
`insertTableCommand`, etc. **This is how a proposed prose edit gets applied.**
A test asserts every button in the format bar names a command the bundle
exports, so a dropped export fails the suite rather than shipping a dead
control.

---

## 3. A suggested build order

Nothing here is binding, but this order keeps every step shippable.

### Step 1 — the pane, prose only, read-only

A right-hand chat pane in the Write tab. It sends the document (via
`/api/notes/{id}/text`) plus the selection, and answers. No editing yet.

Why first: it proves the docking, the context plumbing and the layout without
touching anybody's file. Reuse `#doc-rail` — the Write tab already has a shared
right-hand rail for backlinks/outline/canvas navigation, and "every format
answers 'where am I' in the same place" is an existing principle in that code.

### Step 2 — proposals for prose

Plan/Act, and a proposed edit rendered as a diff card with Accept / Reject.

The mechanism: the model returns a SEARCH/REPLACE block against the document's
markdown. `parseEditBlocks()` in `slides.js` already turns that exact format
into +/− lines for the Code tab's diff cards — reuse it. Applying an accepted
edit goes through the Milkdown kit, not through `innerHTML`.

**Undo has to be real.** The user's roadmap asks for "one-click undo of an
entire agent run". The prose editor has Milkdown's history; a proposal applied
as a single transaction is a single undo. Do not apply an edit as ten separate
transactions.

### Step 3 — LaTeX

The same pane; LaTeX is already plain text so `as_text` passes it through and
the diff is a text diff. `latexnote.js` has `editSelectionWithAI()` already —
a narrower version of this idea. Fold it in rather than leaving two paths.

### Step 4 — deck and canvas, as element operations

**This is where the addressing pays off, and where a text rewrite must not be
used.** A deck edit is not "here is the new document"; it is a list of
operations against the JSON:

```
set text of [2] on slide 2 to "Postgres"
add shape cylinder to slide 3 at 580,200 320×240
delete [4] on slide 1
```

Apply them to `slidesDoc` and re-render. The reason: the JSON carries fills,
rotations, z-order and image data that the text rendering deliberately drops,
and a round trip through prose would erase all of it. `doctext.py` says this
explicitly in its module docstring — that is the constraint, not an
implementation detail.

**`carrot/docedit.py` now exists** — the applier this step needed. Pure
function, no UI: `apply_operations(body, doc_format, operations)` returns a new
body or raises `DocEditError` having changed nothing. Tests in
`tests/test_docedit.py`.

Deck operations: `set_text`, `set_notes`, `add_element`, `delete_element`,
`move_element`, `add_slide`, `delete_slide`. Canvas: `set_text`,
`delete_element`, `move_element`.

Two things about it are load-bearing and should survive whatever is built on
top:

**Every address resolves against the original document, before anything
moves.** A model writes `[2] on slide 1` after reading the rendering of the
document as it now is. Applying operations in order against a document the
earlier ones have already changed means one delete silently shifts the meaning
of every later address — the edit lands on the wrong element, raises nothing,
and reads as a success. So addresses are resolved to element and slide *ids* in
one pass up front, and a second pass mutates by id. `test_a_delete_does_not_
shift_a_later_address` is the test that holds this; if you refactor, keep it.

**A canvas address skips tombstones.** Excalidraw keeps deleted elements in the
scene with `isDeleted` set, and `canvas_as_text` numbers only the live ones —
so `[3]` on a canvas is the fourth *live* element, not the fourth in the array.
Deletion tombstones rather than removes, and every edit bumps `version`, because
Excalidraw reconciles by merging and would otherwise treat the change as stale
and overwrite it.

What it deliberately does not do: **add** an element to a canvas. A labelled
shape there is two elements bound to each other by id (`containerId` one way,
`boundElements` the other), and a scene where that linkage is wrong renders in
ways a unit test cannot see. Decks take the full set because their boxes are
Carrot's own. If you need canvas add, build it against a real scene in a
browser, not against a fixture.

The deck shape registry is duplicated in Python (`DECK_SHAPES`), and a test
parses `SLIDE_SHAPES` out of `slides.js` and asserts the two agree — so a shape
added to the editor and not to the applier fails the suite rather than being
accepted here and silently rendered as a rectangle there.

Still to do for this step: wiring it up. Nothing calls `apply_operations` yet —
there is no endpoint and no UI. That is the next piece.

### Step 5 — context of everything

Add `workspace`, `backlinks` and `open files` as context sources, each with a
toggle, all visible in the Context chip. The chip already exists; this is
extending its registry.

---

## 4. Things that will bite you, learned the hard way

**The installed app on port 8181 is not your checkout.** It is the frozen
desktop build (`AppData\Local\Programs\carrot-desktop\...\carrot-backend.exe`).
Loading `localhost:8181` shows stale code and your edits appear to do nothing.
Preview through `scripts/dev_preview.py <port>` — it also redirects
`CARROT_DATA_DIR` to a temp folder so the real database is untouched. Launch
configs live in the **workspace root** `.claude/launch.json`, not the one
inside `./carrot`.

**Python changes need a server restart.** `dev_preview.py` runs uvicorn without
reload. JS and CSS are served fresh; anything in `carrot/*.py` is not. I lost
time three times to this — a new endpoint field simply missing from the
response.

**The theme's real colours are not the first `:root` in style.css.** It
declares `--card` and `--accent` in more than one `:root` block and the later
one wins. Read colours out of a running window
(`getComputedStyle(document.documentElement)`), never by grepping. I "fixed"
the Alt-Space overlay against the wrong palette because of this.

**Run `node --check` on every JS file you touch.** A literal newline inside a
string breaks the whole file, and every function in it silently vanishes — I
shipped that twice, and the second time it took out an existing Send button.
`for f in carrot/web/js/*.js; do node --check "$f"; done`.

**`scrollIntoView({behavior: 'smooth'})` is a silent no-op** on `#chat-messages`.
`auto` works, and `scroller.scrollTo({behavior: 'smooth'})` works. Measured.

**The test suite is unusually good at catching this codebase's mistakes.**
Every CSS token must be defined; radii must be on the scale; the overlay's
palette must match the app's. When one of those fails after your change, it is
usually right.

**Measure contrast rather than eyeballing it.** `--accent-fill`/`--on-accent`
pairs are tuned to *just* clear AA — ember is 4.72:1 — so there is no headroom
to spend on dimming. An "obviously fine" 0.9 alpha puts it under.

---

## 5. Settings tabs — done, with one thing left

Settings was twenty-one cards in one scroll. It is five groups now — General,
Models, Tools, Connections, Privacy — with the group chosen by a strip of tabs
above the cards and remembered in `localStorage`.

The cards are **tagged in the markup, not moved**: each carries
`data-settings-tab="…"`. Reordering twenty-one blocks of HTML would be a diff
nobody could review; the tag is the same fact in one attribute. Tests in
`tests/test_settings_tabs.py` assert no card is untagged, every declared group
has something in it, and a handful of specific cards are where you would look
for them.

**What is left:** the four sub-page cards at the top (Extensions, Memory,
Leaderboard, Help) are still outside the tab system — they are whole pages
rather than settings, and they currently sit above the strip on every tab. They
are fine there, but if the user wants them grouped, they want a row of their
own rather than a sixth tab.

---

## 6. State

- Branch `claude/asking-sourcing-and-the-dashboard`, **all local, nothing
  pushed**, no remote configured.
- Suite green: **3761 passed, 17 skipped**.
- `phi4:14b` advertises `completion` only — it **cannot call tools**, so it
  cannot run agent tasks. Ollama returns HTTP 400 for the whole request and the
  turn dies before its first token; Carrot now checks `supports_tools` and
  degrades to a toolless answer. For any agent work use a tools-capable model —
  the user's Qwen3 GGUF advertises `tools` and `thinking`.
- The user's roadmap has more on it than this document covers: universal
  command palette, visible memory UI, backlinks/related, screen recall, a
  Today surface, and agent pause/redirect/take-over. Backlinks is the one that
  most helps the document agent, because "what else is connected to this
  document" is context the pane will want.
