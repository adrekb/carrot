// ===== Carrot AI — memory, index, notifications, routing, and approvals =====
// Loaded after app.js, so `api`, `escHtml`, `tokenUrl` and `switchTab` exist.

// ---------- Approval prompts ----------
// A mutating agent tool blocks server-side until the user answers. The prompt
// arrives on the chat stream; this renders it and posts the decision back.

function approvalHost() {
    let host = document.getElementById('approval-host');
    if (!host) {
        host = document.createElement('div');
        host.id = 'approval-host';
        host.className = 'approval-host';
        document.body.appendChild(host);
    }
    return host;
}

// The server decides what a prompt may offer, and this renders exactly that:
// `remember_allowed` false means no "don't ask again" checkbox at all, and a
// `confirm_phrase` means Allow stays disabled until the phrase is typed. Both
// are re-checked server-side — the UI is a convenience, not the control.

function showApprovalPrompt(request) {
    const card = document.createElement('div');
    card.className = 'approval-card risk-' + escHtml(request.risk || 'low');
    card.id = 'approval-' + request.id;

    const rememberRow = request.remember_allowed === false ? '' : `
        <label class="approval-remember">
            <input type="checkbox" id="approval-remember-${escHtml(request.id)}">
            Don't ask again for this action this session
        </label>`;

    const confirmRow = request.confirm_phrase ? `
        <div class="approval-confirm">
            <div class="approval-confirm-note">
                This is a high-consequence action. Type
                <strong>${escHtml(request.confirm_phrase)}</strong> to allow it.
            </div>
            <input type="text" id="approval-confirm-${escHtml(request.id)}"
                   class="approval-confirm-input" autocomplete="off" spellcheck="false">
        </div>` : '';

    // "Allow, and stop asking" only where the server said remembering is
    // allowed — an irreversible action never offers it, whatever this renders.
    const alwaysButton = (request.remember_allowed === false || request.confirm_phrase)
        ? '' : '<button class="btn ghost" data-decision="allow" data-always="1">'
               + 'Allow &amp; stop asking</button>';

    const detail = request.detail ? `
        <div class="approval-detail">${escHtml(String(request.detail).slice(0, 600))}</div>` : '';

    card.innerHTML = `
        <div class="approval-head">Carrot wants to ${escHtml(request.tool === 'start_task' ? 'start a task' : 'take an action')}</div>
        <div class="approval-summary">${escHtml(request.summary)}</div>
        ${detail}
        <div class="approval-tool">${escHtml(request.tool)} · ${escHtml(request.risk)} risk</div>
        ${rememberRow}
        ${confirmRow}
        <div class="approval-actions">
            <button class="btn ghost" data-decision="deny">Deny</button>
            ${alwaysButton}
            <button class="btn primary" data-decision="allow">Allow</button>
        </div>`;

    card.querySelectorAll('button[data-decision]').forEach(button => {
        button.onclick = () => {
            const remember = document.getElementById('approval-remember-' + request.id);
            const confirmation = document.getElementById('approval-confirm-' + request.id);
            resolveApproval(
                request.id,
                button.dataset.decision,
                // The dedicated button is the "minimal input from my end" path:
                // a user who has just said "just do it" should not have to find
                // a checkbox, tick it, and then find the button.
                button.dataset.always === '1' || (remember && remember.checked),
                confirmation ? confirmation.value : '',
            );
        };
    });
    approvalHost().appendChild(card);
    alertAwayFromScreen(request);

    const confirmation = document.getElementById('approval-confirm-' + request.id);
    if (confirmation) confirmation.focus();
}

// A prompt nobody is looking at is a run that has stopped. The agent blocks
// until it is answered, so an unwatched approval does not fail safe — it fails
// *slow*, and the whole task is wasted waiting on a window in the background.
//
// Only when the window is actually unfocused: someone sitting in front of the
// card does not need their operating system to tell them it is there, and a
// toast per step during an attended run is its own kind of broken.
// Clicking the toast raises the window, which is where the card is waiting.
function alertAwayFromScreen(request) {
    const what = request.tool === 'start_task'
        ? 'Carrot is ready to start a task'
        : 'Carrot needs your approval';
    notifyIfAway(what, String(request.summary || request.tool || 'An action is waiting.'));
}

// A run that finished while you were elsewhere is the other half of the same
// problem: the approval notification saves the time a blocked run wastes, this
// one saves the time a *finished* run wastes sitting there unread.
//
// Only for work that ran long enough for someone to have walked away. A toast
// for a turn that took four seconds is noise, and noise is how a notification
// stops being read.
const AWAY_NOTICE_AFTER_MS = 20000;

function notifyWhenLongRunFinishes(startedAt, title, body) {
    if (Date.now() - startedAt < AWAY_NOTICE_AFTER_MS) return;
    notifyIfAway(title, body);
}

