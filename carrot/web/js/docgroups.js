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
// carry more than a string: 🖥 or ☁ for where the model runs, a pencil, a
// paperclip with a count, and a send arrow, each a button that knows what it
// is instead of a region of a pseudo-element identified by arithmetic on the
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
        route.appendChild(chipPart('cg-chip-icon', info.model.local ? '🖥' : '☁',
                                   info.model.local ? 'runs on this machine' : 'runs in the cloud'));
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
            if (group) sendGroup(group);
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
                           + (open.files.length ? ' cg-has-files' : ''),
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
 */
function installGroupPlugin(crepe) {
    const kit = window.CarrotMilkdownKit;
    if (!crepe || !kit || !kit.$prose || !kit.prose) return false;
    try {
        crepe.editor.use(kit.$prose(() => groupProsePlugin()));
        return true;
    } catch (_) {
        return false;
    }
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

async function sendGroup(group) {
    if (!group || !group.text.trim()) return;
    await saveNoteNow();
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
    await dispatchDoc({
        text: directives ? `${group.text}\n\n${directives}` : group.text,
        note_id: currentNoteId,
        title: `${title} (group)`,
        conversation_id: currentConversationId,
        destination: destination || 'chat',
        option: option || '',
    }, `${title} (group)`);
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
