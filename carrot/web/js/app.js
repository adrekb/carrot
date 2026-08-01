// ===== Carrot AI — workspace frontend =====
let currentTab = 'dashboard';
let currentConversationId = null;
let currentModel = null;
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

// EventSource cannot set headers, so SSE URLs carry the token as a query param.
function tokenUrl(path) {
    if (!CARROT_TOKEN) return path;
    return path + (path.includes('?') ? '&' : '?') + 'carrot_token=' + encodeURIComponent(CARROT_TOKEN);
}

async function api(path, options = {}) {
    const resp = await fetch(path, {
        ...options,
        headers: authHeaders(options.headers || {}),
    });
    if (!resp.ok) {
        let detail = resp.statusText;
        try {
            const d = (await resp.json()).detail;
            if (typeof d === 'string') detail = d;
            else if (d) detail = Array.isArray(d) ? (d[0] && d[0].msg ? d.map(x => x.msg).join('; ') : JSON.stringify(d)) : String(d);
        } catch (_) {}
        throw new Error(detail);
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
function switchTab(tab) {
    currentTab = tab;
    document.querySelectorAll('.view').forEach(v => v.classList.remove('active'));
    const el = document.getElementById(`view-${tab}`);
    if (el) el.classList.add('active');
    document.querySelectorAll('.app-nav .nav-item').forEach(b => {
        b.classList.toggle('active', b.dataset.tab === tab);
    });
    // Auto-expand "More" when one of its sub-sections is active.
    const moreList = document.getElementById('nav-more-list');
    if (moreList && moreList.querySelector(`.nav-item[data-tab="${tab}"]`)) {
        moreList.classList.remove('hidden');
        const moreBtn = document.querySelector('.nav-more');
        if (moreBtn) moreBtn.classList.add('open');
    }
    // The chat command bar only belongs to the Conversations view.
    const cmdbar = document.getElementById('cmdbar');
    if (cmdbar) cmdbar.classList.toggle('hidden', tab !== 'workspace');
    const loaders = {
        dashboard: loadDashboard,
        workspace: loadWorkspace,
        settings: loadSettings,
        chats: loadConversations,
        notes: loadNotes,
        code: loadCodeTab,
        goals: loadGoals,
        reminders: loadReminders,
        assignments: loadAssignments,
        extensions: loadExtensions,
        research: () => loadResearch(),
        agent: () => loadAgent(),
        workspaces: () => loadWorkspaces(),
        help: () => loadHelp(),
        leaderboard: loadLeaderboard,
        memory: () => loadMemory(),
        files: () => loadIndex(),
        inbox: () => refreshNotifications(),
    };
    if (loaders[tab]) loaders[tab]();
}

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
async function refreshStatus() {
    const dot = document.getElementById('engine-dot');
    const label = document.getElementById('engine-label');
    try {
        const s = await api('/api/status');
        const ok = s.ollama_available && s.model_loaded;
        dot.className = 'dot ' + (ok ? 'ok' : (s.ollama_available ? 'warn' : 'err'));
        label.textContent = ok ? 'Local Engine Active'
            : (s.ollama_available ? 'Model missing' : 'Engine offline');
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

async function loadRecapConfig() {
    try {
        const cfg = await api('/api/config');
        recapCfg.enabled = !!cfg.recap_auto_enabled;
        recapCfg.time = cfg.recap_auto_time || '04:00';
        recapCfg.last_run = cfg.recap_auto_last_run || '';
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
async function loadModels() {
    try {
        const data = await api('/api/models');
        currentModel = data.active_model;
        document.getElementById('model-label').textContent = currentModel;
        renderModelPop(data);
    } catch (_) {
        document.getElementById('model-label').textContent = 'no engine';
    }
}

function renderModelPop(data) {
    const installedEl = document.getElementById('model-installed');
    const suggestedEl = document.getElementById('model-suggested');
    installedEl.innerHTML = '';
    suggestedEl.innerHTML = '';

    if (!data.installed.length) {
        installedEl.innerHTML = '<div class="empty" style="padding:4px 9px">No models installed yet.</div>';
    }
    for (const m of data.installed) {
        const row = document.createElement('div');
        row.className = 'model-row' + (m.name === data.active_model ? ' active' : '');
        row.innerHTML = `
            <span class="m-name">${escHtml(m.name)}</span>
            <span class="m-meta">${escHtml(m.parameter_size || '')} ${fmtBytes(m.size)}</span>
            ${m.name === data.active_model ? '<svg class="ico m-check"><use href="#i-check"/></svg>' : ''}`;
        row.onclick = () => selectModel(m.name);
        installedEl.appendChild(row);
    }

    const notInstalled = data.suggested.filter(m => !m.installed);
    if (!notInstalled.length) {
        suggestedEl.innerHTML = '<div class="empty" style="padding:4px 9px">All suggestions installed.</div>';
    }
    for (const m of notInstalled) {
        const row = document.createElement('div');
        row.className = 'model-row';
        row.style.cursor = 'default';
        row.innerHTML = `
            <span class="m-name" title="${escHtml(m.blurb)}">${escHtml(m.name)}</span>
            <span class="m-meta">${escHtml(m.size_hint)}</span>`;
        const btn = document.createElement('button');
        btn.className = 'm-install';
        btn.innerHTML = '<svg class="ico"><use href="#i-download"/></svg>Install';
        btn.onclick = (e) => { e.stopPropagation(); pullModel(m.name); };
        row.appendChild(btn);
        suggestedEl.appendChild(row);
    }
}

function toggleModelPop() {
    document.getElementById('model-pop').classList.toggle('hidden');
}

async function selectModel(name) {
    try {
        await api('/api/models/select', { method: 'POST', body: JSON.stringify({ model: name }) });
        currentModel = name;
        document.getElementById('model-label').textContent = name;
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
function clearChatEmpty() {
    const empty = document.getElementById('chat-empty');
    if (empty) empty.remove();
}

function appendMessage(role, content) {
    clearChatEmpty();
    const messagesEl = document.getElementById('chat-messages');
    const div = document.createElement('div');
    div.className = `message ${role}`;
    const body = role === 'assistant' && content
        ? `<div class="content md">${mdToHtml(content)}</div>`
        : `<div class="content">${escHtml(content)}</div>`;
    div.innerHTML = `<div class="role-label">${role === 'user' ? 'You' : 'Carrot'}</div>${body}`;
    messagesEl.appendChild(div);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return div;
}

async function sendChat() {
    const input = document.getElementById('cmd-input');
    const msg = input.value.trim();
    if (!msg) return;
    input.value = '';
    hideSkillPop();
    switchTab('workspace');
    appendMessage('user', msg);
    if (!currentConversationId) {
        document.getElementById('chat-title').textContent = msg.slice(0, 42);
    }

    await streamTurn('/api/chat/stream', {
        message: msg,
        conversation_id: currentConversationId,
        model: currentModel,
        skill: activeSkill ? activeSkill.slug : null,
        search_mode: currentSearchMode,
    }, activeSkill);
}

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
    list.innerHTML = searchModes.map(mode => `
        <button class="pop-item${mode.id === currentSearchMode ? ' active' : ''}"
                onclick="setSearchMode('${escHtml(mode.id)}')">
            <span class="pop-item-name">${escHtml(mode.label)}</span>
            <span class="pop-item-sub">${escHtml(mode.help)}</span>
        </button>`).join('');
}

function toggleSearchPop() {
    const pop = document.getElementById('search-pop');
    if (pop) pop.classList.toggle('hidden');
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
        const resp = await fetch(url, {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify(payload),
        });
        if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || resp.statusText);

        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        let full = '';
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = buffer.indexOf('\n\n')) !== -1) {
                const raw = buffer.slice(0, idx).trim();
                buffer = buffer.slice(idx + 2);
                if (!raw.startsWith('data:')) continue;
                const payload = JSON.parse(raw.slice(5).trim());
                const box = document.getElementById('chat-messages');
                if (payload.skill) toolLine('skill active: ' + payload.skill.name, 'intent');
                if (payload.route) {
                    // Always say where the answer came from — local vs hosted is
                    // the single most important thing to be honest about here.
                    const where = payload.route.local ? 'on-device' : payload.route.provider;
                    toolLine(`${payload.route.model} (${where})`, 'intent');
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
                    toolLine(`tool → ${payload.tool.name}(${JSON.stringify(payload.tool.args)})`, 'search');
                }
                if (payload.tool_result) {
                    toolLine(`  ← ${String(payload.tool_result.result).slice(0, 160)}`, 'stage');
                }
                if (payload.approval_request) {
                    showApprovalPrompt(payload.approval_request);
                }
                if (payload.approval_resolved) {
                    dismissApprovalPrompt(payload.approval_resolved.id);
                }
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
                if (payload.done && payload.conversation_id) {
                    currentConversationId = payload.conversation_id;
                }
            }
        }
        finishThink();
        contentEl.classList.add('md');
        contentEl.innerHTML = full ? mdToHtml(full) : '(no response)';
        if (speakReplies && full) speakText(full);
    } catch (e) {
        contentEl.textContent = e.message;
        contentEl.classList.add('error');
    } finally {
        clearActiveSkill();
    }
}

function newChat() {
    currentConversationId = null;
    document.getElementById('chat-title').textContent = 'New session';
    const messagesEl = document.getElementById('chat-messages');
    messagesEl.innerHTML = `
        <div class="chat-empty" id="chat-empty">
            <span class="logo-mask big"></span>
            <p>Everything runs on your machine. Ask anything below.</p>
        </div>`;
    switchTab('workspace');
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
    for (const m of conv.messages) appendMessage(m.role, m.content);
    switchTab('workspace');
}

// ===== Workspace cards =====
async function loadWorkspace() {
    loadRecapCard();
    loadDeadlinesCard();
    loadMilestonesCard();
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
async function runRecap() {
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
        const resp = await fetch('/api/recap/run/stream', {
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
                if (p.thinking) traceThink(p.thinking);
                if (p.token) {
                    summary += p.token;
                    outEl.innerHTML = mdToHtml(summary);
                    outEl.scrollTop = outEl.scrollHeight;
                }
                if (p.error) traceLine(p.error, 'err');
                if (p.done) traceLine('done — briefing saved', 'ok');
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

function showSplash(s) {
    document.getElementById('splash').classList.remove('hidden');
    const status = document.getElementById('splash-status');
    if (!s.ollama_installed) status.textContent = 'Ollama is not installed. Carrot can set it up for you.';
    else if (!s.model_pulled) status.textContent = `Ollama is ready — now pull ${s.default_model}.`;
    document.getElementById('splash-btn').classList.remove('hidden');
}

function hideSplash() { document.getElementById('splash').classList.add('hidden'); }

async function runBootstrap() {
    const btn = document.getElementById('splash-btn');
    const status = document.getElementById('splash-status');
    const bar = document.getElementById('splash-bar');
    btn.classList.add('hidden');
    status.textContent = 'Installing Ollama and pulling the default model… this may take a while.';
    bar.style.width = '30%';
    try {
        const result = await api('/api/bootstrap/run', { method: 'POST' });
        bar.style.width = '100%';
        if (result.error) {
            status.textContent = result.error;
            btn.classList.remove('hidden');
        } else {
            status.textContent = 'Setup complete. Launching Carrot…';
            setTimeout(() => { hideSplash(); refreshStatus(); loadModels(); }, 900);
        }
    } catch (e) {
        status.textContent = e.message;
        btn.classList.remove('hidden');
    }
}

// ===== Init =====
document.addEventListener('DOMContentLoaded', async () => {
    await loadRecapConfig();
    await refreshStatus();
    loadModels();
    loadSkillCatalog();
    loadSearchModes();
    loadWorkspaces();
    checkBootstrap();
    switchTab('dashboard');
    loadTerminalHistory();
    setInterval(refreshStatus, 15000);

    // Ctrl+K focuses the command bar
    document.addEventListener('keydown', e => {
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
            e.preventDefault();
            focusCmd();
        }
    });

    // Click outside closes model popover
    document.addEventListener('click', e => {
        const picker = document.getElementById('model-picker');
        if (!picker.contains(e.target)) {
            document.getElementById('model-pop').classList.add('hidden');
        }
        const cmdbar = document.getElementById('cmdbar');
        if (!cmdbar.contains(e.target)) hideSkillPop();
    });
});