function notifyIfAway(title, body) {
    if (document.hasFocus()) return;
    body = String(body || '').slice(0, 160);
    try {
        if (window.carrot && window.carrot.notify) {
            window.carrot.notify(title, body);
            return;
        }
        // In a plain browser rather than the desktop app. Same idea, and the
        // permission ask happens on the first approval rather than at startup,
        // where it would be a prompt about nothing.
        if (typeof Notification === 'undefined') return;
        if (Notification.permission === 'granted') {
            new Notification(title, { body }).onclick = () => window.focus();
        } else if (Notification.permission !== 'denied') {
            Notification.requestPermission().then(granted => {
                if (granted === 'granted') {
                    new Notification(title, { body }).onclick = () => window.focus();
                }
            });
        }
    } catch (_) {
        // Never let a failed toast take the approval card down with it.
    }
}

function dismissApprovalPrompt(approvalId) {
    const card = document.getElementById('approval-' + approvalId);
    if (card) card.remove();
}

// The server says, every ten seconds, that it is still waiting. Saying so on
// the card is the difference between a turn that is being patient and a turn
// that has died — which from the outside were the same picture, and got
// reported as the agent hanging without ever finishing.
function noteApprovalWaiting(waiting) {
    const card = document.getElementById('approval-' + waiting.id);
    if (!card) return;
    let line = card.querySelector('.approval-waiting');
    if (!line) {
        line = document.createElement('div');
        line.className = 'approval-waiting';
        card.appendChild(line);
    }
    const left = Math.round((waiting.seconds_left || 0) / 60);
    line.textContent = `Waiting for you — ${waiting.seconds}s so far`
        + (left ? `, giving up in about ${left} min` : '');
}

async function resolveApproval(approvalId, decision, remember, confirmation) {
    dismissApprovalPrompt(approvalId);
    try {
        await api(`/api/agent/approvals/${approvalId}`, {
            method: 'POST',
            body: JSON.stringify({
                decision,
                remember: !!remember,
                confirmation: confirmation || '',
            }),
        });
    } catch (e) {
        console.warn('approval failed', e);
    }
}

// ---------- Notifications ----------

let notificationStream = null;

async function refreshNotifications() {
    try {
        const data = await api('/api/notifications');
        renderNotificationBadge(data.unread);
        renderNotificationList(data.notifications);
    } catch (_) {}
}

function renderNotificationBadge(count) {
    const badge = document.getElementById('notification-badge');
    if (!badge) return;
    badge.textContent = count > 99 ? '99+' : String(count);
    // Said in full wherever the number is. It rides on the Work button, where
    // a bare "7" reads as seven documents — which is the thing Work is full
    // of, and not what this counts.
    const said = count === 1 ? '1 unread notification'
                             : `${count} unread notifications`;
    badge.title = said;
    badge.setAttribute('aria-label', said);
    badge.classList.toggle('hidden', !count);
}

function renderNotificationList(notifications) {
    const list = document.getElementById('notification-list');
    if (!list) return;
    if (!notifications.length) {
        list.innerHTML = '<div class="empty">Nothing needs your attention.</div>';
        return;
    }
    list.innerHTML = '';
    for (const n of notifications) {
        const row = document.createElement('div');
        row.className = 'notif-row sev-' + escHtml(n.severity) + (n.read ? ' read' : '');
        row.innerHTML = `
            <div class="notif-body">
                <div class="notif-title">${escHtml(n.title)}</div>
                <div class="notif-text">${escHtml(n.body)}</div>
            </div>
            <button class="btn ghost tiny" title="Dismiss">×</button>`;
        row.querySelector('button').onclick = () => dismissNotification(n.id);
        list.appendChild(row);
    }
}

async function dismissNotification(id) {
    try {
        await api(`/api/notifications/${id}`, { method: 'DELETE' });
        refreshNotifications();
    } catch (_) {}
}

async function markAllNotificationsRead() {
    try {
        await api('/api/notifications/read-all', { method: 'POST' });
        refreshNotifications();
    } catch (_) {}
}

async function runNotificationChecks() {
    try {
        await api('/api/notifications/check', { method: 'POST' });
        refreshNotifications();
    } catch (_) {}
}

async function startNotificationStream() {
    if (notificationStream) return;
    try {
        // A ticket has to be fetched first, so this is async now. The guard
        // above is not enough on its own any more: two calls can both pass it
        // while the first is still awaiting its ticket, and the second would
        // open a duplicate stream. The flag is claimed before the await.
        notificationStream = 'pending';
        notificationStream = new EventSource(await tokenUrl('/api/notifications/stream'));
        notificationStream.onmessage = (event) => {
            const notification = JSON.parse(event.data);
            refreshNotifications();
            // The Electron shell turns this into a native OS notification.
            if (window.carrot && window.carrot.notify) {
                window.carrot.notify(notification.title, notification.body);
            }
        };
        // The endpoint closes after its window; reconnect on the next tick.
        notificationStream.onerror = () => {
            notificationStream.close();
            notificationStream = null;
            setTimeout(startNotificationStream, 5000);
        };
    } catch (_) {}
}

