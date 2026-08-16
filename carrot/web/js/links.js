// ================================================================
// Links between documents — [[wikilink]] autocomplete, backlinks, graph
// ================================================================
//
// The editor is Milkdown (ProseMirror), which owns its DOM and will revert
// anything written into it from outside. So the autocomplete never edits the
// document directly: it reads what has been typed, and inserts by dispatching
// the same keystrokes a person would. That is slower than a ProseMirror plugin
// and it is the reason this works in the plain-textarea fallback too, without
// a second implementation.

let wikiPopupEl = null;
let wikiState = null;   // {host, kind, query, start, items, active} while open
let wikiSeq = 0;        // Guards against a slow fetch overwriting a fast one.

function wikiPopup() {
    if (!wikiPopupEl) {
        wikiPopupEl = document.createElement('div');
        wikiPopupEl.className = 'wiki-popup hidden';
        document.body.appendChild(wikiPopupEl);
    }
    return wikiPopupEl;
}

function closeWikiPopup() {
    wikiState = null;
    wikiPopup().classList.add('hidden');
}

// The text before the caret, and where the caret is, for either editor.
//
// Milkdown gives us a DOM selection inside a contenteditable; the fallback is
// a textarea with selectionStart. Both reduce to "the current text node's text
// up to the caret", which is all the `[[` scan needs — a wikilink does not
// span a paragraph, so there is never a reason to look further back.
function caretContext() {
    const fallback = document.getElementById('note-fallback');
    if (fallback && !fallback.classList.contains('hidden')) {
        return { kind: 'textarea', el: fallback,
                 text: fallback.value.slice(0, fallback.selectionStart),
                 caret: fallback.selectionStart };
    }
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount || !sel.isCollapsed) return null;
    const node = sel.anchorNode;
    if (!node || node.nodeType !== Node.TEXT_NODE) return null;
    const host = document.getElementById('note-editor-host');
    if (!host || !host.contains(node)) return null;
    return { kind: 'prosemirror', el: node,
             text: node.textContent.slice(0, sel.anchorOffset),
             caret: sel.anchorOffset };
}

// Are we inside an unclosed `[[`, and if so what has been typed since?
function pendingWikiQuery(textBeforeCaret) {
    const open = textBeforeCaret.lastIndexOf('[[');
    if (open === -1) return null;
    const after = textBeforeCaret.slice(open + 2);
    // A `]]` after the last `[[` means that link is finished, not being typed.
    // A newline means the `[[` was abandoned on an earlier line.
    if (after.includes(']]') || after.includes('\n')) return null;
    if (after.length > 80) return null;
    return { query: after, start: open };
}

async function wikiMaybeOpen() {
    const ctx = caretContext();
    if (!ctx) return closeWikiPopup();
    const pending = pendingWikiQuery(ctx.text);
    if (!pending) return closeWikiPopup();

    const seq = ++wikiSeq;
    let items = [];
    try {
        items = await api('/api/links/suggest?q=' + encodeURIComponent(pending.query));
    } catch (_) { items = []; }
    if (seq !== wikiSeq) return;   // A later keystroke already won.

    // Offering to create what has been typed is the point of the feature —
    // you link the thing first and write it afterwards. Only when it is not
    // already an exact title, so the common case does not grow a decoy row.
    const typed = pending.query.trim();
    const exact = items.some(i => (i.title || '').toLowerCase() === typed.toLowerCase());
    if (typed && !exact) items = items.concat([{ id: null, title: typed, create: true }]);
    if (!items.length) return closeWikiPopup();

    wikiState = { ...ctx, ...pending, items, active: 0 };
    renderWikiPopup();
}

function renderWikiPopup() {
    const popup = wikiPopup();
    if (!wikiState) return;
    popup.innerHTML = wikiState.items.map((item, i) => {
        const sub = item.create ? 'Create new document'
                  : (item.format && item.format !== 'markdown' ? item.format : '');
        return `<div class="wiki-item${i === wikiState.active ? ' active' : ''}" data-i="${i}">
                  <span class="wiki-item-title">${escHtml(item.title)}</span>
                  ${sub ? `<span class="wiki-item-sub">${escHtml(sub)}</span>` : ''}
                </div>`;
    }).join('');
    for (const el of popup.querySelectorAll('.wiki-item')) {
        // mousedown, not click: clicking moves focus out of the editor, and by
        // the time click fires the caret we are about to write at is gone.
        el.onmousedown = (e) => { e.preventDefault(); acceptWikiItem(+el.dataset.i); };
    }
    positionWikiPopup(popup);
    popup.classList.remove('hidden');
}

