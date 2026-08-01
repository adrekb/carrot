// ===== Workspaces, folders, and the help section =====
// Loaded after app.js, so `api`, `escHtml`, `mdToHtml` and `switchTab` exist.
//
// The switcher in the header and the Workspaces tab are two views of one piece
// of state, so both read `workspaceTree` and both call `loadWorkspaces()` after
// a change rather than patching their own copy.

let workspaceTree = { folders: [], workspaces: [], active: '' };
let selectedWorkspaceId = null;

// ---------- Loading ----------

async function loadWorkspaces() {
    try {
        workspaceTree = await api('/api/workspaces');
    } catch (e) {
        console.warn('workspaces failed', e);
        return;
    }
    renderWorkspaceSwitcher();
    renderWorkspaceTree();
    renderFolderSelects();
    if (selectedWorkspaceId) openWorkspace(selectedWorkspaceId);
}

/** Every workspace in the tree, flattened, with its folder path for labels. */
function flatWorkspaces() {
    const out = workspaceTree.workspaces.map(w => ({ ...w, path: '' }));
    const walk = (folders, prefix) => {
        for (const folder of folders) {
            const path = prefix ? `${prefix} / ${folder.name}` : folder.name;
            (folder.workspaces || []).forEach(w => out.push({ ...w, path }));
            walk(folder.children || [], path);
        }
    };
    walk(workspaceTree.folders, '');
    return out;
}

function flatFolders() {
    const out = [];
    const walk = (folders, depth) => {
        for (const folder of folders) {
            out.push({ ...folder, depth });
            walk(folder.children || [], depth + 1);
        }
    };
    walk(workspaceTree.folders, 0);
    return out;
}

// ---------- The header switcher ----------

function renderWorkspaceSwitcher() {
    const all = flatWorkspaces();
    const active = all.find(w => w.id === workspaceTree.active);

    const label = document.getElementById('ws-label');
    if (label) label.textContent = active ? active.name : 'All workspaces';
    const button = document.getElementById('ws-btn');
    if (button) button.classList.toggle('ws-scoped', !!active);

    const list = document.getElementById('ws-pop-list');
    if (!list) return;
    const rows = [`
        <button class="pop-item${workspaceTree.active ? '' : ' active'}" onclick="setActiveWorkspace('')">
            <span class="pop-item-name">All workspaces</span>
            <span class="pop-item-sub">Nothing is scoped — search sees everything.</span>
        </button>`];
    for (const workspace of all) {
        const counts = workspace.counts || {};
        const total = Object.values(counts).reduce((a, b) => a + b, 0);
        rows.push(`
            <button class="pop-item${workspace.id === workspaceTree.active ? ' active' : ''}"
                    onclick="setActiveWorkspace('${escHtml(workspace.id)}')">
                <span class="pop-item-name">${escHtml(workspace.name)}</span>
                <span class="pop-item-sub">
                    ${workspace.path ? escHtml(workspace.path) + ' · ' : ''}${total} item${total === 1 ? '' : 's'}
                </span>
            </button>`);
    }
    if (!all.length) {
        rows.push('<div class="empty small" style="padding:8px 10px">No workspaces yet.</div>');
    }
    list.innerHTML = rows.join('');
}

function toggleWorkspacePop() {
    const pop = document.getElementById('ws-pop');
    if (pop) pop.classList.toggle('hidden');
}

async function setActiveWorkspace(workspaceId) {
    try {
        await api('/api/workspaces/active', {
            method: 'POST', body: JSON.stringify({ workspace_id: workspaceId }),
        });
    } catch (e) {
        alert(e.message);
        return;
    }
    document.getElementById('ws-pop').classList.add('hidden');
    await loadWorkspaces();
    // Anything on screen that was scoped is now showing the wrong scope.
    if (typeof currentTab !== 'undefined' && ['chats', 'search', 'memory', 'files'].includes(currentTab)) {
        switchTab(currentTab);
    }
}

// ---------- The tree ----------

