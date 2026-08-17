// ===== Carrot AI — workspace frontend =====
// Conversations is where the app opens. A dashboard of widgets nobody
// chose, in front of the thing people came to do, is a room you walk
// through rather than a room you use.
let currentTab = 'workspace';
let currentConversationId = null;
let currentModel = null;
// The provider that serves `currentModel`. Sent with every turn so the server
// never has to guess: a name like "mistral-medium" is a hosted model to one
// provider and a pulled tag to Ollama, and guessing wrong routed chat to a
// model that was not there.
let currentProvider = null;
// Auto is not a model, so it cannot live in `currentModel`. When it is on, the
// turn carries no model at all and the server reads the task off the message.
let autoModel = false;
// Whether Auto can currently only reach on-device models. The empty state's
// privacy line depends on it, so it comes from the server rather than a guess.
let autoIsLocal = true;
// A chat that is answered but not remembered. Per-conversation rather than a
// global setting, because the reason to want one is usually a single question
// rather than a change of policy.
let temporaryChat = false;
let speakReplies = false;
let mediaRecorder = null;
let recordedChunks = [];
let isRecording = false;
let isPulling = false;
let activeSkill = null;      // {slug, name} when a skill is armed for the next message
let skillCatalog = [];       // cached list of skills for the picker
let recapCfg = { enabled: false, time: '04:00', last_run: '' };  // overnight recap settings

// ===== Utilities =====
function escHtml(str) {
    const d = document.createElement('div');
    d.textContent = str == null ? '' : String(str);
    return d.innerHTML;
}

// The backend injects the session token into this page's <head>. It is the
// only way to obtain it, and the same-origin policy is what keeps another
// origin from reading it — so every API call has to carry it.
const CARROT_TOKEN = (document.querySelector('meta[name="carrot-token"]') || {}).content || '';

function authHeaders(extra = {}) {
    const headers = { 'Content-Type': 'application/json', ...extra };
    if (CARROT_TOKEN) headers['X-Carrot-Token'] = CARROT_TOKEN;
    return headers;
}

// EventSource cannot set headers, so an SSE URL has to carry its credential in
// the query string — where it lands in the server log and the browser's
// history. It used to be the session token itself, which meant the one thing
// the header design exists to protect was written to disk on every launch.
//
// It carries a ticket now: minted by an authenticated POST, single use, and
// valid for thirty seconds. The copy left in the log is dead before anyone
// could read it.
async function tokenUrl(path) {
    if (!CARROT_TOKEN) return path;
    let ticket;
    try {
        ticket = (await api('/api/auth/sse-ticket', { method: 'POST' })).ticket;
    } catch (_) {
        // Older backend, or the mint failed. The query-param token still
        // works, and a stream that does not open at all is worse than one
        // whose credential is in a local log file.
        return path + (path.includes('?') ? '&' : '?')
             + 'carrot_token=' + encodeURIComponent(CARROT_TOKEN);
    }
    return path + (path.includes('?') ? '&' : '?') + 'ticket=' + encodeURIComponent(ticket);
}

async function api(path, options = {}) {
    const resp = await fetch(path, {
        ...options,
        headers: authHeaders(options.headers || {}),
    });
    if (!resp.ok) {
        let detail = resp.statusText;
        let raw = null;
        try {
            const d = (await resp.json()).detail;
            raw = d;
            if (typeof d === 'string') detail = d;
            else if (d && d.message) detail = d.message;
            else if (d) detail = Array.isArray(d) ? (d[0] && d[0].msg ? d.map(x => x.msg).join('; ') : JSON.stringify(d)) : String(d);
        } catch (_) {}
        // Carry the status and the structured detail: a 428 is the backend
        // asking for confirmation, not a failure, and the caller needs the
        // reasons to show. Losing them turned an object detail into
        // "[object Object]".
        const err = new Error(detail);
        err.status = resp.status;
        err.detail = raw;
        throw err;
    }
    return resp.json();
}

function fmtBytes(n) {
    if (!n) return '';
    const units = ['B', 'KB', 'MB', 'GB'];
    let i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
    return n.toFixed(1) + ' ' + units[i];
}

function dueLabel(dueAt) {
    if (!dueAt) return { text: '', urgent: false };
    const due = new Date(dueAt);
    if (isNaN(due)) return { text: '', urgent: false };
    const days = Math.ceil((due - Date.now()) / 86400000);
    if (days < 0) return { text: 'OVERDUE', urgent: true };
    if (days === 0) return { text: 'TODAY', urgent: true };
    if (days === 1) return { text: 'TOMORROW', urgent: false };
    return { text: days + ' DAYS', urgent: false };
}

// ===== Tabs =====
// Tabs that became panes inside Write. Anything still asking for them by name
// — a saved deep link, an older call site — lands in Write rather than on a
// blank screen where a <section> used to be.
const FOLDED_INTO_WRITE = { latex: 'notes', graph: 'notes' };

// Chat and Agent are the same conversation with the leash on or off, so they
// are a switch above the transcript rather than two tabs. Asking about a
// codebase and asking Carrot to go and change it should not feel like two
// applications.
// Both modes are the same conversation: same transcript, same composer. The
// only thing that changes is where what you type goes, and that the agent's
// settings appear in the corner while it is the one answering. Agent used to
// be a page of its own with a task box and three cards of policy above it,
// which made "ask about this" and "go and do this" feel like two applications.
// `owns` is hidden when the mode is not showing; `reveal` is the subset the
// mode actually turns on. The agent's plan and result stay hidden until a run
// fills them, and the availability warning until there is something wrong —
// so agent mode must not switch them all on merely by being entered.
const CHAT_MODES = {
    chat:  { owns: [], reveal: [] },
    // Moving these out of the transcript so they survive it being cleared also
    // took them out of its show/hide, which is why "Browser control is
    // unavailable" started appearing under an ordinary chat — a warning about
    // a tool that chat does not use.
    agent: { owns: ['agent-settings', 'agent-availability', 'agent-plan',
                    'agent-steps', 'agent-result'],
             reveal: ['agent-settings', 'agent-steps'] },
};

function setChatMode(mode) {
    for (const [name, spec] of Object.entries(CHAT_MODES)) {
        const active = name === mode;
        for (const id of spec.owns) {
            const el = document.getElementById(id);
            if (!el) continue;
            if (!active) el.classList.add('hidden');
            else if (spec.reveal.includes(id)) el.classList.remove('hidden');
        }
    }
    // Whether browser control is actually missing is the agent's own to say,
    // and loadAgent() below asks — so the warning is only unhidden by
    // renderAgentAvailability finding something wrong, never by arriving here.
    // `on`, not `active` — that is the class .mode-opt is styled on, the same
    // one the LaTeX split/reading switch uses. Setting `active` matched no rule
    // at all, so both halves rendered identically and there was no way to tell
    // which one you were in.
    for (const name of Object.keys(CHAT_MODES)) {
        document.getElementById(`chat-mode-${name}`)?.classList.toggle('on', name === mode);
    }
    document.getElementById('view-workspace')?.setAttribute('data-chat-mode', mode);
    const input = document.getElementById('cmd-input');
    if (input) {
        input.placeholder = mode === 'agent'
            ? 'Give Carrot a task — it will work in a real browser and report back'
            : 'Ask anything — Ctrl+K to focus, / for skills';
    }
    if (typeof syncChatBlank === 'function') syncChatBlank();
    if (mode === 'agent' && typeof loadAgent === 'function') loadAgent();
}

function isChatMode(mode) {
    return (document.getElementById('view-workspace')?.getAttribute('data-chat-mode') || 'chat') === mode;
}

function switchTab(tab) {
    const folded = FOLDED_INTO_WRITE[tab];
    if (folded) {
        const wantedGraph = tab === 'graph';
        tab = folded;
        if (wantedGraph) {
            switchTab(tab);
            if (typeof toggleGraphPane === 'function' && !isWriteMode('graph')) toggleGraphPane();
            return;
        }
    }
    // Agent is a mode of the conversation, so asking for it opens the
    // conversation and throws the switch.
    if (tab === 'agent') {
        switchTab('workspace');
        setChatMode('agent');
        return;
    }
    currentTab = tab;
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    const el = document.getElementById(`view-${tab}`);
    if (el) el.classList.add('active');
    document.querySelectorAll('.app-nav .nav-item').forEach(b => {
        b.classList.toggle('active', b.dataset.tab === tab);
    });
    // The nav is four entries and there is no drawer to expand. A view with no
    // nav item of its own — the Hub, a Settings sub-page, Research — leaves the
    // highlight on whichever of the four it was opened from, which is where the
    // person still is as far as they are concerned.
    const HOME_TAB = { hub: 'settings', extensions: 'settings', memory: 'settings',
                       leaderboard: 'settings', help: 'settings',
                       research: 'workspace', chats: 'workspace', search: 'workspace',
                       files: 'notes', workspaces: 'notes', inbox: 'notes',
                       goals: 'notes', reminders: 'notes', assignments: 'notes',
                       planner: 'notes', ambient: 'workspace' };
    const home = HOME_TAB[tab];
    if (home) {
        document.querySelector(`.app-nav .nav-item[data-tab="${home}"]`)?.classList.add('active');
    }
    // The chat command bar only belongs to the Conversations view.
    const cmdbar = document.getElementById('cmdbar');
    if (cmdbar) cmdbar.classList.toggle('hidden', tab !== 'workspace');
    if (typeof syncChatBlank === 'function') syncChatBlank();
    const loaders = {
        workspace: loadWorkspace,
        settings: loadSettings,
        chats: loadConversations,
        notes: loadNotes,
        code: loadCodeTab,
        planner: loadPlanner,
        goals: loadGoals,
        reminders: loadReminders,
        assignments: loadAssignments,
        extensions: loadExtensions,
        hub: loadHub,
        research: () => loadResearch(),
        ambient: () => loadAmbient(),
        workspaces: () => loadWorkspaces(),
        help: () => loadHelp(),
        leaderboard: loadLeaderboard,
        memory: () => loadMemory(),
        files: () => loadIndex(),
        inbox: () => refreshNotifications(),
    };
    if (loaders[tab]) loaders[tab]();
}