// ---------- Memory ----------

// `workspace: 'all'` rather than the active one, deliberately. This screen is
// the audit of everything Carrot believes about you; opening it inside a
// project and being shown a third of your memories, with nothing saying so,
// would be the wrong default for the one place you go to check.
let memoryFilter = { kind: '', status: 'active', subject: '', origin: '', workspace: 'all' };

async function loadMemory() {
    const list = document.getElementById('memory-list');
    if (!list) return;
    list.innerHTML = '<div class="empty">Loading…</div>';
    try {
        const params = new URLSearchParams();
        if (memoryFilter.kind) params.set('kind', memoryFilter.kind);
        if (memoryFilter.status) params.set('status', memoryFilter.status);
        if (memoryFilter.subject) params.set('subject', memoryFilter.subject);
        if (memoryFilter.origin) params.set('origin', memoryFilter.origin);
        params.set('workspace', memoryFilter.workspace || 'all');

        const data = await api('/api/memory?' + params.toString());
        renderMemoryOptions(data.origins);
        renderMemoryStats(data.stats);
        renderMemoryList(data.memories);
    } catch (e) {
        list.innerHTML = `<div class="empty error">${escHtml(e.message)}</div>`;
    }
}

// The origin list comes from the server because it is a fact about what
// actually writes memories — offering a filter for something nothing produces
// would be a dropdown entry that always returns nothing.
function renderMemoryOptions(origins) {
    const originEl = document.getElementById('memory-origin');
    if (originEl && origins && originEl.options.length <= 1) {
        for (const origin of origins) {
            const option = document.createElement('option');
            option.value = origin.id;
            // The server writes the whole line. "Learned in " + label reads
            // fine for three origins and produces "Learned in you" for the one
            // that matters most.
            option.textContent = origin.filter || origin.label;
            originEl.appendChild(option);
        }
    }

    // Rebuilt every load, not filled in once: a workspace created after this
    // panel first opened would otherwise never appear in its own filter.
    const wsEl = document.getElementById('memory-workspace');
    if (wsEl && typeof flatWorkspaces === 'function') {
        const chosen = wsEl.value || 'all';
        wsEl.innerHTML = '<option value="all">All workspaces</option>';
        for (const workspace of flatWorkspaces()) {
            const option = document.createElement('option');
            option.value = workspace.id;
            option.textContent = workspace.path
                ? `${workspace.path} / ${workspace.name}` : workspace.name;
            wsEl.appendChild(option);
        }
        // A workspace deleted out from under the filter falls back to "all" —
        // in the dropdown *and* in the filter, so the two cannot disagree
        // about what you are looking at.
        const stillThere = [...wsEl.options].some(o => o.value === chosen);
        wsEl.value = stillThere ? chosen : 'all';
        if (!stillThere) memoryFilter.workspace = 'all';
    }
}

// "manual" is what the column stores; "you" is what it means. The label is
// only ever shown, never sent back as a filter value.
const MEMORY_ORIGIN_LABELS = { chat: 'chat', code: 'code', document: 'document', manual: 'you' };

function memoryOriginLabel(origin) {
    return MEMORY_ORIGIN_LABELS[origin] || origin || 'chat';
}

function renderMemoryStats(stats) {
    const el = document.getElementById('memory-stats');
    if (!el || !stats) return;
    const kinds = Object.entries(stats.by_kind || {})
        .map(([kind, count]) => `${escHtml(kind)} ${count}`).join(' · ');
    const origins = Object.entries(stats.by_origin || {})
        .map(([origin, count]) => `${memoryOriginLabel(origin)} ${count}`).join(' · ');
    el.textContent = `${stats.total} remembered${kinds ? ' — ' + kinds : ''}`
        + (origins ? ` · learned in ${origins}` : '');
}

