// ================================================================
// The palette — one keystroke to everything
// ================================================================
//
// Four tabs is the right number of destinations and the wrong number of ways
// in. Everything the app can do is reachable, and reachable means "in a tab,
// behind a button, once you remember which tab" — which is fine when you know
// the app and is the whole problem when you are trying to do one thing quickly.
//
// Ctrl+K used to focus the composer. This does not take that away: the box is
// a text field, and typing something and pressing Enter with nothing selected
// starts a chat with it. The old keystroke still ends in asking a question; it
// just now also reaches documents, conversations, workspaces and actions
// without a second shortcut to learn.
//
// What it deliberately is not: a second navigation surface. There are no
// entries here that exist only here. Everything it offers is something you
// could already click, which is what keeps it a shortcut rather than a fifth
// place to look.

let paletteOpen = false;
let paletteItems = [];
let paletteCursor = 0;
// The recents load has a counter; the search does not need one.
//
// Both used to share a single sequence number, so each invalidated the other:
// typing bumped it and the in-flight recents load discarded itself, and the
// recents load bumped it and the in-flight search discarded itself. Splitting
// them fixed the recents half. The search half is guarded on the query string
// instead — see searchFromPalette, where the thing being guarded against is
// "is this still the question on screen", which the query answers directly and
// a counter only approximates.
let paletteRecentsSeq = 0;
let paletteTimer = null;
// Recents are read once per opening rather than per keystroke: they do not
// change while you type, and re-fetching them on every letter was the mistake
// already fixed once in the drive.
let paletteRecents = null;

// The things you can start. Kept as data so the palette, and anything else
// that wants a list of what Carrot can begin, agree about it.
const PALETTE_ACTIONS = [
    { id: 'new-chat', label: 'New chat', icon: 'i-chat', hint: 'Enter',
      run: () => { switchTab('workspace'); if (typeof newChat === 'function') newChat(); } },
    { id: 'new-doc', label: 'New document', icon: 'i-note',
      run: () => { switchTab('notes'); if (typeof newNote === 'function') newNote(); } },
    { id: 'new-workspace', label: 'New workspace', icon: 'i-folder-plus',
      run: () => { switchTab('notes');
                   if (typeof newDriveWorkspace === 'function') newDriveWorkspace(); } },
    { id: 'go-work', label: 'Go to Work', icon: 'i-folder', run: () => switchTab('notes') },
    { id: 'go-code', label: 'Go to Code', icon: 'i-terminal', run: () => switchTab('code') },
    { id: 'go-settings', label: 'Go to Settings', icon: 'i-gear', run: () => switchTab('settings') },
];

function openPalette() {
    const box = document.getElementById('palette');
    const input = document.getElementById('palette-input');
    if (!box || !input) return;
    paletteOpen = true;
    paletteRecents = null;
    paletteFound = null;
    paletteSearching = false;
    box.classList.remove('hidden');
    input.value = '';
    input.focus();
    renderPalette('');
    loadPaletteRecents();
}

function closePalette() {
    const box = document.getElementById('palette');
    if (!box) return;
    paletteOpen = false;
    box.classList.add('hidden');
    paletteItems = [];
    paletteCursor = 0;
}

function togglePalette() {
    if (paletteOpen) closePalette(); else openPalette();
}

// Conversations and documents, fetched once per opening.
//
// Documents come from the drive's own endpoint rather than a second listing:
// one place decides what "your work, most recent first" means, and it already
// merges documents with indexed files and sorts the whole set.
async function loadPaletteRecents() {
    const seq = ++paletteRecentsSeq;
    const [convs, items, spaces] = await Promise.all([
        api('/api/conversations?limit=8').catch(() => []),
        api('/api/work/items').then(b => b.items || []).catch(() => []),
        api('/api/work/places').then(b => b.workspaces || []).catch(() => []),
    ]);
    if (seq !== paletteRecentsSeq || !paletteOpen) return;
    paletteRecents = {
        conversations: (Array.isArray(convs) ? convs : []).slice(0, 8),
        documents: items.slice(0, 8),
        workspaces: spaces,
    };
    renderPalette(document.getElementById('palette-input')?.value || '');
}

// ================================================================
// Searching everything, not only what was recent
// ================================================================
//
// The recents lists are what you had open. They are not what you have — a
// document from March is not in the last eight of anything, and typing its name
// found nothing, which makes the box feel broken in exactly the case it should
// feel magic.
//
// `/api/search/all` already answers this: one query across conversations,
// indexed documents and memory, in one workspace scope. So the palette does not
// need its own index; it needs to ask.
let paletteFound = null;
let paletteSearching = false;