// ===== Undo, everywhere =====
//
// Three of the four editors already have it: Milkdown keeps prose history,
// Excalidraw keeps the canvas's, and a textarea keeps LaTeX's. Those are all
// reached by the browser's own Ctrl+Z, so this must not swallow the event when
// one of them has focus — it only steps in for slides, which had no history at
// all until one was written for it.
document.addEventListener('keydown', (e) => {
    const ctrl = e.ctrlKey || e.metaKey;
    if (!ctrl || e.key.toLowerCase() !== 'z' && e.key.toLowerCase() !== 'y') return;
    // Anything with its own undo keeps it.
    const el = e.target;
    if (el && (el.isContentEditable || el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')) return;
    if (typeof isWriteMode !== 'function' || !isWriteMode('slides')) return;
    e.preventDefault();
    const redo = e.key.toLowerCase() === 'y' || e.shiftKey;
    if (redo) redoSlides(); else undoSlides();
}, true);

function focusCmd() {
    document.getElementById('cmd-input').focus();
}

// ===== Skill picker (command bar) =====
async function loadSkillCatalog() {
    try {
        skillCatalog = await api('/api/skills');
    } catch (_) {
        skillCatalog = [];
    }
}

function cmdKeydown(event) {
    const pop = document.getElementById('skill-pop');
    const popOpen = !pop.classList.contains('hidden');
    if (event.key === 'Escape' && popOpen) { hideSkillPop(); return; }
    if (event.key === 'Enter') {
        if (popOpen) {
            const first = pop.querySelector('.skill-opt');
            if (first) { first.click(); return; }
        }
        sendChat();
    }
}

function cmdInputChanged() {
    const input = document.getElementById('cmd-input');
    const val = input.value;
    if (val.startsWith('/')) {
        showSkillPop(val.slice(1).trim().toLowerCase());
    } else {
        hideSkillPop();
    }
}

function showSkillPop(filter) {
    const pop = document.getElementById('skill-pop');
    const list = document.getElementById('skill-pop-list');
    const matches = skillCatalog.filter(s =>
        !filter || s.name.toLowerCase().includes(filter) || (s.description || '').toLowerCase().includes(filter));
    list.innerHTML = '';
    if (!skillCatalog.length) {
        list.innerHTML = '<div class="empty" style="padding:6px 10px">No skills yet. Create one in Extensions.</div>';
    } else if (!matches.length) {
        list.innerHTML = '<div class="empty" style="padding:6px 10px">No matching skills.</div>';
    } else {
        for (const s of matches) {
            const row = document.createElement('div');
            row.className = 'skill-opt';
            row.innerHTML = `<span class="m-name">${escHtml(s.name)}</span><span class="m-meta">${escHtml((s.description || '').slice(0, 40))}</span>`;
            row.onclick = () => pickSkill(s);
            list.appendChild(row);
        }
    }
    pop.classList.remove('hidden');
}

function hideSkillPop() {
    document.getElementById('skill-pop').classList.add('hidden');
}

function pickSkill(skill) {
    activeSkill = { slug: skill.slug, name: skill.name };
    const badge = document.getElementById('active-skill');
    document.getElementById('active-skill-name').textContent = skill.name;
    badge.classList.remove('hidden');
    const input = document.getElementById('cmd-input');
    input.value = '';
    hideSkillPop();
    input.focus();
}

function clearActiveSkill() {
    activeSkill = null;
    document.getElementById('active-skill').classList.add('hidden');
}

// ===== Status / engine =====
async function showBuildVersion() {
    try {
        const h = await api('/api/health');
        const el = document.getElementById('brand-sub');
        if (el && h.version) {
            el.textContent = `v${h.version}`;
            el.title = `Carrot ${h.version} · assets ${h.assets || '?'}`;
        }
    } catch (_) { /* leave the placeholder */ }
}

async function refreshStatus() {
    const dot = document.getElementById('engine-dot');
    const label = document.getElementById('engine-label');
    try {
        const s = await api('/api/status');
        const ok = s.ollama_available && s.model_loaded;
        dot.className = 'dot ' + (ok ? 'ok' : (s.ollama_available ? 'warn' : 'err'));
        // The name "Ollama" already sits next to this, so the label is its
        // state and nothing else. It used to read "Local Engine Active",
        // which was both louder than the fact and wrong about it: Ollama
        // being up says nothing about where *this chat* runs, and next to a
        // conversation routed to a hosted model it read as a claim that the
        // answers were local. The empty state is what says where answers
        // come from; this says whether the local engine is there.
        label.textContent = ok ? 'Ready'
            : (s.ollama_available ? 'No model' : 'Offline');
        renderEngineCard(s);
        return s;
    } catch (e) {
        dot.className = 'dot err';
        label.textContent = 'Server unreachable';
        return null;
    }
}

function renderEngineCard(s) {
    const el = document.getElementById('card-engine');
    if (!el || !s) return;
    const on = recapCfg.enabled;
    el.innerHTML = `
        <div class="engine-row"><span class="dot ${s.ollama_available ? 'ok' : 'err'}"></span><span class="name">Ollama</span><span class="val">${s.ollama_available ? 'running' : 'offline'}</span></div>
        <div class="engine-row"><span class="dot ${s.model_loaded ? 'ok' : 'warn'}"></span><span class="name">Model</span><span class="val">${escHtml(currentModel || s.default_model)}</span></div>
        <div class="engine-row"><span class="dot ok"></span><span class="name">Conversations</span><span class="val">${s.conversations}</span></div>
        <div class="engine-row"><span class="dot ok"></span><span class="name">Messages</span><span class="val">${s.messages}</span></div>
        <div class="engine-auto">
            <label class="switch-row">
                <input type="checkbox" ${on ? 'checked' : ''} onchange="setRecapAuto(this.checked)">
                <span>Overnight briefing</span>
                <input type="time" class="auto-time" value="${escHtml(recapCfg.time || '04:00')}" onchange="setRecapTime(this.value)">
            </label>
            <div class="auto-hint">Auto-runs the deep-research recap daily at this time while Carrot is open${recapCfg.last_run ? ' · last: ' + escHtml(recapCfg.last_run) : ''}.</div>
        </div>`;
}

// How answers are written.
//
// The same research handed over as dense paragraphs or as something skimmable
// is the difference people actually notice, and which one is better is taste
// rather than correctness. So it is a setting with a default, not a rule.
const STYLE_CHOICES = [
    { id: 'brief',    label: 'Brief',    hint: 'the answer, and little else' },
    { id: 'balanced', label: 'Balanced', hint: 'recommended — a claim per point, then the detail' },
    { id: 'full',     label: 'Detailed', hint: 'explains why, not only what' },
];
const STRUCTURE_CHOICES = [
    { id: 'normal', label: 'Normal', hint: 'headings and lists where they help' },
    { id: 'less',   label: 'Fewer',  hint: 'prose unless it is really a list' },
];

function renderAnswerStyle(cfg) {
    _paint('style-choices', STYLE_CHOICES, cfg.answer_style || 'balanced',
           id => _saveStyle('answer_style', id, cfg));
    _paint('structure-choices', STRUCTURE_CHOICES, cfg.answer_structure || 'normal',
           id => _saveStyle('answer_structure', id, cfg));
    const box = document.getElementById('answer-custom');
    if (box) box.value = cfg.answer_custom || '';
}

function _paint(hostId, choices, current, onPick) {
    const host = document.getElementById(hostId);
    if (!host) return;
    host.innerHTML = '';
    for (const choice of choices) {
        const btn = document.createElement('button');
        btn.className = 'ctx-choice' + (choice.id === current ? ' on' : '');
        btn.onclick = () => onPick(choice.id);
        btn.innerHTML = `<span class="ctx-label">${escHtml(choice.label)}</span>`
            + `<span class="ctx-hint">${escHtml(choice.hint)}</span>`;
        host.appendChild(btn);
    }
}

async function _saveStyle(key, value, cfg) {
    cfg[key] = value;
    renderAnswerStyle(cfg);              // paint first; the write is the slow part
    try {
        await api(`/api/config/${key}`, { method: 'PUT', body: JSON.stringify(value) });
    } catch (_) { /* the next load will show what actually stuck */ }
}

async function setAnswerCustom(text) {
    try {
        await api('/api/config/answer_custom', {
            method: 'PUT', body: JSON.stringify(String(text || '').slice(0, 600)),
        });
    } catch (_) { /* ignore */ }
}

// How much a local model may hold in mind at once.
//
// Presented as three choices rather than a token count, because "num_ctx" is
// not a thing a person should have to know, and the real decision behind it is
// a trade against the memory on their machine. The numbers are still shown —
// hiding them from someone who does know would be its own kind of rude.
let _pendingCtxCfg = null;
let _lastModels = null;

const CTX_CHOICES = [
    { tokens: 8192,  label: 'Small',    hint: 'least memory; short turns only' },
    { tokens: 32768, label: 'Balanced', hint: 'recommended — holds several pages' },
    { tokens: 65536, label: 'Large',    hint: 'long documents; needs a strong machine' },
];

function renderContextChoices(cfg, data) {
    const host = document.getElementById('ctx-choices');
    if (!host) return;
    const context = (data || {}).context || {};
    const current = Number(cfg.ollama_num_ctx || context._default || 32768);

    host.innerHTML = '';
    for (const choice of CTX_CHOICES) {
        const btn = document.createElement('button');
        btn.className = 'ctx-choice' + (choice.tokens === current ? ' on' : '');
        btn.onclick = () => setContextWindow(choice.tokens);
        btn.innerHTML = `<span class="ctx-label">${choice.label}</span>`
            + `<span class="ctx-tokens">${fmtCtx(choice.tokens)} tokens</span>`
            + `<span class="ctx-hint">${escHtml(choice.hint)}</span>`;
        host.appendChild(btn);
    }

    // What the model in front of you actually got, which is not always what
    // was asked for: a model that tops out lower than the setting is capped to
    // its own limit, and saying so is better than the number quietly differing.
    const line = document.getElementById('ctx-current');
    if (!line) return;
    const active = data && data.active_model;
    const got = active ? context[active] : 0;
    line.textContent = got
        ? `${active} is running with ${fmtCtx(got)} tokens`
          + (got < current ? ' — its own limit, which is lower than the setting.' : '.')
        : '';
}

async function setContextWindow(tokens) {
    try {
        await api('/api/config/ollama_num_ctx', {
            method: 'PUT', body: JSON.stringify(tokens),
        });
        // The client caches per model, so the new value only applies once the
        // backend re-reads it. Reloading the picker is what shows that.
        loadModels();
    } catch (_) { /* leave the buttons as they were */ }
}

// ===== Advanced: an exact number =====
//
// The three presets are the right shape for the decision most people are
// making. For the ones they are not — someone who knows their card has 24GB
// and their model holds 128k — being told the choices are Small, Balanced and
// Large is worse than useless. This is behind a fold, so it costs the first
// group nothing.
//
// The overhead line is the part that matters. "8,192 tokens" reads as eight
// thousand tokens of conversation and is nothing of the sort: the directive
// and the tool schemas are in the window before the question is, and on a
// multi-turn search they are over a third of an 8k window on their own.
// Someone setting a small window without knowing that will conclude Carrot
// forgets things, which is true, and blame the wrong thing.

const CTX_MIN = 1024;

function renderContextOverhead(overhead) {
    const line = document.getElementById('ctx-overhead');
    if (!line || !overhead) return;
    const worst = Number(overhead.worst || 0);
    if (!worst) { line.textContent = ''; return; }
    const multi = overhead.multi || {};
    line.innerHTML =
        `Carrot's own instructions and its ${escHtml(String(multi.tools || ''))} tool `
        + `definitions occupy about <b>${fmtCtx(worst)} tokens</b> of whatever you set, `
        + 'before your question or any page it reads. With multi-turn search on, that is '
        + `${Math.round(worst / 8192 * 100)}% of an 8K window and `
        + `${Math.round(worst / 32768 * 100)}% of a 32K one. Anything below about `
        + `${fmtCtx(8192)} leaves very little room for the conversation itself.`;
}

function renderCustomContext(cfg) {
    const input = document.getElementById('ctx-custom');
    const note = document.getElementById('ctx-custom-note');
    if (!input) return;
    const current = Number(cfg.ollama_num_ctx || 32768);
    input.value = current;
    // Opened on load when the stored value is not one of the presets —
    // otherwise a number set here would be invisible behind a closed fold
    // with none of the three buttons lit, which reads as nothing being set.
    const isPreset = CTX_CHOICES.some(c => c.tokens === current);
    const fold = document.getElementById('ctx-advanced');
    if (fold && !isPreset) fold.open = true;
    if (note) {
        note.textContent = isPreset
            ? ''
            : `Currently set to ${fmtCtx(current)} tokens, which is not one of the presets above.`;
    }
}

function ctxCustomKeydown(event) {
    if (event.key === 'Enter') { event.preventDefault(); applyCustomContext(); }
}

async function applyCustomContext() {
    const input = document.getElementById('ctx-custom');
    const note = document.getElementById('ctx-custom-note');
    const tokens = Math.floor(Number(input.value));
    // Refused here as well as on the server. A 0 reaches Ollama as "use your
    // default", which is 4096 — so the failure would be silent and would look
    // exactly like the bug this whole setting exists to fix.
    if (!Number.isFinite(tokens) || tokens < CTX_MIN) {
        if (note) note.textContent = `That has to be a whole number of at least ${fmtCtx(CTX_MIN)}.`;
        return;
    }
    if (note) note.textContent = 'Saving…';
    await setContextWindow(tokens);
    if (note) {
        note.textContent = `Set to ${fmtCtx(tokens)} tokens. Carrot still never asks a model `
            + 'for more than it supports, so a model with a smaller limit is capped to its own.';
    }
}

// The reader fallback, for pages that refuse to be read.
//
// Off unless asked for. It sends the page's address to a third party, and the
// address is the private part — "what did you look up" is the question Carrot
// exists not to send anywhere. So this is a switch the user flips knowingly,
// not a default that quietly improves the numbers.
async function setReaderFallback(enabled) {
    try {
        await api('/api/config/reader_fallback', {
            method: 'PUT', body: JSON.stringify(!!enabled),
        });
    } catch (_) {
        // Put the checkbox back where it was rather than showing a state the
        // server does not agree with.
        const box = document.getElementById('reader-fallback-toggle');
        if (box) box.checked = !enabled;
    }
}

function renderReaderFallback(cfg) {
    const box = document.getElementById('reader-fallback-toggle');
    if (box) box.checked = !!(cfg || {}).reader_fallback;
}

async function loadRecapConfig() {
    try {
        const cfg = await api('/api/config');
        recapCfg.enabled = !!cfg.recap_auto_enabled;
        recapCfg.time = cfg.recap_auto_time || '04:00';
        recapCfg.last_run = cfg.recap_auto_last_run || '';
        // Same fetch, so the rail's server copy costs nothing extra.
        syncRailFromServer(cfg);
        renderReaderFallback(cfg);
        renderAnswerStyle(cfg);
        _pendingCtxCfg = cfg;
        renderContextChoices(cfg, _lastModels || {});
        renderCustomContext(cfg);
        renderContextOverhead((_lastModels || {}).overhead);
    } catch (_) {}
}

async function setRecapAuto(enabled) {
    recapCfg.enabled = enabled;
    try {
        await api('/api/config/recap_auto_enabled', { method: 'PUT', body: JSON.stringify(enabled) });
    } catch (e) { alert('Could not save setting: ' + e.message); }
    refreshStatus();
}

async function setRecapTime(value) {
    recapCfg.time = value;
    try {
        await api('/api/config/recap_auto_time', { method: 'PUT', body: JSON.stringify(value) });
    } catch (e) { alert('Could not save setting: ' + e.message); }
}

// ===== Model picker =====
async function loadModels(opts) {
    try {
        // `live` only on request: the popup draws the models you already have
        // without waiting on Hugging Face, and "Find current" is what asks.
        const data = await api('/api/models' + ((opts || {}).live ? '?live=true' : ''));
        // The label has to show what chat *actually* runs on. `active_model` is
        // only the Ollama default, so reading it here made a pinned cloud model
        // silently revert to the local one in the picker on every refresh.
        autoModel = !!data.auto;
        autoIsLocal = data.auto_local !== false;
        if (data.chat_local === false && data.chat_model) {
            currentModel = data.chat_model;
            currentProvider = data.chat_provider || null;
        } else {
            currentModel = data.active_model;
            currentProvider = 'ollama';
        }
        // Under Auto the model is not known until the message is read, so the
        // label must not name one. The route line on each turn says what ran.
        document.getElementById('model-label').textContent = autoModel ? 'Auto' : currentModel;
        // Under Auto the model is picked per message, so no single answer to
        // "can it see" exists yet — assume it can, and let the send-time check
        // be the one that refuses.
        renderAttachAffordance(autoModel || data.chat_vision !== false);
        _lastModels = data;
        if (_pendingCtxCfg) renderContextChoices(_pendingCtxCfg, data);
        // The overhead figure is measured on the server and arrives with the
        // models, so it lands whichever of the two fetches finishes last.
        renderContextOverhead(data.overhead);
        renderModelPop(data);
        renderEmptyStateLine();
    } catch (_) {
        document.getElementById('model-label').textContent = 'no engine';
    }
}

// Offering something that will be refused is worse than not offering it.
//
// The server has always rejected images a model cannot read, but only on
// send, as a 400 — after you had found the file, attached it and written the
// question. The file picker itself now stops listing images, so a text-only
// model simply never presents the choice.
let modelCanSeeImages = true;

function renderAttachAffordance(canSee) {
    modelCanSeeImages = canSee;
    const input = document.getElementById('attach-input');
    const button = document.getElementById('attach-btn');
    if (!input || !button) return;
    const docs = '.pdf,.txt,.md,.csv,.json,.py,.js,.ts,.html,.css,.yaml,.yml,.log';
    input.accept = canSee ? 'image/*,' + docs : docs;
    button.title = canSee
        ? 'Attach an image, PDF or text file'
        : `${currentModel} cannot read images — PDFs and text files only`;
}

// How much a local model is actually being run with.
//
// Ollama's default is 4096 whatever the model can hold, and the difference is
// not cosmetic: in 4k a turn loses the system directive and the pages it just
// read, then answers as though neither existed. That number belongs next to
// the model you are choosing, not buried in a config file nobody opens.
// Context windows are quoted in two different bases and rendering both in one
// is how you get a number nobody recognises. Local models are genuinely powers
// of two — 131072 is "128k" and calling it "131k" is wrong. Hosted models are
// quoted in decimal — Mistral's Codestral is 256,000 and dividing by 1024 shows
// "250k", which matches no figure in any documentation the user can check.
//
// So: powers of two get binary, everything else gets decimal, and a million is
// written as a million because "1024k" is not how anyone says it.
function fmtCtx(tokens) {
    if (!tokens) return '';
    if (tokens < 1000) return String(tokens);
    const isPowerOfTwo = (tokens & (tokens - 1)) === 0;
    if (tokens >= 1_000_000) {
        const millions = tokens / (isPowerOfTwo ? 1_048_576 : 1_000_000);
        return `${millions % 1 === 0 ? millions : millions.toFixed(1)}M`;
    }
    return `${Math.round(tokens / (isPowerOfTwo ? 1024 : 1000))}k`;
}

// ===== The context marker =====
//
// Every model in the picker says how much it can hold. Local models always
// could — Ollama reports a context length — but a Claude or a GPT showed only
// the word "cloud", and a model served by someone's own endpoint showed that
// and nothing else. Which is backwards: the window is the single most
// consequential fact about a model for how Carrot behaves, and it was
// displayed only for the models where it was easiest to obtain rather than
// where it mattered most.
//
// It also says *where the number came from*, because the confidence differs.
// "131k · reported" is the model answering. "200k · known" is Carrot matching
// the family name against a table that will be out of date the week a
// provider ships something new. Presenting a guess in the same typeface as a
// measurement is the same mistake the research pipeline makes when a content
// farm and a press release get the same citation — and worth not repeating
// two features apart.

const WINDOW_SOURCE_SHORT = {
    probed: 'reported',
    known: 'known',
    set: 'you set this',
    unknown: 'unknown',
};

function windowFor(data, provider, model) {
    return ((data || {}).windows || {})[`${provider}/${model}`] || null;
}

function windowChip(win) {
    if (!win) return '';
    if (!win.tokens) {
        // Said out loud rather than left blank. A blank reads as "no window
        // configured for this UI"; unknown is a fact about the model, and it
        // is the one that tells you the Advanced box is where to go next.
        return `<span class="m-ctx unknown" title="${escHtml(win.why || '')}">context unknown</span>`;
    }
    const tag = WINDOW_SOURCE_SHORT[win.source] || win.source;
    return `<span class="m-ctx src-${escHtml(win.source)}" title="${escHtml(win.why || '')}">`
         + `${fmtCtx(win.tokens)} ctx · ${escHtml(tag)}</span>`;
}

function ctxLabel(data, name) {
    // For a local model, what it is *running* with, which is not the same as
    // what it can hold: the configured window caps it, and its own limit caps
    // that. The chip beside this shows the ceiling; this shows the setting.
    const ctx = (data.context || {})[name];
    if (!ctx) return '';
    return ` · running at ${fmtCtx(ctx)}`;
}

// "Set it" for a model Carrot has never heard of.
//
// A tiny inline control rather than a trip to Settings, because the moment
// you learn the window is unknown is the moment you are looking at the model,
// and a fix that lives on another screen is a fix most people will not make.
function contextOverrideControl(provider, model) {
    const wrap = document.createElement('div');
    wrap.className = 'm-ctx-set';
    wrap.innerHTML = `
        <input type="number" min="1024" step="1024" placeholder="tokens">
        <button class="btn btn-ghost btn-sm">Set</button>`;
    // The row behind this selects the model. Typing a number into a control
    // inside it must not also switch the chat to that model.
    wrap.onclick = (e) => e.stopPropagation();
    const input = wrap.querySelector('input');
    const save = async () => {
        const tokens = Math.floor(Number(input.value));
        if (!Number.isFinite(tokens) || tokens < CTX_MIN) return;
        try {
            await api('/api/models/context-window', {
                method: 'PUT',
                body: JSON.stringify({ provider, model, tokens }),
            });
            loadModels();
        } catch (_) { /* the chip stays "unknown", which is still true */ }
    };
    input.onkeydown = (e) => { if (e.key === 'Enter') { e.preventDefault(); save(); } };
    wrap.querySelector('button').onclick = save;
    return wrap;
}

function renderModelPop(data) {
    const installedEl = document.getElementById('model-installed');
    const suggestedEl = document.getElementById('model-suggested');
    const remoteEl = document.getElementById('model-remote');
    installedEl.innerHTML = '';
    suggestedEl.innerHTML = '';
    if (remoteEl) remoteEl.innerHTML = '';

    renderAutoRow(data);

    // A local model is "current" only when chat isn't pinned to a provider,
    // and never while Auto is picking.
    const localActive = (!data.auto && data.chat_local !== false) ? data.active_model : null;

    if (!data.installed.length) {
        installedEl.innerHTML = '<div class="empty" style="padding:4px 9px">No models installed yet.</div>';
    }
    for (const m of data.installed) {
        const row = document.createElement('div');
        row.className = 'model-row' + (m.name === localActive ? ' active' : '');
        row.innerHTML = `
            <span class="m-name">${escHtml(m.name)}</span>
            <span class="m-meta">${escHtml(m.parameter_size || '')} ${fmtBytes(m.size)}${ctxLabel(data, m.name)}</span>
            ${windowChip(windowFor(data, 'ollama', m.name))}
            ${m.name === localActive ? '<svg class="ico m-check"><use href="#i-check"/></svg>' : ''}`;
        row.onclick = () => selectModel(m.name);
        installedEl.appendChild(row);
    }

    // Models from providers you've configured — the key is already saved,
    // so they belong in the same picker as the local ones.
    if (remoteEl) {
        for (const group of (data.remote || [])) {
            const head = document.createElement('div');
            head.className = 'pop-section';
            head.textContent = group.label;
            remoteEl.appendChild(head);

            for (const name of group.models) {
                const isActive = !data.auto && data.chat_local === false
                    && data.chat_provider === group.provider && data.chat_model === name;
                const row = document.createElement('div');
                row.className = 'model-row' + (isActive ? ' active' : '');
                const win = windowFor(data, group.provider, name);
                row.innerHTML = `
                    <span class="m-name">${escHtml(name)}</span>
                    <span class="m-meta">cloud</span>
                    ${windowChip(win)}
                    ${isActive ? '<svg class="ico m-check"><use href="#i-check"/></svg>' : ''}`;
                row.onclick = () => selectRemoteModel(group.provider, name);
                // A model Carrot has no entry for can be told what it holds,
                // rather than dead-ending at "unknown". This is the escape
                // hatch that lets the table be allowed to be incomplete —
                // which it permanently is, because providers ship faster than
                // any bundled table gets updated.
                if (win && !win.tokens) {
                    row.appendChild(contextOverrideControl(group.provider, name));
                }
                remoteEl.appendChild(row);
            }

            // Listing can fail while the provider still works fine. Say why,
            // and let the model be named by hand instead of dead-ending.
            if (group.error) {
                const why = document.createElement('div');
                why.className = 'model-note';
                why.textContent = /401|403|unauthor/i.test(group.error)
                    ? 'Key rejected — check it in Settings → Providers.'
                    : `Could not list models: ${group.error}`.slice(0, 120);
                remoteEl.appendChild(why);
            }
            if (group.error || !group.models.length) {
                const row = document.createElement('div');
                row.className = 'pop-custom';
                row.innerHTML = `
                    <input type="text" placeholder="type a ${escHtml(group.label)} model name"
                           id="remote-custom-${escHtml(group.provider)}">
                    <button class="btn btn-ghost">Use</button>`;
                const input = row.querySelector('input');
                const use = () => {
                    const name = input.value.trim();
                    if (name) selectRemoteModel(group.provider, name);
                };
                input.onkeydown = (e) => { if (e.key === 'Enter') use(); };
                row.querySelector('button').onclick = use;
                remoteEl.appendChild(row);
            }
        }
    }

    // "Find more" — the catalog, sized to this machine. Rows arrive already
    // ordered: what runs well first, what won't run at all last, each of
    // those carrying the reason. The server drops anything already installed,
    // since it is one section above in the same popup.
    const notInstalled = (data.suggested || []).filter(m => !m.installed);
    if (!notInstalled.length) {
        suggestedEl.innerHTML = '<div class="empty" style="padding:4px 9px">Nothing left to add.</div>';
    }
    // Where these rows came from, said before they are read. The list built
    // into a download is a snapshot of the day the build was cut; presenting
    // it as what is good now is the claim worth avoiding, not the age itself.
    if (notInstalled.length && notInstalled[0].source === 'bundled') {
        const bar = document.createElement('div');
        bar.className = 'model-source-note';
        bar.innerHTML = '<span>Built into this download — may be out of date.</span>';
        const go = document.createElement('button');
        go.className = 'm-install';
        go.textContent = 'Find current';
        go.onclick = (e) => { e.stopPropagation(); loadModels({ live: true }); };
        bar.appendChild(go);
        suggestedEl.appendChild(bar);
    }
    const FIT_LABEL = { great: 'Runs great', good: 'Runs well', tight: 'Slow here', too_big: "Won't run" };
    for (const m of notInstalled) {
        const row = document.createElement('div');
        row.className = 'model-row' + (m.runs_here === false ? ' model-row-unfit' : '');
        row.style.cursor = 'default';
        // The reason a row is greyed out is the most useful sentence on this
        // screen, so it goes on the row rather than into a tooltip.
        const meta = m.runs_here === false
            ? escHtml(m.why_not || '')
            : `${escHtml(m.size_hint || '')}${m.est_tps ? ` · ~${m.est_tps} tok/s` : ''}`;
        row.innerHTML = `
            <span class="m-name" title="${escHtml(m.blurb || '')}">${escHtml(m.name)}</span>
            <span class="m-meta">${meta}</span>`;
        if (m.fit && FIT_LABEL[m.fit]) {
            const badge = document.createElement('span');
            badge.className = 'm-fit m-fit-' + m.fit;
            badge.textContent = FIT_LABEL[m.fit];
            row.querySelector('.m-name').after(badge);
        }
        if (m.runs_here !== false) {
            const btn = document.createElement('button');
            btn.className = 'm-install';
            btn.innerHTML = '<svg class="ico"><use href="#i-download"/></svg>Install';
            btn.onclick = (e) => { e.stopPropagation(); pullModel(m.name); };
            row.appendChild(btn);
        }
        suggestedEl.appendChild(row);
    }
}

// Auto sits above the model list because it is the answer to a different
// question — "you decide" rather than "this one". The subtitle says what it
// will actually do, including whether it can leave the machine, because
// picking it is the moment that stops being obvious.
function renderAutoRow(data) {
    const el = document.getElementById('model-auto');
    if (!el) return;
    el.innerHTML = '';
    const row = document.createElement('div');
    row.className = 'model-row model-auto-row' + (data.auto ? ' active' : '');
    const where = data.auto_local === false
        ? 'code and hard questions go to your cloud provider'
        : 'everything still runs on this machine';
    row.innerHTML = `
        <span class="m-name">Auto<span class="m-sub">picks a model per message — ${escHtml(where)}</span></span>
        ${data.auto ? '<svg class="ico m-check"><use href="#i-check"/></svg>' : ''}`;
    row.onclick = () => selectAutoModel();
    el.appendChild(row);
}

async function selectAutoModel() {
    try {
        const state = await api('/api/models/auto',
            { method: 'POST', body: JSON.stringify({ enabled: true }) });
        autoModel = true;
        autoIsLocal = state.auto_local !== false;
        document.getElementById('model-label').textContent = 'Auto';
        renderEmptyStateLine();
        document.getElementById('model-pop').classList.add('hidden');
        loadModels();
        refreshStatus();
    } catch (e) {
        alert('Could not switch to Auto: ' + e.message);
    }
}

// ===== History, in the corner =====
//
// What you have asked and what the agent has run, in one list, told apart by a
// chip rather than by living in two different places. Two kinds of record that
// answer the same question — "what was I doing" — so they belong in one list.
//
// Code is deliberately not here. Its history is the Checkpoints panel in the
// Code tab, and it is a different kind of record: what an agent *did to your
// files*, which you search by file and by change rather than by what you said.
// Folding it in would make both harder to use, which is why Cursor keeps them
// apart too.
let navCollapsed = false;
let historyFilter = 'all';
let historyCache = [];

function toggleNavCollapsed() {
    navCollapsed = !navCollapsed;
    document.body.classList.toggle('nav-collapsed', navCollapsed);
    try { localStorage.setItem('carrot-nav-collapsed', navCollapsed ? '1' : '0'); } catch (_) {}
    const btn = document.getElementById('nav-collapse');
    if (btn) btn.title = navCollapsed ? 'Expand the sidebar' : 'Collapse the sidebar';
}

function restoreNavCollapsed() {
    let stored = '0';
    try { stored = localStorage.getItem('carrot-nav-collapsed') || '0'; } catch (_) {}
    if (stored === '1') toggleNavCollapsed();
}

function toggleHistoryMenu() {
    const pop = document.getElementById('history-pop');
    if (!pop) return;
    const opening = pop.classList.contains('hidden');
    pop.classList.toggle('hidden');
    if (opening) loadHistory();
}

function closeHistoryMenu() {
    document.getElementById('history-pop')?.classList.add('hidden');
}

function setHistoryFilter(kind) {
    historyFilter = kind;
    for (const chip of document.querySelectorAll('.history-chip')) {
        chip.classList.toggle('active', chip.dataset.kind === kind);
    }
    renderHistory();
}

// Epoch seconds, whatever the source called it.
//
// Conversations timestamp in ISO text and documents in seconds. Sorting one
// list by both means comparing a string to a number, which is NaN — and a sort
// whose comparator returns NaN does not error, it just leaves the order
// roughly as it found it. Normalising here is what makes "most recent first"
// actually true across the two.
function historyEpoch(value) {
    if (!value) return 0;
    if (typeof value === 'number') return value;
    const parsed = Date.parse(value);
    return isNaN(parsed) ? 0 : parsed / 1000;
}

async function loadHistory() {
    const [convs, runs] = await Promise.all([
        api('/api/conversations').catch(() => []),
        api('/api/agent/runs').then(r => r.runs || []).catch(() => []),
    ]);
    historyCache = [
        ...(Array.isArray(convs) ? convs : (convs.conversations || []))
            // Code sessions are conversations too — same endpoint, same table.
            // They belong to the Code tab's own history, not to this one.
            .filter(c => (c.metadata || {}).surface !== 'code')
            .map(c => ({
            kind: 'chat',
            id: c.id,
            title: c.title || 'Untitled',
            when: historyEpoch(c.updated_at || c.created_at),
        })),
        ...runs.map(r => ({
            kind: 'agent',
            id: r.id,
            title: r.task || r.title || 'Agent run',
            when: historyEpoch(r.created_at || r.started_at),
        })),
    ].sort((a, b) => b.when - a.when);
    renderHistory();
}

function renderHistory() {
    const host = document.getElementById('history-list');
    if (!host) return;
    const items = historyCache.filter(i => historyFilter === 'all' || i.kind === historyFilter);
    if (!items.length) {
        host.innerHTML = '<div class="history-empty">Nothing here yet.</div>';
        return;
    }
    host.innerHTML = items.slice(0, 40).map(i => `
        <button class="history-item" data-kind="${i.kind}" data-id="${escHtml(String(i.id))}">
          <span class="history-dot chip-${i.kind}"></span>
          <span class="history-title">${escHtml(i.title)}</span>
          <span class="history-when">${escHtml(writeWhen(i.when))}</span>
        </button>`).join('');
    for (const el of host.querySelectorAll('.history-item')) {
        el.onclick = () => openHistoryItem(el.dataset.kind, el.dataset.id);
    }
}

function openHistoryItem(kind, id) {
    closeHistoryMenu();
    switchTab('workspace');
    if (kind === 'agent') {
        setChatMode('agent');
        if (typeof openAgentRun === 'function') openAgentRun(id);
        return;
    }
    setChatMode('chat');
    if (typeof openConversation === 'function') openConversation(id);
}

// ===== Settings sub-pages =====
//
// Extensions, Memory, Leaderboard and Help were four entries in a sidebar that
// had grown past the point anybody could read it. They are still whole pages;
// they are reached from the place you go when you want to change something,
// and Back brings you here rather than to wherever you happened to be before.
// Unlike the Hub, which returns you to your work, these belong to Settings —
// so the door leads back into the room you came from.
function openSettingsPage(tab) {
    switchTab(tab);
}

function backToSettings() {
    switchTab('settings');
}

// ===== The Hub, without a tab =====
//
// "Where do I get more models" is a question you only ask with the model list
// already open, so that list is where it is answered. The Hub keeps its view
// and loses its nav entry: it opens from the picker and closes back to
// whatever you were doing, which is what a place you visit once in a while
// should do rather than sit in the sidebar being walked past.
let tabBeforeModelHub = null;

function openModelHub() {
    document.getElementById('model-pop')?.classList.add('hidden');
    // Not recorded if the Hub is somehow already open, or Close would bring
    // you back to the Hub.
    if (currentTab !== 'hub') tabBeforeModelHub = currentTab;
    switchTab('hub');
}

function closeModelHub() {
    const back = tabBeforeModelHub || 'workspace';
    tabBeforeModelHub = null;
    switchTab(back);
}

// Popovers above the command bar are clamped to the space that actually
// exists. A fixed max-height ran off the top of the screen on short
// windows, leaving options you could see but never scroll to.
function fitPopoverAbove(popId, anchorId, gap = 10, floor = 170) {
    const pop = document.getElementById(popId);
    const anchor = document.getElementById(anchorId);
    if (!pop || !anchor) return;
    const room = anchor.getBoundingClientRect().top - gap - 14;
    pop.style.maxHeight = Math.max(floor, Math.min(460, room)) + 'px';
}

function toggleModelPop() {
    const pop = document.getElementById('model-pop');
    const opening = pop.classList.contains('hidden');
    pop.classList.toggle('hidden');
    if (opening) fitPopoverAbove('model-pop', 'model-btn');
}

// Re-clamp on resize so a popover left open stays reachable.
window.addEventListener('resize', () => {
    if (!document.getElementById('model-pop')?.classList.contains('hidden')) {
        fitPopoverAbove('model-pop', 'model-btn');
    }
    if (!document.getElementById('search-pop')?.classList.contains('hidden')) {
        fitPopoverAbove('search-pop', 'search-btn');
    }
});

// Picking a cloud model pins the 'chat' task to that provider — the same
// mechanism the Task Routing table uses, so the two never disagree.
async function selectRemoteModel(provider, model) {
    try {
        await api('/api/router/route', {
            method: 'PUT',
            body: JSON.stringify({ task: 'chat', provider, model }),
        });
        // Naming a model here is the opposite of asking Carrot to name one.
        // Pinning `chat` in Settings does not do this; the picker does.
        await api('/api/models/auto',
            { method: 'POST', body: JSON.stringify({ enabled: false }) }).catch(() => {});
        autoModel = false;
        currentModel = model;
        currentProvider = provider;
        document.getElementById('model-label').textContent = model;
        renderEmptyStateLine();
        document.getElementById('model-pop').classList.add('hidden');
        loadModels();
        refreshStatus();
        if (typeof loadRouting === 'function') loadRouting();
    } catch (e) {
        alert('Could not switch to that model: ' + e.message);
    }
}

async function selectModel(name) {
    try {
        // Selecting a local model also releases any cloud pin on chat.
        await api('/api/router/route/chat', { method: 'DELETE' }).catch(() => {});
        // /api/models/select clears Auto server-side; mirror it here so the
        // label does not stay on "Auto" until the next refresh.
        await api('/api/models/select', { method: 'POST', body: JSON.stringify({ model: name }) });
        autoModel = false;
        currentModel = name;
        currentProvider = 'ollama';
        document.getElementById('model-label').textContent = name;
        renderEmptyStateLine();
        document.getElementById('model-pop').classList.add('hidden');
        loadModels();
        refreshStatus();
    } catch (e) {
        alert('Could not select model: ' + e.message);
    }
}

function pullCustomModel() {
    const input = document.getElementById('model-custom');
    const name = input.value.trim();
    if (!name) return;
    input.value = '';
    pullModel(name);
}

async function pullModel(name) {
    if (isPulling) return;
    isPulling = true;
    const wrap = document.getElementById('pull-progress');
    const label = document.getElementById('pull-label');
    const bar = document.getElementById('pull-bar');
    wrap.classList.remove('hidden');
    label.textContent = `pulling ${name}…`;
    bar.style.width = '2%';
    try {
        const resp = await fetch('/api/models/pull', {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify({ model: name }),
        });
        if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || resp.statusText);
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = buffer.indexOf('\n\n')) !== -1) {
                const raw = buffer.slice(0, idx).trim();
                buffer = buffer.slice(idx + 2);
                if (!raw.startsWith('data:')) continue;
                const p = JSON.parse(raw.slice(5).trim());
                if (p.error) throw new Error(p.error);
                if (p.total && p.completed != null) {
                    const pct = Math.round((p.completed / p.total) * 100);
                    bar.style.width = pct + '%';
                    label.textContent = `${name} — ${p.status} ${pct}% (${fmtBytes(p.completed)} / ${fmtBytes(p.total)})`;
                } else if (p.status) {
                    label.textContent = `${name} — ${p.status}`;
                }
                if (p.done) {
                    bar.style.width = '100%';
                    label.textContent = `${name} installed`;
                }
            }
        }
        await loadModels();
        setTimeout(() => wrap.classList.add('hidden'), 2500);
    } catch (e) {
        label.textContent = `failed: ${e.message}`;
        bar.style.width = '0';
        setTimeout(() => wrap.classList.add('hidden'), 5000);
    } finally {
        isPulling = false;
    }
}

