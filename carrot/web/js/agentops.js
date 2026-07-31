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

function showApprovalPrompt(request) {
    const card = document.createElement('div');
    card.className = 'approval-card risk-' + escHtml(request.risk || 'low');
    card.id = 'approval-' + request.id;
    card.innerHTML = `
        <div class="approval-head">Carrot wants to run a tool</div>
        <div class="approval-summary">${escHtml(request.summary)}</div>
        <div class="approval-tool">${escHtml(request.tool)} · ${escHtml(request.risk)} risk</div>
        <label class="approval-remember">
            <input type="checkbox" id="approval-remember-${escHtml(request.id)}">
            Don't ask again for this tool this session
        </label>
        <div class="approval-actions">
            <button class="btn ghost" data-decision="deny">Deny</button>
            <button class="btn primary" data-decision="allow">Allow</button>
        </div>`;

    card.querySelectorAll('button[data-decision]').forEach(button => {
        button.onclick = () => {
            const remember = document.getElementById('approval-remember-' + request.id);
            resolveApproval(request.id, button.dataset.decision, remember && remember.checked);
        };
    });
    approvalHost().appendChild(card);
}

function dismissApprovalPrompt(approvalId) {
    const card = document.getElementById('approval-' + approvalId);
    if (card) card.remove();
}

async function resolveApproval(approvalId, decision, remember) {
    dismissApprovalPrompt(approvalId);
    try {
        await api(`/api/agent/approvals/${approvalId}`, {
            method: 'POST',
            body: JSON.stringify({ decision, remember: !!remember }),
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

function startNotificationStream() {
    if (notificationStream) return;
    try {
        notificationStream = new EventSource(tokenUrl('/api/notifications/stream'));
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

let memoryFilter = { kind: '', status: 'active', subject: '' };

async function loadMemory() {
    const list = document.getElementById('memory-list');
    if (!list) return;
    list.innerHTML = '<div class="empty">Loading…</div>';
    try {
        const params = new URLSearchParams();
        if (memoryFilter.kind) params.set('kind', memoryFilter.kind);
        if (memoryFilter.status) params.set('status', memoryFilter.status);
        if (memoryFilter.subject) params.set('subject', memoryFilter.subject);

        const data = await api('/api/memory?' + params.toString());
        renderMemoryStats(data.stats);
        renderMemoryList(data.memories);
    } catch (e) {
        list.innerHTML = `<div class="empty error">${escHtml(e.message)}</div>`;
    }
}

function renderMemoryStats(stats) {
    const el = document.getElementById('memory-stats');
    if (!el || !stats) return;
    const kinds = Object.entries(stats.by_kind || {})
        .map(([kind, count]) => `${escHtml(kind)} ${count}`).join(' · ');
    el.textContent = `${stats.total} remembered${kinds ? ' — ' + kinds : ''}`;
}

function renderMemoryList(memories) {
    const list = document.getElementById('memory-list');
    if (!list) return;
    if (!memories.length) {
        list.innerHTML = '<div class="empty">Nothing remembered yet. Carrot learns as you chat.</div>';
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
        const data = await api('/api/memory/search?q=' + encodeURIComponent(input.value.trim()));
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

// ---------- Model routing ----------

async function loadRouting() {
    const host = document.getElementById('routing-panel');
    if (!host) return;
    try {
        const status = await api('/api/router/status');
        renderRouting(status);
    } catch (e) {
        host.innerHTML = `<div class="empty error">${escHtml(e.message)}</div>`;
    }
}

function renderRouting(status) {
    const host = document.getElementById('routing-panel');
    if (!host) return;

    const rows = Object.entries(status.routes).map(([task, route]) => `
        <tr>
            <td>${escHtml(task)}</td>
            <td class="mono">${escHtml(route.model)}</td>
            <td><span class="tag ${route.provider === 'anthropic' ? 'cloud' : ''}">
                ${route.provider === 'anthropic' ? 'cloud' : 'on-device'}</span></td>
            <td class="subtle">${escHtml(route.reason)}</td>
        </tr>`).join('');

    const recommendation = status.recommendation || {};
    host.innerHTML = `
        <div class="routing-summary">
            ${status.cloud_enabled
                ? `<span class="dot warn"></span> Cloud escalation is on for: ${escHtml(status.cloud_tasks.join(', ') || 'nothing')}`
                : '<span class="dot ok"></span> Everything runs on this machine'}
        </div>
        ${recommendation.model
            ? `<div class="subtle">Suggested local model: <span class="mono">${escHtml(recommendation.model)}</span>
               — ${escHtml(recommendation.reason)}</div>` : ''}
        <table class="routing-table">
            <thead><tr><th>Task</th><th>Model</th><th>Where</th><th>Why</th></tr></thead>
            <tbody>${rows}</tbody>
        </table>
        ${status.cloud_configured && !status.sdk_installed
            ? `<div class="empty error">The anthropic package is not installed —
               run <span class="mono">pip install 'carrot[cloud]'</span> to enable cloud routing.</div>` : ''}`;
}

async function saveCloudSettings() {
    const key = document.getElementById('cloud-key');
    const enabled = document.getElementById('cloud-enabled');
    try {
        if (key && key.value.trim()) {
            await api('/api/config/cloud_api_key', {
                method: 'PUT', body: JSON.stringify(key.value.trim()),
            });
            key.value = '';
        }
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