function positionWikiPopup(popup) {
    let rect;
    if (wikiState.kind === 'textarea') {
        rect = wikiState.el.getBoundingClientRect();
        rect = { left: rect.left + 12, bottom: rect.top + 28 };
    } else {
        const sel = window.getSelection();
        if (sel && sel.rangeCount) {
            const r = sel.getRangeAt(0).getBoundingClientRect();
            // A collapsed range at the start of an empty line has no box; fall
            // back to the element so the popup never lands in the corner.
            rect = (r.bottom || r.left) ? r
                 : wikiState.el.parentElement.getBoundingClientRect();
        } else return;
    }
    popup.style.left = Math.min(rect.left, window.innerWidth - 280) + 'px';
    popup.style.top = (rect.bottom + 6) + 'px';
}

async function acceptWikiItem(index) {
    if (!wikiState) return;
    const item = wikiState.items[index];
    if (!item) return;
    const { kind, el, text, caret, start } = wikiState;
    const typedLen = caret - (start + 2);   // what to replace, after the `[[`
    closeWikiPopup();

    if (item.create) {
        // Created now rather than on click-through, so that the graph shows it
        // immediately and a second `[[` finds it by name.
        try {
            await api('/api/notes', {
                method: 'POST',
                body: JSON.stringify({ title: item.title, content: '', format: 'markdown' }),
            });
            if (typeof loadNotes === 'function') loadNotes();
        } catch (_) { /* the link still stands; it just resolves to nothing yet */ }
    }

    const insert = item.title + ']]';
    if (kind === 'textarea') {
        const v = el.value;
        el.value = v.slice(0, caret - typedLen) + insert + v.slice(caret);
        const pos = caret - typedLen + insert.length;
        el.setSelectionRange(pos, pos);
        el.focus();
        if (typeof scheduleNoteSave === 'function') scheduleNoteSave();
        return;
    }

    // ProseMirror: select the typed fragment and let execCommand replace it, so
    // the change goes through the editor rather than around it.
    const sel = window.getSelection();
    const range = document.createRange();
    range.setStart(el, caret - typedLen);
    range.setEnd(el, caret);
    sel.removeAllRanges();
    sel.addRange(range);
    document.execCommand('insertText', false, insert);
    if (typeof scheduleNoteSave === 'function') scheduleNoteSave();
}

function wikiKeydown(e) {
    if (!wikiState || wikiPopup().classList.contains('hidden')) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        const n = wikiState.items.length;
        wikiState.active = (wikiState.active + (e.key === 'ArrowDown' ? 1 : n - 1)) % n;
        renderWikiPopup();
    } else if (e.key === 'Enter' || e.key === 'Tab') {
        e.preventDefault();
        acceptWikiItem(wikiState.active);
    } else if (e.key === 'Escape') {
        e.preventDefault();
        closeWikiPopup();
    }
}

// Bound on the document in capture phase: Milkdown stops key events at its own
// root, so a listener on the host would never see Enter or the arrows.
document.addEventListener('keydown', wikiKeydown, true);
document.addEventListener('keyup', (e) => {
    if (['ArrowDown', 'ArrowUp', 'Enter', 'Tab', 'Escape'].includes(e.key)) return;
    const host = document.getElementById('note-editor-host');
    const fallback = document.getElementById('note-fallback');
    const inEditor = (host && host.contains(e.target)) || e.target === fallback;
    if (inEditor) wikiMaybeOpen();
});
document.addEventListener('mousedown', (e) => {
    if (wikiPopupEl && !wikiPopupEl.contains(e.target)) closeWikiPopup();
});