// ===== Chat (streaming) =====

// Whether the conversation has anything in it yet.
//
// One function owns this because two things depend on it and they must agree:
// the heading, and where the composer sits. Driven off the empty-state element
// still being present rather than off a counter, so it cannot drift from what
// is actually on screen.
function syncChatBlank() {
    const blank = !!document.getElementById('chat-empty');
    const onChat = currentTab === 'workspace'
        && (typeof isChatMode !== 'function' || isChatMode('chat'));
    document.body.classList.toggle('chat-blank', blank && onChat);
}

function clearChatEmpty() {
    const empty = document.getElementById('chat-empty');
    if (empty) empty.remove();
    syncChatBlank();
}

// The sources behind an answer, as cards above it.
//
// The trace has always listed the URLs, but as debug output in a collapsed
// panel — so an answer that had read four outlets looked exactly like one it
// made up. These say the same thing where it can be read: outlet, headline,
// date, clickable. Rendered when the search returns, before the answer starts,
// because watching the sources arrive is most of the reassurance.
function showSources(assistantEl, contentEl, sources) {
    if (!sources || !sources.length) return;
    let rail = assistantEl.querySelector('.source-cards');
    if (!rail) {
        rail = document.createElement('div');
        rail.className = 'source-cards';
        rail._seen = [];
        assistantEl.insertBefore(rail, contentEl);
    }

    // Every source from every round is kept, and the three shown are chosen
    // from all of them each time. Filling the row first-come put the three
    // index pages from an opening broad search on screen and left the dated
    // article a later round found — the one the answer actually quoted — off
    // it. Articles first, then original order within each group.
    const known = new Set(rail._seen.map(s => s.url));
    for (const source of sources) {
        if (source && source.url && !known.has(source.url)) {
            known.add(source.url);
            rail._seen.push(source);
        }
    }
    // Three, and no more. A search returns six per round; showing all of them
    // pushed the answer far enough down that it looked like there wasn't one.
    // The rest are not lost — everything the answer used is cited inline.
    const MAX_CARDS = 3;
    // Ranked by who is speaking, then by whether it is an article or an index,
    // and only then by the order the searches happened to run in.
    //
    // Order was the whole ranking before, and it argued on behalf of whichever
    // query went first. A question about the F-35 put Slashgear, the Tehran
    // Times and a 2014 Jalopnik story above the answer while every figure in
    // that answer came from Lockheed Martin's own newsroom — which had been
    // read, and was sitting fourth in the list.
    const TIER_RANK = { 'first-party': 0, official: 1, reputable: 2, unknown: 3, low: 4 };
    const best = rail._seen
        .map((s, i) => [TIER_RANK[s.tier] ?? 3, s.kind === 'front' ? 1 : 0, i, s])
        .sort((a, b) => a[0] - b[0] || a[1] - b[1] || a[2] - b[2])
        .slice(0, MAX_CARDS)
        .map(row => row[3]);

    rail.textContent = '';
    for (const source of best) {
        const card = document.createElement('a');
        card.className = 'source-card';
        card.href = source.url;
        card.target = '_blank';
        card.rel = 'noopener noreferrer';
        // Its own hostname, not a remote favicon service: a card that phones
        // out to fetch an icon leaks every source the user was shown.
        const site = source.site || (() => {
            try { return new URL(source.url).hostname.replace(/^www\./, ''); }
            catch (_) { return source.url; }
        })();
        // The tier is on the card, not just behind the ordering. "Reputable"
        // and "someone's blog" look identical as a hostname, and the whole
        // point of ranking them is lost if the reader cannot see which one
        // they are being shown.
        const tier = source.tier && source.tier !== 'unknown' ? source.tier : '';
        card.innerHTML =
            `<div class="source-site"><span class="source-name">${escHtml(site)}</span>`
            + (tier ? `<span class="source-tier tier-${escHtml(tier)}"`
                    + ` title="${escHtml(source.tier_reason || '')}">${escHtml(tier)}</span>` : '')
            + (source.kind === 'front' ? '<span class="source-index">index</span>' : '')
            + `</div>`
            + `<div class="source-title">${escHtml(source.title || source.url)}</div>`
            + (source.date ? `<div class="source-date">${escHtml(source.date)}</div>` : '');
        rail.appendChild(card);
    }
}

function appendMessage(role, content, messageId) {
    clearChatEmpty();
    const messagesEl = document.getElementById('chat-messages');
    const div = document.createElement('div');
    div.className = `message ${role}`;
    // The rendered HTML is not the message. Copy has to hand back what was
    // actually said — markdown and all — not innerText with the formatting
    // flattened out of it, so the source is kept on the element.
    div.dataset.raw = content || '';
    if (messageId != null) div.dataset.messageId = String(messageId);
    const body = role === 'assistant' && content
        ? `<div class="content md">${mdToHtml(content)}</div>`
        : `<div class="content">${escHtml(content)}</div>`;
    div.innerHTML = `<div class="role-label">${role === 'user' ? 'You' : 'Carrot'}</div>${body}`;
    messagesEl.appendChild(div);
    attachMessageActions(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return div;
}

// ===== The plan a run is working to =====
//
// One element per turn, replaced in place as the server re-sends it, so the
// list ticks rather than piling up copies of itself. Rendered above the answer
// because it is the thing you watch while the answer does not exist yet.
//
// Every panel that runs a long job renders the same component from the same
// event, so a plan looks and behaves the same in chat, Research and the Code
// tab — three different-looking progress lists would be three things to learn.

function renderPlan(host, plan) {
    if (!host || !plan || !plan.goals || !plan.goals.length) return null;
    let box = host.querySelector('.plan-box');
    if (!box) {
        box = document.createElement('div');
        box.className = 'plan-box';
        box.innerHTML = '<div class="plan-head">'
            + '<svg class="ico"><use href="#i-check"/></svg><span>Plan</span>'
            + '<span class="plan-count"></span></div><div class="plan-items"></div>';
        // Above whatever the host uses for its prose — `.content` in a chat
        // bubble, `.agent-body` in the Code tab's. Appending instead would put
        // the checklist under the answer, where the one thing it is for
        // (seeing what is left while you wait) cannot happen.
        const content = host.querySelector('.content, .agent-body');
        host.insertBefore(box, content || null);
    }
    const done = new Set(plan.done || []);
    // Steps this revision added, so they can be marked as new rather than
    // just appearing. A list that grows silently between glances reads as a
    // list you misread the first time.
    const added = new Set(plan.added || []);
    const items = box.querySelector('.plan-items');
    items.innerHTML = '';
    for (const goal of plan.goals) {
        const row = document.createElement('div');
        const isDone = done.has(goal);
        row.className = 'plan-item' + (isDone ? ' done' : '')
                      + (added.has(goal) ? ' added' : '');
        row.innerHTML = `<span class="plan-mark">${isDone ? '✓' : '○'}</span>`
            + `<span class="plan-text">${escHtml(goal)}</span>`
            + (added.has(goal) ? '<span class="plan-tag">new</span>' : '');
        items.appendChild(row);
    }

    // A dropped step keeps its row, struck through, with the reason it was
    // dropped. Removing it outright would make the plan shorter and the run
    // look tidier than it was — and the reason is the only thing that lets
    // you tell a plan adapting to what it found from a model talking itself
    // out of the work. It is the single most important thing on this list,
    // so it does not get to disappear.
    for (const drop of (plan.dropped || [])) {
        const row = document.createElement('div');
        row.className = 'plan-item dropped';
        row.innerHTML = `<span class="plan-mark">✕</span>`
            + `<span class="plan-text">${escHtml(drop.step)}</span>`
            + `<span class="plan-why">${escHtml(drop.reason || '')}</span>`;
        items.appendChild(row);
    }

    // Dropped steps are not counted: they are no longer work, and counting
    // them as done would report a run as more complete than it was.
    box.querySelector('.plan-count').textContent = `${done.size}/${plan.goals.length}`;
    box.classList.toggle('complete', done.size === plan.goals.length);
    return box;
}

// ===== Message actions =====
//
// Copy, rerun, branch. All three exist because a chat transcript is not a log
// you only read: you want a paragraph out of it, you want the answer again
// because the first one missed, and you want to ask the question differently
// without losing the answer you are comparing against.
//
// Rerun and branch need a message id, which only exists for messages that came
// back from the server. A turn still streaming has no id yet, so its actions
// appear when the conversation is next opened rather than being offered and
// then failing.

function attachMessageActions(div) {
    if (div.querySelector('.msg-actions')) return;
    const row = document.createElement('div');
    row.className = 'msg-actions';

    row.appendChild(messageAction('Copy', 'i-clipboard', () => copyMessage(div)));

    if (div.dataset.messageId) {
        // Rerun replaces; that is what makes it a rerun rather than asking
        // twice. It only appears on the last answer, because replacing a
        // message from the middle would silently discard everything after it —
        // that is what Branch is for.
        if (div.classList.contains('assistant') && isLastMessage(div)) {
            row.appendChild(messageAction('Rerun', 'i-refresh', () => rerunMessage(div)));
        }
        row.appendChild(messageAction('Branch', 'i-branch', () => branchFromMessage(div)));
    }
    div.appendChild(row);
}

function messageAction(label, icon, onClick) {
    const button = document.createElement('button');
    button.className = 'msg-action';
    button.type = 'button';
    button.title = label;
    button.innerHTML = `<svg class="ico"><use href="#${icon}"/></svg><span>${label}</span>`;
    button.onclick = onClick;
    return button;
}

function isLastMessage(div) {
    const all = document.querySelectorAll('#chat-messages .message');
    return all.length && all[all.length - 1] === div;
}

async function copyMessage(div) {
    const text = div.dataset.raw || div.querySelector('.content')?.innerText || '';
    try {
        await navigator.clipboard.writeText(text);
        flashAction(div, 'Copied');
    } catch (_) {
        // Clipboard access can be refused; a silent no-op looks like a bug.
        flashAction(div, 'Could not copy');
    }
}

// A brief word on the button itself rather than a toast: the feedback belongs
// where the click was, and an alert for a successful copy is worse than none.
function flashAction(div, said) {
    const button = div.querySelector('.msg-action');
    if (!button) return;
    const span = button.querySelector('span');
    if (!span || button.dataset.flashing) return;
    const original = span.textContent;
    button.dataset.flashing = '1';
    span.textContent = said;
    setTimeout(() => {
        span.textContent = original;
        delete button.dataset.flashing;
    }, 1200);
}

async function rerunMessage(div) {
    const previous = div.previousElementSibling;
    const question = previous && previous.classList.contains('user')
        ? previous.dataset.raw : '';
    if (!question) {
        alert('There is no question above this answer to run again.');
        return;
    }
    try {
        // Drop the old answer server-side *before* asking again, or the model
        // is handed a history in which it has already answered.
        await api(`/api/conversations/${currentConversationId}/rewind`, {
            method: 'POST',
            body: JSON.stringify({ message_id: Number(div.dataset.messageId) }),
        });
    } catch (e) {
        alert('Could not clear the old answer: ' + e.message);
        return;
    }
    div.remove();
    await streamTurn('/api/chat/stream', {
        message: question,
        conversation_id: currentConversationId,
        model: autoModel ? null : currentModel,
        provider: autoModel ? null : currentProvider,
        auto: autoModel,
        temporary: temporaryChat,
        memory: useMemory,
        search_mode: currentSearchMode,
        // The question is already in the transcript; rewinding removed only
        // the answer, so re-sending it must not append a duplicate.
        replay: true,
    });
}

async function branchFromMessage(div) {
    try {
        const branch = await api(`/api/conversations/${currentConversationId}/branch`, {
            method: 'POST',
            body: JSON.stringify({ message_id: Number(div.dataset.messageId) }),
        });
        await loadConversations();
        await openConversation(branch.id);
    } catch (e) {
        alert('Could not branch: ' + e.message);
    }
}

async function sendChat() {
    const input = document.getElementById('cmd-input');
    // In Agent mode the same box starts a run. One composer, two things it can
    // mean — which is the whole point of the switch above the transcript.
    if (typeof isChatMode === 'function' && isChatMode('agent')
        && typeof startAgentRun === 'function') {
        const task = input.value.trim();
        if (!task) return;
        input.value = '';
        appendMessage('user', task);
        clearChatEmpty();
        startAgentRun(task);
        return;
    }
    const msg = input.value.trim() + takeArtifactRequest();
    // An attachment on its own is a valid turn ("what is this?").
    if (!msg && !pendingAttachments.length) return;
    const attachments = pendingAttachments.slice();
    input.value = '';
    hideSkillPop();
    switchTab('workspace');
    appendMessage('user', msg + (attachments.length
        ? `\n\n_${attachments.map(a => a.name).join(', ')}_` : ''));
    clearAttachments();
    if (!currentConversationId) {
        document.getElementById('chat-title').textContent = (msg || attachments[0].name).slice(0, 42);
    }

    await streamTurn('/api/chat/stream', {
        message: msg || 'What is in the attached file?',
        attachments: attachments.map(a => ({ name: a.name, mime: a.mime, data: a.data })),
        conversation_id: currentConversationId,
        // Under Auto the turn names no model: an explicit one outranks the
        // classifier, so sending the last-known name would silence it.
        model: autoModel ? null : currentModel,
        provider: autoModel ? null : currentProvider,
        auto: autoModel,
        temporary: temporaryChat,
        // null means "whatever the setting says"; false means "not this turn".
        memory: useMemory,
        skill: activeSkill ? activeSkill.slug : null,
        search_mode: currentSearchMode,
    }, activeSkill);
}

// ===== Attachments =====
// Images go to the model as images (vision models only — the server says so
// plainly rather than dropping them); PDFs and text files are extracted
// server-side and folded into the prompt, so they work with any model.

let pendingAttachments = [];
const ATTACH_MAX_BYTES = 20 * 1024 * 1024;

function attachIcon(mime, name) {
    if ((mime || '').startsWith('image/')) return 'i-image';
    if ((mime || '') === 'application/pdf' || /\.pdf$/i.test(name || '')) return 'i-file-pdf';
    return 'i-doc';
}

function renderAttachTray() {
    const tray = document.getElementById('attach-tray');
    if (!tray) return;
    tray.classList.toggle('hidden', !pendingAttachments.length);
    tray.innerHTML = pendingAttachments.map((a, i) => `
        <span class="attach-chip">
          ${a.thumb
            ? `<img src="${a.thumb}" alt="">`
            : `<svg class="ico"><use href="#${attachIcon(a.mime, a.name)}"/></svg>`}
          <span class="attach-name" title="${escHtml(a.name)}">${escHtml(a.name)}</span>
          <span class="attach-size">${fmtBytes(a.bytes)}</span>
          <button class="attach-x" title="Remove" onclick="removeAttachment(${i})">
            <svg class="ico"><use href="#i-x"/></svg>
          </button>
        </span>`).join('');
}

function removeAttachment(index) {
    pendingAttachments.splice(index, 1);
    renderAttachTray();
}

function clearAttachments() {
    pendingAttachments = [];
    renderAttachTray();
}

async function addAttachments(files) {
    for (const file of Array.from(files || [])) {
        if (file.size > ATTACH_MAX_BYTES) {
            alert(`${file.name} is too large (limit ${fmtBytes(ATTACH_MAX_BYTES)}).`);
            continue;
        }
        const data = await new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(String(reader.result).split(',')[1] || '');
            reader.onerror = reject;
            reader.readAsDataURL(file);
        });
        const isImage = (file.type || '').startsWith('image/');
        // `accept` on the file input only filters the picker. A screenshot
        // pasted in, or a photo dropped on the window, arrives here having
        // never seen it — and would be carried all the way to a 400 on send.
        if (isImage && modelCanSeeImages === false) {
            alert(`${currentModel} cannot read images. Pick a vision model — `
                  + `the Model Hub's 'image: Y' filter lists the ones that fit `
                  + `your machine — or attach a PDF or text file instead.`);
            continue;
        }
        pendingAttachments.push({
            name: file.name, mime: file.type, bytes: file.size, data,
            thumb: isImage ? `data:${file.type};base64,${data}` : null,
        });
    }
    renderAttachTray();
}

