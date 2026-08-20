// ================================================================
// Groups — a paragraph that knows where it goes.
//
// Sending used to be a button in the toolbar: one destination, one model, for
// whatever happened to be selected at the moment you pressed it. That is the
// wrong shape for how a document is actually used. A plan has a paragraph that
// is a question for Research, a list that is a job for the agent, and a block
// of prose that is just prose — and a single Send pointed at all of it means
// doing them one at a time, re-picking the destination between each.
//
// So routing stops being a toolbar and becomes a property of the text. Select
// something, group it, and the group carries its own destination, model and
// evidence.
//
// **How a group survives.** In the markdown, as a pair of HTML comments:
//
//     <!--carrot:group to=research/deep model=local/phi4:14b files=src%2Frouter.py-->
//     The paragraph that goes to Research.
//     <!--/carrot:group-->
//
// Comments were chosen after testing what the editor does to them: they
// round-trip through Milkdown unchanged, they are invisible in every other
// markdown reader, and — the part that matters — they live *in the file*. The
// alternative was a side table of character offsets, which is wrong the first
// time somebody edits a paragraph above the group.
//
// Paths are percent-encoded in `files=` because an attribute here ends at the
// first space and half the files worth citing have one in the name.
//
// **How a group is drawn, and the layer that does not work.**
//
// The first version of this put classes and data attributes on the editor's
// own nodes and let CSS hide the marker text and draw a chip in its place. It
// worked for about a tenth of a second: ProseMirror rebuilds its DOM from its
// document state and drops anything it did not put there, so the classes were
// stripped on the next redraw — measured at ~120ms after applying them, with
// no keystroke in between. A MutationObserver reapplying them is a loop that
// races the editor and loses, because it is answering the wrong question.
//
// The chip is now a ProseMirror decoration, registered as a Milkdown plugin
// where Crepe is created in `mountEditor`. Decorations are part of editor
// state rather than a thing done to editor DOM, so the editor redraws the chip
// itself, and there is nothing left to be discarded. The marker keeps its real
// text — the file stays honest — and a node decoration hides it while a widget
// decoration draws the chip in the same place.
//
// Being a real element rather than a `::before` is also what lets the chip
// carry more than a string: the app's own computer/cloud glyph for where the
// model runs, a pencil, and a send arrow, each a button that knows what it is
// instead of a region of a pseudo-element identified by arithmetic on the
// click's x coordinate.
//
// **What a group's route means.** It is written back into the text as the very
// `@/to`, `@/model` and `@/file` lines the document format already understands,
// so `/api/doc/send` routes a group by exactly the path it routes a whole note.
// There is no second router, and nothing new on the server.
// ================================================================

// Lazy, and not `[^>]*`: greedy matching ate the closing `--` of the comment
// and then had nothing left to match `-->` against, so no marker was ever
// recognised in the editor even though the same text parsed fine from the
// markdown by line.
const GROUP_OPEN = /^<!--\s*carrot:group(.*?)\s*-->$/;
const GROUP_CLOSE = /^<!--\s*\/carrot:group\s*-->$/;

// Six, cycled. Enough that neighbouring groups are told apart, few enough that
// they stay a palette rather than a rainbow.
const GROUP_COLOURS = 6;

let groupMenuState = null;

function parseGroupAttrs(text) {
    const attrs = {};
    for (const [, key, value] of text.matchAll(/(\w+)=([^\s>]+)/g)) attrs[key] = value;
    return attrs;
}

/** A document with its group markers taken out.
 *
 * The server strips these in `doc_agent.resolve`, which covers everything sent
 * through `/api/doc/send`. A document staged into the composer does not go
 * that way: it rides the ordinary attachment pipeline, so without this the
 * markers reach the model inside the .md itself — invisible in the chip, and
 * the whole reason they were being complained about in the first place.
 */
function stripGroupMarkers(text) {
    const kept = String(text || '').split('\n').filter(line => {
        const trimmed = line.trim();
        return !GROUP_OPEN.test(trimmed) && !GROUP_CLOSE.test(trimmed);
    });
    return kept.join('\n').replace(/\n{3,}/g, '\n\n').trim();
}

/** The files a group cites, decoded back into ordinary paths. */
function groupFiles(attrs) {
    if (!attrs.files) return [];
    return attrs.files.split(',').filter(Boolean).map(part => {
        try { return decodeURIComponent(part); } catch (_) { return part; }
    });
}

/** One opening marker, built from an attrs object.
 *
 * Everything goes through here so that editing one attribute cannot drop
 * another: the first version rebuilt the marker from the two fields the menu
 * happened to know about, which silently discarded a group's cited files the
 * next time anybody changed its destination.
 */
function groupMarkerLine(attrs) {
    const parts = [];
    if (attrs.to) parts.push(`to=${attrs.to}`);
    if (attrs.model) parts.push(`model=${attrs.model}`);
    const files = groupFiles(attrs);
    if (files.length) parts.push('files=' + files.map(encodeURIComponent).join(','));
    for (const [key, value] of Object.entries(attrs)) {
        if (!['to', 'model', 'files'].includes(key) && value) parts.push(`${key}=${value}`);
    }
    return `<!--carrot:group${parts.length ? ' ' + parts.join(' ') : ''}-->`;
}

// "research quick", or "chat" when only a model is pinned.
function groupDestinationLabel(attrs) {
    return (attrs.to || 'chat').split('/').filter(Boolean).join(' ');
}