// ================================================================
// Backlinks — what points here
// ================================================================
// Fetches, renders, and then decides whether the rail is worth showing.
//
// That last part has to happen here rather than in showWriteMode: this is
// async, and the mode is set the instant the document opens, so anything
// deciding "is there anything to show" at that moment is asking before the
// answer exists. The rail stayed hidden on every document that had backlinks.
async function refreshBacklinks(noteId) {
    const panel = document.getElementById('note-backlinks');
    if (!panel) return;
    let items = [];
    if (noteId) {
        try { items = await api('/api/links/backlinks/' + noteId); } catch (_) { items = []; }
    }
    // The document may have been closed, or another opened, while this was in
    // flight — writing a stale document's backlinks into the rail is worse
    // than showing none.
    if (noteId && noteId !== currentNoteId) return;

    const total = items.reduce((sum, i) => sum + i.count, 0);
    panel.innerHTML = !items.length ? '' :
        `<div class="backlinks-head">${total} link${total === 1 ? '' : 's'} to this document</div>`
        + items.map(item => `
            <div class="backlink" onclick="openNote('${item.id}')">
              <div class="backlink-title">${escHtml(item.title)}</div>
              ${item.contexts.map(c => `<div class="backlink-ctx">${escHtml(c)}</div>`).join('')}
            </div>`).join('');
    // Only prose puts backlinks in the rail; a canvas open by now must not
    // have its navigator replaced because a fetch finished late.
    if (typeof isWriteMode === 'function' && !isWriteMode('prose')) return;
    document.getElementById('doc-rail')?.classList.toggle('hidden', !items.length);
}

// A rendered `[[link]]` is clickable. Milkdown renders the raw text, so this
// binds on the host and reads what was clicked rather than decorating nodes.
document.addEventListener('click', async (e) => {
    const host = document.getElementById('note-editor-host');
    if (!host || !host.contains(e.target)) return;
    if (!(e.metaKey || e.ctrlKey)) return;   // plain clicks still place the caret
    const text = e.target.textContent || '';
    const m = /\[\[([^\[\]|\n]+?)(?:\|[^\[\]\n]*?)?\]\]/.exec(text);
    if (!m) return;
    e.preventDefault();
    const res = await api('/api/links/resolve?title=' + encodeURIComponent(m[1]));
    if (res.found) { openNote(res.id); return; }
    if (confirm(`Nothing is called "${m[1]}" yet. Create it?`)) {
        const created = await api('/api/notes', {
            method: 'POST',
            body: JSON.stringify({ title: m[1], content: '', format: 'markdown' }),
        });
        await loadNotes();
        openNote(created.id);
    }
});

// ================================================================
// Graph — a force-directed view of the whole vault
// ================================================================
//
// Hand-rolled rather than pulled from a library: nothing suitable is vendored,
// and a graph of a few hundred nodes needs Barnes-Hut and quadtrees about as
// much as it needs a build step. This is plain Verlet-ish integration on a
// canvas, cooling to a stop so it does not spin a laptop fan forever.

let graphState = null;
let graphRaf = null;

async function loadGraphTab() {
    const canvas = document.getElementById('graph-canvas');
    if (!canvas) return;
    let data;
    try { data = await api('/api/links/graph'); } catch (_) { data = { nodes: [], edges: [] }; }

    const empty = document.getElementById('graph-empty');
    if (!data.nodes.length) {
        empty.classList.remove('hidden');
        canvas.classList.add('hidden');
        return;
    }
    empty.classList.add('hidden');
    canvas.classList.remove('hidden');

    const rect = canvas.parentElement.getBoundingClientRect();
    // Seeded on a circle rather than at random: an identical vault produces an
    // identical picture, so the graph you learn the shape of stays that shape.
    const nodes = data.nodes.map((n, i) => {
        const angle = (i / data.nodes.length) * Math.PI * 2;
        const radius = Math.min(rect.width, rect.height) * 0.32;
        return { ...n,
                 x: rect.width / 2 + Math.cos(angle) * radius,
                 y: rect.height / 2 + Math.sin(angle) * radius,
                 vx: 0, vy: 0 };
    });
    const byId = Object.fromEntries(nodes.map(n => [n.id, n]));
    const edges = data.edges
        .map(e => ({ ...e, a: byId[e.source], b: byId[e.target] }))
        .filter(e => e.a && e.b);

    graphState = { canvas, ctx: canvas.getContext('2d'), nodes, edges,
                   alpha: 1, hover: null, pan: { x: 0, y: 0 }, zoom: 1,
                   drag: null, filter: '' };
    resizeGraphCanvas();
    bindGraphEvents();
    runGraph();
}