// Paste a screenshot straight into the composer, and drop files anywhere.
document.addEventListener('paste', (e) => {
    if (!e.clipboardData || currentTab !== 'workspace') return;
    const files = Array.from(e.clipboardData.files || []);
    if (files.length) { e.preventDefault(); addAttachments(files); }
});
// The drag overlay is switched on by a class, and every way a drag can end
// has to switch it off again. Missing one of them used to leave a full-window
// element on screen that swallowed clicks and keystrokes app-wide, with
// nothing visible to explain it. The overlay is `pointer-events: none` now so
// a miss is harmless, and these make a miss unlikely as well.
function stopDropping() {
    document.body.classList.remove('dropping');
}
document.addEventListener('dragover', (e) => {
    if (e.dataTransfer && e.dataTransfer.types.includes('Files')) {
        e.preventDefault();
        document.body.classList.add('dropping');
    }
});
document.addEventListener('dragleave', (e) => {
    // relatedTarget is null when the pointer leaves the window — but not
    // reliably, and not at all in some Chromium builds. Falling back to the
    // pointer being outside the viewport catches the rest.
    if (e.relatedTarget === null
        || e.clientX <= 0 || e.clientY <= 0
        || e.clientX >= window.innerWidth || e.clientY >= window.innerHeight) {
        stopDropping();
    }
});
// A drag cancelled with Escape, or released outside the window, fires neither
// drop nor a useful dragleave. This is the one event that always arrives.
document.addEventListener('dragend', stopDropping);
window.addEventListener('blur', stopDropping);
document.addEventListener('mousedown', stopDropping);
document.addEventListener('drop', (e) => {
    // Cleared before the early return, not after it: dropping something that
    // is not a file — a text selection, a link — used to leave the overlay up.
    stopDropping();
    if (!e.dataTransfer || !e.dataTransfer.files.length) return;
    e.preventDefault();
    switchTab('workspace');
    addAttachments(e.dataTransfer.files);
});

// ===== Search mode =====
// Three postures for one question: never reach the web, reach it once, or keep
// going until the gaps are closed. The choice is sent with the turn and also
// saved, so it is both a per-message override and a default.

let currentSearchMode = null;
let searchModes = [];

async function loadSearchModes() {
    try {
        const body = await api('/api/chat/search-modes');
        searchModes = body.modes || [];
        currentSearchMode = currentSearchMode || body.current;
        renderSearchModes();
    } catch (e) {
        console.warn('search modes failed', e);
    }
}

function searchModeLabel(id) {
    const mode = searchModes.find(m => m.id === id);
    return mode ? mode.label : 'Search';
}

function renderSearchModes() {
    const label = document.getElementById('search-label');
    if (label) label.textContent = searchModeLabel(currentSearchMode);

    const button = document.getElementById('search-btn');
    if (button) button.classList.toggle('search-off', currentSearchMode === 'off');

    const list = document.getElementById('search-mode-list');
    if (!list) return;
    // A tick on the one that is set, rather than a filled block behind it. The
    // block was the loudest thing in the menu and it was marking the option you
    // had already chosen — the thing you least need drawn to your attention
    // while reading the other two.
    list.innerHTML = searchModes.map(mode => `
        <button class="pop-item${mode.id === currentSearchMode ? ' active' : ''}"
                onclick="setSearchMode('${escHtml(mode.id)}')">
            <span class="pop-item-name">${escHtml(mode.label)}</span>
            <span class="pop-item-sub">${escHtml(mode.help)}</span>
            ${mode.id === currentSearchMode
                ? '<svg class="ico pop-item-check"><use href="#i-check"/></svg>' : ''}
        </button>`).join('');
}

// ===== The plus menu =====
//
// Attach, Temporary, Memory, Council, voice in and voice out. All six are
// settings you touch once, and on the row they crowded out the two controls
// that are live state. Each keeps its id and its handler — the menu is where
// they are drawn, not what they do.

function toggleToolMenu() {
    const pop = document.getElementById('tool-pop');
    if (!pop) return;
    const opening = pop.classList.contains('hidden');
    pop.classList.toggle('hidden');
    document.getElementById('tool-btn')?.classList.toggle('open', opening);
}

function closeToolMenu() {
    document.getElementById('tool-pop')?.classList.add('hidden');
    document.getElementById('tool-btn')?.classList.remove('open');
}

function toggleSearchPop() {
    const pop = document.getElementById('search-pop');
    if (!pop) return;
    const opening = pop.classList.contains('hidden');
    pop.classList.toggle('hidden');
    if (opening) fitPopoverAbove('search-pop', 'search-btn');
}

async function setSearchMode(id) {
    currentSearchMode = id;
    renderSearchModes();
    document.getElementById('search-pop').classList.add('hidden');
    // Persist as the default too — a user who turns search off for a private
    // conversation means it, and should not have to turn it off again.
    try {
        await api('/api/config/chat_search_mode', {
            method: 'PUT', body: JSON.stringify(id),
        });
    } catch (e) {
        console.warn('could not save search mode', e);
    }
}

// ===== The composer's plus menu =====
//
// Three of these are the things you reach for while writing a question, and
// they all point at machinery that already exists. Upload opens the file
// picker, Deep research is the research run, web search is the existing mode
// picker surfaced where you look for it rather than as its own control.

// Deep research, from the conversation. It was a tab, which meant deciding
// before you started typing that this was a research question rather than
// realising it halfway through writing one.
function startDeepResearch() {
    const input = document.getElementById('cmd-input');
    const question = (input?.value || '').trim();
    switchTab('research');
    // Carries the half-written question across rather than making you retype
    // it — realising mid-sentence that this needs research is the common case.
    const target = document.getElementById('research-question');
    if (target && question) {
        target.value = question;
        if (input) input.value = '';
    }
    if (target) target.focus();
}

// The web search mode picker already exists on the composer row. This opens
// that rather than duplicating its state, because two controls for one setting
// is how they end up disagreeing.
function toggleWebSearchFromMenu() {
    closeToolMenu();
    toggleSearchPop();
}

// Searching your own conversations, documents and files. Carries whatever is
// half-typed in the composer across, for the same reason Deep research does.
function openSearchFromMenu() {
    closeToolMenu();
    const input = document.getElementById('cmd-input');
    const query = (input?.value || '').trim();
    switchTab('search');
    const target = document.getElementById('search-input');
    if (target) {
        if (query) { target.value = query; if (input) input.value = ''; }
        target.focus();
        if (query && typeof doSearch === 'function') doSearch();
    }
}

// Artifacts are made when the model calls show_artifact, so there is nothing
// to "open" — what this does is ask for one, by adding the request to the turn
// you are about to send.
//
// A per-turn flag on the request would be tidier, and is the right shape once
// the server has somewhere to put it. This does the same job with no protocol
// change and, importantly, is visible: you can see what was asked for in the
// message you sent, rather than wondering why this reply came back as a chart.
let artifactRequested = false;

function toggleArtifactMode() {
    artifactRequested = !artifactRequested;
    const item = document.getElementById('artifact-item');
    const sub = document.getElementById('artifact-sub');
    if (item) item.classList.toggle('on', artifactRequested);
    if (sub) sub.textContent = artifactRequested
        ? 'on — the next reply will build one'
        : 'build something you can open and use';
}

// Consumed by sendChat. Clears itself, because "make this an artifact" is
// about the turn you are sending and not a mode you leave switched on.
function takeArtifactRequest() {
    if (!artifactRequested) return '';
    toggleArtifactMode();
    return '\n\nBuild this as an artifact I can open and use.';
}

// Renders one streamed turn into the chat view. Shared by the chat box and by
// "send to agent" in Notes, so both get the same tool trace, reasoning panel
// and approval prompts without duplicating any of it.
async function streamTurn(url, payload, skill) {
    const assistantEl = appendMessage('assistant', '');
    const contentEl = assistantEl.querySelector('.content');
    contentEl.innerHTML = '<span class="caret">&nbsp;</span>';

    // Lazily created tool-call trace box (terminal-style, above the answer).
    let toolEl = null;
    function toolLine(text, cls) {
        if (!toolEl) {
            toolEl = document.createElement('div');
            toolEl.className = 'trace tool-trace';
            assistantEl.insertBefore(toolEl, contentEl);
        }
        const div = document.createElement('div');
        div.className = 'trace-line' + (cls ? ' ' + cls : '');
        div.textContent = text;
        toolEl.appendChild(div);
        toolEl.scrollTop = toolEl.scrollHeight;
    }
    if (skill) toolLine('skill: ' + skill.name, 'intent');

    // Lazily created reasoning trace box (for thinking models).
    let thinkEl = null;
    let thinkBody = null;
    function ensureThink() {
        if (thinkEl) return;
        thinkEl = document.createElement('details');
        thinkEl.className = 'think streaming';
        thinkEl.open = true;
        thinkEl.innerHTML = '<summary>Thinking</summary><div class="think-body"></div>';
        thinkBody = thinkEl.querySelector('.think-body');
        assistantEl.insertBefore(thinkEl, contentEl);
    }
    function finishThink() {
        if (!thinkEl) return;
        thinkEl.classList.remove('streaming');
        thinkEl.querySelector('summary').textContent = 'Thought process';
        thinkEl.open = false;
    }

    try {
        // The abort is the backstop, not the mechanism. Aborting alone closes
        // the socket and leaves the provider call running and billing, and
        // throws away the half-answer the user was reading — so the button
        // asks the server to stop first and only aborts if that fails.
        chatAbort = new AbortController();
        setChatRunning(true);
        const resp = await fetch(url, {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify(payload),
            signal: chatAbort.signal,
        });
        if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || resp.statusText);

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let full = '';
        // Set if the turn ended by asking rather than answering.
        let askedQuestions = null;
        let wasStopped = false;
        const pendingArtifacts = [];
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = buffer.indexOf('\n\n')) !== -1) {
                const raw = buffer.slice(0, idx).trim();
                buffer = buffer.slice(idx + 2);
                if (!raw.startsWith('data:')) continue;
                // One malformed frame used to throw out of the whole read loop
                // and discard every token that had already arrived. Skip it.
                let payload;
                try {
                    payload = JSON.parse(raw.slice(5).trim());
                } catch (err) {
                    continue;
                }
                const box = document.getElementById('chat-messages');
                if (payload.skill) toolLine('skill active: ' + payload.skill.name, 'intent');
                if (payload.route) {
                    // Always say where the answer came from — local vs hosted is
                    // the single most important thing to be honest about here.
                    const where = payload.route.local ? 'on-device' : payload.route.provider;
                    // A model the user did not choose has to say why it was
                    // chosen — otherwise Auto is just the model changing
                    // underneath you between one message and the next.
                    const why = payload.route.auto && payload.route.reason
                        ? ` — ${payload.route.reason.replace(/^auto: /, '')}` : '';
                    toolLine(`${payload.route.model} (${where})${why}`, 'intent');
                }
                // What the run is actually trying to find out, ticking as it
                // finds it. A long run gave no sense of this at all — you
                // watched searches go past with no way to tell whether any of
                // them were the point, or how much was left.
                if (payload.plan && payload.plan.goals) {
                    renderPlan(assistantEl, payload.plan);
                }
                // Why an answer was pushed back. Shown because a turn that
                // silently takes four extra rounds looks like it is stuck.
                if (payload.gate) {
                    const unmet = (payload.gate.unmet || []).length;
                    toolLine(unmet
                        ? `  ↻ ${unmet} thing(s) from the plan still unanswered`
                        : '  ↻ sent back: ' + String(payload.gate.reason).split('\n')[0],
                        'intent');
                }
                if (payload.document) {
                    // A doc send reports what it actually attached, before any
                    // tokens arrive — a citation that silently failed is worse
                    // than useless, so failures are shown too.
                    for (const ref of payload.document.references || []) {
                        toolLine(`${ref.raw} ${ref.ok ? '✓' : '✗'} ${ref.detail}`,
                                 ref.ok ? 'search' : 'error');
                    }
                    for (const warning of payload.document.warnings || []) {
                        toolLine(warning, 'error');
                    }
                }
                if (payload.tool) {
                    // A rejected call used to print exactly like a real one and
                    // then show no result — four of those in a row is what a
                    // dead turn looked like from the outside, with nothing on
                    // screen to say why. Say why.
                    const kind = payload.tool.rejected ? 'error' : 'search';
                    const why = payload.tool.rejected
                        ? `  ✗ not run: ${payload.tool.reason || 'refused'}` : '';
                    toolLine(`tool → ${payload.tool.name}(${JSON.stringify(payload.tool.args)})${why}`, kind);
                }
                // A step handed to another model. Named in the trace, with
                // the model that answered it: the delegating model tends to
                // absorb the reply as its own, and "which model actually
                // said this" is not recoverable from the prose afterwards.
                if (payload.delegation) {
                    const d = payload.delegation;
                    toolLine(`asked ${d.provider}/${d.model}`
                             + (d.local ? '' : ' (cloud)')
                             + ` — ${d.question}`, 'intent');
                }
                if (payload.provider_error) {
                    toolLine(`${payload.route ? '' : ''}provider stopped the turn: `
                             + payload.provider_error.message, 'error');
                }
                if (payload.tool_result) {
                    const raw = String(payload.tool_result.result);
                    // show_artifact answers with a marker the UI swaps for the
                    // rendered thing; the raw marker is noise in the trace.
                    for (const id of artifactIdsIn(raw)) pendingArtifacts.push(id);
                    toolLine(`  ← ${stripArtifactMarkers(raw).slice(0, 160)}`, 'stage');
                }
                if (payload.approval_request) {
                    // The line goes in the trace first, because that is where
                    // the user is looking. The card is in the corner and can
                    // be read past — and a card read past is a turn that dies
                    // at the timeout with nothing on screen explaining why.
                    toolLine(`  ⏸ waiting for you: ${payload.approval_request.summary}`
                             + ' — see the card, bottom right', 'intent');
                    showApprovalPrompt(payload.approval_request);
                }
                if (payload.approval_waiting) noteApprovalWaiting(payload.approval_waiting);
                if (payload.approval_resolved) {
                    dismissApprovalPrompt(payload.approval_resolved.id);
                    if (payload.approval_resolved.decision === 'timeout') {
                        toolLine('  ⏸ nobody answered, so that action did not run.'
                                 + ' Settings → Security can stop the asking.', 'error');
                    }
                }
                // What the answer is about to be built from, shown before the
                // answer arrives. The trace already listed the URLs, but as
                // debug output nobody reads — these are the sources as a
                // person would want them: outlet, headline, date, clickable.
                if (payload.sources) showSources(assistantEl, contentEl, payload.sources);
                if (payload.thinking) {
                    ensureThink();
                    thinkBody.textContent += payload.thinking;
                    thinkBody.scrollTop = thinkBody.scrollHeight;
                    box.scrollTop = box.scrollHeight;
                }
                if (payload.chunk) {
                    if (!full) finishThink();
                    full += payload.chunk;
                    contentEl.innerHTML = mdToHtml(full);
                    box.scrollTop = box.scrollHeight;
                }
                // The turn ended by asking. The backend has already cut off
                // everything the model wrote after the question, so `full` is
                // the preamble and nothing more — there is no answer here to
                // be overwritten by rendering the form.
                if (payload.questions) {
                    askedQuestions = payload;
                }
                // Sent as the first frame, before any model call — the wait a
                // user most wants to end is the one before a token appears.
                if (payload.turn_id) currentTurnId = payload.turn_id;
                if (payload.stopped) wasStopped = true;
                if (payload.done && payload.conversation_id) {
                    currentConversationId = payload.conversation_id;
                }
            }
        }
        finishThink();
        contentEl.classList.add('md');
        // "(no response)" told the user nothing. The backend now guarantees
        // text on every path, so reaching here means the connection itself
        // died mid-turn — say that, since it is the one thing the browser
        // knows and the server never will.
        //
        // Unless the turn ended on a question, where empty prose is the
        // correct outcome and not a dropped connection: the model asked
        // before it said anything, which is the good version of this.
        //
        // Nor if the user stopped it. A half-answer they chose to cut short is
        // not a failed turn, and telling them the backend may have restarted
        // when they pressed the button themselves is nonsense.
        if (askedQuestions && !full) {
            contentEl.innerHTML = '';
        } else if (wasStopped) {
            contentEl.innerHTML = mdToHtml(full || '');
            const note = document.createElement('div');
            note.className = 'stopped-note';
            note.textContent = 'Stopped.';
            contentEl.appendChild(note);
        } else {
            contentEl.innerHTML = full ? mdToHtml(full) : mdToHtml(
                'The connection to Carrot ended before any answer arrived. The '
                + 'backend may have restarted — check that it is running, then ask '
                + 'again. Anything the turn found is in the trace above.');
        }
        if (askedQuestions) {
            chatQuestions(assistantEl, askedQuestions.questions,
                          askedQuestions.blocking);
        }
        // Charts and diagrams land under the finished answer, in the order the
        // model produced them.
        if (pendingArtifacts.length && typeof mountArtifacts === 'function') {
            mountArtifacts(contentEl.parentElement,
                           pendingArtifacts.map(id => `[[carrot:artifact:${id}]]`).join(' '));
        }
        if (speakReplies && full) speakText(full);
        // A commitment Carrot noticed, offered rather than assumed. Runs
        // after the answer because the proposal is written by the same
        // post-turn bookkeeping that writes memories, and asking before that
        // has finished would find nothing.
        if (typeof mountGoalChips === 'function') mountGoalChips(assistantEl);
        // Copy needs the markdown, not the rendered HTML, and the answer only
        // exists now that the stream has finished.
        assistantEl.dataset.raw = full;
        // Rerun and branch need ids the server assigns, and the moment you
        // most want "run that again" is right after reading the answer — not
        // after reopening the conversation. So the ids are collected as soon
        // as the turn lands rather than on the next load.
        await syncMessageIds();
    } catch (e) {
        // An abort is the user pressing stop, not a failure, and painting it
        // red is telling them their own action went wrong. It only gets here
        // when the server-side stop did not take and the fetch was cut.
        if (e.name === 'AbortError') {
            contentEl.classList.add('md');
            const note = document.createElement('div');
            note.className = 'stopped-note';
            note.textContent = 'Stopped.';
            contentEl.appendChild(note);
        } else {
            contentEl.textContent = e.message;
            contentEl.classList.add('error');
        }
    } finally {
        setChatRunning(false);
        chatAbort = null;
        currentTurnId = null;
        clearActiveSkill();
    }
}

