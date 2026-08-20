// ================================================================
// Several documents open at once.
//
// Work held exactly one. Opening a second closed the first — not visibly, it
// just stopped being on screen — so anything that needs two documents at the
// same time (writing from notes, reconciling two drafts, copying a table out
// of one and into another) meant going back to the grid, finding the other
// one, opening it, and losing your place in the first.
//
// The model is a browser's, because that is the one everybody already has:
// a strip of tabs, the active one lit, a middle-click or an × to close, and
// closing the last one puts you back at the grid rather than at a blank
// editor with nothing in it.
//
// Two things are deliberately *not* browser-like:
//
//   * A document opens once. Asking for one already open switches to its tab
//     instead of making a second — two tabs on one file is two editors on one
//     autosave, and the second one to save wins.
//   * The strip is absent with one document open. A single tab is chrome that
//     explains itself and nothing else; it appears when there is a choice to
//     make.
//
// Switching flushes the pending save first. Autosave is on an 800ms timer, so
// leaving a document within a second of typing in it would otherwise drop the
// last thing typed — which is exactly the moment somebody switches away.
// ================================================================

const DOC_TABS_KEY = 'carrot-open-docs';
const DOC_TABS_MAX = 12;

// {id, title, format}. Titles are kept here so the strip can draw before any
// of them have been fetched — reopening the app should not blink through a
// row of "Untitled".
let openDocs = [];

function loadOpenDocs() {
    try {
        const stored = JSON.parse(localStorage.getItem(DOC_TABS_KEY) || '[]');
        openDocs = Array.isArray(stored) ? stored.filter(d => d && d.id).slice(0, DOC_TABS_MAX) : [];
    } catch (_) {
        openDocs = [];
    }
}

function saveOpenDocs() {
    try {
        localStorage.setItem(DOC_TABS_KEY, JSON.stringify(openDocs.slice(0, DOC_TABS_MAX)));
    } catch (_) {}
}

// Called by openNote once it knows what it opened. Not before: the format
// decides which editor mounts, and a tab that named the wrong one would send
// you to a different pane than the one you left.
function noteOpened(note) {
    if (!note || !note.id) return;
    const existing = openDocs.find(d => d.id === note.id);
    if (existing) {
        existing.title = note.title || existing.title || 'Untitled';
        existing.format = note.format || existing.format || 'markdown';
    } else {
        openDocs.push({
            id: note.id,
            title: note.title || 'Untitled',
            format: note.format || 'markdown',
        });
        // The oldest one that is not the one being opened. A cap that could
        // close the document you just asked for would be a cap that fights
        // the click that triggered it.
        while (openDocs.length > DOC_TABS_MAX) {
            const victim = openDocs.findIndex(d => d.id !== note.id);
            openDocs.splice(victim === -1 ? 0 : victim, 1);
        }
    }
    saveOpenDocs();
    renderDocTabs();
}

// The title changes as you type it, and a strip that only updated on reopen
// would show the old name for the rest of the session.
function docTabTitleChanged(id, title) {
    const tab = openDocs.find(d => d.id === id);
    if (!tab || tab.title === title) return;
    tab.title = title || 'Untitled';
    saveOpenDocs();
    renderDocTabs();
}

async function switchToDoc(id) {
    if (id === currentNoteId) return;
    // Before anything else. See the note at the top: the autosave timer is
    // 800ms and people switch documents faster than that.
    await flushPendingNoteSave();
    await openNote(id);
}

async function closeDoc(id, event) {
    if (event) { event.stopPropagation(); event.preventDefault(); }
    const index = openDocs.findIndex(d => d.id === id);
    if (index === -1) return;
    const wasActive = id === currentNoteId;
    if (wasActive) await flushPendingNoteSave();
    openDocs.splice(index, 1);
    saveOpenDocs();
    if (!wasActive) { renderDocTabs(); return; }
    // The neighbour, the way every editor does it: the one to the right, or
    // the one to the left when the closed tab was last.
    const next = openDocs[index] || openDocs[index - 1];
    if (next) {
        await openNote(next.id);
    } else {
        // Nothing left. The grid, not an empty editor pointed at no document.
        if (typeof showWriteStart === 'function') await showWriteStart();
        renderDocTabs();
    }
}

// Closing a document that no longer exists — deleted from the grid, or by the
// Delete button in its own toolbar — has to take its tab with it.
function forgetDoc(id) {
    const before = openDocs.length;
    openDocs = openDocs.filter(d => d.id !== id);
    if (openDocs.length !== before) { saveOpenDocs(); renderDocTabs(); }
}

const DOC_TAB_ICON = {
    markdown: 'i-note', latex: 'i-cap', canvas: 'i-palette', slides: 'i-grid',
};

function renderDocTabs() {
    const host = document.getElementById('doc-tabs');
    if (!host) return;
    // One document is not a choice, so it is not a strip.
    if (openDocs.length < 2) {
        host.classList.add('hidden');
        host.innerHTML = '';
        return;
    }
    host.classList.remove('hidden');
    host.innerHTML = openDocs.map(doc => {
        const on = doc.id === currentNoteId;
        return '<div class="doc-tab' + (on ? ' on' : '') + '"'
             +   ' data-doc="' + escHtml(doc.id) + '"'
             +   ' title="' + escHtml(doc.title || 'Untitled') + '">'
             +   '<svg class="ico"><use href="#'
             +     escHtml(DOC_TAB_ICON[doc.format] || 'i-note') + '"/></svg>'
             +   '<span class="doc-tab-name">' + escHtml(doc.title || 'Untitled') + '</span>'
             +   '<button class="doc-tab-close" aria-label="Close ' + escHtml(doc.title || 'Untitled') + '">×</button>'
             + '</div>';
    }).join('');
    for (const tab of host.querySelectorAll('.doc-tab')) {
        const id = tab.dataset.doc;
        tab.onclick = () => switchToDoc(id);
        tab.querySelector('.doc-tab-close').onclick = (e) => closeDoc(id, e);
        // Middle-click closes, which is the one browser gesture people try
        // without being told.
        tab.onauxclick = (e) => { if (e.button === 1) closeDoc(id, e); };
    }
}

document.addEventListener('DOMContentLoaded', () => {
    loadOpenDocs();
    renderDocTabs();
});
