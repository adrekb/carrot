// ================================================================
// Doc to agent — send a note to the model, with @file and @model
// references resolved at send time.
//
// The '@' menu is implemented at the DOM level rather than as a Milkdown
// plugin, so the same code drives the WYSIWYG editor and the plain-textarea
// fallback. Text is inserted through execCommand('insertText'), which fires
// the beforeinput/input events ProseMirror listens for — writing to the DOM
// directly would leave the editor's internal state out of sync.
// ================================================================

// Matches the reference being typed immediately before the caret. Everything
// after '@/' is slash-delimited, which is what lets the menu walk through
// kind -> provider -> model one segment at a time.
const MENTION_PATTERN = /@\/?([a-z]*)(?:\/([^\s@]*))?$/;

let mentionMenu = null;
let mentionState = null;      // { stage, kind, provider, query, node, start, end }
let mentionIndex = 0;
let lastMentionKey = '';
let docParseTimer = null;
let lastParsedText = '';

// ---------- Sending ----------

function noteSelectionOrBody() {
    const fallback = document.getElementById('note-fallback');
    if (fallback && !fallback.classList.contains('hidden')) {
        const { selectionStart: start, selectionEnd: end, value } = fallback;
        if (end > start) return { text: value.slice(start, end), partial: true };
        return { text: value, partial: false };
    }
    const selected = (window.getSelection()?.toString() || '').trim();
    const host = document.getElementById('note-editor-host');
    if (selected && host && host.contains(window.getSelection().anchorNode)) {
        return { text: selected, partial: true };
    }
    return { text: getEditorMarkdown(), partial: false };
}

async function sendDocToAgent() {
    if (!currentNoteId) return;
    const { text, partial } = noteSelectionOrBody();
    if (!text.trim()) {
        setNoteStatus('nothing to send');
        return;
    }
    // Send what is on disk, not what is in the buffer — the agent may read the
    // very file being edited, and a citation should never see a stale version.
    await saveNoteNow();

    const title = document.getElementById('note-title').value.trim() || 'Untitled note';
    switchTab('workspace');
    clearChatEmpty();
    appendMessage('user', text);
    if (!currentConversationId) {
        document.getElementById('chat-title').textContent =
            (partial ? `${title} (selection)` : title).slice(0, 42);
    }
    await streamTurn('/api/doc/send', {
        text,
        note_id: currentNoteId,
        title: partial ? `${title} (selection)` : title,
        conversation_id: currentConversationId,
    }, null);
}

// ---------- Reference preview ----------

function scheduleDocParse() {
    clearTimeout(docParseTimer);
    docParseTimer = setTimeout(refreshDocReferences, 500);
}

async function refreshDocReferences() {
    const bar = document.getElementById('doc-refs');
    if (!bar || !currentNoteId) return;
    // A reference being typed is always momentarily incomplete. Reporting that
    // back as a problem while the picker is still open is just noise.
    if (mentionMenu && !mentionMenu.classList.contains('hidden')) {
        scheduleDocParse();
        return;
    }
    const text = getEditorMarkdown();
    if (text === lastParsedText) return;
    lastParsedText = text;

    if (!text.includes('@')) {
        bar.innerHTML = '';
        bar.classList.add('hidden');
        return;
    }
    try {
        const parsed = await api('/api/doc/parse', {
            method: 'POST', body: JSON.stringify({ text }),
        });
        renderDocReferences(parsed);
    } catch (_) {
        bar.classList.add('hidden');
    }
}

function renderDocReferences(parsed) {
    const bar = document.getElementById('doc-refs');
    if (!bar) return;
    const refs = parsed.references || [];
    if (!refs.length) {
        bar.innerHTML = '';
        bar.classList.add('hidden');
        return;
    }
    bar.classList.remove('hidden');
    const chips = refs.map(ref => `
        <span class="doc-ref ${ref.ok ? 'ok' : 'bad'}" title="${escHtml(ref.detail || '')}">
            <span class="mono">${escHtml(ref.raw)}</span>
            ${ref.detail ? `<span class="doc-ref-detail">${escHtml(ref.detail)}</span>` : ''}
        </span>`).join('');
    const route = parsed.route
        ? `<span class="doc-ref route">will run on
             <span class="mono">${escHtml(parsed.route.model)}</span>
             (${escHtml(parsed.route.local ? 'on-device' : parsed.route.provider)})</span>`
        : '';
    bar.innerHTML = chips + route;
}

// ---------- The '@' menu ----------