// ===== Stopping a turn =====
//
// Research and Agent have had a kill switch since they were written. Chat had
// none, and a multi-turn search that has decided to read six more pages is the
// longest thing the app does. Closing the tab was the only way out, which
// leaves the provider call running and discards whatever had been written.

let chatAbort = null;
let currentTurnId = null;

function setChatRunning(running) {
    // Send and Stop share a slot and swap, so the button under the cursor is
    // always the one that applies.
    document.getElementById('send-btn')?.classList.toggle('hidden', running);
    document.getElementById('stop-btn')?.classList.toggle('hidden', !running);
}

async function stopChat() {
    // In Agent mode the same button stops the run, for the same reason the
    // send button starts one: from the outside they are the same act.
    if (typeof isChatMode === 'function' && isChatMode('agent')
        && typeof agentRunId !== 'undefined' && agentRunId) {
        if (typeof stopAgentRun === 'function') await stopAgentRun();
        return;
    }
    // Ask the server first. That stops the provider call, keeps the text
    // already written, and stores it — none of which aborting the fetch does.
    const stopped = currentTurnId && await api(
        `/api/chat/turns/${currentTurnId}/stop`, { method: 'POST' },
    ).then(r => r.stopped).catch(() => false);
    // The abort is the fallback for a turn the server no longer knows about,
    // or a backend that has gone away. Doing it unconditionally would race the
    // clean stop and throw away the partial answer the clean stop preserves.
    if (!stopped && chatAbort) chatAbort.abort();
    // Disabled rather than hidden: a stop that is still settling should not
    // look ignored, and it should not be pressable twice.
    const button = document.getElementById('stop-btn');
    if (button) button.disabled = true;
    setTimeout(() => { if (button) button.disabled = false; }, 2000);
}

// Stamp stored ids onto messages that were rendered while streaming.
//
// Matched from the end backwards. The rendered list can be shorter than the
// stored one — an older conversation scrolled off, a trace box between turns —
// but the *last* n messages are always the same n, because that is the end
// both of them just grew from.
async function syncMessageIds() {
    if (!currentConversationId) return;
    let stored;
    try {
        stored = (await api(`/api/conversations/${currentConversationId}`)).messages || [];
    } catch (_) {
        return;   // Actions are a convenience; failing to get them is not an error.
    }
    const rendered = [...document.querySelectorAll('#chat-messages .message')];
    for (let i = 1; i <= Math.min(rendered.length, stored.length); i++) {
        const div = rendered[rendered.length - i];
        const message = stored[stored.length - i];
        // A mismatched role means the two lists have drifted; stop rather than
        // hang an id on the wrong message, which is how Rerun would delete
        // something nobody pointed at.
        if (!div.classList.contains(message.role)) break;
        div.dataset.messageId = String(message.id);
        const row = div.querySelector('.msg-actions');
        if (row) row.remove();
        attachMessageActions(div);
    }
}

// The clarifying questions a chat turn ended on, as a form.
//
// The coder panel has had this since plans got questions; chat emitted the
// same event and nothing listened, so in chat the questions were invisible
// *and* self-answered. This is the listener, plus the one thing the coder
// version does not need: a turn that asked before saying anything is waiting,
// and has to look like it is waiting rather than like it failed.
//
// `blocking` comes from the server, which is the only place that knows how the
// turn ended. Re-deriving it here from the length of the prose would be a
// second opinion on a question that already has an answer, and the two would
// disagree the first time either changed.
function chatQuestions(wrap, questions, blocking) {
    if (!questions || !questions.length) return;
    if (wrap.querySelector('.agent-questions')) return;   // one form per turn

    const box = document.createElement('div');
    box.className = 'agent-questions' + (blocking ? ' blocking' : '');
    const head = document.createElement('div');
    head.className = 'questions-head';
    head.textContent = blocking
        ? 'Waiting on you — answer what matters, skip the rest.'
        : 'Want to adjust any of this?';
    box.appendChild(head);

    const chosen = new Map();
    questions.forEach((q, i) => {
        const field = document.createElement('div');
        field.className = 'question';
        const label = document.createElement('div');
        label.className = 'question-text';
        label.textContent = q.question;
        field.appendChild(label);

        const row = document.createElement('div');
        row.className = 'question-options';
        q.options.forEach((option, j) => {
            const button = document.createElement('button');
            button.type = 'button';
            button.className = 'question-option';
            button.textContent = option;
            // The first option is pre-selected, so the form already reads as
            // the answer you get by skipping it.
            if (j === 0) { button.classList.add('on'); chosen.set(i, option); }
            button.onclick = () => {
                row.querySelectorAll('.question-option').forEach(b => b.classList.remove('on'));
                button.classList.add('on');
                chosen.set(i, option);
                custom.value = '';
            };
            row.appendChild(button);
        });
        field.appendChild(row);

        const custom = document.createElement('input');
        custom.type = 'text';
        custom.className = 'question-custom';
        custom.placeholder = 'or say something else…';
        custom.oninput = () => {
            if (!custom.value.trim()) return;
            row.querySelectorAll('.question-option').forEach(b => b.classList.remove('on'));
            chosen.set(i, custom.value.trim());
        };
        field.appendChild(custom);
        box.appendChild(field);
    });

    const actions = document.createElement('div');
    actions.className = 'questions-actions';

    const go = document.createElement('button');
    go.className = 'btn btn-primary';
    go.textContent = blocking ? 'Answer' : 'Redo with these';
    go.onclick = () => submitChatQuestions(box, questions, chosen);

    const skip = document.createElement('button');
    skip.className = 'btn btn-ghost';
    // Skipping is a real answer in the blocking case — it accepts the model's
    // own first option for each, which is what "just pick something" means.
    // In the refinement case there is already an answer, so skipping is just
    // leaving it alone, and saying "defaults" would misdescribe that.
    skip.textContent = blocking
        ? 'Skip — just pick sensible defaults'
        : 'Leave it as is';
    skip.onclick = () => {
        if (!blocking) { box.remove(); return; }
        questions.forEach((q, i) => chosen.set(i, q.options[0]));
        submitChatQuestions(box, questions, chosen);
    };

    actions.append(go, skip);
    box.appendChild(actions);
    wrap.appendChild(box);
    const messages = document.getElementById('chat-messages');
    if (messages) messages.scrollTop = 1e9;
}

async function submitChatQuestions(box, questions, chosen) {
    box.querySelectorAll('button, input').forEach(el => { el.disabled = true; });
    const pairs = questions.map((q, i) => ({ question: q.question, answer: chosen.get(i) || '' }));
    const answered = pairs.filter(p => p.answer);
    box.querySelector('.questions-head').textContent =
        answered.map(p => `${p.question} — ${p.answer}`).join('; ') || 'Using the defaults.';

    const input = document.getElementById('cmd-input');
    input.value = 'Answers to your questions:\n'
        + answered.map(p => `- ${p.question} — ${p.answer}`).join('\n')
        + '\n\nGo ahead on that basis.';
    await sendChat();
}

function newChat() {
    currentConversationId = null;
    document.getElementById('chat-title').textContent = 'New session';
    const messagesEl = document.getElementById('chat-messages');
    messagesEl.innerHTML = `
        <div class="chat-empty" id="chat-empty">
            <h1 class="chat-empty-title">Where should we begin?</h1>
            <p id="chat-empty-line" class="chat-empty-note"></p>
        </div>`;
    renderEmptyStateLine();
    switchTab('workspace');
    syncChatBlank();
    focusCmd();
}

// ===== Conversations (Chats tab) =====
let chatCollapsed = {};
let chatFoldersCache = [];
let chatNewFolderOpen = false;
let chatRenamingFolder = null;

async function loadConversations() {
    const listEl = document.getElementById('conversations-list');
    try {
        const [convs, folders] = await Promise.all([
            api('/api/conversations?limit=200'),
            api('/api/chat-folders'),
        ]);
        chatFoldersCache = folders;
        listEl.innerHTML = '';
        if (chatNewFolderOpen) {
            listEl.appendChild(folderEditorRow('', createFolderSubmit, cancelNewFolder));
        }
        if (!convs.length && !folders.length) {
            if (!chatNewFolderOpen) {
                listEl.innerHTML = '<div class="empty">No conversations yet. Start one from the command bar below.</div>';
            }
            return;
        }
        const starred = convs.filter(c => (c.metadata || {}).starred);
        const unfiled = convs.filter(c => !(c.metadata || {}).folder_id);

        if (starred.length) {
            listEl.appendChild(chatSection({ id: '__starred', name: 'Starred', icon: 'i-star' }, starred, folders, false));
        }
        for (const f of folders) {
            const inFolder = convs.filter(c => (c.metadata || {}).folder_id === f.id);
            listEl.appendChild(chatSection(f, inFolder, folders, true));
        }
        listEl.appendChild(chatSection({ id: '', name: 'All chats', icon: 'i-chat' }, unfiled, folders, false));
    } catch (e) {
        listEl.innerHTML = `<div class="empty">${escHtml(e.message)}</div>`;
    }
}

// Inline folder name editor (works in Electron, unlike prompt()).
function folderEditorRow(initial, onSave, onCancel) {
    const row = document.createElement('div');
    row.className = 'chat-folder-editor';
    row.innerHTML = `
        <svg class="ico chat-section-ico"><use href="#i-folder"/></svg>
        <input type="text" class="chat-folder-input" placeholder="Folder name" value="${escHtml(initial)}">
        <button class="btn btn-primary">Save</button>
        <button class="btn btn-ghost">Cancel</button>`;
    const input = row.querySelector('input');
    const btns = row.querySelectorAll('button');
    const save = () => { const v = input.value.trim(); if (v) onSave(v); };
    btns[0].onclick = save;
    btns[1].onclick = () => onCancel();
    input.onkeydown = (e) => {
        if (e.key === 'Enter') save();
        else if (e.key === 'Escape') onCancel();
    };
    setTimeout(() => { input.focus(); input.select(); }, 0);
    return row;
}

function chatSection(section, convs, folders, isFolder) {
    if (isFolder && section.id === chatRenamingFolder) {
        return folderEditorRow(
            section.name,
            (name) => submitRenameFolder(section.id, name),
            () => { chatRenamingFolder = null; loadConversations(); }
        );
    }
    const key = section.id || '__all';
    const collapsed = !!chatCollapsed[key];
    const wrap = document.createElement('div');
    wrap.className = 'chat-section';
    const head = document.createElement('div');
    head.className = 'chat-section-head';
    head.innerHTML = `
        <svg class="ico chev chat-chev${collapsed ? '' : ' open'}"><use href="#i-chevron"/></svg>
        <svg class="ico chat-section-ico"><use href="#${section.icon || 'i-folder'}"/></svg>
        <span class="chat-section-name">${escHtml(section.name)}</span>
        <span class="chat-section-count">${convs.length}</span>
        ${isFolder ? `
          <button class="icon-btn" title="Rename folder" onclick="event.stopPropagation();renameFolder('${section.id}')"><svg class="ico"><use href="#i-edit"/></svg></button>
          <button class="icon-btn" title="Delete folder" onclick="event.stopPropagation();deleteFolder('${section.id}')"><svg class="ico"><use href="#i-trash"/></svg></button>
        ` : ''}`;
    head.onclick = () => { chatCollapsed[key] = !chatCollapsed[key]; loadConversations(); };
    wrap.appendChild(head);

    const body = document.createElement('div');
    body.className = 'chat-section-body' + (collapsed ? ' hidden' : '');
    if (!convs.length) {
        body.innerHTML = '<div class="empty small">No chats here.</div>';
    } else {
        for (const c of convs) body.appendChild(chatRow(c, folders));
    }
    wrap.appendChild(body);
    return wrap;
}

function chatRow(c, folders) {
    const meta = c.metadata || {};
    const div = document.createElement('div');
    div.className = 'chat-row';
    const dateStr = (c.updated_at || c.created_at || '').slice(0, 10);
    const folderOpts = ['<option value="">No folder</option>'].concat(
        folders.map(f => `<option value="${f.id}"${meta.folder_id === f.id ? ' selected' : ''}>${escHtml(f.name)}</option>`)
    ).join('');
    div.innerHTML = `
        <button class="chat-star${meta.starred ? ' on' : ''}" title="${meta.starred ? 'Unstar' : 'Star'}" onclick="toggleStar('${c.id}', ${meta.starred ? 'false' : 'true'})">
          <svg class="ico"><use href="#i-star"/></svg>
        </button>
        <div class="chat-row-main" onclick="openConversation('${c.id}')">
          <div class="chat-row-title">${escHtml(c.title || 'Untitled')}</div>
          <div class="chat-row-sub">${escHtml(dateStr)}</div>
        </div>
        <select class="chat-folder-select" title="Move to folder" onchange="moveToFolder('${c.id}', this.value)" onclick="event.stopPropagation()">
          ${folderOpts}
        </select>
        <button class="icon-btn" title="Delete chat" onclick="deleteChat('${c.id}')"><svg class="ico"><use href="#i-trash"/></svg></button>`;
    return div;
}

async function toggleStar(convId, starred) {
    try { await api(`/api/conversations/${convId}`, { method: 'PATCH', body: JSON.stringify({ starred }) }); loadConversations(); }
    catch (e) { alert('Could not update chat: ' + e.message); }
}

async function moveToFolder(convId, folderId) {
    try { await api(`/api/conversations/${convId}`, { method: 'PATCH', body: JSON.stringify({ folder_id: folderId }) }); loadConversations(); }
    catch (e) { alert('Could not move chat: ' + e.message); }
}

async function deleteChat(convId) {
    if (!confirm('Delete this chat and its messages?')) return;
    try { await api(`/api/conversations/${convId}`, { method: 'DELETE' }); loadConversations(); }
    catch (e) { alert('Could not delete chat: ' + e.message); }
}

async function newFolder() {
    chatNewFolderOpen = true;
    loadConversations();
}

async function createFolderSubmit(name) {
    chatNewFolderOpen = false;
    try { await api('/api/chat-folders', { method: 'POST', body: JSON.stringify({ name }) }); }
    catch (e) { alert('Could not create folder: ' + e.message); }
    loadConversations();
}

function cancelNewFolder() {
    chatNewFolderOpen = false;
    loadConversations();
}

function renameFolder(folderId) {
    chatRenamingFolder = folderId;
    loadConversations();
}

async function submitRenameFolder(folderId, name) {
    chatRenamingFolder = null;
    try { await api(`/api/chat-folders/${folderId}`, { method: 'PUT', body: JSON.stringify({ name }) }); }
    catch (e) { alert('Could not rename folder: ' + e.message); }
    loadConversations();
}

async function deleteFolder(folderId) {
    if (!confirm('Delete this folder? Chats inside will move back to All chats.')) return;
    try { await api(`/api/chat-folders/${folderId}`, { method: 'DELETE' }); loadConversations(); }
    catch (e) { alert('Could not delete folder: ' + e.message); }
}

async function openConversation(convId) {
    currentConversationId = convId;
    const conv = await api(`/api/conversations/${convId}`);
    const messagesEl = document.getElementById('chat-messages');
    messagesEl.innerHTML = '';
    document.getElementById('chat-title').textContent = conv.title || 'Untitled';
    const rendered = conv.messages.map(m => {
        const el = appendMessage(m.role, m.content, m.id);
        // What the turn actually did. Rendered live and then lost on every
        // reload — reopening a chat gave you the prose and none of the
        // evidence, which is the half you cannot reconstruct by reading.
        replayTrace(el, (m.metadata || {}).trace);
        // A turn that ended on a question is not finished, and the form is the
        // only way to finish it. Restoring the prose without it would leave a
        // conversation stopped mid-sentence with no way forward — the user
        // would retype the whole request, which is the dead end the form was
        // built to remove.
        const meta = m.metadata || {};
        if (meta.questions && meta.questions.length) {
            chatQuestions(el, meta.questions, !!meta.awaiting_answers);
        }
        return el;
    });
    // Charts made earlier in this conversation are part of it — reopening a
    // chat and finding the figures gone would make them feel disposable.
    if (typeof mountArtifacts === 'function') {
        try {
            const { artifacts } = await api(`/api/conversations/${convId}/artifacts`);
            const last = rendered[rendered.length - 1];
            const host = last && last.querySelector('.content');
            if (host && artifacts && artifacts.length) {
                mountArtifacts(host.parentElement,
                    artifacts.map(a => `[[carrot:artifact:${a.id}]]`).join(' '));
            }
        } catch (_) { /* older conversation, or none stored */ }
    }
    // An undecided proposal is a question, and a question that disappears on
    // reload is one the user never got to answer. Reopening the conversation
    // asks it again, under the last reply.
    if (typeof mountGoalChips === 'function') {
        const last = rendered[rendered.length - 1];
        if (last) mountGoalChips(last, convId);
    }
    switchTab('workspace');
}

// Rebuild a stored trace above a message that has already been rendered.
//
// The same shapes the live stream draws, from the same event names, so a
// reopened turn reads exactly like the one you watched. It is deliberately
// one-way and lossy: the stored events carry a clipped tool result, and the
// plan is drawn at whatever state it finished in rather than re-ticking.
// Replaying it as an animation would be a re-enactment, not a record.
function replayTrace(messageEl, trace) {
    if (!messageEl || !Array.isArray(trace) || !trace.length) return;
    const content = messageEl.querySelector('.content');
    const box = document.createElement('div');
    box.className = 'trace tool-trace';
    messageEl.insertBefore(box, content || null);

    const line = (text, cls) => {
        const div = document.createElement('div');
        div.className = 'trace-line' + (cls ? ' ' + cls : '');
        div.textContent = text;
        box.appendChild(div);
    };

    let plan = null;
    for (const event of trace) {
        // Collapsed, and labelled the way a finished one is labelled live.
        // Reopening a conversation and finding the reasoning gone made the
        // turn look like it had simply produced an answer from nowhere.
        if (event.thinking) {
            const block = document.createElement('details');
            block.className = 'think';
            block.innerHTML = '<summary>Thought process</summary>'
                + `<div class="think-body">${escHtml(event.thinking)}</div>`;
            box.appendChild(block);
        }
        if (event.skill) line('skill: ' + event.skill.name, 'intent');
        if (event.search_mode) line('search: ' + event.search_mode, 'intent');
        // Replayed as well as streamed. Which model answered which step is
        // exactly the sort of thing you want to check *after* reading an
        // answer, which means after the page has been reloaded.
        if (event.delegation) {
            const d = event.delegation;
            line(`asked ${d.provider}/${d.model}${d.local ? '' : ' (cloud)'} — ${d.question}`,
                 'intent');
        }
        if (event.plan) plan = event.plan;          // the last one is the outcome
        if (event.gate) {
            const unmet = (event.gate.unmet || []).length;
            line(unmet ? `  ↻ ${unmet} thing(s) from the plan still unanswered`
                       : '  ↻ sent back: ' + String(event.gate.reason).split('\n')[0],
                 'intent');
        }
        if (event.tool) {
            const why = event.tool.rejected
                ? `  ✗ not run: ${event.tool.reason || 'refused'}` : '';
            line(`tool → ${event.tool.name}(${JSON.stringify(event.tool.args)})${why}`,
                 event.tool.rejected ? 'error' : 'search');
        }
        if (event.tool_result) line('← ' + String(event.tool_result.result), 'result');
        if (event.provider_error) line('provider: ' + event.provider_error.message, 'error');
        if (event.error) line('error: ' + event.error, 'error');
    }
    if (plan && plan.goals) renderPlan(messageEl, plan);
    if (!box.childElementCount) box.remove();
}

// ===== Which panels the conversation page shows =====
//
// Four cards nobody chose, on the page you open most. A morning recap and a
// deadline list are useful to some people and pure noise to others, and the
// home page is the worst place to guess — so it is a choice, and it lives
// next to the panels rather than three levels into Settings.
//
// Local-first for the same reason the theme is: the rail paints before any
// network call finishes, and a panel you switched off must not flash on and
// then vanish. The server copy is a best-effort mirror, never waited on.

const RAIL_PANELS = [
    { id: 'recap', label: 'Morning Recap', sub: 'your daily briefing' },
    { id: 'deadlines', label: 'Deadlines', sub: 'what is due' },
    { id: 'milestones', label: 'Milestones', sub: 'progress on goals' },
    { id: 'engine', label: 'This machine', sub: 'what is running, and where' },
];
const RAIL_KEY = 'carrot.rail';

// Absent means all of them. A new panel added in a later version is therefore
// shown by default rather than silently hidden by an older stored list.
let railHidden = readRailPref();

function readRailPref() {
    try {
        const raw = localStorage.getItem(RAIL_KEY);
        if (!raw) return [];
        const parsed = JSON.parse(raw);
        const known = RAIL_PANELS.map(p => p.id);
        return Array.isArray(parsed) ? parsed.filter(id => known.includes(id)) : [];
    } catch (e) {
        return [];
    }
}

function applyRail() {
    for (const panel of RAIL_PANELS) {
        const card = document.querySelector(`#ws-left .card[data-panel="${panel.id}"]`);
        if (card) card.classList.toggle('hidden', railHidden.includes(panel.id));
    }
    // With everything off the rail goes away rather than standing there empty,
    // and the conversation gets the width back. That is the whole point of
    // switching them off — a 320px column with nothing in it is not less
    // clutter, it is the same clutter with the content removed.
    document.getElementById('ws-left')
        ?.classList.toggle('hidden', railHidden.length >= RAIL_PANELS.length);
    renderRailMenu();
}