// How long the palette will wait for the server before giving up on it.
//
// `/api/search/all` embeds the query to search semantically, and embedding is a
// model call: measured at 4.1s here, and its client timeout is thirty seconds.
// A palette that waits for that is a palette that hangs — the box has to answer
// from what it already has and let the deeper results arrive if they arrive.
const PALETTE_SEARCH_TIMEOUT = 1500;

async function searchFromPalette(query) {
    paletteSearching = true;
    let found = null;
    try {
        found = await Promise.race([
            api('/api/search/all?limit=6&q=' + encodeURIComponent(query)),
            new Promise((_, reject) =>
                setTimeout(() => reject(new Error('search timed out')), PALETTE_SEARCH_TIMEOUT)),
        ]);
    } catch (err) {
        // Local matches are already on screen and stay there. This is a search
        // that did not add anything, not a failure worth telling anybody about.
        console.warn('palette: search did not return in time', err);
    }

    // Guarded on the query rather than on a counter.
    //
    // A slow answer for "th" must not land after "thesis" and show results for
    // a question nobody is looking at any more — but a sequence number is a
    // proxy for that, and a proxy that can be bumped by anything else sharing
    // it discards good answers instead. Comparing what was asked against what
    // is still in the box says exactly what the guard is for, and cannot be
    // wrong about it.
    const current = (document.getElementById('palette-input')?.value || '').trim();
    if (!paletteOpen || current !== query) return;

    // Only replaced when there is something to replace it with: a timed-out
    // search must not wipe results an earlier, faster one already put up.
    if (found) paletteFound = found;
    paletteSearching = false;
    renderPalette(current);
}

// Loose matching, so an action can be found by how somebody would type it in
// a hurry. "sett" finds Go to Settings, "new doc" finds New document, and
// "gowork" finds Go to Work — the characters in order, not necessarily
// adjacent. Substring first because an exact run should always outrank a
// scattered one.
function paletteMatches(text, needle) {
    if (!needle) return true;
    const hay = String(text || '').toLowerCase();
    if (hay.includes(needle)) return true;
    // Every word of the query somewhere in the text, in any order: "doc new"
    // should still find New document.
    const words = needle.split(/\s+/).filter(Boolean);
    if (words.length > 1 && words.every(w => hay.includes(w))) return true;
    // Subsequence, for the abbreviations people actually type.
    let at = 0;
    for (const ch of needle.replace(/\s+/g, '')) {
        at = hay.indexOf(ch, at);
        if (at === -1) return false;
        at += 1;
    }
    return true;
}

// How good a match is, so the list can be ordered rather than merely filtered.
// A title that starts with what you typed is what you meant; a subsequence hit
// buried in the middle is a guess, and guesses go last.
function paletteScore(text, needle) {
    if (!needle) return 0;
    const hay = String(text || '').toLowerCase();
    const at = hay.indexOf(needle);
    if (at === 0) return 100;
    if (at > 0) return 70 - Math.min(at, 20);
    return 20;
}

// Six static actions took nearly half the useful height every time it opened,
// and once you have typed something they are almost never what you want. So the
// list is ranked rather than fixed: before typing it is a short shelf of what
// you start and what you were last in; once typing begins, matches take over
// and actions shrink to the ones that actually match.
const PALETTE_ACTIONS_AT_REST = 4;

// Where a result came from, when that is what tells two of them apart. Two
// documents called "notes" are indistinguishable without it.
function paletteWhere(item) {
    if (item.workspace_name) return item.workspace_name;
    if (item.path) {
        const parts = String(item.path).split(/[\\/]/).filter(Boolean);
        return parts.slice(-3, -1).join(' › ');
    }
    return '';
}