function activeEditable() {
    const fallback = document.getElementById('note-fallback');
    if (fallback && !fallback.classList.contains('hidden')) return fallback;
    return document.getElementById('note-editor-host');
}

/**
 * Work out which of the three stages the caret is in.
 *
 *   @/            -> kind      (file | model)
 *   @/model/      -> provider  (openai, google, anthropic, local, …)
 *   @/model/o…/   -> model     (whatever that provider serves for your key)
 *   @/file/       -> file      (the query may itself contain slashes)
 */
function describeMention(match) {
    const kind = (match[1] || '').toLowerCase();
    const rest = match[2];

    if (rest === undefined) return { stage: 'kind', kind: '', query: kind };
    if (kind !== 'file' && kind !== 'model') return null;
    if (kind === 'file') return { stage: 'file', kind, query: rest };

    const slash = rest.indexOf('/');
    if (slash === -1) return { stage: 'provider', kind, provider: '', query: rest };
    return {
        stage: 'model', kind,
        provider: rest.slice(0, slash), query: rest.slice(slash + 1),
    };
}

/** Read what is being typed immediately before the caret, if anything. */
function readMentionAtCaret() {
    const fallback = document.getElementById('note-fallback');
    let node, caret, isTextarea;

    if (fallback && !fallback.classList.contains('hidden')
        && document.activeElement === fallback) {
        node = fallback;
        caret = fallback.selectionStart;
        isTextarea = true;
    } else {
        const selection = window.getSelection();
        if (!selection || !selection.rangeCount || !selection.isCollapsed) return null;
        const host = document.getElementById('note-editor-host');
        node = selection.anchorNode;
        if (!node || !host || !host.contains(node) || node.nodeType !== Node.TEXT_NODE) return null;
        caret = selection.anchorOffset;
        isTextarea = false;
    }

    const text = isTextarea ? node.value : node.textContent;
    const match = MENTION_PATTERN.exec(text.slice(0, caret));
    if (!match) return null;
    const described = describeMention(match);
    if (!described) return null;
    return { ...described, node, isTextarea, start: caret - match[0].length, end: caret };
}

function ensureMentionMenu() {
    if (mentionMenu) return mentionMenu;
    mentionMenu = document.createElement('div');
    mentionMenu.className = 'mention-menu hidden';
    document.body.appendChild(mentionMenu);
    return mentionMenu;
}

function hideMentionMenu() {
    mentionState = null;
    mentionIndex = 0;
    if (mentionMenu) mentionMenu.classList.add('hidden');
}

function positionMentionMenu() {
    const menu = ensureMentionMenu();
    let rect;
    if (mentionState?.isTextarea) {
        rect = mentionState.node.getBoundingClientRect();
        rect = { left: rect.left + 12, bottom: rect.top + 28 };
    } else {
        const range = window.getSelection()?.rangeCount
            ? window.getSelection().getRangeAt(0).cloneRange() : null;
        const box = range?.getBoundingClientRect();
        rect = box && (box.left || box.bottom)
            ? { left: box.left, bottom: box.bottom }
            : { left: 200, bottom: 200 };
    }
    menu.style.left = `${Math.min(rect.left, window.innerWidth - 320)}px`;
    menu.style.top = `${Math.min(rect.bottom + 6, window.innerHeight - 260)}px`;
}

async function updateMentionMenu() {
    const state = readMentionAtCaret();
    if (!state) {
        hideMentionMenu();
        return;
    }
    // Moving to a different stage or query starts the highlight over, so the
    // selection never lands somewhere arbitrary in a freshly filtered list.
    const key = `${state.stage}:${state.provider || ''}:${state.query || ''}`;
    if (key !== lastMentionKey) {
        lastMentionKey = key;
        mentionIndex = 0;
    }
    mentionState = state;

    // Stage one: '@' typed, but no kind chosen yet.
    if (state.stage === 'kind') {
        const kinds = [
            { value: 'file', hint: 'cite a file as context' },
            { value: 'model', hint: 'pick the model for this note' },
        ].filter(k => !state.query || k.value.startsWith(state.query.toLowerCase()));
        if (!kinds.length) {
            hideMentionMenu();
            return;
        }
        renderMentionMenu(kinds.map(k => ({
            value: k.value, label: `/${k.value}`, source: k.hint, stage: 'kind',
        })));
        return;
    }

    // Stages two and three: complete against real candidates. A model needs its
    // provider chosen first, so the server hands back the provider list until
    // one is named and only then the models that key can reach.
    const params = new URLSearchParams({
        kind: state.stage === 'file' ? 'file' : 'model',
        q: state.query || '',
    });
    if (state.stage === 'model') params.set('provider', state.provider);

    try {
        const data = await api(`/api/doc/candidates?${params}`);
        const stage = data.kind === 'provider' ? 'provider' : 'value';
        renderMentionMenu((data.candidates || []).map(c => ({ ...c, stage })));
    } catch (_) {
        hideMentionMenu();
    }
}