// Which model the group will actually use, and whether that was its choice.
//
// A group with no `model=` is not a group with no model — it runs on whatever
// the app is set to. Leaving the chip blank there was the worst of both: it
// looked like the question was unanswered when in fact it was answered
// elsewhere, by something the reader could not see from here. So the inherited
// model is shown too, in a lighter weight, which is the difference between
// "this is pinned" and "this is what it will use".
function groupModelDisplay(attrs) {
    if (attrs.model) {
        const [provider, ...rest] = attrs.model.split('/');
        return { name: rest.join('/') || attrs.model, local: provider === 'local', pinned: true };
    }
    if (typeof autoModel !== 'undefined' && autoModel) {
        return { name: 'Auto', local: typeof autoIsLocal === 'undefined' ? true : !!autoIsLocal, pinned: false };
    }
    const name = (typeof currentModel !== 'undefined' && currentModel) || '';
    if (!name) return null;
    const provider = typeof currentProvider === 'undefined' ? null : currentProvider;
    return { name, local: provider === 'ollama' || provider === null, pinned: false };
}

// ===== Drawing: a ProseMirror decoration =====

// The marker's text, given a top-level node of the document.
//
// Not `node.textContent`. Milkdown parses a block HTML comment into a
// paragraph wrapping an `html` node, and that node is an atom holding its
// source in `attrs.value` — it has no text content at all, so the obvious
// reading returns an empty string for every marker in the document. The DOM
// version got away with it because it was reading rendered HTML; this reads
// the document, so it has to ask the node.
function groupMarkerText(node) {
    if (!node) return '';
    if (node.type.name === 'html') return (node.attrs.value || '').trim();
    if (node.childCount === 1 && node.firstChild && node.firstChild.type.name === 'html') {
        return (node.firstChild.attrs.value || '').trim();
    }
    // A marker typed by hand, before the editor has re-parsed it as HTML.
    return (node.textContent || '').trim();
}

// On `pointerdown`, and only `pointerdown`.
//
// Three attempts, each of which looked right and did nothing under a real
// mouse. `click` never fires, because a click needs its press and release
// on the same element and these buttons sit in a contenteditable that
// re-renders on the press. `mousedown` never fires either — something in
// the editor stack calls `preventDefault()` on the pointer event, and that
// suppresses the whole compatibility sequence: mousedown, mouseup and
// click all stop existing. Instrumenting the chip with a listener for each
// of the four is what settled it: `pointerdown` arrives, nothing else does.
//
// Worth naming because it is invisible from a test: a synthesised
// `dispatchEvent(new MouseEvent('click'))` runs the handler perfectly well,
// so a button dead under every real mouse can pass a test that clicks it.
// Only driving the actual pane found this.
function chipButton(cls, text, title, handler) {
    const el = document.createElement('button');
    el.type = 'button';
    el.className = cls;
    if (text) el.textContent = text;
    el.title = title;
    el.addEventListener('pointerdown', (event) => {
        event.preventDefault();
        event.stopPropagation();
        handler();
    });
    el.addEventListener('click', (event) => {
        event.preventDefault();
        event.stopPropagation();
        // Keyboard reaches it by the ordinary route: `detail` is 0 for a click
        // a button raised from Enter or Space, and non-zero for a pointer one,
        // which is what keeps this from firing twice on a browser that does
        // deliver both.
        if (event.detail === 0) handler();
    });
    return el;
}

// The app's own glyphs rather than an emoji.
//
// `#i-computer` and `#i-cloud` are the pair the empty state already uses to
// say where an answer comes from, and the same fact should not be drawn two
// ways in one app — an emoji also renders as whatever the platform feels like,
// which for 🖥 is a beige CRT.
//
// `setAttribute` rather than `.className`, because on an SVG element that
// property is a read-only `SVGAnimatedString` and assigning to it silently
// does nothing.
const SVG_NS = 'http://www.w3.org/2000/svg';

function chipIcon(symbol, title) {
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('class', 'ico cg-chip-icon');
    if (title) {
        const label = document.createElementNS(SVG_NS, 'title');
        label.textContent = title;
        svg.appendChild(label);
    }
    const use = document.createElementNS(SVG_NS, 'use');
    use.setAttribute('href', symbol);
    svg.appendChild(use);
    return svg;
}

function chipPart(cls, text, title) {
    const el = document.createElement('span');
    el.className = cls;
    el.textContent = text;
    if (title) el.title = title;
    return el;
}

// The chip itself. A real element, so each thing it offers is a button rather
// than a guess about which end of a pseudo-element was clicked.
function groupChipElement(info) {
    const chip = document.createElement('span');
    chip.className = 'cg-chip cg-c' + info.colour;
    chip.contentEditable = 'false';
    chip.dataset.groupIndex = String(info.index);

    const route = chipButton('cg-chip-route', '', 'Change where this group goes',
                             () => openGroupMenu(chip, { mode: 'edit', index: info.index }));
    if (info.model) {
        route.appendChild(chipIcon(info.model.local ? '#i-computer' : '#i-cloud',
                                   info.model.local ? 'runs on this computer' : 'runs in the cloud'));
    }
    route.appendChild(chipPart('cg-chip-where', info.where));
    if (info.model) {
        route.appendChild(chipPart('cg-chip-arrow', '→'));
        route.appendChild(chipPart(
            'cg-chip-model' + (info.model.pinned ? '' : ' cg-inherited'),
            info.model.name,
            info.model.pinned ? 'pinned to this group'
                              : 'not pinned — this is the model Carrot is set to'));
    }

    chip.append(
        route,
        chipButton('cg-chip-edit', '✎', 'Edit this group — route, model, files',
                   () => openGroupMenu(chip, { mode: 'edit', index: info.index })),
        chipButton('cg-chip-send', '➤', 'Send this group', () => {
            const group = groupsInDocument()[info.index];
            if (group) sendGroup(group, info.index);
        }),
    );
    return chip;
}

// The evidence a group cites, drawn along the bottom edge of its box.
//
// This was a paperclip and a count on the chip, which is the wrong shape twice
// over: it says how many files there are without saying which, and it puts
// them in the header, where the one thing you cannot do is take one off. Files
// are contents, so they go inside — the box grows downward to hold them, the
// way the composer's own attachments grow it upward, and each one is a chip
// you can read and remove.
function groupFilesElement(info) {
    const row = document.createElement('span');
    row.className = 'cg-files';
    row.contentEditable = 'false';
    for (const path of info.files) {
        const item = document.createElement('span');
        item.className = 'cg-file cg-c' + info.colour;
        // The basename, with the full path on hover. A rail of half-elided
        // directory names says less about a file than its name does.
        item.appendChild(chipPart('cg-file-name', path.split(/[\\/]/).pop() || path, path));
        item.appendChild(chipButton('cg-file-drop', '×', 'Stop citing ' + path,
                                    () => detachGroupFile(info.index, path)));
        row.appendChild(item);
    }
    return row;
}