function paletteGroups(query) {
    const needle = query.trim().toLowerCase();
    const groups = [];
    const seen = new Set();

    // Deduped across sources: a document that is both in the recents shelf and
    // in the search results is one thing, and listing it twice makes the
    // palette look like it cannot count.
    const add = (list, item) => {
        if (seen.has(item.id)) return;
        seen.add(item.id);
        list.push(item);
    };

    const docItem = (d) => ({
        id: 'doc:' + d.id, label: d.name || d.title || 'Untitled',
        icon: d.kind === 'file' ? 'i-archive' : 'i-note',
        hint: writeWhen(d.updated), where: paletteWhere(d),
        score: paletteScore(d.name || d.title, needle),
        run: () => { switchTab('notes');
                     if (typeof openDriveItem === 'function') openDriveItem(d.kind, d.id); },
    });
    const convItem = (c) => ({
        id: 'conv:' + c.id, label: c.title || 'Untitled chat', icon: 'i-chat',
        hint: writeWhen(c.updated_at || c.created_at || c.timestamp),
        where: c.workspace_name || '',
        score: paletteScore(c.title, needle),
        run: () => { switchTab('workspace');
                     if (typeof openConversation === 'function') openConversation(c.id); },
    });

    const actions = PALETTE_ACTIONS
        .filter(a => paletteMatches(a.label, needle))
        .map(a => ({ ...a, score: paletteScore(a.label, needle) }))
        .sort((x, y) => y.score - x.score);

    const convs = [], docs = [], mems = [], spaces = [];
    for (const c of (paletteRecents?.conversations || [])) {
        if (paletteMatches(c.title || c.id, needle)) add(convs, convItem(c));
    }
    for (const d of (paletteRecents?.documents || [])) {
        if (paletteMatches(d.name, needle)) add(docs, docItem(d));
    }
    for (const w of (paletteRecents?.workspaces || [])) {
        if (!paletteMatches(w.name, needle)) continue;
        add(spaces, { id: 'ws:' + w.id, label: w.name, icon: 'i-folder',
            hint: w.count ? w.count + ' item' + (w.count === 1 ? '' : 's') : 'Empty',
            score: paletteScore(w.name, needle),
            run: () => { switchTab('notes');
                         if (typeof setDriveWorkspace === 'function') setDriveWorkspace(w.id); } });
    }

    // Everything the server found, which reaches past the recents shelf.
    if (needle && paletteFound) {
        for (const c of (paletteFound.conversations || [])) {
            // A hit here is a *message*, so it carries the conversation's id
            // and title under different names than the conversation list uses.
            add(convs, convItem({ id: c.conversation_id || c.id,
                                  title: c.conversation_title || c.title,
                                  updated_at: c.timestamp }));
        }
        for (const d of (paletteFound.documents || [])) {
            add(docs, { id: 'file:' + d.path, label: (d.path || '').split(/[\\/]/).pop(),
                icon: 'i-archive', hint: '', where: paletteWhere(d),
                score: paletteScore((d.path || '').split(/[\\/]/).pop(), needle),
                run: () => switchTab('files') });
        }
        for (const m of (paletteFound.memories || [])) {
            add(mems, { id: 'mem:' + (m.id || m.content),
                label: String(m.content || '').slice(0, 90),
                icon: 'i-brain', hint: m.kind || '', where: m.subject || '',
                score: paletteScore(m.content, needle),
                run: () => switchTab('settings') });
        }
    }

    for (const list of [convs, docs, mems, spaces]) list.sort((x, y) => y.score - x.score);

    // Results first once you have typed, because then you had something
    // specific in mind. Actions first at rest, because opening the palette
    // without typing is almost always "start something".
    const resultGroups = [
        { title: 'Your work', items: docs },
        { title: 'Conversations', items: convs },
        { title: 'Memory', items: mems },
        { title: 'Workspaces', items: spaces },
    ].filter(g => g.items.length);

    if (needle) {
        for (const g of resultGroups) groups.push(g);
        if (actions.length) groups.push({ title: 'Actions', items: actions });
        if (paletteSearching) groups.push({ title: '', items: [], loading: true });
        // Never a dead end. Asking is what the box is for, and "no results" for
        // a question you could simply ask would be the palette refusing its own
        // purpose — so this is last rather than first, where it was crowding
        // out real matches.
        groups.push({ title: '', items: [{
            id: 'ask', label: 'Ask Carrot about “' + query.trim() + '”',
            icon: 'i-send', hint: 'Enter', run: () => askFromPalette(query.trim()) }] });
    } else {
        groups.push({ title: 'Actions', items: actions.slice(0, PALETTE_ACTIONS_AT_REST) });
        for (const g of resultGroups) groups.push(g);
        if (paletteRecents === null) groups.push({ title: '', items: [], loading: true });
    }
    return groups;
}

