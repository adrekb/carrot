// ===== Context · what the model is actually receiving =====
//
// The system half of a prompt is assembled from eight independent switches —
// answer style, the search directive, a skill, a document, the workspace's
// rules, the calendar, the screen roster, memory, the rolling summary — and
// until now the only way to find out which of them fired was to read app.py.
// That is the wrong place for it: "why does it know about my calendar" and
// "why did it not use what it remembers" are questions asked at the composer,
// about the turn you are typing.
//
// The counts come from the server building the real prompt with the model call
// left off, so this reports what the turn *would* carry rather than a second
// implementation's opinion about it.
//
// It reads the composer, because memory recall is a search against what you
// are about to ask: an inspector that previewed the empty string would always
// say Carrot remembers nothing about you, which is both wrong and the most
// discouraging possible thing for it to say.

let contextData = null;
let contextTimer = null;

function toggleContextPop() {
    const pop = document.getElementById('context-pop');
    if (!pop) return;
    const opening = pop.classList.contains('hidden');
    pop.classList.toggle('hidden');
    if (opening) loadContext();
}

function closeContextPop() {
    document.getElementById('context-pop')?.classList.add('hidden');
}

async function loadContext() {
    const params = new URLSearchParams();
    const input = document.getElementById('cmd-input');
    if (input && input.value.trim()) params.set('message', input.value.trim().slice(0, 2000));
    if (typeof currentConversationId === 'string' && currentConversationId) {
        params.set('conversation_id', currentConversationId);
    }
    try {
        contextData = await api('/api/context?' + params.toString());
    } catch (_) {
        contextData = null;
    }
    renderContext();
}

function renderContext() {
    const label = document.getElementById('context-label');
    const list = document.getElementById('context-list');
    const foot = document.getElementById('context-foot');
    if (!contextData) {
        if (label) label.textContent = 'Context';
        if (list) list.innerHTML = '<div class="context-empty">Could not read the prompt.</div>';
        return;
    }
    // The count is of what is actually going, not of what is switched on: a
    // calendar that is enabled and empty is not an item in the prompt.
    if (label) {
        label.textContent = 'Context · ' + contextData.items
            + ' item' + (contextData.items === 1 ? '' : 's');
    }
    if (!list) return;
    list.innerHTML = contextData.sources.map(source => {
        // A source contributing nothing this turn is shown, greyed, rather
        // than hidden. A list that changes length as you type is a list you
        // cannot learn, and "memory: nothing this turn" is information.
        const off = !source.enabled;
        const idle = source.present ? '' : ' idle';
        // Two controls per row, because they answer different questions. The
        // tick is "should this go" — that was the whole row before. The eye is
        // "what *is* this", which had no answer at all: a row reading
        // "Memory · 1.2k chars" tells you something is being sent about you
        // and not one word of what.
        //
        // The text opens in a window rather than in the row. Ten rows that can
        // each grow four hundred words is a list that reorders itself under
        // the hand every time you look at one — and the picker is a thing you
        // glance at over the composer, not a page.
        return `
          <div class="context-item${off ? ' off' : ''}${idle}">
            <div class="context-row">
              <button class="context-check${source.toggleable ? '' : ' fixed'}"
                      ${source.toggleable ? '' : 'disabled'}
                      data-toggle="${escHtml(source.id)}"
                      aria-pressed="${!off}"
                      title="${source.toggleable
                                ? (off ? 'Switch this back on' : 'Leave this out of the prompt')
                                : 'Always sent — this is what you just asked'}">${off ? '' : '✓'}</button>
              <span class="context-name" title="${escHtml(source.detail)}">${escHtml(source.label)}</span>
              <span class="context-size">${source.present ? contextSize(source.chars) : '—'}</span>
              ${source.present ? `
              <button class="context-peek" data-open="${escHtml(source.id)}"
                      title="See exactly what this sends">
                <svg class="ico"><use href="#i-eye"/></svg>
              </button>` : '<span class="context-peek-gap"></span>'}
            </div>
          </div>`;
    }).join('');
    for (const cell of list.querySelectorAll('.context-check:not([disabled])')) {
        cell.onclick = () => setContextSource(cell.dataset.toggle);
    }
    for (const cell of list.querySelectorAll('.context-peek')) {
        cell.onclick = () => openContextSource(cell.dataset.open);
    }
    renderContextMeter(foot);
}