/** Take one file off a group, leaving everything else about it alone. */
async function detachGroupFile(index, path) {
    const group = groupsInDocument()[index];
    if (!group) return;
    const attrs = { ...group.attrs };
    const files = group.files.filter(one => one !== path);
    if (files.length) attrs.files = files.map(encodeURIComponent).join(',');
    else delete attrs.files;
    await rewriteGroupMarker(index, attrs);
}

// ===== Watching what a group is doing =====
//
// Sending a group used to be a one-way door: the arrow threw the paragraph at
// Research or the agent, the app changed tabs, and the document you were
// writing lost track of the thing it had started. Come back to the note ten
// minutes later and there is nothing on the group to say a run exists, let
// alone whether it finished — the only way to find out is to remember which
// tab it went to and go look.
//
// A group is a subtask, so it keeps its own progress. Send it and the chip
// becomes a bar; when the bar fills, it becomes a button that opens the
// result. That is the whole of it, and it is why the run has to be watched
// from here rather than from the tab that happens to be showing it.
//
// **Where the run's identity comes from.** The stream that answers a send is
// the only place a run's id exists before the run is over, so the two stream
// handlers report it (see js/agents.js) and `groupRunStarted` catches it for
// whichever group is mid-send. A slot rather than a return value, because
// `dispatchDoc` hands off to three different streams and threading a value
// back through all of them to reach one caller is more wiring than the fact is
// worth.
//
// **Where progress comes from.** `/api/activity/run`, which reads the run's
// own row and keeps answering after it finishes — `/api/activity` drops a job
// the moment it stops, and "stopped" is the one transition this is waiting for.
//
// **What it is keyed by.** The note and the group's ordinal, held in memory
// and dropped when the note changes. Deliberately not written into the marker:
// a run id is a fact about this afternoon, and the marker is a fact about the
// document. Editing a note heavily enough to renumber its groups while one is
// running will point a bar at its neighbour; that is the honest cost of not
// putting ephemeral state in the user's file.

const groupRuns = new Map();
let groupRunPending = null;
let groupRunTimer = null;
// The two kinds `/api/activity/run` answers for. A chat group is watched too,
// but it has no row anywhere — its answer arrives in the transcript, and the
// send itself is the whole of its progress — so polling one would 404 on the
// first tick and turn a working group into "no longer there".
const WATCHABLE_RUNS = ['research', 'agent'];
// Whether a poll is either running or scheduled. Without it, `pollGroupRuns`
// could only ever be restarted by itself, so anything that momentarily left no
// run in the `running` state — a 404 blip, a dismissed row — ended the loop for
// good and froze every other bar at whatever it last said.
let groupRunPolling = false;

function groupRunKey(index) {
    return `${currentNoteId || ''}:${index}`;
}

/** A run has started for whichever group was mid-send. Called from the streams. */
function groupRunStarted(kind, runId) {
    if (!groupRunPending || !runId) return;
    const key = groupRunPending;
    groupRunPending = null;
    groupRuns.set(key, { kind, id: runId, status: 'running', done: 0, total: 0 });
    refreshGroupChips();
    ensureGroupRunPolling();
}

/** Start watching, if anything is being watched and nothing is watching yet. */
function watchable(run) {
    return run && run.status === 'running' && run.id && WATCHABLE_RUNS.includes(run.kind);
}

function ensureGroupRunPolling() {
    if (groupRunPolling) return;
    if (![...groupRuns.values()].some(watchable)) return;
    groupRunPolling = true;
    groupRunTimer = setTimeout(pollGroupRuns, 0);
}

/** Ask after every run still working, and stop asking when none is. */
async function pollGroupRuns() {
    clearTimeout(groupRunTimer);
    groupRunPolling = true;
    const live = [...groupRuns.entries()].filter(([, run]) => watchable(run));
    if (!live.length) { groupRunPolling = false; return; }
    let changed = false;
    for (const [key, run] of live) {
        // Through `settleGroupRun`, so the request and its 404 handling exist
        // once. Two copies of "ask after a run" is how one of them ends up
        // knowing something about the answer that the other does not.
        await settleGroupRun(key);
        const next = groupRuns.get(key) || {};
        if (next.status !== run.status || next.done !== run.done || next.total !== run.total) {
            changed = true;
        }
    }
    if (changed) refreshGroupChips();
    // Two seconds while something is live. The runs being watched take minutes,
    // and this is a poll on top of the rail's own.
    if ([...groupRuns.values()].some(watchable)) {
        groupRunTimer = setTimeout(pollGroupRuns, 2000);
    } else {
        groupRunPolling = false;
    }
}

/** Ask after one run once, now.
 *
 * The poll is on a two-second timer, which is right for a bar somebody is
 * watching and wrong for a queue deciding whether to start the next group:
 * the stream has ended, the row already says how it ended, and waiting two
 * seconds to read it would put a stale "working…" under every finished group
 * in a batch. Returns the settled status.
 */
async function settleGroupRun(key) {
    const run = groupRuns.get(key);
    if (!run) return 'gone';
    if (!run.id || !WATCHABLE_RUNS.includes(run.kind)) return run.status;
    try {
        const next = await api('/api/activity/run?kind=' + encodeURIComponent(run.kind)
                               + '&id=' + encodeURIComponent(run.id));
        groupRuns.set(key, { ...run, ...next });
        return next.status;
    } catch (_) {
        // A 404 means the row is gone — deleted, or a database swapped out
        // underneath a stale id. Either way there is nothing left to watch, and
        // a bar that polls a missing run for ever is a worse answer than
        // admitting it.
        groupRuns.set(key, { ...run, status: 'gone' });
        return 'gone';
    }
}