function renderRailMenu() {
    const list = document.getElementById('rail-pop-list');
    if (!list) return;
    list.innerHTML = '';
    for (const panel of RAIL_PANELS) {
        const on = !railHidden.includes(panel.id);
        const row = document.createElement('button');
        row.className = 'tool-item' + (on ? ' on' : '');
        row.onclick = () => toggleRailPanel(panel.id);
        row.innerHTML = `<svg class="ico"><use href="#i-${on ? 'check' : 'x'}"/></svg>`
            + `<span>${escHtml(panel.label)}</span>`
            + `<span class="tool-sub">${escHtml(panel.sub)}</span>`;
        list.appendChild(row);
    }
}

function toggleRailPanel(id) {
    railHidden = railHidden.includes(id)
        ? railHidden.filter(x => x !== id)
        : railHidden.concat([id]);
    applyRail();
    try { localStorage.setItem(RAIL_KEY, JSON.stringify(railHidden)); } catch (e) { /* ignore */ }
    if (typeof api === 'function') {
        api('/api/config/ui_rail_hidden', {
            method: 'PUT', body: JSON.stringify(railHidden),
        }).catch(() => {});
    }
}

// The composer floats over the workspace, so the column underneath has to be
// told how much room to leave. This was a flat 84px in the stylesheet, which
// was wrong the moment the bar grew a second row and had never been right with
// the attachment tray open — the terminal ended up underneath it. Measured
// rather than guessed, because the bar's height is genuinely variable: one
// row or two, tray or no tray, one skill chip or none.
function watchComposerHeight() {
    const bar = document.getElementById('cmdbar');
    if (!bar) return;
    const tray = document.getElementById('attach-tray');
    const apply = () => {
        const box = bar.getBoundingClientRect();
        let top = box.top;
        // The attachment tray is `position: absolute; bottom: 100%` — it hangs
        // *above* the bar, outside its border box, so the bar's own height
        // does not include it. Attach a file and the chip is the topmost thing
        // in the composer; measuring only the bar puts it over the terminal,
        // which is the exact case that was reported.
        if (tray && !tray.classList.contains('hidden')) {
            const trayBox = tray.getBoundingClientRect();
            if (trayBox.height) top = Math.min(top, trayBox.top);
        }
        const h = Math.round(box.bottom - top);
        if (h > 0) document.documentElement.style.setProperty('--composer-h', h + 'px');
    };
    apply();
    if (window.ResizeObserver) {
        const observer = new ResizeObserver(apply);
        observer.observe(bar);
        if (tray) observer.observe(tray);
    } else {
        window.addEventListener('resize', apply);
    }
    // Both the tray and the bar itself are shown and hidden by class. A
    // `display: none` element measures zero and a ResizeObserver does not
    // report it, so on any tab other than the workspace the reserve would
    // never be computed at all — which is why the variable was coming back
    // empty on first load, the bar being hidden until you open the tab.
    if (window.MutationObserver) {
        const watch = { attributes: true, attributeFilter: ['class'], childList: true };
        new MutationObserver(apply).observe(bar, watch);
        if (tray) new MutationObserver(apply).observe(tray, watch);
    }
}

function toggleRailMenu() {
    const pop = document.getElementById('rail-pop');
    if (!pop) return;
    pop.classList.toggle('hidden');
}

function closeRailMenu() {
    document.getElementById('rail-pop')?.classList.add('hidden');
}

// For a machine whose localStorage was cleared. A stored local choice always
// wins: it is the more recent expression of intent, and it is what already
// painted.
function syncRailFromServer(cfg) {
    let stored = null;
    try { stored = localStorage.getItem(RAIL_KEY); } catch (e) { /* ignore */ }
    if (stored || !Array.isArray(cfg?.ui_rail_hidden)) return;
    const known = RAIL_PANELS.map(p => p.id);
    railHidden = cfg.ui_rail_hidden.filter(id => known.includes(id));
    applyRail();
}

// ===== Workspace cards =====
async function loadWorkspace() {
    // The rail and its four cards are gone, so the three calls that filled
    // them are gone with it. Leaving them in would have cost a recap, a
    // deadline list and a milestone query on every visit to the page people
    // open most, all of it rendering into elements that no longer exist.
    //
    // refreshStatus stays: it also drives the engine dot in the menu bar and
    // the privacy line under the heading, neither of which was that panel.
    refreshStatus();
}

async function loadRecapCard() {
    const el = document.getElementById('card-recap');
    try {
        const briefing = await api('/api/recap/briefing/today');
        if (briefing.available && briefing.markdown) {
            el.innerHTML = `<div class="recap-briefing md">${mdToHtml(briefing.markdown)}</div>`;
            return;
        }
        const recaps = await api('/api/recap');
        if (!recaps.length) {
            el.innerHTML = '<div class="empty">No briefing yet today. Run one to research your morning digest.</div>';
            return;
        }
        el.innerHTML = '';
        for (const r of recaps.slice(0, 3)) {
            const row = document.createElement('div');
            row.className = 'krow';
            row.innerHTML = `<span class="k-dot"></span><span class="k-main">${escHtml(r.title)}</span><span class="k-sub">${escHtml((r.created_at || '').slice(5, 10))}</span>`;
            el.appendChild(row);
        }
    } catch (_) {
        el.innerHTML = '<div class="empty">Recap unavailable.</div>';
    }
}

async function loadDeadlinesCard() {
    const el = document.getElementById('card-deadlines');
    const badge = document.getElementById('deadline-badge');
    try {
        const reminders = await api('/api/reminders');
        const open = reminders.filter(r => !r.completed);
        open.sort((a, b) => (a.due_at || '9999') < (b.due_at || '9999') ? -1 : 1);
        if (!open.length) {
            el.innerHTML = '<div class="empty">Nothing due. Add reminders to see them here.</div>';
            badge.classList.add('hidden');
            return;
        }
        const urgent = open.filter(r => dueLabel(r.due_at).urgent).length;
        if (urgent > 0) {
            badge.textContent = urgent + ' urgent';
            badge.classList.remove('hidden');
        } else {
            badge.classList.add('hidden');
        }
        el.innerHTML = '';
        for (const r of open.slice(0, 4)) {
            const d = dueLabel(r.due_at);
            const row = document.createElement('div');
            row.className = 'krow';
            row.innerHTML = `<span class="k-dot"></span><span class="k-main">${escHtml(r.title)}</span>` +
                (d.text ? `<span class="${d.urgent ? 'k-urgent' : 'k-sub'}">${d.text}</span>` : '');
            el.appendChild(row);
        }
    } catch (_) {
        el.innerHTML = '<div class="empty">Reminders unavailable.</div>';
    }
}

async function loadMilestonesCard() {
    const el = document.getElementById('card-milestones');
    try {
        const goals = await api('/api/goals');
        if (!goals.length) {
            el.innerHTML = '<div class="empty">No goals yet.</div>';
            return;
        }
        el.innerHTML = '';
        for (const g of goals.slice(0, 4)) {
            const row = document.createElement('div');
            row.className = 'krow';
            row.innerHTML = `<span class="k-dot"></span><span class="k-main">${escHtml(g.title)}</span><span class="k-sub">${escHtml(g.category || '')}</span>`;
            el.appendChild(row);
        }
    } catch (_) {
        el.innerHTML = '<div class="empty">Goals unavailable.</div>';
    }
}

// ===== Speech: voice input (STT) =====
async function toggleVoiceInput() {
    if (isRecording) { stopRecording(); return; }
    try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        recordedChunks = [];
        mediaRecorder = new MediaRecorder(stream);
        mediaRecorder.ondataavailable = e => { if (e.data.size) recordedChunks.push(e.data); };
        mediaRecorder.onstop = async () => {
            stream.getTracks().forEach(t => t.stop());
            setRecordingUI(false);
            const blob = new Blob(recordedChunks, { type: mediaRecorder.mimeType });
            await transcribeBlob(blob);
        };
        mediaRecorder.start();
        isRecording = true;
        setRecordingUI(true);
    } catch (e) {
        alert('Microphone unavailable: ' + e.message);
    }
}

function stopRecording() {
    if (mediaRecorder && isRecording) mediaRecorder.stop();
    isRecording = false;
}

function setRecordingUI(on) {
    const el = document.getElementById('mic-btn');
    if (el) el.classList.toggle('recording', on);
}

async function transcribeBlob(blob) {
    try {
        const arrayBuf = await blob.arrayBuffer();
        const audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        const audioBuf = await audioCtx.decodeAudioData(arrayBuf);
        const wavB64 = await audioBufferToWavBase64(audioBuf, 16000);
        const result = await api('/api/speech/transcribe', {
            method: 'POST',
            body: JSON.stringify({ audio_base64: wavB64 }),
        });
        if (result.success && result.text) {
            document.getElementById('cmd-input').value = result.text;
            focusCmd();
        } else {
            alert('Transcription failed: ' + (result.error || 'no speech detected'));
        }
    } catch (e) {
        alert('Transcription error: ' + e.message);
    }
}

// Encode an AudioBuffer to 16-bit PCM WAV (resampled) and return base64.
function audioBufferToWavBase64(buffer, targetRate) {
    const offline = new OfflineAudioContext(1,
        Math.ceil(buffer.duration * targetRate), targetRate);
    const src = offline.createBufferSource();
    src.buffer = buffer;
    src.connect(offline.destination);
    src.start(0);
    return offline.startRendering().then(rendered => {
        const data = rendered.getChannelData(0);
        const pcm = new Int16Array(data.length);
        for (let i = 0; i < data.length; i++) {
            const s = Math.max(-1, Math.min(1, data[i]));
            pcm[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
        }
        const wav = new ArrayBuffer(44 + pcm.length * 2);
        const view = new DataView(wav);
        const writeStr = (o, s) => { for (let i = 0; i < s.length; i++) view.setUint8(o + i, s.charCodeAt(i)); };
        writeStr(0, 'RIFF'); view.setUint32(4, 36 + pcm.length * 2, true); writeStr(8, 'WAVE');
        writeStr(12, 'fmt '); view.setUint32(16, 16, true); view.setUint16(20, 1, true);
        view.setUint16(22, 1, true); view.setUint32(24, targetRate, true);
        view.setUint32(28, targetRate * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true);
        writeStr(36, 'data'); view.setUint32(40, pcm.length * 2, true);
        new Int16Array(wav, 44).set(pcm);
        let bin = '';
        const bytes = new Uint8Array(wav);
        for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
        return btoa(bin);
    });
}

// ===== Speech: read replies aloud (TTS) =====
function toggleSpeak() {
    speakReplies = !speakReplies;
    const btn = document.getElementById('speak-toggle');
    btn.querySelector('use').setAttribute('href', speakReplies ? '#i-speaker' : '#i-speaker-off');
    btn.title = speakReplies ? 'Reading replies aloud' : 'Read replies aloud';
    btn.style.color = speakReplies ? 'var(--accent)' : '';
}

async function speakText(text) {
    try {
        const result = await api('/api/speech/speak', {
            method: 'POST',
            body: JSON.stringify({ text: text.slice(0, 1200) }),
        });
        if (result.success && result.audio_base64) {
            const audio = new Audio('data:audio/wav;base64,' + result.audio_base64);
            audio.play();
        }
    } catch (_) { /* TTS optional — fail silently */ }
}

// ===== Search =====
async function doSearch() {
    const q = document.getElementById('search-input').value.trim();
    if (!q) return;
    const container = document.getElementById('search-results');
    container.innerHTML = '<div class="empty">Searching…</div>';
    try {
        const results = await api(`/api/search?q=${encodeURIComponent(q)}&limit=20`);
        container.innerHTML = `<div class="empty">${results.count} results for "${escHtml(q)}"</div>`;
        for (const r of results.results) {
            const div = document.createElement('div');
            div.className = 'list-item';
            div.innerHTML = `
                <div class="sub">${escHtml((r.timestamp || '').slice(0, 16).replace('T', ' '))} · ${escHtml(r.role)} · ${escHtml(r.conversation_title || r.conversation_id)}</div>
                <div class="body">${escHtml((r.content || '').slice(0, 400))}</div>`;
            container.appendChild(div);
        }
    } catch (e) {
        container.innerHTML = `<div class="empty">${escHtml(e.message)}</div>`;
    }
}

// ===== Terminal =====
function toggleTerminal() {
    document.getElementById('terminal-panel').classList.toggle('collapsed');
}

function termAppend(text, cls) {
    const outputEl = document.getElementById('terminal-output');
    const span = document.createElement('span');
    if (cls) span.className = cls;
    span.textContent = text;
    outputEl.appendChild(span);
    outputEl.scrollTop = outputEl.scrollHeight;
}

async function runTerminal() {
    const input = document.getElementById('terminal-input');
    const cmd = input.value.trim();
    if (!cmd) return;
    input.value = '';
    termAppend(`$ ${cmd}\n`, 't-cmd');
    await executeTerminal(cmd, false);
}

// The server answers 428 for commands it judges destructive. That is a
// question, not a failure — ask, then re-send with confirm set.
async function executeTerminal(cmd, confirm) {
    try {
        const resp = await fetch('/api/terminal/execute', {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify({ command: cmd, confirm: !!confirm }),
        });

        if (resp.status === 428) {
            const detail = (await resp.json()).detail || {};
            const reasons = (detail.reasons || []).join(', ');
            termAppend(`⚠ ${reasons || 'this command looks destructive'}\n`, 't-warn');
            if (window.confirm(`This command ${reasons || 'looks destructive'}.\n\n${cmd}\n\nRun it anyway?`)) {
                return executeTerminal(cmd, true);
            }
            termAppend('cancelled\n', 't-err');
            return;
        }
        if (!resp.ok) {
            const detail = (await resp.json().catch(() => ({}))).detail;
            throw new Error(typeof detail === 'string' ? detail : resp.statusText);
        }
        const data = await resp.json();
        termAppend((data.output || '') + '\n');
    } catch (e) {
        termAppend('error: ' + e.message + '\n', 't-err');
    }
}

async function loadTerminalHistory() {
    const outputEl = document.getElementById('terminal-output');
    outputEl.innerHTML = '';
    try {
        const history = await api('/api/terminal/history');
        for (const h of history.slice(0, 20).reverse()) {
            termAppend(`$ ${h.command}\n`, 't-cmd');
            termAppend((h.output || '') + '\n');
        }
    } catch (_) {}
}

// ===== Notes ===== (implemented in features.js)

// ===== Goals =====
async function loadGoals() {
    const container = document.getElementById('goals-list');
    container.innerHTML = '';
    try {
        const goals = await api('/api/goals');
        if (!goals.length) { container.innerHTML = '<div class="empty">No goals yet.</div>'; return; }
        for (const g of goals) {
            const div = document.createElement('div');
            div.className = 'list-item';
            div.innerHTML = `<div class="goal-head"><strong>${escHtml(g.title)}</strong>${g.category ? `<span class="tag">${escHtml(g.category)}</span>` : ''}</div>` +
                (g.description ? `<div class="body">${escHtml(g.description)}</div>` : '');
            container.appendChild(div);
        }
    } catch (e) { container.innerHTML = `<div class="empty">${escHtml(e.message)}</div>`; }
}

async function addGoal() {
    const title = document.getElementById('new-goal-title').value.trim();
    const category = document.getElementById('new-goal-category').value.trim();
    if (!title) return;
    await api('/api/goals', { method: 'POST', body: JSON.stringify({ title, category }) });
    document.getElementById('new-goal-title').value = '';
    document.getElementById('new-goal-category').value = '';
    loadGoals();
}

// ===== Reminders =====
async function loadReminders() {
    const container = document.getElementById('reminders-list');
    container.innerHTML = '';
    try {
        const reminders = await api('/api/reminders');
        if (!reminders.length) { container.innerHTML = '<div class="empty">No reminders yet.</div>'; return; }
        for (const r of reminders) {
            const d = dueLabel(r.due_at);
            const div = document.createElement('div');
            div.className = `list-item rem-row ${r.completed ? 'completed' : ''}`;
            div.innerHTML = `
                <input type="checkbox" ${r.completed ? 'checked' : ''} onchange="toggleReminder('${r.id}', this.checked)">
                <span class="rem-title" style="flex:1">${escHtml(r.title)}</span>` +
                (d.text ? `<span class="tag ${d.urgent ? 'hot' : ''}">${d.text}</span>` : '');
            container.appendChild(div);
        }
    } catch (e) { container.innerHTML = `<div class="empty">${escHtml(e.message)}</div>`; }
}

async function addReminder() {
    const title = document.getElementById('new-reminder-title').value.trim();
    const dueAt = document.getElementById('new-reminder-due').value;
    if (!title) return;
    await api('/api/reminders', { method: 'POST', body: JSON.stringify({ title, due_at: dueAt || null }) });
    document.getElementById('new-reminder-title').value = '';
    document.getElementById('new-reminder-due').value = '';
    loadReminders();
    loadDeadlinesCard();
}

async function toggleReminder(id, completed) {
    await api(`/api/reminders/${id}/complete`, {
        method: 'POST',
        body: JSON.stringify({ completed }),
    });
    loadReminders();
    loadDeadlinesCard();
}

// ===== Recap =====
// The briefing, from what you have been asking about.
//
// `runRecap()` is the original general one and stays: a fresh install has no
// history to derive an interest from, and the server falls back to it on its
// own when there is nothing recurring. This is the same renderer pointed at
// the interest-driven endpoint, which streams Research's own event shapes —
// so the trace gets sources and verdicts for free.
async function runInterestRecap() {
    return runRecap('/api/recap/run/interests');
}

async function runRecap(endpoint) {
    const el = document.getElementById('card-recap');
    el.innerHTML = '<div class="trace" id="recap-trace"></div><div class="recap-out" id="recap-out"></div>';
    const traceEl = document.getElementById('recap-trace');
    const outEl = document.getElementById('recap-out');

    function traceLine(text, cls) {
        const div = document.createElement('div');
        div.className = 'trace-line' + (cls ? ' ' + cls : '');
        div.textContent = text;
        traceEl.appendChild(div);
        traceEl.scrollTop = traceEl.scrollHeight;
    }

    let thinkLine = null;
    function traceThink(text) {
        if (!thinkLine) {
            thinkLine = document.createElement('div');
            thinkLine.className = 'trace-think';
            traceEl.appendChild(thinkLine);
        }
        thinkLine.textContent = ('thinking: ' + (thinkLine.dataset.raw = (thinkLine.dataset.raw || '') + text)).slice(0, 4000);
        traceEl.scrollTop = traceEl.scrollHeight;
    }

    try {
        const resp = await fetch(endpoint || '/api/recap/run/stream', {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify({}),
        });
        if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || resp.statusText);

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let summary = '';
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = buffer.indexOf('\n\n')) !== -1) {
                const raw = buffer.slice(0, idx).trim();
                buffer = buffer.slice(idx + 2);
                if (!raw.startsWith('data:')) continue;
                const p = JSON.parse(raw.slice(5).trim());
                if (p.stage) traceLine(`${p.stage}: ${p.detail || ''}`, 'stage');
                if (p.intents) {
                    for (const it of p.intents) traceLine('intent → ' + it, 'intent');
                }
                if (p.search) {
                    traceLine(`search [${p.search.topic}] ${p.search.title || ''} — ${p.search.url || ''}`.trim(), 'search');
                }
                // What Carrot concluded you have been asking about, with the
                // evidence. Shown before any research runs — an assistant that
                // decides what you care about and then presents the results is
                // unnerving in a way one that shows its working is not.
                if (p.topics) {
                    traceLine('reading your recent questions — ' + (p.detail || ''), 'intent');
                    for (const t of p.topics) {
                        traceLine('  · ' + t.topic + (t.why ? ' — ' + t.why : ''), 'intent');
                    }
                }
                if (p.fallback) {
                    traceLine('nothing recurring yet — general briefing instead', 'stage');
                }
                // Passed straight through from Research, so a source read for
                // the briefing looks exactly like one read for a report,
                // tier and all.
                if (p.source) {
                    const tier = p.source.tier || 'unknown';
                    traceLine(`read [${p.source.id}] (${tier}) ${p.source.title || ''}`,
                              tier === 'low' ? 'warn' : 'search');
                }
                if (p.verdict) {
                    traceLine(`${p.verdict.verdict}: ${p.verdict.claim}`,
                              p.verdict.verdict === 'supported' ? 'ok' : 'warn');
                }
                if (p.thinking) traceThink(p.thinking);
                if (p.token) {
                    summary += p.token;
                    outEl.innerHTML = mdToHtml(summary);
                    outEl.scrollTop = outEl.scrollHeight;
                }
                if (p.error) traceLine(p.error, 'err');
                if (p.done) {
                    traceLine('done — briefing saved'
                        + (p.sources ? ` — ${p.sources} sources` : ''), 'ok');
                    // The interest briefing is assembled from finished reports
                    // rather than streamed a token at a time, so it arrives
                    // here whole.
                    if (p.summary && !summary) summary = p.summary;
                }
            }
        }
        if (summary) {
            outEl.classList.add('md');
            outEl.innerHTML = mdToHtml(summary);
        } else {
            outEl.innerHTML = '<div class="empty">No summary produced.</div>';
        }
    } catch (e) {
        traceLine(e.message, 'err');
    }
}