function renderWorkspaceTree() {
    const host = document.getElementById('ws-tree');
    if (!host) return;

    const workspaceRow = (workspace) => {
        const counts = workspace.counts || {};
        const total = Object.values(counts).reduce((a, b) => a + b, 0);
        return `
            <div class="ws-row${workspace.id === workspaceTree.active ? ' active' : ''}">
                <span class="ws-row-main" onclick="openWorkspace('${escHtml(workspace.id)}')">
                    <span class="ws-row-name">${escHtml(workspace.name)}</span>
                    <span class="ws-row-meta">${total} item${total === 1 ? '' : 's'}</span>
                </span>
                <button class="btn btn-ghost small" onclick="setActiveWorkspace('${escHtml(workspace.id)}')"
                        title="Work in this workspace">Use</button>
                <button class="btn btn-ghost small" onclick="deleteWorkspace('${escHtml(workspace.id)}')"
                        title="Delete the grouping; its contents are unfiled, not deleted">×</button>
            </div>`;
    };

    const folderNode = (folder, depth) => `
        <div class="ws-folder" style="margin-left:${depth * 14}px">
            <div class="ws-folder-head">
                <svg class="ico"><use href="#i-folder"/></svg>
                <span class="ws-folder-name">${escHtml(folder.name)}</span>
                <button class="btn btn-ghost small" onclick="renameFolder('${escHtml(folder.id)}')">Rename</button>
                <button class="btn btn-ghost small" onclick="deleteFolder('${escHtml(folder.id)}')"
                        title="Delete the folder; its workspaces move to the top level">×</button>
            </div>
            ${(folder.workspaces || []).map(workspaceRow).join('')}
            ${(folder.children || []).map(child => folderNode(child, depth + 1)).join('')}
        </div>`;

    const parts = [
        ...workspaceTree.folders.map(folder => folderNode(folder, 0)),
        ...workspaceTree.workspaces.map(workspaceRow),
    ];
    host.innerHTML = parts.length
        ? parts.join('')
        : '<div class="empty">No workspaces yet. Add one above to start grouping a project.</div>';
}

function renderFolderSelects() {
    const options = ['<option value="">No folder</option>'].concat(
        flatFolders().map(folder =>
            `<option value="${escHtml(folder.id)}">${'— '.repeat(folder.depth)}${escHtml(folder.name)}</option>`)
    ).join('');
    for (const id of ['ws-new-folder', 'folder-new-parent']) {
        const select = document.getElementById(id);
        if (select) select.innerHTML = options;
    }
}

// ---------- Mutations ----------

async function createWorkspace() {
    const input = document.getElementById('ws-new-name');
    const name = input.value.trim();
    if (!name) return;
    try {
        await api('/api/workspaces', {
            method: 'POST',
            body: JSON.stringify({
                name, folder_id: document.getElementById('ws-new-folder').value || null,
            }),
        });
        input.value = '';
        loadWorkspaces();
    } catch (e) {
        alert(e.message);
    }
}

async function createFolder() {
    const input = document.getElementById('folder-new-name');
    const name = input.value.trim();
    if (!name) return;
    try {
        await api('/api/folders', {
            method: 'POST',
            body: JSON.stringify({
                name, parent_id: document.getElementById('folder-new-parent').value || null,
            }),
        });
        input.value = '';
        loadWorkspaces();
    } catch (e) {
        alert(e.message);
    }
}

async function renameFolder(folderId) {
    const name = prompt('Rename folder to:');
    if (!name) return;
    await api(`/api/folders/${folderId}`, { method: 'PATCH', body: JSON.stringify({ name }) });
    loadWorkspaces();
}

async function deleteFolder(folderId) {
    if (!confirm('Delete this folder? Its workspaces move to the top level and keep their contents.')) return;
    await api(`/api/folders/${folderId}`, { method: 'DELETE' });
    loadWorkspaces();
}

async function deleteWorkspace(workspaceId) {
    if (!confirm('Delete this workspace? Its chats, memories and files are unfiled, not deleted.')) return;
    await api(`/api/workspaces/${workspaceId}`, { method: 'DELETE' });
    if (selectedWorkspaceId === workspaceId) selectedWorkspaceId = null;
    loadWorkspaces();
}

// ---------- Contents ----------

async function openWorkspace(workspaceId) {
    selectedWorkspaceId = workspaceId;
    const host = document.getElementById('ws-detail');
    if (!host) return;
    let data;
    try {
        data = await api(`/api/workspaces/${workspaceId}/contents`);
    } catch (e) {
        host.innerHTML = `<div class="empty error">${escHtml(e.message)}</div>`;
        return;
    }

    const workspace = data.workspace;
    const sections = Object.entries(data.items)
        .filter(([, items]) => items.length)
        .map(([kind, items]) => `
            <h4 class="pack-section">${escHtml(data.labels[kind] || kind)}</h4>
            ${items.map(item => `
                <div class="ws-item">
                    <span class="ws-item-label">${escHtml(item.label)}</span>
                    <button class="btn btn-ghost small"
                            onclick="unfileItem('${escHtml(kind)}', '${escHtml(item.item_id)}')"
                            title="Remove from this workspace; the item itself is kept">Unfile</button>
                </div>`).join('')}`)
        .join('');

    host.innerHTML = `
        <div class="ws-detail-head">
            <h3>${escHtml(workspace.name)}</h3>
            <div class="muted small">${escHtml(workspace.description || 'No description.')}</div>
        </div>
        ${sections || '<div class="empty">Nothing filed here yet. Make this workspace active and new chats, notes and runs will land here.</div>'}`;
}