function renderMemoryList(memories) {
    const list = document.getElementById('memory-list');
    if (!list) return;
    if (!memories.length) {
        // "Nothing remembered yet" is false the moment a filter is on, and it
        // is exactly the wrong thing to read after narrowing to a workspace.
        const filtered = memoryFilter.kind || memoryFilter.origin
            || (memoryFilter.workspace && memoryFilter.workspace !== 'all')
            || memoryFilter.status !== 'active';
        list.innerHTML = filtered
            ? '<div class="empty">Nothing matches these filters.</div>'
            : '<div class="empty">Nothing remembered yet. Carrot learns as you chat.</div>';
        return;
    }
    list.innerHTML = '';
    for (const m of memories) {
        const row = document.createElement('div');
        row.className = 'memory-row' + (m.pinned ? ' pinned' : '');
        row.innerHTML = `
            <div class="memory-main">
                <div class="memory-content" contenteditable="true">${escHtml(m.content)}</div>
                <div class="memory-meta">
                    <span class="tag">${escHtml(m.kind)}</span>
                    <span class="tag subtle">${escHtml(m.subject)}</span>
                    <span class="tag origin origin-${escHtml(m.origin || 'chat')}">${escHtml(memoryOriginLabel(m.origin))}</span>
                    <span class="subtle">confidence ${Math.round((m.confidence || 0) * 100)}%</span>
                    ${m.source_conversation_id
                        ? `<a href="#" class="subtle memory-source">source</a>` : '<span class="subtle">no source</span>'}
                </div>
            </div>
            <div class="memory-actions">
                <button class="btn ghost tiny" data-act="pin">${m.pinned ? 'Unpin' : 'Pin'}</button>
                <button class="btn ghost tiny" data-act="save">Save</button>
                <button class="btn ghost tiny" data-act="reject">Wrong</button>
            </div>`;

        const content = row.querySelector('.memory-content');
        row.querySelector('[data-act="pin"]').onclick = () => updateMemory(m.id, { pinned: !m.pinned });
        row.querySelector('[data-act="save"]').onclick = () =>
            updateMemory(m.id, { content: content.textContent.trim() });
        row.querySelector('[data-act="reject"]').onclick = () => rejectMemory(m.id);

        const source = row.querySelector('.memory-source');
        if (source) {
            source.onclick = (event) => {
                event.preventDefault();
                openConversation(m.source_conversation_id);
            };
        }
        list.appendChild(row);
    }
}

async function updateMemory(id, fields) {
    try {
        await api(`/api/memory/${id}`, { method: 'PUT', body: JSON.stringify(fields) });
        loadMemory();
    } catch (e) {
        alert(e.message);
    }
}

async function rejectMemory(id) {
    if (!confirm('Mark this as wrong? Carrot will stop recording this subject.')) return;
    try {
        await api(`/api/memory/${id}/reject`, { method: 'POST' });
        loadMemory();
    } catch (e) {
        alert(e.message);
    }
}

function setMemoryFilter(field, value) {
    memoryFilter[field] = value;
    loadMemory();
}

async function searchMemory() {
    const input = document.getElementById('memory-search');
    if (!input || !input.value.trim()) return loadMemory();
    try {
        // The workspace filter is a scope, not a decoration on the list — a
        // search inside a project that answers from every project is a lie
        // about what you were looking at.
        const data = await api('/api/memory/search?q=' + encodeURIComponent(input.value.trim())
            + '&workspace=' + encodeURIComponent(memoryFilter.workspace || 'all'));
        renderMemoryList(data.results);
    } catch (e) {
        alert(e.message);
    }
}

// ---------- Document index ----------

let indexPoll = null;

async function loadIndex() {
    try {
        const status = await api('/api/index/status');
        renderIndexStatus(status);
        if (status.running && !indexPoll) {
            indexPoll = setInterval(loadIndex, 1500);
        } else if (!status.running && indexPoll) {
            clearInterval(indexPoll);
            indexPoll = null;
        }
    } catch (_) {}
}

function renderIndexStatus(status) {
    const el = document.getElementById('index-status');
    if (!el) return;
    const stats = status.stats || {};
    el.innerHTML = status.running
        ? `<span class="dot warn"></span> Indexing — ${status.scanned} scanned, ${status.indexed} added
           <div class="subtle mono">${escHtml((status.current || '').slice(-70))}</div>`
        : `<span class="dot ok"></span> ${stats.documents || 0} documents · ${stats.chunks || 0} chunks
           · ${stats.embedded_chunks || 0} embedded`;

    const dirs = document.getElementById('index-dirs');
    if (!dirs) return;
    const configured = status.dirs || [];
    if (!configured.length) {
        dirs.innerHTML = '<div class="empty">No folders indexed yet. Add one to search your files.</div>';
        return;
    }
    dirs.innerHTML = '';
    for (const dir of configured) {
        const row = document.createElement('div');
        row.className = 'index-dir';
        row.innerHTML = `<span class="mono">${escHtml(dir)}</span>
                         <button class="btn ghost tiny">Remove</button>`;
        row.querySelector('button').onclick = () => removeIndexDir(dir);
        dirs.appendChild(row);
    }
}

async function addIndexDir() {
    const input = document.getElementById('index-dir-input');
    if (!input || !input.value.trim()) return;
    try {
        await api('/api/index/dirs', {
            method: 'POST',
            body: JSON.stringify({ path: input.value.trim() }),
        });
        input.value = '';
        loadIndex();
    } catch (e) {
        alert(e.message);
    }
}

async function removeIndexDir(path) {
    try {
        await api('/api/index/dirs?path=' + encodeURIComponent(path), { method: 'DELETE' });
        loadIndex();
    } catch (e) {
        alert(e.message);
    }
}