/** Drop the runs belonging to other notes. Called when a note is opened.
 *
 * Pruning rather than clearing, so that re-opening the note you are already in
 * — which happens on every retarget — does not throw away the bar you are
 * watching.
 */
function resetGroupRuns() {
    const mine = `${currentNoteId || ''}:`;
    for (const key of [...groupRuns.keys()]) {
        if (!key.startsWith(mine)) groupRuns.delete(key);
    }
    groupRunPending = null;
    ensureGroupRunPolling();
}

// The bar, and what it becomes.
//
// A determinate bar only where there is something real to count: research
// knows its sub-questions once it has planned them, an agent run knows its
// step budget. Before the plan exists there is no denominator, and a bar that
// invents one is a bar that lies smoothly for thirty seconds — so that case is
// drawn as an indeterminate sweep instead.
function groupRunElement(info, run) {
    const wrap = document.createElement('span');
    wrap.className = 'cg-run cg-c' + info.colour + ' cg-run-' + run.status;
    wrap.contentEditable = 'false';

    // Waiting its turn in a batch. Drawn before it starts rather than left
    // blank, so pressing Run all shows the whole plan at once instead of
    // revealing it one group at a time — which is the difference between a
    // queue and a document that keeps surprising you.
    if (run.status === 'queued') {
        wrap.appendChild(chipPart('cg-run-note', 'queued',
                                  'waiting for the groups above it'));
        return wrap;
    }

    if (run.status === 'running') {
        const track = document.createElement('span');
        track.className = 'cg-run-track' + (run.total ? '' : ' cg-run-unknown');
        const fill = document.createElement('span');
        fill.className = 'cg-run-fill';
        if (run.total) fill.style.width = Math.round((run.done / run.total) * 100) + '%';
        track.appendChild(fill);
        wrap.appendChild(track);
        wrap.appendChild(chipPart('cg-run-note',
            run.total ? `${run.done} of ${run.total}` : 'working…',
            run.label || ''));
        wrap.appendChild(chipButton('cg-run-view', 'Watch', 'Open the run and watch it',
                                    () => openGroupRun(run)));
        return wrap;
    }

    // Finished, one way or another. The word is the run's own status rather
    // than a cheerful "done" over a run that failed.
    // `complete` is the word both a research run and an agent run write to
    // their row; `completed` is here because a map that only knows one spelling
    // of success fails by printing a database value at the reader, which is
    // exactly what this did before anybody looked.
    const words = { complete: 'done', completed: 'done', failed: 'failed',
                    cancelled: 'stopped', interrupted: 'interrupted',
                    gone: 'no longer there' };
    wrap.appendChild(chipPart('cg-run-note', words[run.status] || run.status, run.label || ''));
    // Only where there is something to open. A group whose *send* failed has
    // no run and no id — `openGroupRun` looks the kind up in the rail's
    // openers, finds nothing and returns — so offering View there is a button
    // that does nothing at all, on the one chip whose reader most wants to
    // press something.
    if (run.status !== 'gone' && run.id) {
        wrap.appendChild(chipButton('cg-run-view', 'View', 'Open what this produced',
                                    () => openGroupRun(run)));
    }
    wrap.appendChild(chipButton('cg-run-clear', '×', 'Stop showing this run',
                                () => { groupRuns.delete(groupRunKey(info.index)); refreshGroupChips(); }));
    return wrap;
}

/** Open a run where it actually lives — the rail's own openers, reused. */
function openGroupRun(run) {
    const open = typeof ACTIVITY_OPEN !== 'undefined' ? ACTIVITY_OPEN[run.kind] : null;
    if (open) open(run.id);
}

// Every decoration the document currently wants: a hidden marker and a chip at
// each opening, a bottom edge at each closing, and the group's tint behind
// everything between them.
function groupDecorations(doc) {
    const prose = window.CarrotMilkdownKit && window.CarrotMilkdownKit.prose;
    if (!prose) return null;
    const { Decoration, DecorationSet } = prose;
    const decorations = [];
    let index = 0;
    let open = null;

    doc.forEach((node, offset) => {
        const text = groupMarkerText(node);
        const opening = GROUP_OPEN.exec(text);
        if (opening) {
            const attrs = parseGroupAttrs(opening[1] || '');
            const info = {
                index,
                colour: index % GROUP_COLOURS,
                where: groupDestinationLabel(attrs),
                model: groupModelDisplay(attrs),
                files: groupFiles(attrs),
                run: groupRuns.get(groupRunKey(index)) || null,
            };
            open = info;
            index += 1;
            decorations.push(Decoration.node(offset, offset + node.nodeSize, {
                class: 'cg-marker cg-open cg-c' + info.colour,
            }));
            // Inside the marker's paragraph rather than between blocks, so the
            // chip sits where the marker's text was instead of adding a line.
            decorations.push(Decoration.widget(offset + 1, () => groupChipElement(info), {
                side: -1,
                // Identity, so a redraw reuses the element it already built —
                // which is also what keeps the click handlers on it alive.
                // Everything the chip draws is in the key, or a chip would go
                // on showing the route it had before it was changed.
                key: [
                    'cg-chip', info.index, info.where, info.files.length,
                    info.model ? `${info.model.name}:${info.model.local}:${info.model.pinned}` : '',
                ].join(':'),
                // The chip is furniture, not text. Without these, clicking a
                // button on it puts a cursor in the document instead.
                stopEvent: () => true,
                ignoreSelection: true,
            }));
            return;
        }
        if (GROUP_CLOSE.test(text)) {
            if (open) {
                decorations.push(Decoration.node(offset, offset + node.nodeSize, {
                    class: 'cg-marker cg-close cg-c' + open.colour
                           + (open.files.length || open.run ? ' cg-has-files' : ''),
                }));
                // The closing marker is otherwise just the bottom edge of the
                // box, which makes it where the cited files belong: they
                // extend the region downward rather than being counted in its
                // header.
                if (open.files.length) {
                    const closing = open;
                    decorations.push(Decoration.widget(offset + 1, () => groupFilesElement(closing), {
                        side: -1,
                        key: `cg-files:${closing.index}:${closing.files.join('|')}`,
                        stopEvent: () => true,
                        ignoreSelection: true,
                    }));
                }
                // And what it is doing, under them: a group is a subtask, so
                // its progress belongs on it rather than in whichever tab the
                // send happened to open.
                if (open.run) {
                    const closing = open;
                    const run = closing.run;
                    decorations.push(Decoration.widget(offset + 1, () => groupRunElement(closing, run), {
                        side: 1,
                        key: `cg-run:${closing.index}:${run.id}:${run.status}:${run.done}/${run.total}`,
                        stopEvent: () => true,
                        ignoreSelection: true,
                    }));
                }
            }
            open = null;
            return;
        }
        if (open) {
            decorations.push(Decoration.node(offset, offset + node.nodeSize, {
                class: 'cg-body cg-c' + open.colour,
            }));
        }
    });

    // How many there are is the same walk, so Run all is told here rather than
    // by a second scan that could disagree with the chips about what exists.
    syncRunAllButton(index);
    return DecorationSet.create(doc, decorations);
}

