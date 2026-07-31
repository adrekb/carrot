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

// Matches the reference being typed immediately before the caret.
const MENTION_PATTERN = /@(?:(file|model):)?([^\s@]*)$/;

let mentionMenu = null;
let mentionState = null;      // { kind, query, node, start, end, isTextarea }
let mentionIndex = 0;
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

/** Read what is being typed immediately before the caret, if anything. */
function readMentionAtCaret() {
    const fallback = document.getElementById('note-fallback');
    if (fallback && !fallback.classList.contains('hidden')
        && document.activeElement === fallback) {
        const caret = fallback.selectionStart;
        const match = MENTION_PATTERN.exec(fallback.value.slice(0, caret));
        if (!match) return null;
        return {
            kind: match[1] || '', query: match[2] || '', isTextarea: true,
            node: fallback, start: caret - match[0].length, end: caret,
        };
    }

    const selection = window.getSelection();
    if (!selection || !selection.rangeCount || !selection.isCollapsed) return null;
    const node = selection.anchorNode;
    const host = document.getElementById('note-editor-host');
    if (!node || !host || !host.contains(node) || node.nodeType !== Node.TEXT_NODE) return null;

    const caret = selection.anchorOffset;
    const match = MENTION_PATTERN.exec(node.textContent.slice(0, caret));
    if (!match) return null;
    return {
        kind: match[1] || '', query: match[2] || '', isTextarea: false,
        node, start: caret - match[0].length, end: caret,
    };
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
    mentionState = state;
    const menu = ensureMentionMenu();

    // Stage one: '@' typed, but no kind chosen yet.
    if (!state.kind) {
        const kinds = [
            { value: 'file', label: 'file', hint: 'cite a file as context' },
            { value: 'model', label: 'model', hint: 'pick the model for this note' },
        ].filter(k => !state.query || k.value.startsWith(state.query.toLowerCase()));
        if (!kinds.length) {
            hideMentionMenu();
            return;
        }
        renderMentionMenu(kinds.map(k => ({
            value: k.value, label: `/${k.label}`, source: k.hint, stage: 'kind',
        })));
        return;
    }

    // Stage two: a kind is chosen, so complete against real candidates.
    try {
        const data = await api(
            `/api/doc/candidates?kind=${encodeURIComponent(state.kind)}`
            + `&q=${encodeURIComponent(state.query)}`);
        renderMentionMenu((data.candidates || []).map(c => ({ ...c, stage: 'value' })));
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
}

/** Replace the in-progress mention with the chosen value. */
function chooseMention(item) {
    if (!mentionState || !item) return;
    const state = mentionState;
    const needsQuotes = item.stage === 'value' && /\s/.test(item.value);
    const replacement = item.stage === 'kind'
        ? `@${item.value}:`
        : `@${state.kind}:${needsQuotes ? `"${item.value}"` : item.value} `;

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
    // Choosing "file" or "model" leaves a half-finished reference, so reopen
    // the menu straight away on the values for that kind.
    if (item.stage === 'kind') setTimeout(updateMentionMenu, 0);
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