async function startIndexScan(force) {
    try {
        await api('/api/index/scan', { method: 'POST', body: JSON.stringify({ force: !!force }) });
        loadIndex();
    } catch (e) {
        alert(e.message);
    }
}

async function backfillEmbeddings() {
    try {
        const result = await api('/api/vectors/backfill', { method: 'POST' });
        const failed = (result.results || []).find(r => r.error);
        alert(failed ? `Stopped: ${failed.error}` : 'Embedding backfill complete.');
        loadIndex();
    } catch (e) {
        alert(e.message);
    }
}

// ---------- Unified search ----------

async function searchEverything(query) {
    const host = document.getElementById('unified-results');
    if (!host) return;
    host.innerHTML = '<div class="empty">Searching…</div>';
    try {
        const data = await api('/api/search/all?q=' + encodeURIComponent(query));
        host.innerHTML = '';
        host.appendChild(unifiedSection('Memory', data.memories, m =>
            `<div class="u-title">${escHtml(m.subject)}</div><div>${escHtml(m.content)}</div>`));
        host.appendChild(unifiedSection('Your files', data.documents, d =>
            `<div class="u-title">${escHtml(d.title)}</div>
             <div class="subtle mono">${escHtml(d.path)}</div>
             <div>${escHtml(String(d.content).slice(0, 240))}</div>`));
        host.appendChild(unifiedSection('Conversations', data.conversations, c =>
            `<div class="u-title">${escHtml(c.conversation_title || 'Untitled')}</div>
             <div class="subtle">${escHtml((c.timestamp || '').slice(0, 10))}</div>
             <div>${escHtml(String(c.content).slice(0, 240))}</div>`));
    } catch (e) {
        host.innerHTML = `<div class="empty error">${escHtml(e.message)}</div>`;
    }
}

function unifiedSection(title, items, render) {
    const section = document.createElement('div');
    section.className = 'u-section';
    section.innerHTML = `<h4>${escHtml(title)} <span class="subtle">${(items || []).length}</span></h4>`;
    if (!items || !items.length) {
        section.innerHTML += '<div class="empty">No matches.</div>';
        return section;
    }
    for (const item of items) {
        const row = document.createElement('div');
        row.className = 'u-row';
        row.innerHTML = render(item);
        section.appendChild(row);
    }
    return section;
}

// ---------- Providers and model routing ----------

// The last /api/router/status payload, so the task table can be re-rendered
// (on a provider change, say) without another round trip.
let routerState = null;
// Models are fetched from each provider on demand and cached for the session —
// Carrot never hardcodes model names, because they go stale.
const providerModels = {};

async function loadRouting() {
    const host = document.getElementById('routing-panel');
    if (!host) return;
    try {
        routerState = await api('/api/router/status');
        renderProviders(routerState);
        renderRouting(routerState);
        const enabled = document.getElementById('cloud-enabled');
        if (enabled) enabled.checked = !!routerState.cloud_enabled;
    } catch (e) {
        host.innerHTML = `<div class="empty error">${escHtml(e.message)}</div>`;
    }
}

function providerLabel(id) {
    const provider = (routerState?.providers || []).find(p => p.id === id);
    return provider ? provider.label : id;
}

// ---------- Providers ----------

function renderProviders(status) {
    const host = document.getElementById('providers-panel');
    if (!host) return;

    host.innerHTML = (status.providers || []).map(provider => {
        const state = !provider.enabled ? ['dot', 'off']
            : provider.configured ? ['dot', 'ok'] : ['dot', 'warn'];
        const detail = !provider.enabled ? 'disabled'
            : provider.configured
                ? (provider.requires_key ? 'key configured' : 'no key needed')
                : `needs a key${provider.env_var ? ` (or $${provider.env_var})` : ''}`;
        const id = escHtml(provider.id);
        return `
        <div class="provider-row">
          <div class="provider-main">
            <span class="${state.join(' ')}"></span>
            <span class="provider-name">${escHtml(provider.label)}</span>
            <span class="tag">${escHtml(provider.kind)}</span>
            ${provider.builtin ? '' : '<span class="tag">custom</span>'}
            <span class="subtle mono">${escHtml(provider.base_url || 'local')}</span>
          </div>
          <div class="provider-detail subtle">${escHtml(detail)}</div>
          <div class="provider-actions">
            ${provider.requires_key ? `
              <input type="password" id="key-${id}" class="provider-key"
                     placeholder="${provider.key_set ? 'Replace key' : 'Paste API key'}" spellcheck="false">
              <button class="btn btn-ghost" onclick="saveProviderKey('${id}')">Save</button>` : ''}
            ${provider.key_set ? `<button class="btn btn-ghost" onclick="clearProviderKey('${id}')">Forget</button>` : ''}
            <button class="btn btn-ghost" onclick="testProvider('${id}')">Test</button>
            <button class="btn btn-ghost" onclick="toggleProvider('${id}', ${!provider.enabled})">
              ${provider.enabled ? 'Disable' : 'Enable'}</button>
            ${provider.builtin ? '' : `<button class="btn btn-ghost" onclick="removeProvider('${id}')">Remove</button>`}
          </div>
          <div class="provider-result subtle" id="test-${id}"></div>
        </div>`;
    }).join('');

    const preset = document.getElementById('prov-preset');
    if (preset && !preset.options.length) {
        preset.innerHTML = (status.presets || [])
            .map(p => `<option value="${escHtml(p.id)}">${escHtml(p.label)}</option>`).join('');
        applyProviderPreset();
    }

    if (status.cloud_configured && !status.sdk_installed) {
        host.innerHTML += `<div class="empty error">The anthropic package is not installed —
            run <span class="mono">pip install 'carrot[cloud]'</span> to use Anthropic.</div>`;
    }
}