function renderPalette(query) {
    const host = document.getElementById('palette-list');
    if (!host) return;
    const groups = paletteGroups(query);
    paletteItems = groups.flatMap(g => g.items);
    if (paletteCursor >= paletteItems.length) paletteCursor = 0;

    host.innerHTML = groups.map(g =>
        (g.title ? '<div class="palette-group">' + escHtml(g.title) + '</div>' : '')
        + (g.loading ? '<div class="palette-loading">Looking…</div>' : '')
        + g.items.map(item => {
            const index = paletteItems.indexOf(item);
            return '<button class="palette-item' + (index === paletteCursor ? ' on' : '')
                + '" role="option" data-index="' + index + '">'
                + '<svg class="ico"><use href="#' + item.icon + '"/></svg>'
                + '<span class="palette-text">'
                + '<span class="palette-label">' + escHtml(item.label) + '</span>'
                // Where it came from, when that is what tells two results
                // apart — two documents called "notes" are otherwise
                // indistinguishable.
                + (item.where ? '<span class="palette-where">' + escHtml(item.where) + '</span>' : '')
                + '</span>'
                + (item.hint ? '<span class="palette-hint">' + escHtml(item.hint) + '</span>' : '')
                + '</button>';
        }).join('')).join('');

    if (!paletteItems.length) {
        host.innerHTML = '<div class="palette-loading">Nothing matches that.</div>';
    }
    for (const el of host.querySelectorAll('.palette-item')) {
        // mousedown, not click: the input has focus, and a click that first
        // blurs the field can close the palette out from under itself.
        el.onmousedown = (e) => { e.preventDefault(); runPaletteItem(+el.dataset.index); };
        el.onmouseenter = () => { paletteCursor = +el.dataset.index; paintPaletteCursor(); };
    }
}

// Only the highlight moves on arrow keys. Re-rendering the whole list would
// rebuild every row to move one class, and would fight the mouse.
function paintPaletteCursor() {
    const host = document.getElementById('palette-list');
    if (!host) return;
    for (const el of host.querySelectorAll('.palette-item')) {
        const on = +el.dataset.index === paletteCursor;
        el.classList.toggle('on', on);
        if (on) el.scrollIntoView({ block: 'nearest' });
    }
}

function runPaletteItem(index) {
    const item = paletteItems[index];
    if (!item) return;
    closePalette();
    try { item.run(); } catch (err) { console.warn('palette action failed', err); }
}

// Enter on typed text: put the question in the composer rather than sending it.
//
// Sending straight from the palette would mean a stray Enter fires a model
// call, and there is no undo for that. The composer is one more keystroke and
// it is the keystroke that makes it deliberate.
function askFromPalette(text) {
    switchTab('workspace');
    const input = document.getElementById('cmd-input');
    if (!input) return;
    input.value = text;
    input.focus();
    input.dispatchEvent(new Event('input', { bubbles: true }));
}

document.addEventListener('keydown', (e) => {
    const ctrl = e.ctrlKey || e.metaKey;
    if (ctrl && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        togglePalette();
        return;
    }
    if (!paletteOpen) return;
    if (e.key === 'Escape') { e.preventDefault(); closePalette(); return; }
    if (e.key === 'ArrowDown') {
        e.preventDefault();
        paletteCursor = paletteItems.length ? (paletteCursor + 1) % paletteItems.length : 0;
        paintPaletteCursor();
    } else if (e.key === 'ArrowUp') {
        e.preventDefault();
        paletteCursor = paletteItems.length
            ? (paletteCursor - 1 + paletteItems.length) % paletteItems.length : 0;
        paintPaletteCursor();
    } else if (e.key === 'Enter') {
        e.preventDefault();
        if (paletteItems.length) runPaletteItem(paletteCursor);
    }
}, true);

document.addEventListener('DOMContentLoaded', () => {
    const input = document.getElementById('palette-input');
    if (!input) return;
    input.addEventListener('input', () => {
        // Debounced for the same reason the drive's search is: a keystroke is
        // not a question, and eight of them are not eight questions.
        clearTimeout(paletteTimer);
        // The local lists re-filter immediately — that is free and it is what
        // makes typing feel connected to the screen. The server search is what
        // waits, because that is the part with a socket on the end of it.
        paletteCursor = 0;
        renderPalette(input.value);
        paletteTimer = setTimeout(() => {
            const query = input.value.trim();
            if (query.length >= 2) searchFromPalette(query);
            else { paletteFound = null; renderPalette(input.value); }
        }, 160);
    });
    const box = document.getElementById('palette');
    if (box) box.addEventListener('mousedown', (e) => {
        if (e.target === box) closePalette();   // the backdrop, not the card
    });
});
