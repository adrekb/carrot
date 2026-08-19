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
        return `
          <button class="context-row${off ? ' off' : ''}${idle}"
                  ${source.toggleable ? '' : 'disabled'}
                  data-source="${escHtml(source.id)}"
                  title="${escHtml(source.detail)}${source.toggleable ? '' : ' — always sent'}">
            <span class="context-check">${off ? '' : '✓'}</span>
            <span class="context-name">${escHtml(source.label)}</span>
            <span class="context-size">${source.present ? contextSize(source.chars) : '—'}</span>
          </button>`;
    }).join('');
    for (const row of list.querySelectorAll('.context-row:not([disabled])')) {
        row.onclick = () => setContextSource(row.dataset.source);
    }
    renderContextMeter(foot);
}

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