function applyProviderPreset() {
    const select = document.getElementById('prov-preset');
    const preset = (routerState?.presets || []).find(p => p.id === select?.value);
    if (!preset) return;
    const set = (elementId, value) => {
        const element = document.getElementById(elementId);
        if (element) element.value = value;
    };
    set('prov-id', preset.id === 'custom' ? '' : preset.id);
    set('prov-label', preset.id === 'custom' ? '' : preset.label);
    set('prov-base', preset.base_url);
    set('prov-kind', preset.kind);
}

async function addProvider() {
    const value = id => (document.getElementById(id)?.value || '').trim();
    try {
        await api('/api/router/providers', {
            method: 'POST',
            body: JSON.stringify({
                id: value('prov-id'),
                label: value('prov-label'),
                kind: value('prov-kind') || 'openai',
                base_url: value('prov-base'),
                api_key: value('prov-key'),
            }),
        });
        const key = document.getElementById('prov-key');
        if (key) key.value = '';
        loadRouting();
    } catch (e) {
        alert(e.message);
    }
}

async function saveProviderKey(providerId) {
    const input = document.getElementById(`key-${providerId}`);
    if (!input || !input.value.trim()) return;
    try {
        await api(`/api/router/providers/${encodeURIComponent(providerId)}/key`, {
            method: 'PUT', body: JSON.stringify({ api_key: input.value.trim() }),
        });
        input.value = '';
        delete providerModels[providerId];
        loadRouting();
    } catch (e) {
        alert(e.message);
    }
}

async function clearProviderKey(providerId) {
    if (!confirm(`Forget the stored API key for ${providerLabel(providerId)}?`)) return;
    try {
        await api(`/api/router/providers/${encodeURIComponent(providerId)}/key`, {
            method: 'PUT', body: JSON.stringify({ api_key: '' }),
        });
        loadRouting();
    } catch (e) {
        alert(e.message);
    }
}

async function toggleProvider(providerId, enabled) {
    try {
        await api(`/api/router/providers/${encodeURIComponent(providerId)}/enabled`, {
            method: 'PUT', body: JSON.stringify({ enabled }),
        });
        loadRouting();
    } catch (e) {
        alert(e.message);
    }
}

async function removeProvider(providerId) {
    if (!confirm(`Remove ${providerLabel(providerId)} and forget its key?`)) return;
    try {
        await api(`/api/router/providers/${encodeURIComponent(providerId)}`, { method: 'DELETE' });
        loadRouting();
    } catch (e) {
        alert(e.message);
    }
}

async function testProvider(providerId) {
    const host = document.getElementById(`test-${providerId}`);
    if (host) host.textContent = 'Testing…';
    try {
        const result = await api(`/api/router/providers/${encodeURIComponent(providerId)}/test`,
            { method: 'POST' });
        if (host) {
            host.textContent = result.ok
                ? `Reachable${result.models ? ` — ${result.models} models` : ''}`
                : `Failed: ${result.error}`;
            host.className = `provider-result subtle ${result.ok ? 'ok' : 'error'}`;
        }
    } catch (e) {
        if (host) {
            host.textContent = `Failed: ${e.message}`;
            host.className = 'provider-result subtle error';
        }
    }
}

// ---------- Task routing ----------