function resizeGraphCanvas() {
    if (!graphState) return;
    const { canvas } = graphState;
    const rect = canvas.parentElement.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    canvas.width = rect.width * dpr;
    canvas.height = rect.height * dpr;
    canvas.style.width = rect.width + 'px';
    canvas.style.height = rect.height + 'px';
    graphState.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    graphState.w = rect.width;
    graphState.h = rect.height;
}

function runGraph() {
    if (graphRaf) cancelAnimationFrame(graphRaf);
    const step = () => {
        if (!graphState) return;
        // Below this the picture is settled and further frames are wasted work.
        if (graphState.alpha > 0.005) { tickGraphLayout(); graphState.alpha *= 0.985; }
        drawGraph();
        graphRaf = requestAnimationFrame(step);
    };
    step();
}

function tickGraphLayout() {
    const { nodes, edges, alpha, w, h } = graphState;
    // Repulsion, O(n²). At a few hundred notes that is a few tens of thousands
    // of operations a frame — cheaper than the quadtree that would replace it.
    for (let i = 0; i < nodes.length; i++) {
        for (let j = i + 1; j < nodes.length; j++) {
            const a = nodes[i], b = nodes[j];
            let dx = b.x - a.x, dy = b.y - a.y;
            let d2 = dx * dx + dy * dy;
            if (d2 < 0.01) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 0.01; }
            if (d2 > 90000) continue;   // far enough to ignore
            const force = 900 / d2;
            const d = Math.sqrt(d2);
            const fx = (dx / d) * force, fy = (dy / d) * force;
            a.vx -= fx; a.vy -= fy;
            b.vx += fx; b.vy += fy;
        }
    }
    // Springs along edges.
    for (const e of edges) {
        const dx = e.b.x - e.a.x, dy = e.b.y - e.a.y;
        const d = Math.sqrt(dx * dx + dy * dy) || 1;
        const force = (d - 110) * 0.012;
        const fx = (dx / d) * force, fy = (dy / d) * force;
        e.a.vx += fx; e.a.vy += fy;
        e.b.vx -= fx; e.b.vy -= fy;
    }
    // Gravity toward the middle, so disconnected notes do not drift off-screen.
    for (const n of nodes) {
        n.vx += (w / 2 - n.x) * 0.0016;
        n.vy += (h / 2 - n.y) * 0.0016;
        if (graphState.drag === n) continue;
        n.vx *= 0.82; n.vy *= 0.82;
        n.x += n.vx * alpha; n.y += n.vy * alpha;
    }
}

function graphNodeRadius(n) { return 4 + Math.min(9, n.degree * 1.4); }

function drawGraph() {
    const { ctx, nodes, edges, w, h, pan, zoom, hover, filter } = graphState;
    const css = getComputedStyle(document.documentElement);
    const accent = css.getPropertyValue('--accent').trim() || '#f4813f';
    const muted = css.getPropertyValue('--muted').trim() || '#99a0ae';
    const text = css.getPropertyValue('--text').trim() || '#eceef4';

    ctx.clearRect(0, 0, w, h);
    ctx.save();
    ctx.translate(pan.x, pan.y);
    ctx.scale(zoom, zoom);

    const dim = (n) => filter && !(n.title || '').toLowerCase().includes(filter);

    for (const e of edges) {
        ctx.beginPath();
        ctx.moveTo(e.a.x, e.a.y);
        ctx.lineTo(e.b.x, e.b.y);
        const lit = hover && (e.a === hover || e.b === hover);
        ctx.strokeStyle = lit ? accent : muted;
        ctx.globalAlpha = lit ? 0.55 : (dim(e.a) && dim(e.b) ? 0.05 : 0.16);
        // An unresolved link is drawn thinner — it is a mention, not a document.
        ctx.lineWidth = (lit ? 1.6 : 1) / zoom;
        if (!e.resolved) ctx.setLineDash([3 / zoom, 3 / zoom]);
        ctx.stroke();
        ctx.setLineDash([]);
    }

    ctx.globalAlpha = 1;
    for (const n of nodes) {
        const r = graphNodeRadius(n);
        ctx.beginPath();
        ctx.arc(n.x, n.y, r, 0, Math.PI * 2);
        // A node that does not exist yet is hollow — the shape says "mentioned,
        // not written" without needing a legend.
        if (!n.exists) {
            ctx.strokeStyle = muted;
            ctx.globalAlpha = dim(n) ? 0.2 : 0.7;
            ctx.lineWidth = 1.4 / zoom;
            ctx.stroke();
        } else {
            ctx.fillStyle = n === hover ? accent : (n.format === 'canvas' || n.format === 'slides' ? muted : text);
            ctx.globalAlpha = dim(n) ? 0.15 : 1;
            ctx.fill();
        }
        // Labels only where they can be read: everything at high zoom, the
        // well-connected and the hovered at low. Drawing 160 of them at once
        // produces a grey smear, not a graph.
        const labelled = zoom > 1.4 || n === hover || n.degree >= 3 || (filter && !dim(n));
        if (labelled) {
            ctx.globalAlpha = dim(n) ? 0.2 : (n === hover ? 1 : 0.75);
            ctx.fillStyle = n === hover ? accent : muted;
            ctx.font = `${11 / zoom}px ${css.getPropertyValue('--sans').trim() || 'sans-serif'}`;
            ctx.textAlign = 'center';
            ctx.fillText(n.title || n.id, n.x, n.y - r - 4 / zoom);
        }
        ctx.globalAlpha = 1;
    }
    ctx.restore();
}

