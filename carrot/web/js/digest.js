// ================================================================
// The summary — a conversation as a document you can hand to another one.
//
// Carrying Tuesday's chat into today's meant scrolling up, selecting several
// hundred lines and pasting them into the box. That is the wrong object twice
// over: a transcript is mostly the *shape* of a conversation rather than what
// it concluded, and pasting one spends the context window on prose the model
// then has to re-derive the point from.
//
// So a conversation can produce a short `.md` — what it was about, what was
// decided, what is still open — and that file goes into the attachment tray
// like any other, which is the whole reason it is a file with a name rather
// than a paragraph in a popover. `stageDocument` is the same function the
// Write tab uses to send a note to chat, so a summary and a document arrive in
// the composer as the same kind of thing.
//
// **Why the icon rather than a row in a menu.** It is a property of the
// conversation you are looking at, so it lives beside its title, and it is
// only ever meaningful while a conversation is open — with none open there is
// nothing to summarise and the button is not drawn at all. The ⓘ inside says
// what the icon means, because an unlabelled glyph in a corner is a thing
// people learn by pressing, and this one costs a model call to press.
//
// **What the dot means.** The document is older than the conversation: turns
// have happened since it was written. A summary that is quietly out of date is
// worse than none, because it is the one you would attach.
// ================================================================

let digestState = null;
let digestBusy = false;

/** Ask what summary this conversation has. Cheap — it never runs a model. */
async function loadDigest() {
    if (!currentConversationId) {
        digestState = null;
        return null;
    }
    try {
        digestState = await api(`/api/conversations/${currentConversationId}/digest`);
    } catch (_) {
        digestState = null;
    }
    return digestState;
}

/** The button beside the chat title: present, and what it is saying. */
async function syncDigestButton() {
    const button = document.getElementById('digest-btn');
    if (!button) return;
    // Nothing to summarise before the conversation exists. Hidden rather than
    // disabled: a control that can never be pressed on a blank chat is one
    // more thing on a screen whose whole job is to be empty.
    if (!currentConversationId) {
        button.classList.add('hidden');
        document.getElementById('digest-pop')?.classList.add('hidden');
        digestState = null;
        return;
    }
    button.classList.remove('hidden');
    await loadDigest();
    renderDigestButton();
}

function renderDigestButton() {
    const button = document.getElementById('digest-btn');
    const dot = document.getElementById('digest-dot');
    if (!button) return;
    const has = !!(digestState && digestState.exists);
    const stale = !!(digestState && digestState.stale);
    button.classList.toggle('on', has);
    if (dot) dot.classList.toggle('hidden', !stale);
    button.title = !has
        ? 'Summary — write a short .md of this chat you can attach to another one'
        : (stale
            ? 'Summary — out of date, there have been turns since it was written'
            : 'Summary — a short .md of this chat you can attach to another one');
}

function closeDigestPop() {
    document.getElementById('digest-pop')?.classList.add('hidden');
}

async function toggleDigestPop() {
    const pop = document.getElementById('digest-pop');
    if (!pop) return;
    const opening = pop.classList.contains('hidden');
    pop.classList.toggle('hidden');
    if (!opening) return;
    // Re-read on open rather than trusting what the button last knew: the
    // question being asked is "is this still true", and the answer changes
    // with every turn.
    await loadDigest();
    renderDigestButton();
    renderDigestPop();
}

function renderDigestPop() {
    const body = document.getElementById('digest-body');
    const foot = document.getElementById('digest-foot');
    if (!body || !foot) return;

    if (digestBusy) {
        body.innerHTML = '<div class="digest-empty">Reading the conversation…</div>';
        foot.innerHTML = '';
        return;
    }
    if (!digestState) {
        body.innerHTML = '<div class="digest-empty">Could not read this conversation.</div>';
        foot.innerHTML = '';
        return;
    }
    if (!digestState.exists) {
        // The offer, with its cost stated. Writing one reads the thread through
        // a model, and a button that silently spends thirty seconds is a button
        // people press twice.
        body.innerHTML = '<div class="digest-empty">'
            + 'No summary yet. Writing one reads the '
            + digestState.messages + ' message' + (digestState.messages === 1 ? '' : 's')
            + ' in this chat and condenses them into a short markdown file.'
            + '</div>';
        foot.innerHTML = '<button class="digest-act primary" data-act="write">Write it</button>';
        wireDigestActions();
        return;
    }

    const stale = digestState.stale;
    body.innerHTML =
        (stale ? '<div class="digest-stale">Out of date — there have been turns '
                 + 'since this was written.</div>' : '')
        + '<div class="digest-name">' + escHtml(digestState.filename) + '</div>'
        + '<div class="digest-md md">' + mdToHtml(digestState.markdown || '') + '</div>';
    foot.innerHTML = [
        '<button class="digest-act primary" data-act="new">New chat with this</button>',
        '<button class="digest-act" data-act="attach">Attach here</button>',
        '<button class="digest-act" data-act="copy">Copy</button>',
        '<button class="digest-act" data-act="write">'
            + (stale ? 'Update' : 'Rewrite') + '</button>',
    ].join('');
    wireDigestActions();
}

function wireDigestActions() {
    for (const button of document.querySelectorAll('#digest-foot .digest-act')) {
        button.onclick = () => {
            const act = button.dataset.act;
            if (act === 'write') return writeDigest();
            if (act === 'attach') return attachDigest(false);
            if (act === 'new') return attachDigest(true);
            if (act === 'copy') return copyDigest();
        };
    }
}

/** Write or rewrite it. The one action here that costs a model call. */
async function writeDigest() {
    if (!currentConversationId || digestBusy) return;
    digestBusy = true;
    renderDigestPop();
    try {
        digestState = await api(`/api/conversations/${currentConversationId}/digest`,
                                { method: 'POST' });
    } catch (e) {
        digestBusy = false;
        const body = document.getElementById('digest-body');
        if (body) {
            body.innerHTML = '<div class="digest-empty">Could not write it — '
                + escHtml(e.message || 'the request failed') + '</div>';
        }
        return;
    }
    digestBusy = false;
    renderDigestButton();
    renderDigestPop();
}

/** Put it in the composer's tray, here or in a fresh chat.
 *
 * `fresh` first, because the ordinary use is carrying this thread into the
 * next one — and `newChat` empties the transcript but leaves the tray alone,
 * so the order is: clear the room, then put the file on the table.
 */
function attachDigest(fresh) {
    if (!digestState || !digestState.markdown) return;
    const name = digestState.filename;
    const text = digestState.markdown;
    closeDigestPop();
    if (fresh) newChat();
    if (typeof stageDocument === 'function' && stageDocument(name, text)) {
        switchTab('workspace');
        document.getElementById('cmd-input')?.focus();
    }
}

async function copyDigest() {
    if (!digestState || !digestState.markdown) return;
    try {
        await navigator.clipboard.writeText(digestState.markdown);
    } catch (_) { /* no clipboard permission — the text is on screen anyway */ }
    closeDigestPop();
}

// Clicking away closes it, the same rule every other popover in the app
// follows. `mousedown` rather than `click`, so it closes on the press.
document.addEventListener('mousedown', (event) => {
    if (!event.target.closest('#digest-picker')) closeDigestPop();
});