// ===== Assignments =====
async function loadAssignments() {
    const container = document.getElementById('assignments-list');
    container.innerHTML = '';
    try {
        const result = await api('/api/assignments');
        if (!result.assignments.length) { container.innerHTML = '<div class="empty">No assignments found. Scan your files to index them.</div>'; return; }
        for (const a of result.assignments) {
            const div = document.createElement('div');
            div.className = 'list-item';
            div.innerHTML = `<strong>${escHtml(a.name)}</strong><span class="tag">${escHtml(a.extension)}</span><div class="sub">${escHtml(a.directory)}</div>`;
            container.appendChild(div);
        }
    } catch (e) { container.innerHTML = `<div class="empty">${escHtml(e.message)}</div>`; }
}

async function scanAssignments() {
    const container = document.getElementById('assignments-list');
    container.innerHTML = '<div class="empty">Scanning…</div>';
    try {
        const result = await api('/api/computer_use/scan', { method: 'POST' });
        loadAssignments();
    } catch (e) {
        container.innerHTML = `<div class="empty">${escHtml(e.message)}</div>`;
    }
}

// ===== Leaderboard =====
async function loadLeaderboard() {
    const list = document.getElementById('leaderboard-list');
    try {
        const data = await api('/api/leaderboard?limit=50');
        const stats = await api('/api/leaderboard/stats');
        document.getElementById('lb-total').innerHTML = `<div class="value">${stats.total_submissions}</div><div class="label">Total submissions</div>`;
        const modelEl = document.getElementById('lb-top-model');
        const osEl = document.getElementById('lb-top-os');
        const gpuEl = document.getElementById('lb-top-gpu');
        modelEl.innerHTML = stats.by_model.length
            ? `<div class="value">${escHtml(stats.by_model[0].model)}</div><div class="label">Top model (${stats.by_model[0].count})</div>`
            : '<div class="value">—</div><div class="label">Top model</div>';
        osEl.innerHTML = stats.by_os.length
            ? `<div class="value">${escHtml(stats.by_os[0].os)}</div><div class="label">Top OS (${stats.by_os[0].count})</div>`
            : '<div class="value">—</div><div class="label">Top OS</div>';
        gpuEl.innerHTML = stats.top_gpus.length
            ? `<div class="value">${escHtml(stats.top_gpus[0].gpu.slice(0, 22))}</div><div class="label">Top GPU</div>`
            : '<div class="value">—</div><div class="label">Top GPU</div>';
        renderLeaderboardList(data, list);
        const modelSelect = document.getElementById('lb-filter-model');
        modelSelect.innerHTML = '<option value="">All Models</option>';
        for (const m of (stats.by_model || [])) {
            const opt = document.createElement('option');
            opt.value = m.model; opt.textContent = m.model;
            modelSelect.appendChild(opt);
        }
    } catch (e) { list.innerHTML = '<div class="empty">Failed to load leaderboard.</div>'; }
}

function renderLeaderboardList(data, list) {
    list.innerHTML = '';
    if (!data.length) {
        list.innerHTML = '<div class="empty">No submissions yet. Be the first to share your setup.</div>';
        return;
    }
    for (const entry of data) {
        const div = document.createElement('div');
        div.className = 'list-item';
        div.innerHTML = `
            <strong>${escHtml(entry.os)} · ${entry.ram_gb}GB</strong>
            <span class="tag">${escHtml(entry.gpu ? entry.gpu.slice(0, 36) : 'N/A')}</span>
            <span class="tag">${escHtml(entry.active_model || 'No model')}</span>
            <div class="sub">${entry.submitted_at ? entry.submitted_at.slice(0, 10) : ''}</div>`;
        list.appendChild(div);
    }
}

async function filterLeaderboard() {
    const os = document.getElementById('lb-filter-os').value;
    const ram = document.getElementById('lb-filter-ram').value;
    const model = document.getElementById('lb-filter-model').value;
    let url = '/api/leaderboard?limit=50';
    const params = [];
    if (os) params.push(`os_name=${encodeURIComponent(os)}`);
    if (ram) params.push(`ram_gb_min=${encodeURIComponent(ram)}`);
    if (model) params.push(`model=${encodeURIComponent(model)}`);
    if (params.length) url += '&' + params.join('&');
    const data = await api(url);
    renderLeaderboardList(data, document.getElementById('leaderboard-list'));
}

async function submitToLeaderboard() {
    const result = await api('/api/leaderboard/submit', { method: 'POST' });
    if (result.anonymous_id) alert('Thanks for submitting your setup. Anonymous ID: ' + result.anonymous_id);
}

// ===== Bootstrap splash =====
async function checkBootstrap() {
    try {
        const s = await api('/api/bootstrap/status');
        if (s.bootstrap_complete) { hideSplash(); return; }
        showSplash(s);
    } catch (_) { hideSplash(); }
}

let splashModel = null; // model picked on the splash; null = stock default
let splashHub = null;   // /api/hub payload, reused by the in-splash catalog

async function showSplash(s) {
    document.getElementById('splash').classList.remove('hidden');
    const status = document.getElementById('splash-status');
    if (!s.ollama_installed) status.textContent = 'Ollama is not installed. Carrot can set it up for you.';
    else if (!s.model_pulled) status.textContent = 'Ollama is ready — pick a model that fits your machine.';
    document.getElementById('splash-btn').classList.remove('hidden');
    document.getElementById('splash-skip').classList.remove('hidden');
    // Hardware-based picks from the Hub. New users shouldn't have to know
    // which model or quantization suits their specs — show what fits, let
    // experienced users skip, and link the full daily catalog.
    try {
        const hub = await api('/api/hub');
        renderSplashPicks(hub);
    } catch (_) { /* no picks — the default-model path still works */ }
}

function renderSplashPicks(hub) {
    splashHub = hub;
    const specsEl = document.getElementById('splash-specs');
    const picksEl = document.getElementById('splash-picks');
    const link = document.getElementById('splash-hub-link');
    const s = hub.specs || {};
    specsEl.textContent = `Detected: ${hubSpecLine(s)} — ${s.model_budget_gb} GB usable for models`;
    specsEl.classList.remove('hidden');
    link.classList.remove('hidden');

    // What this machine cannot do on-device, said before the user tries it.
    // The limit is real either way; the only question is whether they learn
    // it here or after twenty minutes of the Code tab going nowhere.
    const feas = hub.feasibility || {};
    const warnEl = document.getElementById('splash-feasibility');
    if (warnEl) {
        if (feas.warning) {
            const rough = (feas.tasks || []).filter(t => t.verdict !== 'on_device');
            warnEl.innerHTML = `<strong>Worth knowing about this machine.</strong> `
                + escHtml(feas.warning)
                + (rough.length ? `<ul class="splash-feasibility-list">` + rough.map(t =>
                    `<li>${escHtml(t.label)} — ${escHtml(t.detail)}</li>`).join('') + `</ul>` : '');
            warnEl.classList.remove('hidden');
        } else {
            warnEl.classList.add('hidden');
        }
    }

    // No suggestions here. The bundled catalog is a snapshot of whatever was
    // good on the day this build was cut, and by the time somebody downloads
    // and runs it that is a recommendation for last quarter's models made
    // with total confidence. Naming a model on this screen is a promise about
    // the present that a shipped binary cannot keep.
    //
    // So the screen offers to go and look instead, and the answer comes from
    // Hugging Face. The bundle stays in the build for one job — being the
    // fallback when there is no network — and when it is used it says so.
    const find = document.getElementById('splash-find');
    find.textContent = 'Find models for my machine →';
    find.onclick = splashFindForMachine;
    find.disabled = false;
    find.classList.remove('hidden');
}

// Both the bundled recommendations and the live Hugging Face results draw the
// same card, because they are the same decision — the only difference is where
// the row came from.
function renderSplashCards(picks) {
    const picksEl = document.getElementById('splash-picks');
    picksEl.innerHTML = '';
    picksEl.classList.remove('hidden');
    for (const p of picks) {
        const card = document.createElement('button');
        card.type = 'button';
        card.className = 'splash-pick';
        card.dataset.model = p.m.id;
        card.innerHTML = `
            <span class="splash-pick-role">${escHtml(p.role)}</span>
            <strong>${escHtml(p.m.label || p.m.id)}</strong>
            <span class="muted small">${p.m.download_gb} GB · ${escHtml(p.m.quant || '')}${p.m.est_tps ? ` · ~${p.m.est_tps} tok/s` : ''} · ${escHtml(p.m.blurb || '')}</span>`;
        card.onclick = () => {
            splashModel = p.m.id;
            picksEl.querySelectorAll('.splash-pick').forEach(el =>
                el.classList.toggle('selected', el.dataset.model === splashModel));
        };
        picksEl.appendChild(card);
    }
    // Preselect the first so plain "Set up now" does the right thing.
    if (picks.length) {
        splashModel = picks[0].m.id;
        picksEl.querySelector('.splash-pick').classList.add('selected');
    }
}

// "Find models for my machine" — the only way a model gets named on this
// screen. It asks Hugging Face what exists right now; the sizing stays local
// (specs → quant plan → fit), so what comes back is current *and* runnable,
// rather than a trending list that ignores the machine or a bundled list that
// ignores the date.
async function splashFindForMachine() {
    const btn = document.getElementById('splash-find');
    const note = document.getElementById('splash-find-note');
    btn.disabled = true;
    btn.textContent = 'Asking Hugging Face…';
    let live = [];
    let offline = '';
    try {
        // `workload=assistant` rather than an unfiltered trending list: with
        // no stated use case the ranking is fit plus popularity, and the most
        // downloaded thing that fits any machine is a 0.6B speech model. This
        // screen is choosing the thing that answers questions.
        const data = await api('/api/hub/search?workload=assistant&sort=trending&limit=6');
        live = (data.results || []).filter(m => m.fit !== 'too_big');
        if (!live.length) offline = data.detail || '';
    } catch (e) {
        offline = 'Could not reach Hugging Face.';
    }

    if (live.length) {
        renderSplashCards(live.map((m, i) => ({
            role: i === 0 ? 'Best match for this machine' : 'Also fits',
            m,
        })));
        note.textContent = `Live from Hugging Face · ${live.length} that run on this machine`;
        note.classList.remove('hidden');
        btn.classList.add('hidden');
        return;
    }

    // No network. This is the one job the bundled catalog still has, and it
    // is labelled as what it is — an old list — because a stale
    // recommendation presented as a current one is the thing worth avoiding,
    // not the staleness itself.
    const bundled = splashBundledPicks();
    if (bundled.length) {
        renderSplashCards(bundled);
        note.textContent = (offline ? offline + ' ' : '')
            + 'Showing the list built into this download, which may be out of date.';
    } else {
        note.textContent = (offline || 'Nothing found.')
            + ' You can set up Ollama now and pick a model later.';
    }
    note.classList.remove('hidden');
    btn.textContent = 'Try again';
    btn.disabled = false;
}

// The offline fallback, from the payload the splash already has.
function splashBundledPicks() {
    const recs = (splashHub || {}).recommendations || {};
    if (!recs.best) return [];
    const picks = [{ role: 'Fits this machine', m: recs.best }];
    if (recs.light && recs.light.id !== recs.best.id) {
        picks.push({ role: 'Light & fast', m: recs.light });
    }
    return picks;
}

function hideSplash() { document.getElementById('splash').classList.add('hidden'); }

// The full catalog, right on the setup screen — including the models that
// do NOT fit, each saying why. Seeing "needs 12 GB, you have 3.9" is more
// reassuring than a short list with no explanation.
function toggleSplashCatalog() {
    const el = document.getElementById('splash-catalog');
    const link = document.getElementById('splash-hub-link');
    if (!el.classList.contains('hidden')) {
        el.classList.add('hidden');
        link.textContent = 'See every model and why some won\'t run here →';
        return;
    }
    if (!splashHub) return;
    const budget = (splashHub.specs || {}).model_budget_gb || 0;
    const fitOrder = { great: 0, good: 1, tight: 2, too_big: 3 };
    const models = [...(splashHub.models || [])].sort((a, b) =>
        (fitOrder[a.fit] - fitOrder[b.fit]) || (a.min_mem_gb - b.min_mem_gb));
    // Compact badge text — the full wording would squeeze out model names.
    const SHORT_FIT = { great: 'Great', good: 'Good', tight: 'Tight', too_big: 'Too big' };
    el.innerHTML = models.map(m => {
        const why = m.fit === 'too_big'
            ? `needs ${m.min_mem_gb} GB, you have ${budget}`
            : (m.fit === 'tight'
                ? `needs ${m.min_mem_gb} GB — slow`
                : `${m.download_gb} GB${m.est_tps ? ` · ~${m.est_tps} tok/s` : ''}`);
        return `
          <button type="button" class="splash-cat-row fit-${m.fit}"
                  ${m.fit === 'too_big' ? 'disabled' : `onclick="pickSplashModel('${escHtml(m.id)}')"`}>
            <span class="splash-cat-name">${escHtml(m.label || m.id)}</span>
            <span class="fit-badge fit-${m.fit}">${SHORT_FIT[m.fit] || m.fit}</span>
            <span class="splash-cat-why">${escHtml(why)}</span>
          </button>`;
    }).join('');
    el.classList.remove('hidden');
    link.textContent = 'Hide the full catalog ←';
}

function pickSplashModel(id) {
    splashModel = id;
    // Reflect the choice in both the picks row and the catalog list.
    document.querySelectorAll('#splash-picks .splash-pick').forEach(el =>
        el.classList.toggle('selected', el.dataset.model === id));
    document.querySelectorAll('#splash-catalog .splash-cat-row').forEach(el =>
        el.classList.toggle('selected', el.textContent.trim().startsWith(
            (splashHub.models.find(m => m.id === id) || {}).label || id)));
    const status = document.getElementById('splash-status');
    if (status) status.textContent = `${id} selected — press Set up now.`;
}

function skipModelChoice() {
    // Skipping the picker is not skipping the sizing. `splashModel = null`
    // sends the server to `get_target_model()`, which asks the Hub what fits
    // this machine — it used to reach a constant, so the one path taken by
    // users who did not want to think about it was the one that ignored
    // their hardware entirely.
    splashModel = null;
    runBootstrap();
}

function splashFailed(message) {
    const btn = document.getElementById('splash-btn');
    document.getElementById('splash-status').textContent = message;
    document.getElementById('splash-detail').textContent = '';
    btn.textContent = 'Retry';
    btn.classList.remove('hidden');
    document.getElementById('splash-skip').classList.remove('hidden');
    document.getElementById('splash-picks').classList.remove('hidden');
    document.getElementById('splash-hub-link').classList.remove('hidden');
}

// Setup streams over SSE so the bar tracks the actual download. A model
// is gigabytes; a bar that jumps 30% -> 100% just looks frozen.
async function runBootstrap() {
    const btn = document.getElementById('splash-btn');
    const status = document.getElementById('splash-status');
    const detail = document.getElementById('splash-detail');
    const bar = document.getElementById('splash-bar');
    btn.classList.add('hidden');
    document.getElementById('splash-skip').classList.add('hidden');
    document.getElementById('splash-picks').classList.add('hidden');
    document.getElementById('splash-catalog').classList.add('hidden');
    document.getElementById('splash-hub-link').classList.add('hidden');
    status.textContent = 'Setting up…';
    detail.textContent = '';
    bar.style.width = '2%';

    const url = await tokenUrl('/api/bootstrap/stream'
        + (splashModel ? `?model=${encodeURIComponent(splashModel)}` : ''));
    const src = new EventSource(url);
    let started = Date.now();

    src.onmessage = (ev) => {
        let p;
        try { p = JSON.parse(ev.data); } catch (_) { return; }

        if (p.type === 'status' || p.type === 'install') {
            status.textContent = p.message || '';
        } else if (p.type === 'download') {
            // Downloading the Ollama installer itself.
            const pct = p.total ? Math.round(p.downloaded / p.total * 100) : 0;
            status.textContent = 'Downloading Ollama…';
            bar.style.width = Math.max(pct * 0.2, 2) + '%';   // installer = first 20%
            detail.textContent = `${fmtBytes(p.downloaded)} of ${fmtBytes(p.total)}`;
        } else if (p.type === 'pull') {
            status.textContent = `Downloading ${p.model || 'model'}…`;
            if (p.total && p.completed != null) {
                const pct = Math.round(p.completed / p.total * 100);
                bar.style.width = (20 + pct * 0.8) + '%';      // model = remaining 80%
                const secs = (Date.now() - started) / 1000;
                const rate = secs > 2 ? ` · ${fmtBytes(p.completed / secs)}/s` : '';
                detail.textContent = `${fmtBytes(p.completed)} of ${fmtBytes(p.total)} (${pct}%)${rate}`;
            } else if (p.status) {
                detail.textContent = p.status;
            }
        } else if (p.type === 'error') {
            detail.textContent = p.message || '';
        } else if (p.type === 'done') {
            src.close();
            if (p.error) { splashFailed(p.error); return; }
            bar.style.width = '100%';
            status.textContent = 'Setup complete. Launching Carrot…';
            detail.textContent = '';
            setTimeout(() => { hideSplash(); refreshStatus(); loadModels(); }, 900);
        }
    };

    src.onerror = () => {
        src.close();
        splashFailed('Lost contact with Carrot during setup. Press Retry.');
    };
}

// ===== Init =====
// ===== Tabs that belong to an extension =====
//
// A pack used to ship tools, skills and settings — things the model reaches
// for. A tab is the first thing a pack contributes that the *user* reaches
// for, and it works the same way: one switch for a whole feature.
//
// The page ships the full nav markup, so hiding a tab means knowing which
// tabs are pack-managed at all. Without that list a tab whose pack is off is
// indistinguishable from an ordinary one and simply stays visible.
//
// Hidden, not removed. The view and its routes stay mounted, so enabling the
// pack is instant and a bookmarked URL into a disabled feature still works
// rather than 404ing.
async function applyExtensionTabs() {
    let info;
    try {
        info = await api('/api/extensions/tabs');
    } catch (_) {
        return;   // A failed probe must not hide a tab that should be there.
    }
    const managed = Object.keys(info.managed || {});
    const enabled = new Set(info.enabled || []);
    for (const tab of managed) {
        const on = enabled.has(tab);
        // Nav items and the Work shortcuts alike: with the sidebar collapsed to
        // four, a pack's way in is a button on the Work screen, and gating one
        // without the other leaves a live door to a switched-off pack.
        document.querySelectorAll(`.nav-item[data-tab="${tab}"], .drive-place[data-tab="${tab}"]`)
            .forEach(el => el.classList.toggle('hidden', !on));
        // Somebody sitting on a tab when its pack is switched off should not
        // be left looking at a view they can no longer navigate back to.
        if (!on && currentTab === tab) switchTab('workspace');
    }
}

document.addEventListener('DOMContentLoaded', async () => {
    // Before the first await, so the workspace never paints a frame with the
    // composer sitting on top of the terminal.
    watchComposerHeight();
    restoreNavCollapsed();
    await loadRecapConfig();
    await refreshStatus();
    showBuildVersion();
    loadModels();
    loadSkillCatalog();
    loadSearchModes();
    loadWorkspaces();
    // Before the first switchTab, so a disabled pack's tab never paints.
    await applyExtensionTabs();
    // Chat is the mode the app opens in, and saying so is what hides the
    // agent's settings. Without this the class list has never run, so the
    // control sits in the tab strip until somebody toggles the switch once.
    setChatMode('chat');
    // Onboarding decides whether the bootstrap splash runs at all.
    maybeShowOnboarding();
    switchTab('workspace');
    loadTerminalHistory();
    setInterval(refreshStatus, 15000);
    // The council chip lives in the composer, so its state has to be known
    // from the first paint rather than only after a visit to Settings.
    if (typeof loadConsensusPanel === 'function') loadConsensusPanel();

    // Ctrl+K is the palette now — see palette.js, which owns the binding.
    //
    // It used to focus the composer from here. Both handlers listening for the
    // same chord would have opened the palette and then moved the cursor into
    // the box behind it, so this one goes rather than being left to fight. The
    // behaviour survives: the palette's field is a text input, and typing into
    // it and pressing Enter puts the text in the composer.

    // Click outside closes the popovers. The search picker was never in here:
    // its menu only closed by picking a mode or by toggling the same button
    // again, so clicking anywhere else left it hanging over the conversation.
    document.addEventListener('click', e => {
        const picker = document.getElementById('model-picker');
        if (!picker.contains(e.target)) {
            document.getElementById('model-pop').classList.add('hidden');
        }
        if (!document.getElementById('search-picker')?.contains(e.target)) {
            document.getElementById('search-pop')?.classList.add('hidden');
        }
        if (!document.getElementById('tool-menu')?.contains(e.target)) closeToolMenu();
        if (!document.getElementById('history-menu')?.contains(e.target)) closeHistoryMenu();
        const cmdbar = document.getElementById('cmdbar');
        if (!cmdbar.contains(e.target)) hideSkillPop();
    });
});