function graphPointAt(clientX, clientY) {
    const rect = graphState.canvas.getBoundingClientRect();
    return { x: (clientX - rect.left - graphState.pan.x) / graphState.zoom,
             y: (clientY - rect.top - graphState.pan.y) / graphState.zoom };
}

function graphNodeAt(clientX, clientY) {
    const p = graphPointAt(clientX, clientY);
    for (const n of graphState.nodes) {
        const r = graphNodeRadius(n) + 4;
        if ((n.x - p.x) ** 2 + (n.y - p.y) ** 2 <= r * r) return n;
    }
    return null;
}

function bindGraphEvents() {
    const { canvas } = graphState;
    if (canvas.dataset.bound) return;
    canvas.dataset.bound = '1';

    canvas.addEventListener('mousemove', (e) => {
        if (!graphState) return;
        if (graphState.drag) {
            const p = graphPointAt(e.clientX, e.clientY);
            graphState.drag.x = p.x; graphState.drag.y = p.y;
            graphState.drag.vx = 0; graphState.drag.vy = 0;
            graphState.alpha = Math.max(graphState.alpha, 0.35);   // re-settle
            return;
        }
        if (graphState.panning) {
            graphState.pan.x += e.clientX - graphState.panning.x;
            graphState.pan.y += e.clientY - graphState.panning.y;
            graphState.panning = { x: e.clientX, y: e.clientY };
            return;
        }
        const hit = graphNodeAt(e.clientX, e.clientY);
        graphState.hover = hit;
        canvas.style.cursor = hit ? 'pointer' : 'grab';
    });
    canvas.addEventListener('mousedown', (e) => {
        const hit = graphNodeAt(e.clientX, e.clientY);
        if (hit) graphState.drag = hit;
        else graphState.panning = { x: e.clientX, y: e.clientY };
    });
    window.addEventListener('mouseup', () => {
        if (!graphState) return;
        graphState.drag = null;
        graphState.panning = null;
    });
    canvas.addEventListener('click', (e) => {
        const hit = graphNodeAt(e.clientX, e.clientY);
        // A ghost node has nothing to open — it is a title somebody mentioned.
        if (hit && hit.exists) { switchTab('notes'); openNote(hit.id); }
    });
    canvas.addEventListener('wheel', (e) => {
        e.preventDefault();
        const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1;
        const next = Math.max(0.2, Math.min(4, graphState.zoom * factor));
        // Zoom about the pointer, not the origin, so the thing under the cursor
        // stays under the cursor.
        const rect = graphState.canvas.getBoundingClientRect();
        const mx = e.clientX - rect.left, my = e.clientY - rect.top;
        graphState.pan.x = mx - (mx - graphState.pan.x) * (next / graphState.zoom);
        graphState.pan.y = my - (my - graphState.pan.y) * (next / graphState.zoom);
        graphState.zoom = next;
    }, { passive: false });

    window.addEventListener('resize', () => { if (graphState) resizeGraphCanvas(); });
}

function filterGraph(value) {
    if (!graphState) return;
    graphState.filter = (value || '').toLowerCase();
}

function resetGraphView() {
    if (!graphState) return;
    graphState.pan = { x: 0, y: 0 };
    graphState.zoom = 1;
    graphState.alpha = 1;   // let it re-settle from wherever it was dragged
}