function renderMentionMenu(items) {
    const menu = ensureMentionMenu();
    if (!items.length) {
        menu.innerHTML = '<div class="mention-empty">No matches — type a value and keep going.</div>';
        menu.classList.remove('hidden');
        positionMentionMenu();
        return;
    }
    mentionIndex = Math.min(mentionIndex, items.length - 1);
    menu.dataset.count = items.length;
    menu._items = items;
    menu.innerHTML = items.map((item, i) => `
        <div class="mention-item ${i === mentionIndex ? 'active' : ''}" data-i="${i}">
            <span class="mention-label">${escHtml(item.label)}</span>
            <span class="mention-source">${escHtml(item.source || '')}</span>
        </div>`).join('');
    menu.querySelectorAll('.mention-item').forEach(el => {
        el.onmousedown = (event) => {
            event.preventDefault();
            chooseMention(items[Number(el.dataset.i)]);
        };
    });
    menu.classList.remove('hidden');
    positionMentionMenu();
    // A provider can serve dozens of models, so keep the highlight in view
    // rather than letting arrow keys walk it off the bottom of the list.
    menu.querySelector('.mention-item.active')?.scrollIntoView({ block: 'nearest' });
}

/** Replace the in-progress mention with the chosen value. */
function chooseMention(item) {
    if (!mentionState || !item) return;
    const state = mentionState;

    // The first two stages narrow the reference and reopen the menu; only the
    // last one completes it and moves the caret past a trailing space.
    let replacement;
    if (item.stage === 'kind') replacement = `@/${item.value}/`;
    else if (item.stage === 'provider') replacement = `@/model/${item.value}/`;
    else if (state.stage === 'file') {
        replacement = `@/file/${/\s/.test(item.value) ? `"${item.value}"` : item.value} `;
    } else replacement = `@/model/${item.value} `;

    if (state.isTextarea) {
        const element = state.node;
        const before = element.value.slice(0, state.start);
        const after = element.value.slice(state.end);
        element.value = before + replacement + after;
        const caret = before.length + replacement.length;
        element.setSelectionRange(caret, caret);
        element.dispatchEvent(new Event('input', { bubbles: true }));
    } else {
        const range = document.createRange();
        range.setStart(state.node, state.start);
        range.setEnd(state.node, state.end);
        const selection = window.getSelection();
        selection.removeAllRanges();
        selection.addRange(range);
        // execCommand keeps ProseMirror's document in sync; a direct DOM write
        // would not, and the editor would overwrite it on the next keystroke.
        document.execCommand('insertText', false, replacement);
    }

    hideMentionMenu();
    scheduleNoteSave();
    // A kind or a provider leaves a half-finished reference, so reopen the menu
    // straight away on whatever comes next.
    if (item.stage === 'kind' || item.stage === 'provider') setTimeout(updateMentionMenu, 0);
    else setTimeout(scheduleDocParse, 0);
}

function mentionKeydown(event) {
    if (!mentionState || !mentionMenu || mentionMenu.classList.contains('hidden')) return;
    const items = mentionMenu._items || [];
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        mentionIndex = (mentionIndex + (event.key === 'ArrowDown' ? 1 : -1) + items.length)
            % Math.max(items.length, 1);
        renderMentionMenu(items);
    } else if (event.key === 'Enter' || event.key === 'Tab') {
        if (!items.length) return;
        event.preventDefault();
        chooseMention(items[mentionIndex]);
    } else if (event.key === 'Escape') {
        event.preventDefault();
        hideMentionMenu();
    }
}

function initDocAgent() {
    // Capture phase, so the menu claims Enter/Arrow keys before the editor does.
    document.addEventListener('keydown', (event) => {
        const editable = activeEditable();
        if (!editable || !editable.contains(event.target) && event.target !== editable) return;
        mentionKeydown(event);
    }, true);

    document.addEventListener('input', (event) => {
        const editable = activeEditable();
        if (!editable) return;
        if (editable !== event.target && !editable.contains(event.target)) return;
        updateMentionMenu();
        scheduleDocParse();
    });

    document.addEventListener('click', (event) => {
        if (mentionMenu && !mentionMenu.contains(event.target)) hideMentionMenu();
    });
}

document.addEventListener('DOMContentLoaded', initDocAgent);