// The block itself, in a window, as the model receives it.
//
// Verbatim rather than described: "your calendar" is a category, and the
// question behind opening a row is which four events, spelled how. The cap is
// stated rather than silent — a preview that quietly stops is one that gets
// trusted about the wrong things.
function openContextSource(id) {
    const source = (contextData?.sources || []).find(s => s.id === id);
    if (!source || !source.present) return;
    // Where it comes from, for the sources that come from somewhere you can
    // go. Showing somebody something they object to and offering no way to
    // change it makes the problem worse.
    const where = CONTEXT_SOURCE_HOME[source.id];
    closeContextPop();
    openReaderPage({
        title: source.label,
        sub: source.detail + ' · ' + contextSize(source.chars),
        text: source.preview || '',
        cut: source.truncated
            ? 'Showing the first ' + (source.preview || '').length.toLocaleString()
              + ' of ' + source.chars.toLocaleString() + ' characters.'
            : '',
        // Going on to the settings replaces this page rather than returning to
        // it — you are leaving, not coming back through.
        action: where && { label: where.label, onClick: () => switchTab(where.tab) },
    });
}

// Only the ones with a home. A skill's instructions and the document you sent
// are properties of this turn, not settings — there is nowhere to send you.
const CONTEXT_SOURCE_HOME = {
    style:    { tab: 'settings', label: 'Change how answers are written' },
    search:   { tab: 'settings', label: 'Change search settings' },
    calendar: { tab: 'settings', label: 'Change calendar settings' },
    screen:   { tab: 'ambient',  label: 'Manage screen history' },
    memory:   { tab: 'memory',   label: 'Manage what Carrot remembers' },
    coder:    { tab: 'code',     label: 'Open the Code tab' },
};


// How full the window is.
//
// The same `.ctx-meter` the Code tab draws, rather than a second bar that
// means the same thing in a different shape — and the same tight/full
// thresholds, so amber means the same thing in both places.
//
// Tokens are characters over four. A divisor rather than a real count, and
// said as "about" wherever it is shown: tokenising the prompt to draw a bar
// costs more than the bar is worth, and the bar's question is "am I near the
// edge", which four-to-one answers well enough to act on.
function renderContextMeter(host) {
    if (!host) return;
    if (!contextData || !contextData.chars) {
        host.className = 'context-foot';
        host.textContent = 'Nothing to send yet';
        return;
    }
    const window = contextData.window || 0;
    const tokens = contextData.tokens || 0;
    if (!window) {
        // No window known for this model — a bar with no scale is a bar that
        // invents one, so it says the number and stops.
        host.className = 'context-foot';
        host.textContent = `about ${tokens.toLocaleString()} tokens`;
        return;
    }
    const fraction = Math.min(1, tokens / window);
    const percent = Math.round(fraction * 100);
    host.className = 'context-foot ctx-meter'
        + (fraction > 0.85 ? ' full' : fraction > 0.7 ? ' tight' : '');
    host.innerHTML = '<div class="ctx-bar"><span></span></div><span class="ctx-text"></span>';
    host.querySelector('.ctx-bar > span').style.width = Math.max(percent, 1) + '%';
    const thousands = n => n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
    host.querySelector('.ctx-text').textContent =
        `about ${thousands(tokens)} / ${thousands(window)}`;
}

// Characters, not tokens. The number the server has is characters, and
// dividing by four to print a token count would be inventing precision — the
// point of the row is relative size, and characters carry that honestly.
function contextSize(chars) {
    if (!chars) return '0';
    if (chars < 1000) return chars + ' chars';
    return (chars / 1000).toFixed(1).replace(/\.0$/, '') + 'k chars';
}