function renderRouting(status) {
    const host = document.getElementById('routing-panel');
    if (!host) return;

    const usable = (status.providers || []).filter(p => p.enabled && p.configured);
    const assignments = status.assignments || {};

    const rows = (status.tasks || []).map(task => {
        const route = status.routes[task.id] || {};
        const pinned = assignments[task.id];
        const options = ['<option value="">auto</option>'].concat(usable.map(p =>
            `<option value="${escHtml(p.id)}"${pinned && pinned.provider === p.id ? ' selected' : ''}>
                ${escHtml(p.label)}</option>`)).join('');
        return `
        <tr>
            <td>
              <div>${escHtml(task.label)}</div>
              <div class="subtle mono">${escHtml(task.id)}</div>
            </td>
            <td><select id="route-provider-${escHtml(task.id)}"
                        onchange="loadRouteModels('${escHtml(task.id)}')">${options}</select></td>
            <td>
              <div class="combo" id="combo-${escHtml(task.id)}">
                <input id="route-model-${escHtml(task.id)}" class="mono combo-input"
                       placeholder="${escHtml(route.model || 'auto')}"
                       value="${escHtml(pinned ? pinned.model : '')}" spellcheck="false"
                       autocomplete="off"
                       oninput="comboFilter('${escHtml(task.id)}')"
                       onfocus="comboOpen('${escHtml(task.id)}')">
                <button type="button" class="combo-caret" tabindex="-1"
                        onclick="comboToggle('${escHtml(task.id)}')">
                  <svg class="ico"><use href="#i-chevron"/></svg>
                </button>
                <div class="combo-pop hidden" id="combo-pop-${escHtml(task.id)}"></div>
              </div>
            </td>
            <td>
              <span class="tag ${route.local ? '' : 'cloud'}">
                ${route.local ? 'on-device' : escHtml(route.provider || '')}</span>
              <div class="subtle">${escHtml(route.reason || '')}</div>
            </td>
            <td class="route-actions">
              <button class="btn btn-ghost" onclick="saveRoute('${escHtml(task.id)}')">Save</button>
              ${pinned ? `<button class="btn btn-ghost" onclick="clearRoute('${escHtml(task.id)}')">Auto</button>` : ''}
              ${task.builtin ? '' : `<button class="btn btn-ghost" onclick="removeTask('${escHtml(task.id)}')">Delete</button>`}
            </td>
        </tr>`;
    }).join('');

    const recommendation = status.recommendation || {};
    host.innerHTML = `
        <div class="routing-summary">
            ${status.cloud_enabled
                ? `<span class="dot warn"></span> Unpinned tasks may escalate to
                   ${escHtml(providerLabel(status.escalation_provider))}: ${escHtml(status.cloud_tasks.join(', ') || 'nothing')}`
                : '<span class="dot ok"></span> Everything not pinned to a provider runs on this machine'}
        </div>
        ${recommendation.model
            ? `<div class="subtle">Suggested local model: <span class="mono">${escHtml(recommendation.model)}</span>
               — ${escHtml(recommendation.reason)}</div>` : ''}
        <table class="routing-table">
            <thead><tr><th>Task</th><th>Provider</th><th>Model</th><th>Resolves to</th><th></th></tr></thead>
            <tbody>${rows}</tbody>
        </table>`;

    for (const task of status.tasks || []) {
        if (assignments[task.id]) fillModelList(task.id, assignments[task.id].provider);
    }
}

async function loadRouteModels(taskId) {
    const select = document.getElementById(`route-provider-${taskId}`);
    if (select) fillModelList(taskId, select.value);
}

async function fillModelList(taskId, providerId) {
    if (!providerId) return;
    try {
        if (!providerModels[providerId]) {
            const result = await api(`/api/router/providers/${encodeURIComponent(providerId)}/models`);
            providerModels[providerId] = result.models || [];
        }
        comboRender(taskId);
    } catch (_) {
        // A provider that cannot list its models is still usable — the model
        // field is a free-text input, so the user can just type the name.
    }
}

// ---------- Model combobox ----------
//
// A native <datalist> popup is drawn by the browser, so it ignores the app's
// theme entirely and scrolls badly inside Electron. This is a plain styled
// listbox: same free-text input, but a scrollable, filterable, themed popup.

function comboModels(taskId) {
    const providerId = document.getElementById(`route-provider-${taskId}`)?.value || '';
    return providerModels[providerId] || [];
}

// Filtering only ever happens while the user is typing. Opening the list
// always shows everything — otherwise a saved value filters the list down
// to itself and the route can never be changed again.
function comboRender(taskId, filtered) {
    const pop = document.getElementById(`combo-pop-${taskId}`);
    const input = document.getElementById(`route-model-${taskId}`);
    if (!pop || !input) return;
    const typed = filtered ? input.value.trim().toLowerCase() : '';
    const models = comboModels(taskId)
        .filter(m => !typed || m.toLowerCase().includes(typed));
    if (!models.length) {
        pop.innerHTML = `<div class="combo-empty">${
            comboModels(taskId).length ? 'No match — type any model name.'
                                       : 'Pick a provider to list its models.'}</div>`;
        return;
    }
    pop.innerHTML = models.map(m => `
        <button type="button" class="combo-opt${m === input.value ? ' selected' : ''}"
                onmousedown="comboPick('${escHtml(taskId)}', '${escHtml(m)}')">${escHtml(m)}</button>`
    ).join('');
}

function comboOpen(taskId) {
    // Only one popup at a time.
    document.querySelectorAll('.combo-pop').forEach(p => p.classList.add('hidden'));
    comboRender(taskId, false);   // show every model, whatever is in the box
    document.getElementById(`combo-pop-${taskId}`)?.classList.remove('hidden');
}