// The key the plugin is registered under, kept at module scope so a redraw can
// be asked for from outside — see `refreshGroupChips`.
let groupPluginKey = null;

// A plain ProseMirror plugin. Rebuilt on a document change, and on an explicit
// nudge — scanning the top level of the document is cheap, but doing it on
// every selection move would be work for nothing.
function groupProsePlugin() {
    const { Plugin, PluginKey } = window.CarrotMilkdownKit.prose;
    const key = new PluginKey('carrot-groups');
    groupPluginKey = key;
    return new Plugin({
        key,
        state: {
            init: (_config, state) => groupDecorations(state.doc),
            apply: (tr, previous, _old, state) => (
                (tr.docChanged || tr.getMeta(key)) ? groupDecorations(state.doc) : previous
            ),
        },
        props: {
            decorations(state) { return key.getState(state); },
        },
    });
}

/** Redraw the chips without the document having changed.
 *
 * A chip showing an inherited model is showing something that lives outside
 * the document, so switching the app's model has to reach in here — otherwise
 * every group that has not pinned one keeps naming the model you just stopped
 * using.
 */
function refreshGroupChips() {
    // Any redraw is also a chance to notice that something is being watched and
    // nothing is watching it.
    ensureGroupRunPolling();
    const kit = window.CarrotMilkdownKit;
    if (!groupPluginKey || !crepeInstance || !crepeReady || !kit) return;
    try {
        const view = crepeInstance.editor.ctx.get(kit.ctx.editorViewCtx);
        view.dispatch(view.state.tr.setMeta(groupPluginKey, true));
    } catch (_) { /* no editor open */ }
}

/** Register the chip with a Crepe instance, before it is created.
 *
 * Returns false rather than throwing when the vendor bundle predates the
 * export it needs: the rest of the Write tab works without a chip, and an
 * exception here would take the editor down with it.
 *
 * It also *records* which of those things went wrong. Failing quietly here is
 * how a document ends up showing an opening group marker as literal text in
 * the middle of somebody's prose: the marker is still in the file, correctly,
 * and the thing that was supposed to hide it and draw a chip over it never
 * registered. From the outside that reads as the app printing its own syntax
 * at you, with nothing anywhere saying why.
 */
let groupPluginProblem = '';

function installGroupPlugin(crepe) {
    const kit = window.CarrotMilkdownKit;
    if (!crepe) {
        groupPluginProblem = 'the editor did not start';
        return false;
    }
    if (!kit) {
        groupPluginProblem = 'the editor bundle did not load';
        return false;
    }
    if (!kit.$prose || !kit.prose) {
        // The specific one worth naming: a desktop build carrying a vendor
        // bundle older than the feature. Everything looks fine and no chip is
        // ever drawn.
        groupPluginProblem = 'this editor bundle is older than group chips'
                           + ' — it exports no decoration hooks';
        return false;
    }
    try {
        crepe.editor.use(kit.$prose(() => groupProsePlugin()));
        groupPluginProblem = '';
        return true;
    } catch (exc) {
        groupPluginProblem = 'the editor refused the chip plugin'
                           + (exc && exc.message ? ' — ' + exc.message : '');
        return false;
    }
}

/** Markers in the file and no chips on the screen — say so, and offer a way out.
 *
 * This is the one failure in the Write tab that puts editor syntax in front of
 * the reader, so it is the one that must not be silent. Checked by comparing
 * the two facts rather than by trusting the install: a plugin that registered
 * and then drew nothing fails exactly the same way from where the user sits.
 */
function checkGroupChips() {
    const strip = document.getElementById('doc-note');
    if (!strip) return;
    let markers = 0;
    try {
        markers = groupsInDocument().length;
    } catch (_) {
        return;     // no editor open
    }
    const chips = document.querySelectorAll('.cg-chip').length;
    if (!markers || chips) {
        strip.classList.add('hidden');
        strip.innerHTML = '';
        return;
    }
    const why = groupPluginProblem || 'the chips could not be drawn';
    strip.classList.remove('hidden');
    strip.innerHTML = '<span class="doc-note-text">'
        + escHtml(`This document has ${markers} group${markers === 1 ? '' : 's'} in it, `
                  + `shown as raw comments because ${why}. They still route correctly `
                  + 'when sent.')
        + '</span>'
        + '<button class="doc-note-act" data-act="strip">Remove them</button>';
    strip.querySelector('[data-act="strip"]').onclick = () => removeAllGroupMarkers();
}