// ===== First-run onboarding =====
// Runs in front of the bootstrap splash. "Which kind of setup do you want"
// and "which model should I download" are different questions, and asking
// them together is what made first run confusing: a new user was shown a
// list of quantized model names before anyone had explained what a model is.

const ONBOARD_KEY_PAGES = {
    anthropic: 'https://console.anthropic.com/settings/keys',
    openai: 'https://platform.openai.com/api-keys',
    openrouter: 'https://openrouter.ai/keys',
    groq: 'https://console.groq.com/keys',
    together: 'https://api.together.xyz/settings/api-keys',
    deepseek: 'https://platform.deepseek.com/api_keys',
    mistral: 'https://console.mistral.ai/api-keys',
};

function onboardStep(step) {
    document.querySelectorAll('#onboard .onboard-step').forEach(el => {
        el.classList.toggle('hidden', el.dataset.step !== step);
    });
    // Choosing to run locally used to close the whole flow immediately, which
    // meant most people never saw where anything was. It goes to the tour now,
    // and the model download starts behind it.
    if (step === 'local') { startLocalSetup(); return; }
    if (step === 'key') onboardLoadProviders();
    if (step === 'subscription') onboardCheckSubscription();
}

let onboardingBootstrapStarted = false;

function startLocalSetup() {
    onboardingBootstrapStarted = true;
    onboardStep('tour');
}

// ---------- "I want to use my own AI subscription" ----------
//
// Most people who reach this screen are already paying one of these companies
// every month. Being told to go create a second, separately-billed developer
// account is the worst five minutes in the app, and it is where people stop.

async function onboardCheckSubscription() {
    const status = document.getElementById('onboard-sub-status');
    const select = document.getElementById('onboard-sub-provider');
    if (!status || !select) return;
    status.textContent = '';
    try {
        const state = await api(`/api/auth/status/${encodeURIComponent(select.value)}`);
        if (state.signed_in) {
            status.textContent = `Already signed in to ${select.value}.`;
        } else if (!state.oauth_configured) {
            // Saying so beats a button that fails for reasons nobody can see.
            status.textContent = 'This copy of Carrot does not have sign-in details for '
                + 'that provider yet, so an API key is the reliable path for now.';
        }
    } catch (_) { /* the screen still works without this */ }
}

async function startOnboardingSignIn() {
    const select = document.getElementById('onboard-sub-provider');
    const status = document.getElementById('onboard-sub-status');
    const provider = select.value;
    status.textContent = 'Opening the sign-in page…';
    try {
        await api(`/api/auth/mode/${encodeURIComponent(provider)}`,
            { method: 'PUT', body: JSON.stringify({ mode: 'subscription' }) });
        const started = await api(`/api/auth/login/${encodeURIComponent(provider)}`,
            { method: 'POST' });
        if (window.carrot?.openExternal) window.carrot.openExternal(started.url);
        else window.open(started.url, '_blank', 'noopener');
        status.textContent = 'Finish signing in in your browser, then come back here.';
        pollOnboardingSignIn(provider, status);
    } catch (e) {
        status.textContent = e.detail || e.message;
    }
}

function pollOnboardingSignIn(provider, status) {
    let tries = 0;
    const timer = setInterval(async () => {
        tries += 1;
        try {
            const state = await api(`/api/auth/status/${encodeURIComponent(provider)}`);
            if (state.signed_in) {
                clearInterval(timer);
                status.textContent = 'Signed in.';
                onboardStep('tour');
            }
        } catch (_) { clearInterval(timer); }
        if (tries > 90) clearInterval(timer);
    }, 2000);
}

async function onboardLoadProviders() {
    const select = document.getElementById('onboard-provider');
    if (select.dataset.loaded) return;
    select.dataset.loaded = '1';
    // The hosted ones only. Offering "LM Studio (local)" on the screen for
    // people who chose the cloud path is just noise.
    let options = [
        { id: 'anthropic', label: 'Anthropic (Claude)' },
        { id: 'openai', label: 'OpenAI (GPT)' },
    ];
    try {
        const body = await api('/api/router/providers');
        for (const preset of (body.presets || [])) {
            if (/local/i.test(preset.label || '')) continue;
            if (!options.some(o => o.id === preset.id)) {
                options.push({ id: preset.id, label: preset.label });
            }
        }
    } catch (_) { /* the two built-ins are enough to get started */ }
    select.innerHTML = options
        .map(o => `<option value="${escHtml(o.id)}">${escHtml(o.label)}</option>`).join('');
    onboardProviderChanged();
}

function onboardProviderChanged() {
    const id = document.getElementById('onboard-provider').value;
    const link = document.getElementById('onboard-key-link');
    const url = ONBOARD_KEY_PAGES[id];
    link.href = url || '#';
    link.classList.toggle('hidden', !url);
}

async function saveOnboardingKey() {
    const provider = document.getElementById('onboard-provider').value;
    const key = document.getElementById('onboard-key').value.trim();
    const status = document.getElementById('onboard-key-status');
    const button = document.getElementById('onboard-key-btn');
    if (!key) { status.textContent = 'Paste a key first.'; status.className = 'onboard-status bad'; return; }

    button.disabled = true;
    status.className = 'onboard-status';
    status.textContent = 'Checking the key…';
    try {
        await api(`/api/router/providers/${encodeURIComponent(provider)}/key`, {
            method: 'PUT', body: JSON.stringify({ api_key: key }),
        });
        // Saving a key that does not work is worse than not saving one: the
        // failure surfaces later, in the middle of an answer. /test exists for
        // exactly this and reports the provider's own error — listing models
        // is not a check, because it falls back to a cached list and returns
        // an `error` field rather than failing, so a garbage key looked fine.
        const probe = await api(`/api/router/providers/${encodeURIComponent(provider)}/test`,
                                { method: 'POST' });
        if (!probe.ok) {
            status.className = 'onboard-status bad';
            status.textContent = 'That key did not work: ' + (probe.error || 'the provider rejected it');
            return;
        }
        status.className = 'onboard-status good';
        status.textContent = probe.models
            ? `Working — ${probe.models} models available.`
            : 'Working.';
        await api(`/api/router/providers/${encodeURIComponent(provider)}/enabled`, {
            method: 'PUT', body: JSON.stringify({ enabled: true }),
        }).catch(() => {});
        // Everyone ends on the tour, whichever path they took.
        setTimeout(() => onboardStep('tour'), 1200);
    } catch (e) {
        status.className = 'onboard-status bad';
        status.textContent = 'That key did not work: ' + e.message;
    } finally {
        button.disabled = false;
    }
}

async function finishOnboarding(skipped, goTo) {
    document.getElementById('onboard').classList.add('hidden');
    try {
        await api('/api/config/onboarding_done', { method: 'PUT', body: JSON.stringify(true) });
    } catch (_) { /* it is only a "do not show again" flag */ }
    if (goTo && typeof switchTab === 'function') {
        // Landing on Help rather than being told where it is: the difference
        // between knowing a page exists and having seen it.
        switchTab(goTo);
        return;
    }
    // Hand over to the model-download splash unless they skipped outright, or
    // already chose a cloud provider and need no local model.
    if (!skipped && onboardingBootstrapStarted && typeof checkBootstrap === 'function') {
        checkBootstrap();
    }
}

async function maybeShowOnboarding() {
    let done = false;
    try {
        done = !!(await api('/api/config')).onboarding_done;
    } catch (_) { done = true; }        // cannot ask: do not block the app
    if (done) { checkBootstrap(); return; }
    document.getElementById('onboard').classList.remove('hidden');
    onboardStep('welcome');
}

// ===== Temporary chats =====
//
// No memory extraction, no rolling summary, no workspace filing, and deleted
// on the next start. The banner is not decoration: a mode that silently
// changes whether you are being remembered is a mode people forget they are
// in, and the whole value here is knowing.

// Whether what Carrot remembers may be read on this turn. `null` follows the
// saved default; the chip only ever turns it off, because turning it on for a
// chat where the user disabled it globally would be overriding them.
let useMemory = null;

function toggleMemoryUse() {
    // No new chat, unlike Temporary. That one changes what happens to this
    // conversation afterwards, so switching mid-way would misdescribe the
    // turns already taken. This only changes what the next turn reads, and
    // wanting it off from here on is the normal case — you find out it is
    // bringing up your dog by watching it do so.
    useMemory = useMemory === false ? null : false;
    renderMemoryState();
}

function renderMemoryState() {
    const button = document.getElementById('memory-btn');
    if (!button) return;
    const off = useMemory === false;
    // `on` is the chip's lit state, and the lit state here means "being
    // ignored" — the thing worth a highlight is the departure from normal.
    button.classList.toggle('on', off);
    button.querySelector('span').textContent = off ? 'Memory off' : 'Memory';
    button.title = off
        ? 'Ignoring what Carrot remembers about you — click to use it again'
        : 'Ignore what Carrot remembers about you for this chat';
}

function toggleTemporaryChat() {
    // Switching mode mid-conversation would be a lie either way — the earlier
    // turns are already remembered, or already not — so it starts a new one.
    if (currentConversationId) newChat();
    temporaryChat = !temporaryChat;
    renderTemporaryState();
}

function renderTemporaryState() {
    document.getElementById('temp-btn')?.classList.toggle('on', temporaryChat);
    let banner = document.getElementById('temp-banner');
    if (!temporaryChat) {
        banner?.remove();
        return;
    }
    if (banner) return;
    const log = document.getElementById('chat-log') || document.getElementById('messages');
    if (!log) return;
    banner = document.createElement('div');
    banner.id = 'temp-banner';
    banner.className = 'temp-banner';
    banner.innerHTML = `
      <strong>Temporary chat.</strong> Nothing here is saved to memory, summarised,
      or filed in a workspace, and the whole conversation is deleted when Carrot
      next starts. Attachments you send are still processed normally.`;
    log.prepend(banner);
}

// A new chat inherits the mode you are in, so the banner has to follow it.
document.addEventListener('DOMContentLoaded', renderTemporaryState);

// ===== Where the answer comes from — a mark, not a sentence =====
//
// The empty state used to say "everything runs on your machine"
// unconditionally. With a hosted model selected that was simply false, and a
// privacy claim that is false in the one place people read it is worse than
// no claim at all. So it became accurate, and then it became a line of prose
// under a five-word heading — "Answers come from ministral-14b-latest over the
// internet" is a model id and a caveat in the first thing you see.
//
// Which is the wrong weight for it. Local or not local is a *state*, and a
// state is a glyph: a cloud when the answer leaves the machine, a computer
// when it does not. The sentence is still there, behind an i, for the moment
// somebody wants to know which model and why.

// Under Auto there is no chosen model to name, and the promise holds only if
// none of the tasks Auto can reach escalates — so the claim is made from what
// Auto could actually do, not from what the last turn happened to use.
//
// One function, because the empty state and the status chip in the rail are
// two renderings of the same fact and must never be able to disagree about it.
function answersStayLocal() {
    return autoModel ? autoIsLocal
                     : (currentProvider === 'ollama' || currentProvider === null);
}

function renderEmptyStateLine() {
    const line = document.getElementById('chat-empty-line');
    if (!line) return;
    const local = answersStayLocal();

    let detail;
    if (local && autoModel) {
        detail = 'Carrot picks a model for each message, and every one it can '
               + 'reach runs on this computer. Nothing is sent anywhere.';
    } else if (local) {
        detail = `Answers come from ${currentModel || 'a local model'}, running on this `
               + 'computer. Nothing is sent anywhere.';
    } else if (autoModel) {
        // Wrapped between sentences rather than mid-phrase: "over the internet"
        // is the claim, and split across a concatenation it stops being
        // findable in the source by anything checking that the claim is made.
        detail = 'Carrot picks a model for each message. Some of them run '
               + 'over the internet, not on this computer.';
    } else {
        detail = `Answers come from ${currentModel || 'a hosted model'}. `
               + 'It runs over the internet, not on this computer.';
    }

    // `title` on the row as well as the button: the glyph is the thing people
    // point at, and a tooltip only on the i would mean hovering the icon that
    // raised the question tells you nothing.
    // Named whole rather than assembled from a prefix and a word: an icon id
    // that only exists once the strings are joined cannot be grepped for, so
    // nothing tells you it broke when the symbol is renamed.
    const glyph = local ? '#i-computer' : '#i-cloud';
    line.innerHTML =
        '<span class="where-mark" title="' + escHtml(detail) + '">'
        + '<svg class="ico"><use href="' + glyph + '"/></svg>'
        + '<span class="sr-only">' + escHtml(detail) + '</span>'
        + '</span>'
        + '<button type="button" class="where-why" aria-label="Where answers come from"'
        + ' title="' + escHtml(detail) + '">'
        + '<svg class="ico"><use href="#i-info"/></svg></button>';

    // Clicking says it out loud, for touch, and for anyone who does not know a
    // tooltip is there to be waited for.
    line.querySelector('.where-why').onclick = () => {
        const said = line.querySelector('.where-said');
        if (said) { said.remove(); return; }
        const note = document.createElement('span');
        note.className = 'where-said';
        note.textContent = detail;
        line.appendChild(note);
    };

    line.classList.toggle('cloud', !local);
    renderPrivacyChip();
}


// ===== What is leaving this machine =====
//
// The one claim the whole application rests on, in the one place it can be
// read without going to look for it. A dot, where it runs, and which model —
// and behind it a list of every capability that could send something
// somewhere, each saying plainly whether it is on.
//
// The list is built from the same state the assistant is told about, not from
// a second copy of it: the calendar switches, the ambient switches, the search
// mode, memory. If this panel and the model disagree about what Carrot can
// see, this panel is the bug — so it reads the same sources.

function renderPrivacyChip() {
    const chip = document.getElementById('privacy-chip');
    if (!chip) return;
    const local = answersStayLocal();
    const where = document.getElementById('privacy-where');
    const model = document.getElementById('privacy-model');
    const dot = document.getElementById('privacy-dot');

    if (where) where.textContent = local ? 'Local' : 'Cloud';
    if (model) {
        model.textContent = autoModel
            ? (local ? 'Auto · on this computer' : 'Auto · may use the internet')
            : (currentModel || (local ? 'Ollama' : 'a hosted model'));
    }
    if (dot) dot.classList.toggle('cloud', !local);
    chip.classList.toggle('cloud', !local);
    chip.title = local
        ? 'Answers are generated on this computer. Click for what else is on.'
        : 'Answers go over the internet. Click for what else is on.';
}

function togglePrivacyPanel() {
    const panel = document.getElementById('privacy-panel');
    const chip = document.getElementById('privacy-chip');
    if (!panel) return;
    const opening = panel.classList.contains('hidden');
    panel.classList.toggle('hidden', !opening);
    chip?.setAttribute('aria-expanded', String(opening));
    if (opening) { placePrivacyPanel(); fillPrivacyPanel(); }
}

// Put it beside the chip, in viewport coordinates.
//
// The panel is `position: fixed` because the rail scrolls and would otherwise
// clip it — so nothing places it for us. It opens upward from the chip, and
// only slides down if there is genuinely no room above, which on a rail this
// tall there almost never is.
function placePrivacyPanel() {
    const panel = document.getElementById('privacy-panel');
    const chip = document.getElementById('privacy-chip');
    if (!panel || !chip) return;
    const box = chip.getBoundingClientRect();
    panel.style.left = Math.round(box.left) + 'px';
    // Measured after it is laid out, not guessed from a line count — the rows
    // are as tall as their text wraps.
    panel.style.bottom = '';
    panel.style.top = '-9999px';
    const height = panel.getBoundingClientRect().height || 240;
    panel.style.top = '';
    if (box.top - height - 8 >= 8) {
        panel.style.bottom = Math.round(window.innerHeight - box.top + 8) + 'px';
    } else {
        panel.style.bottom = '8px';
    }
}

window.addEventListener('resize', () => {
    const panel = document.getElementById('privacy-panel');
    if (panel && !panel.classList.contains('hidden')) placePrivacyPanel();
});

// Every row is a real switch read from the server, never a guess. A panel that
// claims the screen is not being read while it is would be worse than not
// having one.
async function fillPrivacyPanel() {
    const panel = document.getElementById('privacy-panel');
    if (!panel) return;
    panel.innerHTML = '<div class="privacy-loading">Checking…</div>';

    const rows = [];
    const local = answersStayLocal();
    rows.push({ on: !local, label: 'Answers',
                detail: local ? 'On this computer' + (currentModel ? ' · ' + currentModel : '')
                              : 'Over the internet' + (currentModel ? ' · ' + currentModel : ''),
                leaves: !local });

    // Each of these is asked for separately and contained separately: one
    // endpoint being unreachable should grey out its own row, not empty the
    // panel and leave somebody unable to find out anything.
    //
    // A failed read says so. It used to print "Unknown", which is the exact
    // silence this panel exists to remove — indistinguishable from "off" at a
    // glance, and on a screen whose whole job is saying what is switched on,
    // "off" is the reading that matters. The reason goes to the console,
    // because a row cannot hold a stack trace and somebody debugging needs it.
    const ask = async (path, read) => {
        try { return read(await api(path)); }
        catch (err) {
            console.warn('privacy panel: could not read ' + path, err);
            return { on: false, unknown: true,
                     detail: 'Could not check — see Settings' };
        }
    };

    rows.push({ label: 'Calendar', leaves: false,
                ...await ask('/api/calendar/status', (c) => ({
                    on: !!(c.enabled && c.agent_aware && c.url_set),
                    detail: !c.url_set ? 'Not connected'
                          : !c.agent_aware ? 'Connected, not shared with the assistant'
                          : 'The assistant can read your next few days' })) });

    // `/api/ambient/policy`, not `/api/ambient`: the latter also probes memory,
    // VRAM and Ollama, which on a cold process is several seconds — mostly
    // nvidia-smi starting up. This panel wants to know what is switched on, and
    // waiting for the graphics card to answer that is how a row ends up saying
    // "could not check" on a machine where nothing is wrong.
    rows.push({ label: 'Screen history', leaves: false,
                ...await ask('/api/ambient/policy', (a) => ({
                    on: !!(a.policy || {}).enabled,
                    detail: !(a.policy || {}).enabled ? 'Not recording'
                          : (a.policy || {}).agent_aware
                              ? 'Recording, and the assistant can search it'
                              : 'Recording, not shared with the assistant' })) });

    const webOn = typeof currentSearchMode !== 'undefined' && currentSearchMode !== 'off';
    rows.push({ on: webOn, leaves: webOn, label: 'Web search',
                detail: webOn ? 'Searches leave this computer' : 'Off' });

    rows.push({ label: 'Memory', leaves: false,
                ...await ask('/api/config', (c) => ({
                    on: c.memory_enabled !== false,
                    detail: c.memory_enabled === false ? 'Off'
                                                       : 'Stored on this computer' })) });

    // "Answers and web search leave this computer" — the subject named, and
    // capitalised, because it opens the sentence. Listing what does leave is
    // more use than a count: two amber dots tell you something is going out
    // and not what.
    const leaving = rows.filter(r => r.leaves).map(r => r.label.toLowerCase());
    const named = leaving.length > 1
        ? leaving.slice(0, -1).join(', ') + ' and ' + leaving[leaving.length - 1]
        : leaving[0];
    // Phrased with a colon rather than a verb, because the verb has to agree
    // with a subject that is sometimes plural ("answers") and sometimes not
    // ("web search"), and agreeing with the length of the *list* gets it wrong
    // the moment one plural thing is going out on its own: "Answers leaves
    // this computer". A label avoids the problem instead of solving it badly.
    // "Nothing is leaving this computer" is a promise, and it cannot be made
    // over settings that could not be read. A row that failed to answer is not
    // evidence of a quiet machine — it is a gap, and the headline says so
    // rather than reassuring past it.
    const unchecked = rows.some(r => r.unknown);
    const headline = leaving.length
        ? 'Leaving this computer: ' + named
        : unchecked
            ? 'Nothing known to be leaving — some settings could not be checked'
            : 'Nothing is leaving this computer';
    panel.innerHTML =
        '<div class="privacy-head">' + escHtml(headline) + '</div>'
        + rows.map(r =>
            '<div class="privacy-row' + (r.leaves ? ' leaves' : '')
            + (r.unknown ? ' unchecked' : '') + '">'
            + '<span class="privacy-row-dot' + (r.on ? ' on' : '') + '"></span>'
            + '<span class="privacy-row-text"><strong>' + escHtml(r.label) + '</strong>'
            + '<span>' + escHtml(r.detail) + '</span></span></div>').join('')
        + '<button class="privacy-more" onclick="togglePrivacyPanel(); switchTab(\'settings\')">'
        + 'Change any of this in Settings</button>';

    // The rows arrive after the panel was first placed, and they are taller
    // than the "Checking…" line they replace — so without this it opens in the
    // right spot and then grows downward off the bottom of the window.
    placePrivacyPanel();
}

document.addEventListener('mousedown', (e) => {
    const panel = document.getElementById('privacy-panel');
    if (!panel || panel.classList.contains('hidden')) return;
    if (e.target.closest('#privacy-panel') || e.target.closest('#privacy-chip')) return;
    togglePrivacyPanel();
});