function comboToggle(taskId) {
    const pop = document.getElementById(`combo-pop-${taskId}`);
    if (!pop) return;
    if (pop.classList.contains('hidden')) {
        document.getElementById(`route-model-${taskId}`)?.focus();
        comboOpen(taskId);
    } else {
        pop.classList.add('hidden');
    }
}

function comboFilter(taskId) {
    comboRender(taskId, true);    // typing narrows the list
    document.getElementById(`combo-pop-${taskId}`)?.classList.remove('hidden');
}

function comboPick(taskId, model) {
    const input = document.getElementById(`route-model-${taskId}`);
    if (input) input.value = model;
    document.getElementById(`combo-pop-${taskId}`)?.classList.add('hidden');
}

// Clicking anywhere else closes any open model popup.
document.addEventListener('click', (e) => {
    if (!e.target.closest || !e.target.closest('.combo')) {
        document.querySelectorAll('.combo-pop').forEach(p => p.classList.add('hidden'));
    }
});

async function saveRoute(taskId) {
    const provider = document.getElementById(`route-provider-${taskId}`)?.value || '';
    const model = (document.getElementById(`route-model-${taskId}`)?.value || '').trim();
    try {
        if (!provider || !model) {
            await api(`/api/router/route/${encodeURIComponent(taskId)}`, { method: 'DELETE' });
        } else {
            await api('/api/router/route', {
                method: 'PUT',
                body: JSON.stringify({ task: taskId, provider, model }),
            });
        }
        loadRouting();
    } catch (e) {
        alert(e.message);
    }
}

async function clearRoute(taskId) {
    try {
        await api(`/api/router/route/${encodeURIComponent(taskId)}`, { method: 'DELETE' });
        loadRouting();
    } catch (e) {
        alert(e.message);
    }
}

async function addTask() {
    const value = id => (document.getElementById(id)?.value || '').trim();
    try {
        await api('/api/router/tasks', {
            method: 'POST',
            body: JSON.stringify({
                id: value('task-id'),
                label: value('task-label'),
                description: value('task-desc'),
            }),
        });
        for (const id of ['task-id', 'task-label', 'task-desc']) {
            const element = document.getElementById(id);
            if (element) element.value = '';
        }
        loadRouting();
    } catch (e) {
        alert(e.message);
    }
}

async function removeTask(taskId) {
    if (!confirm(`Delete the '${taskId}' task and its assignment?`)) return;
    try {
        await api(`/api/router/tasks/${encodeURIComponent(taskId)}`, { method: 'DELETE' });
        loadRouting();
    } catch (e) {
        alert(e.message);
    }
}

async function saveCloudSettings() {
    const enabled = document.getElementById('cloud-enabled');
    try {
        if (enabled) {
            await api('/api/config/cloud_enabled', {
                method: 'PUT', body: JSON.stringify(enabled.checked),
            });
        }
        loadRouting();
    } catch (e) {
        alert(e.message);
    }
}

// ---------- Backup ----------

async function exportBackup() {
    try {
        const result = await api('/api/backup/export', { method: 'POST', body: JSON.stringify({}) });
        alert(`Backup written to:\n${result.path}\n(${Math.round(result.size_bytes / 1024)} KB)`);
        loadBackups();
    } catch (e) {
        alert(e.message);
    }
}

async function loadBackups() {
    const list = document.getElementById('backup-list');
    if (!list) return;
    try {
        const data = await api('/api/backup');
        if (!data.backups.length) {
            list.innerHTML = '<div class="empty">No backups yet.</div>';
            return;
        }
        list.innerHTML = '';
        for (const backup of data.backups) {
            const row = document.createElement('div');
            row.className = 'backup-row';
            row.innerHTML = `
                <div><span class="mono">${escHtml(backup.name)}</span>
                     <span class="subtle">${escHtml(backup.created_at.slice(0, 16))} ·
                     ${Math.round(backup.size_bytes / 1024)} KB</span></div>
                <button class="btn ghost tiny">Restore</button>`;
            row.querySelector('button').onclick = () => importBackup(backup.path);
            list.appendChild(row);
        }
    } catch (_) {}
}

async function importBackup(path) {
    if (!confirm('Restoring replaces everything currently in Carrot.\n\n'
        + 'A safety copy of the current state is taken first. Continue?')) return;
    try {
        const result = await api('/api/backup/import', {
            method: 'POST', body: JSON.stringify({ path, safety_copy: true }),
        });
        alert('Restored. Reloading.\nSafety copy: ' + (result.safety_copy || 'none'));
        location.reload();
    } catch (e) {
        alert(e.message);
    }
}

// ---------- Startup ----------

document.addEventListener('DOMContentLoaded', () => {
    refreshNotifications();
    startNotificationStream();
    setInterval(refreshNotifications, 60000);
});