/** Take the markers out of the document, for somebody who cannot see the chips.
 *
 * Their routes go with them — that is the honest cost, and the alternative is
 * a document you cannot read. `stripGroupMarkers` is the same function the
 * composer uses when staging a document, so there is one definition of what a
 * marker is.
 */
async function removeAllGroupMarkers() {
    const cleaned = stripGroupMarkers(getEditorMarkdown());
    await mountEditor(cleaned);
    scheduleNoteSave();
    setNoteStatus('group markers removed — their routes went with them');
}

// ===== Making one =====

// Wrapping happens in the markdown rather than in the editor's document.
//
// Getting a ProseMirror position from a DOM selection and inserting two nodes
// around it is possible, but it is the editor's own model and Crepe does not
// expose it. The markdown is the file, the selection's text is in it, and
// remounting is a few milliseconds — so the simple thing is also the one that
// cannot leave the editor's state disagreeing with the file.
async function groupSelection(attrs) {
    const selected = (window.getSelection()?.toString() || '').trim();
    if (!selected) { setNoteStatus('select something to group first'); return; }
    const markdown = getEditorMarkdown();
    const at = markdown.indexOf(selected);
    if (at === -1) {
        // Selections spanning formatting boundaries do not appear verbatim in
        // the source. Said plainly rather than silently doing nothing.
        setNoteStatus('could not group that — try selecting whole paragraphs');
        return;
    }
    const wrapped = `${groupMarkerLine(attrs)}\n\n${selected}\n\n<!--/carrot:group-->`;
    const next = markdown.slice(0, at) + wrapped + markdown.slice(at + selected.length);
    await mountEditor(next);
    scheduleNoteSave();
}

// ===== Reading one back =====

/** Every group in the document, with its text, its route and its files. */
function groupsInDocument() {
    const lines = getEditorMarkdown().split('\n');
    const groups = [];
    let current = null;
    for (const line of lines) {
        const opening = GROUP_OPEN.exec(line.trim());
        if (opening) {
            current = { attrs: parseGroupAttrs(opening[1] || ''), lines: [] };
            continue;
        }
        if (GROUP_CLOSE.test(line.trim())) {
            if (current) {
                groups.push({
                    ...current,
                    files: groupFiles(current.attrs),
                    text: current.lines.join('\n').trim(),
                });
            }
            current = null;
            continue;
        }
        if (current) current.lines.push(line);
    }
    return groups;
}

// ===== Sending one =====

async function sendGroup(group, index, options = {}) {
    if (!group || !group.text.trim()) return;
    await saveNoteNow();
    const key = typeof index === 'number' ? groupRunKey(index) : null;
    // Claimed before the send, because the run's id arrives on the stream the
    // send opens and there is no other moment at which the two are connected.
    if (key) groupRunPending = key;
    const [destination, option] = (group.attrs.to || 'chat').split('/');
    // The route travels as the directives the document format already speaks,
    // so the server resolves a group exactly as it resolves a note — including
    // the model, which `doc_agent.resolve` reads off `@/model`, and the cited
    // files, whose contents it reads at send time.
    const directives = [
        group.attrs.to ? `@/to/${group.attrs.to}` : '',
        group.attrs.model ? `@/model/${group.attrs.model}` : '',
        // Quoted when the path has a space in it, which is the form
        // `REFERENCE_PATTERN` accepts and the reason the attribute is encoded.
        ...(group.files || []).map(path => `@/file/${/\s/.test(path) ? `"${path}"` : path}`),
    ].filter(Boolean).join('\n');
    const title = document.getElementById('note-title')?.value.trim() || 'Untitled note';
    // A chat group has no run row: its answer is a turn in the transcript, and
    // the send is the whole of its progress. Marked by hand at both ends so a
    // chat group in a batch is not the one chip in the document sitting blank
    // while it works.
    const chatty = (destination || 'chat') === 'chat';
    const watched = key && options.quiet && chatty;
    if (watched) {
        groupRuns.set(key, { kind: 'conversation', id: currentConversationId,
                             status: 'running', done: 0, total: 0 });
        refreshGroupChips();
    }
    try {
        await dispatchDoc({
            text: directives ? `${group.text}\n\n${directives}` : group.text,
            note_id: currentNoteId,
            title: `${title} (group)`,
            conversation_id: currentConversationId,
            destination: destination || 'chat',
            option: option || '',
        }, `${title} (group)`, { quiet: !!options.quiet });
    } finally {
        // Released whether or not a run claimed it. `groupRunStarted` clears
        // the slot when a stream reports an id, and a destination that reports
        // none — chat — used to leave it set: the *next* group's run id then
        // arrived and was filed against this group, so one bar showed another
        // group's progress and the real one showed nothing.
        groupRunPending = null;
    }
    if (watched) {
        // The conversation only exists once the turn has landed, when the send
        // was the thing that created it.
        groupRuns.set(key, { kind: 'conversation', id: currentConversationId,
                             status: 'complete', done: 0, total: 0 });
        refreshGroupChips();
    }
}


// ===== Running the whole document =====
//
// One group at a time was the only way to run a plan, which meant a document
// with six groups in it was six presses spread over however long the slowest
// of them took — and the sixth press happened when you remembered, not when
// the fifth finished.
//
// **Top to bottom, one at a time, and that is a decision rather than an
// implementation detail.** These are jobs on one machine: a local model can
// serve one agent run at a time, and firing six at once would have them
// queueing inside Ollama where nothing in this document can see the queue.
// Order is the document's order because that is the only order the document
// states — a plan is written down the page, and the paragraph above is the one
// you meant to happen first.
//
// **A failure does not stop the rest.** Groups are independent routes, not
// steps in a pipeline: the third paragraph failing to research says nothing
// about whether the fourth can. So the run continues and the failure stays on
// its own chip, where it is attached to the thing that failed. Stopping is a
// decision, and it has a button.
//
// **Stop stops the queue, not the run.** The group in flight keeps going,
// because cancelling a research or agent run is that run's own business and
// its tab has the control for it. What this button promises is that nothing
// further will be started, which is the thing you actually want when you press
// it in a hurry.