async function setContextSource(source) {
    const row = (contextData?.sources || []).find(s => s.id === source);
    if (!row) return;
    try {
        await api('/api/context/toggle', {
            method: 'POST',
            body: JSON.stringify({ source, enabled: !row.enabled }),
        });
    } catch (_) {
        return;
    }
    await loadContext();
}

// Recount as the question changes, because memory recall depends on it — but
// not on every keystroke: this builds a real prompt server-side, and doing that
// per character is a database search per character.
function scheduleContextCount() {
    clearTimeout(contextTimer);
    contextTimer = setTimeout(() => {
        if (!document.getElementById('context-pop')?.classList.contains('hidden')) loadContext();
        else loadContextLabelOnly();
    }, 600);
}

async function loadContextLabelOnly() {
    await loadContext();
}

document.addEventListener('mousedown', (e) => {
    if (!e.target.closest('#context-picker')) closeContextPop();
});

window.addEventListener('DOMContentLoaded', () => {
    const input = document.getElementById('cmd-input');
    if (input) input.addEventListener('input', scheduleContextCount);
    loadContext();
});

// ===== Settings, in groups =====
//
// Twenty-one cards in one scroll is a page you search rather than read:
// everything is on it, so nothing is anywhere in particular, and the way to
// change your model was to remember roughly how far down it lived.
//
// Five groups, ordered by how often you go looking: what the app looks like
// and how it answers, then models, then the things it can reach, then what it
// is connected to, then what it may see. The group is the thing you know
// before you know the setting's name — "it's a model thing" arrives before
// "it's called Task Routing".
//
// The cards are tagged in the markup rather than moved. Reordering twenty-one
// blocks of HTML to group them would be a diff nobody can review, and the tag
// is the same fact in one attribute.
const SETTINGS_TABS = [
    ['general',     'General'],
    ['models',      'Models'],
    ['tools',       'Tools'],
    ['connections', 'Connections'],
    ['privacy',     'Privacy'],
];

let settingsTab = 'general';

function renderSettingsTabs() {
    const host = document.getElementById('settings-tabs');
    if (!host) return;
    host.innerHTML = SETTINGS_TABS.map(([id, label]) => {
        // The count is drawn from the page rather than kept in a list here,
        // so a card added to a group is counted without anybody remembering.
        const cards = document.querySelectorAll(`.settings-card[data-settings-tab="${id}"]`).length;
        if (!cards) return '';
        return `<button class="settings-tab${id === settingsTab ? ' on' : ''}"
                        role="tab" aria-selected="${id === settingsTab}"
                        data-tab="${id}" onclick="setSettingsTab('${id}')">${escHtml(label)}</button>`;
    }).join('');
}

function setSettingsTab(id) {
    settingsTab = id;
    for (const card of document.querySelectorAll('.settings-card[data-settings-tab]')) {
        card.classList.toggle('hidden', card.dataset.settingsTab !== id);
    }
    for (const tab of document.querySelectorAll('.settings-tab')) {
        const on = tab.dataset.tab === id;
        tab.classList.toggle('on', on);
        tab.setAttribute('aria-selected', String(on));
    }
    // Back to the top: the tabs are above the cards, and switching group while
    // scrolled halfway down the last one lands you in the middle of the new
    // one with no idea you have moved.
    document.querySelector('#view-settings .page')?.scrollTo({ top: 0 });
    try { localStorage.setItem('carrot-settings-tab', id); } catch (_) {}
}

function restoreSettingsTab() {
    let stored = 'general';
    try { stored = localStorage.getItem('carrot-settings-tab') || 'general'; } catch (_) {}
    if (!SETTINGS_TABS.some(([id]) => id === stored)) stored = 'general';
    renderSettingsTabs();
    setSettingsTab(stored);
}

window.addEventListener('DOMContentLoaded', restoreSettingsTab);
