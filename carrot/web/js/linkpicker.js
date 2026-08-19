// ================================================================
// `//` in the composer — point at a document you have already written.
//
// The thing this replaces is describing your own notes to the model. You have
// the Q3 plan open in the next tab, and the only way to get it into the answer
// was to select it, copy it, and paste a wall of it into a chat box — losing
// its title, and leaving the conversation carrying a copy that goes stale the
// moment you edit the original.
//
// So: type `//`, pick a document, and the message carries `[[Its Title]]`.
// That is the same link syntax notes already use, so a reference means the
// same thing wherever it is written; and the server reads the document at send
// time (`with_linked_documents`), which is what makes it a reference rather
// than a copy — edit the note afterwards and the next turn sees the new
// version.
//
// **The workspace is a filter, not a target.** You cannot link a workspace: it
// is not a document, and attaching one would mean attaching everything in it.
// What it is good for is the case this menu exists for — two hundred documents,
// four of which are the project you are in.
// ================================================================

// `[[` opens it, and `//` still does.
//
// `[[` is the primary because it is already this app's link syntax — it is
// literally what this picker *inserts*, and typing an opening bracket to be
// offered what goes inside it is as direct as a trigger gets. It also leaves
// `/` alone: `/` means commands in every app people arrive here from, and this
// composer's own placeholder has advertised `/ for skills` since before any of
// this existed.
//
// `//` is kept because it is what was asked for and it costs one branch — but
// only at the start of the message or after a space. Without that guard,
// typing `https://` opened a document picker over the URL somebody was in the
// middle of pasting, which is the kind of thing that makes a feature feel
// like it is in the way.
const LINK_TRIGGER = /(\[\[|(?:^|\s)\/\/)([^\[\]\n]{0,60})$/;

let linkState = null;         // {start, end, query} in #cmd-input
let linkItems = [];
let linkIndex = 0;
let linkWorkspace = '';       // '' is every workspace
let linkWorkspaces = [];
let linkTimer = null;

function ensureLinkMenu() {
    let menu = document.getElementById('link-menu');
    if (!menu) {
        menu = document.createElement('div');
        menu.id = 'link-menu';
        menu.className = 'link-menu hidden';
        document.body.appendChild(menu);
    }
    return menu;
}

function hideLinkMenu() {
    document.getElementById('link-menu')?.classList.add('hidden');
    linkState = null;
    linkItems = [];
}

function linkMenuOpen() {
    const menu = document.getElementById('link-menu');
    return !!menu && !menu.classList.contains('hidden');
}

// What is being typed immediately before the caret, if it is a `//`.
function readLinkAtCaret() {
    const input = document.getElementById('cmd-input');
    if (!input || document.activeElement !== input) return null;
    const caret = input.selectionStart;
    const match = LINK_TRIGGER.exec(input.value.slice(0, caret));
    if (!match) return null;
    // The `//` branch may have swallowed the space in front of it, and that
    // space is the user's text rather than part of the trigger — replacing it
    // would eat the gap between the previous word and the link.
    const trigger = match[1];
    const leading = trigger.length - (trigger.endsWith('[[') ? 2 : 2);
    return {
        start: caret - match[0].length + leading,
        end: caret,
        query: match[2] || '',
    };
}

function scheduleLinkMenu() {
    clearTimeout(linkTimer);
    // Debounced because every keystroke is a search across every document, and
    // the trigger is two characters somebody may well be typing on their way
    // to something else.
    linkTimer = setTimeout(updateLinkMenu, 120);
}

async function updateLinkMenu() {
    const state = readLinkAtCaret();
    if (!state) { hideLinkMenu(); return; }
    const fresh = !linkState || linkState.query !== state.query;
    linkState = state;
    if (fresh) linkIndex = 0;
    try {
        const params = new URLSearchParams({ q: state.query });
        if (linkWorkspace) params.set('workspace', linkWorkspace);
        const data = await api('/api/link/candidates?' + params);
        linkItems = data.documents || [];
        linkWorkspaces = data.workspaces || [];
    } catch (_) {
        hideLinkMenu();
        return;
    }
    renderLinkMenu();
}

const LINK_ICON = {
    markdown: 'i-note', latex: 'i-cap', canvas: 'i-palette', slides: 'i-grid',
};

function renderLinkMenu() {
    const menu = ensureLinkMenu();
    // The filter row stays even when the list under it is empty — it is very
    // often the reason the list is empty, and hiding it would leave somebody
    // looking at "no documents" with no way to see that they are filtered.
    const filters = '<div class="link-filters">'
        + '<button class="link-filter' + (linkWorkspace ? '' : ' on') + '"'
        +   ' data-ws="">All</button>'
        + linkWorkspaces.map(space =>
            '<button class="link-filter' + (space.id === linkWorkspace ? ' on' : '') + '"'
          + ' data-ws="' + escHtml(space.id) + '">' + escHtml(space.name) + '</button>').join('')
        + '</div>';

    const list = linkItems.length
        ? linkItems.map((doc, i) =>
            '<div class="link-item' + (i === linkIndex ? ' active' : '') + '" data-i="' + i + '">'
          +   '<svg class="ico"><use href="#' + (LINK_ICON[doc.format] || 'i-note') + '"/></svg>'
          +   '<span class="link-name">' + escHtml(doc.title) + '</span>'
          + '</div>').join('')
        : '<div class="link-empty">'
          + (linkWorkspace ? 'No documents in this workspace match.' : 'No documents match.')
          + '</div>';

    menu.innerHTML = filters + '<div class="link-list">' + list + '</div>';
    menu.querySelectorAll('.link-filter').forEach(button => {
        button.onmousedown = (e) => {
            e.preventDefault();
            linkWorkspace = button.dataset.ws;
            linkIndex = 0;
            updateLinkMenu();
        };
    });
    menu.querySelectorAll('.link-item').forEach(el => {
        el.onmousedown = (e) => { e.preventDefault(); chooseLink(linkItems[Number(el.dataset.i)]); };
    });
    menu.classList.remove('hidden');
    positionLinkMenu();
    menu.querySelector('.link-item.active')?.scrollIntoView({ block: 'nearest' });
}

// Above the composer, aligned to its left edge. The composer is fixed to the
// bottom of the window, so there is never room underneath it.
function positionLinkMenu() {
    const menu = document.getElementById('link-menu');
    const bar = document.getElementById('cmdbar');
    if (!menu || !bar) return;
    const box = bar.getBoundingClientRect();
    menu.style.left = Math.round(box.left) + 'px';
    menu.style.width = Math.round(Math.min(box.width, 420)) + 'px';
    menu.style.bottom = Math.round(window.innerHeight - box.top + 6) + 'px';
}

function chooseLink(doc) {
    if (!doc || !linkState) return;
    const input = document.getElementById('cmd-input');
    const before = input.value.slice(0, linkState.start);
    const after = input.value.slice(linkState.end);
    const inserted = `[[${doc.title}]] `;
    input.value = before + inserted + after;
    const caret = before.length + inserted.length;
    input.setSelectionRange(caret, caret);
    hideLinkMenu();
    input.focus();
    // The composer's own listeners do the rest: the Context chip recounts, and
    // the send button notices there is something to send.
    input.dispatchEvent(new Event('input'));
}

// Keys are handled before the composer's own, and only while the menu is up —
// Enter has to send the message every other time.
function linkMenuKeydown(event) {
    if (!linkMenuOpen() || !linkItems.length) {
        if (linkMenuOpen() && event.key === 'Escape') { hideLinkMenu(); return true; }
        return false;
    }
    if (event.key === 'ArrowDown') {
        linkIndex = (linkIndex + 1) % linkItems.length;
        renderLinkMenu(); return true;
    }
    if (event.key === 'ArrowUp') {
        linkIndex = (linkIndex - 1 + linkItems.length) % linkItems.length;
        renderLinkMenu(); return true;
    }
    if (event.key === 'Enter' || event.key === 'Tab') {
        chooseLink(linkItems[linkIndex]); return true;
    }
    if (event.key === 'Escape') { hideLinkMenu(); return true; }
    return false;
}

document.addEventListener('DOMContentLoaded', () => {
    const input = document.getElementById('cmd-input');
    if (!input) return;
    input.addEventListener('input', scheduleLinkMenu);
    input.addEventListener('blur', () => setTimeout(hideLinkMenu, 120));
    window.addEventListener('resize', () => { if (linkMenuOpen()) positionLinkMenu(); });
});