let batchRun = null;

/** The whole document, in order. */
async function runAllGroups() {
    if (batchRun) return;
    const runnable = groupsInDocument()
        .map((group, index) => ({ group, index }))
        .filter(item => item.group.text.trim());
    if (!runnable.length) {
        setNoteStatus('there are no groups to run — select some text and group it first');
        return;
    }
    await saveNoteNow();

    batchRun = { total: runnable.length, at: 0, done: 0, failed: 0, stop: false };
    setRunAllBusy(true);
    // Queued up front, so the document shows the whole plan the moment you
    // press the button rather than one group at a time as it gets there.
    for (const item of runnable) {
        groupRuns.set(groupRunKey(item.index), { kind: '', id: null, status: 'queued' });
    }
    refreshGroupChips();
    renderBatchBar();

    // `finally`, because a batch that dies on an unexpected throw would
    // otherwise leave `batchRun` set for the rest of the session — and the
    // first thing `runAllGroups` does is refuse to start when one is already
    // running, so the button would be dead until a reload with nothing on
    // screen saying why.
    try {
        for (const item of runnable) {
            if (batchRun.stop) break;
            batchRun.at += 1;
            renderBatchBar();
            const key = groupRunKey(item.index);
            let status = '';
            try {
                await sendGroup(item.group, item.index, { quiet: true });
                status = await settleGroupRun(key);
            } catch (_) {
                // The send itself failed — a 400 from the router, a stream that
                // never opened. There is no run to ask about, so the chip is
                // told directly; it is the only place this stays visible once
                // the batch bar has moved on.
                groupRuns.set(key, { kind: '', id: null, status: 'failed' });
                status = 'failed';
            }
            if (status === 'complete' || status === 'completed') batchRun.done += 1;
            else batchRun.failed += 1;
            refreshGroupChips();
            renderBatchBar();
        }
    } finally {
        // Anything still queued was never reached. Left saying "queued" it
        // would describe a queue that no longer exists.
        for (const item of runnable) {
            const key = groupRunKey(item.index);
            if (groupRuns.get(key)?.status === 'queued') groupRuns.delete(key);
        }
        const finished = batchRun;
        batchRun = null;
        setRunAllBusy(false);
        refreshGroupChips();
        renderBatchBar(finished);
    }
}

/** Run all, while it is already running. Pressing it again does nothing, and
 * a button that does nothing should not look like one that does. */
function setRunAllBusy(busy) {
    const button = document.getElementById('doc-runall-btn');
    if (button) button.disabled = busy;
}

function stopBatchRun() {
    if (!batchRun) return;
    batchRun.stop = true;
    renderBatchBar();
}

/** The strip under the toolbar: where the queue has got to, and a way out. */
function renderBatchBar(finished) {
    const bar = document.getElementById('doc-batch');
    if (!bar) return;
    if (!batchRun) {
        if (!finished) {
            bar.classList.add('hidden');
            bar.innerHTML = '';
            return;
        }
        // The tally, once. Named plainly: "4 done, 1 failed" is the sentence
        // somebody who walked away wants, and the failed one is still marked
        // on its own chip for anybody who wants to know which.
        const parts = [`Ran ${finished.at} of ${finished.total}`];
        if (finished.done) parts.push(`${finished.done} done`);
        if (finished.failed) parts.push(`${finished.failed} failed`);
        bar.classList.remove('hidden');
        bar.innerHTML = '<span class="doc-batch-note'
            + (finished.failed ? ' bad' : '') + '">' + escHtml(parts.join(' · ')) + '</span>'
            + '<button class="doc-batch-stop" data-act="dismiss">Dismiss</button>';
        bar.querySelector('[data-act="dismiss"]').onclick = () => {
            bar.classList.add('hidden');
            bar.innerHTML = '';
        };
        return;
    }
    bar.classList.remove('hidden');
    const label = batchRun.stop
        ? `Finishing group ${batchRun.at} — nothing further will start`
        : `Running group ${batchRun.at} of ${batchRun.total}`;
    const failed = batchRun.failed ? ` · ${batchRun.failed} failed` : '';
    bar.innerHTML = '<span class="doc-batch-note">' + escHtml(label + failed) + '</span>'
        + (batchRun.stop ? ''
            : '<button class="doc-batch-stop" data-act="stop"'
              + ' title="Stop starting new groups. The one running now carries on —'
              + ' stop that from its own tab.">Stop</button>');
    const stop = bar.querySelector('[data-act="stop"]');
    if (stop) stop.onclick = () => stopBatchRun();
}

/** Show Run all only when there is more than nothing to run.
 *
 * Called from the decoration builder because that is the one function that
 * runs on every change to the document, and "how many groups are there" is
 * already the question it is answering.
 */
function syncRunAllButton(count) {
    const button = document.getElementById('doc-runall-btn');
    if (!button) return;
    button.classList.toggle('hidden', !count);
    button.textContent = count > 1 ? `Run all ${count}` : 'Run';
    button.title = count > 1
        ? `Run all ${count} groups, top to bottom, one at a time`
        : 'Run this document\u2019s group';
}

// ===== The menu on a group, and on a selection =====
//
// The same stages the `@/` references already walk — destination, then
// provider, then model — because they are the same question and the answers
// come from the same endpoint. Files are the fourth, and they are not on that
// path: attaching evidence is something you do to a group that already exists,
// usually more than once, so it is reachable from the menu's foot rather than
// being a step everybody walks through to get to the end.

function closeGroupMenu() {
    document.getElementById('group-menu')?.remove();
    groupMenuState = null;
}