async function unfileItem(kind, itemId) {
    await api(`/api/workspaces/items/${kind}/${encodeURIComponent(itemId)}`, { method: 'DELETE' });
    loadWorkspaces();
}

// ===== Help =====

let helpTopics = [];

async function loadHelp() {
    try {
        const body = await api('/api/help');
        helpTopics = body.topics || [];
        renderHelpList(helpTopics);
        if (helpTopics.length) showHelpTopic(helpTopics[0].id);
    } catch (e) {
        console.warn('help failed', e);
    }
    loadTutorial();
}

function renderHelpList(topics) {
    const host = document.getElementById('help-list');
    if (!host) return;
    if (!topics.length) {
        host.innerHTML = '<div class="empty">Nothing matches that.</div>';
        return;
    }
    // Grouped by section, in the order the server returned them, so a search
    // result list stays ranked rather than being re-sorted into sections.
    const seen = new Set();
    host.innerHTML = topics.map(topic => {
        const header = seen.has(topic.section)
            ? ''
            : `<div class="pop-section">${escHtml(topic.section)}</div>`;
        seen.add(topic.section);
        return header + `
            <button class="help-item" onclick="showHelpTopic('${escHtml(topic.id)}')">
                <span class="help-item-title">${escHtml(topic.title)}</span>
                <span class="help-item-sub">${escHtml(topic.summary)}</span>
            </button>`;
    }).join('');
}

async function showHelpTopic(topicId) {
    const host = document.getElementById('help-body');
    if (!host) return;
    const cached = helpTopics.find(t => t.id === topicId);
    const topic = cached || await api(`/api/help/${topicId}`).catch(() => null);
    if (!topic) return;
    host.innerHTML = `<h2>${escHtml(topic.title)}</h2>` + mdToHtml(topic.body);
    document.querySelectorAll('.help-item').forEach(el => el.classList.remove('active'));
}

let helpSearchTimer = null;

function searchHelp() {
    clearTimeout(helpSearchTimer);
    helpSearchTimer = setTimeout(async () => {
        const query = document.getElementById('help-search').value.trim();
        try {
            const body = await api('/api/help?q=' + encodeURIComponent(query));
            renderHelpList(body.topics || []);
        } catch (_) { /* leave the list as it was */ }
    }, 200);
}

// ---------- Tutorial ----------

async function loadTutorial() {
    let data;
    try {
        data = await api('/api/help/tutorial');
    } catch (e) {
        return;
    }
    const count = document.getElementById('tutorial-count');
    if (count) count.textContent = `${data.completed} of ${data.total} done`;
    const bar = document.getElementById('tutorial-bar');
    if (bar) bar.style.width = `${Math.round((data.completed / data.total) * 100)}%`;

    const host = document.getElementById('tutorial-steps');
    if (!host) return;
    host.innerHTML = data.steps.map(step => {
        // Three states, not two: unknown means the check could not run, which
        // is different from "you have not done it".
        const state = step.done === true ? 'done' : (step.done === null ? 'unknown' : 'todo');
        const mark = step.done === true ? '✓' : (step.done === null ? '?' : '');
        return `
            <div class="tut-step ${state}${step.id === data.next ? ' next' : ''}">
                <span class="tut-mark">${mark}</span>
                <div class="tut-body">
                    <div class="tut-title">${escHtml(step.title)}</div>
                    <div class="tut-text">${escHtml(step.body)}</div>
                    <div class="tut-detail">${escHtml(step.detail)}</div>
                    <div class="tut-actions">
                        ${step.action ? `<button class="btn btn-ghost small"
                            onclick="switchTab('${escHtml(step.action.tab)}')">${escHtml(step.action.label)}</button>` : ''}
                        ${step.topic ? `<button class="btn btn-ghost small"
                            onclick="switchTab('help');showHelpTopic('${escHtml(step.topic)}')">Read more</button>` : ''}
                    </div>
                </div>
            </div>`;
    }).join('');
}