async function openGroupMenu(anchor, options) {
    closeGroupMenu();
    groupMenuState = { stage: 'to', attrs: {}, ...options };
    // Editing starts from what the group already says, so changing its
    // destination does not throw away its model or its files.
    if (options.mode === 'edit') {
        const group = groupsInDocument()[options.index];
        if (group) groupMenuState.attrs = { ...group.attrs };
    }
    const menu = document.createElement('div');
    menu.id = 'group-menu';
    menu.className = 'group-menu';
    document.body.appendChild(menu);
    const box = anchor.getBoundingClientRect();
    menu.style.left = Math.round(box.left) + 'px';
    menu.style.top = Math.round(box.bottom + 6) + 'px';
    await renderGroupMenu();
}

async function renderGroupMenu() {
    const menu = document.getElementById('group-menu');
    if (!menu || !groupMenuState) return;
    const stage = groupMenuState.stage;
    const attached = groupFiles(groupMenuState.attrs);
    let items = [];
    let heading = '';
    try {
        if (stage === 'to') {
            heading = 'Where does this go?';
            items = (await api('/api/doc/candidates?kind=to&q=')).candidates || [];
        } else if (stage === 'provider') {
            heading = 'Which provider?';
            items = (await api('/api/doc/candidates?kind=model&q=')).candidates || [];
        } else if (stage === 'file') {
            heading = attached.length
                ? `Evidence — ${attached.length} attached`
                : 'Which file should it read?';
            items = (await api('/api/doc/candidates?kind=file&q=')).candidates || [];
        } else {
            heading = 'Which model?';
            items = (await api('/api/doc/candidates?kind=model&q=&provider='
                               + encodeURIComponent(groupMenuState.provider))).candidates || [];
        }
    } catch (_) {
        items = [];
    }
    const foot = [];
    if (stage === 'model' || stage === 'provider') {
        // Leaving the model unset is a real answer: the group goes wherever it
        // goes on whatever the router would have picked anyway.
        foot.push('<button class="group-menu-skip" data-act="apply">Use the usual model</button>');
    }
    if (stage !== 'file') {
        foot.push('<button class="group-menu-skip" data-act="files">📎 Attach a file…'
                  + (attached.length ? ` (${attached.length})` : '') + '</button>');
    } else if (attached.length) {
        foot.push('<button class="group-menu-skip" data-act="clear">Remove all attached files</button>');
    }
    menu.innerHTML = '<div class="group-menu-head">' + escHtml(heading) + '</div>'
        + '<div class="group-menu-list">'
        + items.map((item, i) =>
            '<button class="group-menu-item' + (stage === 'file' && attached.includes(item.value) ? ' on' : '')
          + '" data-i="' + i + '">' + escHtml(item.label) + '</button>').join('')
        + '</div>'
        + foot.join('');
    menu.querySelectorAll('.group-menu-item').forEach(button => {
        button.onclick = () => chooseGroupStage(items[Number(button.dataset.i)]);
    });
    menu.querySelectorAll('.group-menu-skip').forEach(button => {
        button.onclick = () => {
            const act = button.dataset.act;
            if (act === 'files') { groupMenuState.stage = 'file'; renderGroupMenu(); return; }
            if (act === 'clear') { delete groupMenuState.attrs.files; applyGroupRoute(); return; }
            applyGroupRoute();
        };
    });
}

async function chooseGroupStage(item) {
    if (!item || !groupMenuState) return;
    if (groupMenuState.stage === 'to') {
        groupMenuState.attrs.to = item.value;
        groupMenuState.stage = 'provider';
        await renderGroupMenu();
        return;
    }
    if (groupMenuState.stage === 'provider') {
        groupMenuState.provider = item.value;
        groupMenuState.stage = 'model';
        await renderGroupMenu();
        return;
    }
    if (groupMenuState.stage === 'file') {
        // Toggling, so the same list both attaches and detaches — a second
        // menu to take one off again would be a menu nobody finds.
        const files = groupFiles(groupMenuState.attrs);
        const at = files.indexOf(item.value);
        if (at === -1) files.push(item.value); else files.splice(at, 1);
        groupMenuState.attrs.files = files.map(encodeURIComponent).join(',');
        if (!files.length) delete groupMenuState.attrs.files;
        await applyGroupRoute();
        return;
    }
    groupMenuState.attrs.model = item.value;
    await applyGroupRoute();
}

async function applyGroupRoute() {
    const state = groupMenuState;
    closeGroupMenu();
    if (!state) return;
    if (state.mode === 'create') {
        await groupSelection(state.attrs);
        return;
    }
    // Retargeting one that exists: rewrite its opening marker in place.
    await rewriteGroupMarker(state.index, state.attrs);
}

/** Replace the nth group's opening marker, and remount on the result.
 *
 * One place, because detaching a file and changing a destination are the same
 * edit to the same line — and two copies of it is how the second one ends up
 * knowing about an attribute the first has never heard of.
 */
async function rewriteGroupMarker(index, attrs) {
    let seen = 0;
    const rewritten = getEditorMarkdown().split('\n').map(line => {
        if (!GROUP_OPEN.test(line.trim())) return line;
        if (seen++ !== index) return line;
        return groupMarkerLine(attrs);
    }).join('\n');
    await mountEditor(rewritten);
    scheduleNoteSave();
}

// ===== Wiring =====

document.addEventListener('DOMContentLoaded', () => {
    const host = document.getElementById('note-editor-host');
    if (!host) return;

    // Right-click a selection to group it. The browser's own menu still opens
    // everywhere else in the editor, because taking that away to offer one item
    // would be a bad trade.
    host.addEventListener('contextmenu', (event) => {
        const selected = (window.getSelection()?.toString() || '').trim();
        if (!selected) return;
        event.preventDefault();
        openGroupMenu(
            { getBoundingClientRect: () => ({ left: event.clientX, bottom: event.clientY }) },
            { mode: 'create' });
    });

    // Clicking the chip is handled on the chip, which is a real element built
    // by the decoration — there is no delegation here because there is nothing
    // left to guess about which part of it was hit.

    document.addEventListener('mousedown', (event) => {
        if (!event.target.closest('#group-menu') && !event.target.closest('.cg-chip')) {
            closeGroupMenu();
        }
    });
});
