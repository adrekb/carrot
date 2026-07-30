// ===== Carrot AI — editors & extensions (Notes/Code/Extensions) =====
// Loaded before app.js so its function definitions (loadNotes, loadCodeTab,
// loadExtensions, mdToHtml, ...) are available to the tab loader map.

// ===== Markdown rendering =====
// Renders markdown via the vendored `marked`, then sanitizes: drop dangerous
// tags/attributes and force links to open safely in a new tab.
function mdToHtml(md) {
    if (md == null) return '';
    if (!window.marked) {
        const div = document.createElement('div');
        div.textContent = String(md);
        return div.innerHTML.replace(/\n/g, '<br>');
    }
    let html;
    try {
        html = window.marked.parse(String(md), { breaks: true, gfm: true });
    } catch (_) {
        const div = document.createElement('div');
        div.textContent = String(md);
        return div.innerHTML;
    }
    const tpl = document.createElement('template');
    tpl.innerHTML = html;
    tpl.content.querySelectorAll('script, style, iframe, object, embed, link, meta').forEach(n => n.remove());
    tpl.content.querySelectorAll('*').forEach(el => {
        [...el.attributes].forEach(attr => {
            const name = attr.name.toLowerCase();
            const val = (attr.value || '').trim().toLowerCase();
            if (name.startsWith('on')) el.removeAttribute(attr.name);
            if ((name === 'href' || name === 'src') && (val.startsWith('javascript:') || val.startsWith('data:text/html'))) {
                el.removeAttribute(attr.name);
            }
        });
        if (el.tagName === 'A') {
            el.setAttribute('target', '_blank');
            el.setAttribute('rel', 'noopener noreferrer');
        }
    });
    return tpl.innerHTML;
}

// ===== Vendor lazy loaders (offline bundles) =====
const _vendorLoaded = {};
function _loadScript(src) {
    return new Promise((resolve, reject) => {
        const s = document.createElement('script');
        s.src = src;
        s.onload = resolve;
        s.onerror = () => reject(new Error('failed to load ' + src));
        document.head.appendChild(s);
    });
}
function _loadCss(href) {
    const link = document.createElement('link');
    link.rel = 'stylesheet';
    link.href = href;
    document.head.appendChild(link);
}

async function ensureMonaco() {
    if (window.monaco) return window.monaco;
    if (!_vendorLoaded.monaco) {
        _loadCss('/vendor/monaco.css');
        _vendorLoaded.monaco = _loadScript('/vendor/monaco.js');
    }
    await _vendorLoaded.monaco;
    return window.monaco;
}

async function ensureMilkdown() {
    if (window.CarrotCrepe) return window.CarrotCrepe;
    if (!_vendorLoaded.milkdown) {
        _loadCss('/vendor/milkdown.css');
        _vendorLoaded.milkdown = _loadScript('/vendor/milkdown.js');
    }
    await _vendorLoaded.milkdown;
    return window.CarrotCrepe;
}

// ================================================================
// Notes — two-pane Milkdown (Crepe) editor
// ================================================================
let notesCache = [];
let currentNoteId = null;
let crepeInstance = null;
let crepeReady = false;
let noteSaveTimer = null;
let lastSavedBody = '';
let noteAutosaveInterval = null;

async function loadNotes() {
    try {
        notesCache = await api('/api/notes');
    } catch (_) {
        notesCache = [];
    }
    renderNotesList();
    if (!noteAutosaveInterval) {
        noteAutosaveInterval = setInterval(noteAutosaveTick, 1500);
    }
}

function renderNotesList() {
    const filter = (document.getElementById('notes-filter').value || '').toLowerCase();
    const container = document.getElementById('notes-list');
    container.innerHTML = '';
    const items = notesCache.filter(n =>
        !filter || (n.title || '').toLowerCase().includes(filter) || (n.body || '').toLowerCase().includes(filter));
    if (!items.length) {
        container.innerHTML = '<div class="empty" style="padding:10px">No notes.</div>';
        return;
    }
    for (const n of items) {
        const div = document.createElement('div');
        div.className = 'side-item' + (n.id === currentNoteId ? ' active' : '');
        const preview = (n.body || '').replace(/[#*`>\-]/g, '').trim().slice(0, 48);
        div.innerHTML = `<div class="si-title">${escHtml(n.title || n.id)}</div><div class="si-sub">${escHtml(preview)}</div>`;
        div.onclick = () => openNote(n.id);
        container.appendChild(div);
    }
}

function filterNotesList() { renderNotesList(); }

async function newNote() {
    const created = await api('/api/notes', {
        method: 'POST',
        body: JSON.stringify({ title: 'Untitled note', content: '' }),
    });
    await loadNotes();
    openNote(created.id);
}

async function openNote(noteId) {
    const note = await api(`/api/notes/${noteId}`);
    currentNoteId = noteId;
    lastSavedBody = note.body || '';
    document.getElementById('note-empty').classList.add('hidden');
    document.getElementById('note-title').value = note.title || '';
    renderNotesList();
    await mountEditor(note.body || '');
    updateWordCount(note.body || '');
    setNoteStatus('');
}

async function mountEditor(markdown) {
    const host = document.getElementById('note-editor-host');
    const fallback = document.getElementById('note-fallback');
    let Crepe = null;
    try { Crepe = await ensureMilkdown(); } catch (_) { Crepe = null; }

    if (crepeInstance) {
        try { crepeInstance.destroy(); } catch (_) {}
        crepeInstance = null;
        crepeReady = false;
    }
    host.innerHTML = '';

    if (!Crepe) {
        // Degrade to a plain textarea when the vendor bundle is missing.
        host.classList.add('hidden');
        fallback.classList.remove('hidden');
        fallback.value = markdown;
        return;
    }
    fallback.classList.add('hidden');
    host.classList.remove('hidden');
    try {
        crepeInstance = new Crepe({ root: host, defaultValue: markdown });
        try {
            crepeInstance.on((listener) => {
                listener.markdownUpdated(() => scheduleNoteSave());
            });
        } catch (_) { /* older Crepe: rely on autosave poll */ }
        await crepeInstance.create();
        crepeReady = true;
    } catch (e) {
        crepeInstance = null;
        crepeReady = false;
        host.classList.add('hidden');
        fallback.classList.remove('hidden');
        fallback.value = markdown;
    }
}

function getEditorMarkdown() {
    if (crepeInstance && crepeReady) {
        try { return crepeInstance.getMarkdown(); } catch (_) { return lastSavedBody; }
    }
    return document.getElementById('note-fallback').value;
}

function scheduleNoteSave() {
    if (!currentNoteId) return;
    setNoteStatus('editing…');
    clearTimeout(noteSaveTimer);
    noteSaveTimer = setTimeout(saveNoteNow, 800);
}

function noteAutosaveTick() {
    if (!currentNoteId) return;
    const body = getEditorMarkdown();
    if (body !== lastSavedBody) scheduleNoteSave();
}

async function saveNoteNow() {
    if (!currentNoteId) return;
    const body = getEditorMarkdown();
    const title = document.getElementById('note-title').value.trim() || 'Untitled note';
    try {
        await api(`/api/notes/${currentNoteId}`, {
            method: 'PUT',
            body: JSON.stringify({ content: body, title }),
        });
        lastSavedBody = body;
        setNoteStatus('saved');
        updateWordCount(body);
        // Refresh the list title/preview without stealing editor focus.
        const n = notesCache.find(x => x.id === currentNoteId);
        if (n) { n.title = title; n.body = body; renderNotesList(); }
    } catch (e) {
        setNoteStatus('save failed');
    }
}

function updateWordCount(text) {
    const words = (text.trim().match(/\S+/g) || []).length;
    document.getElementById('note-words').textContent = words + (words === 1 ? ' word' : ' words');
}

function setNoteStatus(msg) {
    document.getElementById('note-status').textContent = msg;
}

async function deleteCurrentNote() {
    if (!currentNoteId) return;
    if (!confirm('Delete this note?')) return;
    await api(`/api/notes/${currentNoteId}`, { method: 'DELETE' });
    currentNoteId = null;
    if (crepeInstance) { try { crepeInstance.destroy(); } catch (_) {} crepeInstance = null; crepeReady = false; }
    document.getElementById('note-editor-host').innerHTML = '';
    document.getElementById('note-editor-host').classList.add('hidden');
    document.getElementById('note-fallback').classList.add('hidden');
    document.getElementById('note-title').value = '';
    document.getElementById('note-words').textContent = '';
    document.getElementById('note-empty').classList.remove('hidden');
    await loadNotes();
}

// ================================================================
// Code — Monaco editor with a sandboxed file tree
// ================================================================
let monacoEditor = null;
const openFiles = {};      // path -> monaco model
let activeFilePath = null;
let codeRoot = '';

const LANG_BY_EXT = {
    js: 'javascript', jsx: 'javascript', mjs: 'javascript', ts: 'typescript', tsx: 'typescript',
    py: 'python', json: 'json', html: 'html', htm: 'html', css: 'css', scss: 'scss', less: 'less',
    md: 'markdown', markdown: 'markdown', sh: 'shell', bat: 'bat', ps1: 'powershell',
    yml: 'yaml', yaml: 'yaml', xml: 'xml', sql: 'sql', java: 'java', c: 'c', cpp: 'cpp', h: 'cpp',
    cs: 'csharp', go: 'go', rs: 'rust', rb: 'ruby', php: 'php', toml: 'ini', ini: 'ini', txt: 'plaintext',
};
function langForPath(path) {
    const ext = (path.split('.').pop() || '').toLowerCase();
    return LANG_BY_EXT[ext] || 'plaintext';
}

async function loadCodeTab() {
    try {
        const r = await api('/api/files/root');
        codeRoot = r.root;
        document.getElementById('code-root-label').textContent = r.root.split(/[\\/]/).pop() || r.root;
        document.getElementById('code-root-label').title = r.root;
    } catch (_) {}
    loadCodeTree();
}

async function loadCodeTree() {
    const container = document.getElementById('code-tree');
    container.innerHTML = '<div class="empty" style="padding:10px">Loading…</div>';
    try {
        const data = await api('/api/files/tree?path=');
        container.innerHTML = '';
        renderTreeEntries(container, data.entries, 0);
        if (!data.entries.length) {
            container.innerHTML = '<div class="empty" style="padding:10px">Empty workspace. Change the folder with the gear icon.</div>';
        }
    } catch (e) {
        container.innerHTML = `<div class="empty" style="padding:10px">${escHtml(e.message)}</div>`;
    }
}

function renderTreeEntries(parent, entries, depth) {
    for (const entry of entries) {
        const row = document.createElement('div');
        row.className = 'tree-row' + (entry.is_dir ? ' dir' : ' file');
        row.style.paddingLeft = (8 + depth * 14) + 'px';
        row.innerHTML = `<span class="tree-caret">${entry.is_dir ? '▸' : ''}</span><span class="tree-name">${escHtml(entry.name)}</span>`;
        parent.appendChild(row);
        if (entry.is_dir) {
            const childBox = document.createElement('div');
            childBox.className = 'tree-children hidden';
            parent.appendChild(childBox);
            let loaded = false;
            row.onclick = async () => {
                const open = childBox.classList.toggle('hidden');
                row.querySelector('.tree-caret').textContent = open ? '▸' : '▾';
                if (!loaded && !open) {
                    loaded = true;
                    try {
                        const data = await api(`/api/files/tree?path=${encodeURIComponent(entry.path)}`);
                        renderTreeEntries(childBox, data.entries, depth + 1);
                    } catch (e) {
                        childBox.innerHTML = `<div class="empty" style="padding:6px">${escHtml(e.message)}</div>`;
                    }
                }
            };
        } else {
            row.onclick = () => openFile(entry.path);
        }
    }
}

async function openFile(path) {
    const monaco = await ensureMonaco();
    ensureMonacoEditor(monaco);
    let model = openFiles[path];
    if (!model) {
        let content = '';
        try {
            const data = await api(`/api/files/read?path=${encodeURIComponent(path)}`);
            content = data.content;
        } catch (e) {
            alert('Could not open: ' + e.message);
            return;
        }
        model = monaco.editor.createModel(content, langForPath(path));
        openFiles[path] = model;
    }
    activeFilePath = path;
    monacoEditor.setModel(model);
    document.getElementById('code-empty').classList.add('hidden');
    document.getElementById('code-editor-host').classList.remove('hidden');
    renderCodeTabs();
    setCodeStatus('');
}

function ensureMonacoEditor(monaco) {
    if (monacoEditor) return;
    monaco.editor.defineTheme('carrot-dark', {
        base: 'vs-dark',
        inherit: true,
        rules: [],
        colors: {
            'editor.background': '#1a1712',
            'editor.foreground': '#e8e2d6',
            'editorLineNumber.foreground': '#5c5344',
            'editorCursor.foreground': '#e8912b',
            'editor.selectionBackground': '#3a3122',
        },
    });
    monacoEditor = monaco.editor.create(document.getElementById('code-editor-host'), {
        model: null,
        theme: 'carrot-dark',
        automaticLayout: true,
        fontSize: 13,
        minimap: { enabled: false },
        scrollBeyondLastLine: false,
    });
    monacoEditor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, saveCurrentFile);
}

function renderCodeTabs() {
    const bar = document.getElementById('code-tabs');
    bar.innerHTML = '';
    for (const path of Object.keys(openFiles)) {
        const tab = document.createElement('div');
        tab.className = 'code-tab' + (path === activeFilePath ? ' active' : '');
        const name = path.split('/').pop();
        tab.innerHTML = `<span class="ct-name">${escHtml(name)}</span><span class="ct-close">×</span>`;
        tab.querySelector('.ct-name').onclick = () => openFile(path);
        tab.querySelector('.ct-close').onclick = (e) => { e.stopPropagation(); closeFile(path); };
        bar.appendChild(tab);
    }
}

function closeFile(path) {
    const model = openFiles[path];
    if (model) { try { model.dispose(); } catch (_) {} delete openFiles[path]; }
    if (activeFilePath === path) {
        const remaining = Object.keys(openFiles);
        activeFilePath = remaining[0] || null;
        if (activeFilePath) {
            monacoEditor.setModel(openFiles[activeFilePath]);
        } else if (monacoEditor) {
            monacoEditor.setModel(null);
            document.getElementById('code-empty').classList.remove('hidden');
        }
    }
    renderCodeTabs();
}

async function saveCurrentFile() {
    if (!activeFilePath || !monacoEditor) return;
    const content = monacoEditor.getValue();
    try {
        await api('/api/files/write', {
            method: 'POST',
            body: JSON.stringify({ path: activeFilePath, content }),
        });
        setCodeStatus('saved ' + activeFilePath.split('/').pop());
    } catch (e) {
        setCodeStatus('save failed: ' + e.message);
    }
}

async function openInVSCode() {
    try {
        const r = await api('/api/files/open-vscode', {
            method: 'POST',
            body: JSON.stringify({ path: activeFilePath || '' }),
        });
        setCodeStatus('opened in VS Code');
    } catch (e) {
        alert(e.message);
    }
}

async function changeCodeRoot() {
    const folder = prompt('Workspace folder (absolute path):', codeRoot);
    if (!folder) return;
    try {
        const r = await api('/api/files/root', { method: 'POST', body: JSON.stringify({ root: folder }) });
        codeRoot = r.root;
        document.getElementById('code-root-label').textContent = r.root.split(/[\\/]/).pop() || r.root;
        document.getElementById('code-root-label').title = r.root;
        loadCodeTree();
    } catch (e) {
        alert('Could not set folder: ' + e.message);
    }
}

function setCodeStatus(msg) {
    document.getElementById('code-status').textContent = msg;
}

// ================================================================
// Extensions — Skills + MCP servers
// ================================================================
let editingSkillSlug = null;

async function loadExtensions() {
    loadSkillsList();
    loadMcpList();
}

async function loadSkillsList() {
    const container = document.getElementById('skills-list');
    try {
        const skills = await api('/api/skills');
        container.innerHTML = '';
        if (!skills.length) {
            container.innerHTML = '<div class="empty">No skills yet. Create one to customize the chat agent.</div>';
        }
        for (const s of skills) {
            const div = document.createElement('div');
            div.className = 'list-item';
            div.innerHTML = `<div class="goal-head"><strong>${escHtml(s.name)}</strong><span class="tag">/${escHtml(s.slug)}</span></div>` +
                (s.description ? `<div class="body">${escHtml(s.description)}</div>` : '');
            const row = document.createElement('div');
            row.className = 'row';
            const edit = document.createElement('button');
            edit.className = 'btn btn-ghost';
            edit.textContent = 'Edit';
            edit.onclick = () => editSkill(s.slug);
            const del = document.createElement('button');
            del.className = 'btn btn-ghost';
            del.textContent = 'Delete';
            del.onclick = () => deleteSkill(s.slug);
            row.appendChild(edit);
            row.appendChild(del);
            div.appendChild(row);
            container.appendChild(div);
        }
    } catch (e) {
        container.innerHTML = `<div class="empty">${escHtml(e.message)}</div>`;
    }
}

function newSkill() {
    editingSkillSlug = null;
    document.getElementById('skill-name').value = '';
    document.getElementById('skill-desc').value = '';
    document.getElementById('skill-instructions').value = '';
    document.getElementById('skill-editor').classList.remove('hidden');
    document.getElementById('skill-name').focus();
}

async function editSkill(slug) {
    const s = await api(`/api/skills/${slug}`);
    editingSkillSlug = slug;
    document.getElementById('skill-name').value = s.name;
    document.getElementById('skill-desc').value = s.description || '';
    document.getElementById('skill-instructions').value = s.instructions || '';
    document.getElementById('skill-editor').classList.remove('hidden');
}

function closeSkillEditor() {
    document.getElementById('skill-editor').classList.add('hidden');
}

async function saveSkill() {
    const name = document.getElementById('skill-name').value.trim();
    const description = document.getElementById('skill-desc').value.trim();
    const instructions = document.getElementById('skill-instructions').value;
    if (!name) { alert('Skill name is required.'); return; }
    try {
        await api('/api/skills', {
            method: 'POST',
            body: JSON.stringify({ name, description, instructions, slug: editingSkillSlug }),
        });
        closeSkillEditor();
        loadSkillsList();
        if (typeof loadSkillCatalog === 'function') loadSkillCatalog();
    } catch (e) {
        alert('Could not save skill: ' + e.message);
    }
}

async function deleteSkill(slug) {
    if (!confirm('Delete skill /' + slug + '?')) return;
    await api(`/api/skills/${slug}`, { method: 'DELETE' });
    loadSkillsList();
    if (typeof loadSkillCatalog === 'function') loadSkillCatalog();
}

async function loadMcpList() {
    const container = document.getElementById('mcp-list');
    try {
        const data = await api('/api/mcp/servers');
        const servers = data.servers || {};
        const names = Object.keys(servers);
        container.innerHTML = '';
        if (!names.length) {
            container.innerHTML = '<div class="empty">No MCP servers configured.</div>';
            return;
        }
        for (const name of names) {
            const spec = servers[name];
            const div = document.createElement('div');
            div.className = 'list-item';
            div.id = 'mcp-' + name;
            const cmd = escHtml(spec.command + ' ' + (spec.args || []).join(' '));
            div.innerHTML = `<div class="goal-head"><strong>${escHtml(name)}</strong>` +
                `<span class="tag ${spec.enabled ? 'hot' : ''}">${spec.enabled ? 'enabled' : 'disabled'}</span></div>` +
                `<div class="sub mono">${cmd}</div><div class="mcp-tools sub">tools: —</div>`;
            const row = document.createElement('div');
            row.className = 'row';
            const toggle = document.createElement('button');
            toggle.className = 'btn btn-ghost';
            toggle.textContent = spec.enabled ? 'Disable' : 'Enable';
            toggle.onclick = () => toggleMcpServer(name, !spec.enabled);
            const del = document.createElement('button');
            del.className = 'btn btn-ghost';
            del.textContent = 'Delete';
            del.onclick = () => deleteMcpServer(name);
            row.appendChild(toggle);
            row.appendChild(del);
            div.appendChild(row);
            container.appendChild(div);
        }
    } catch (e) {
        container.innerHTML = `<div class="empty">${escHtml(e.message)}</div>`;
    }
}

async function addMcpServer() {
    const name = document.getElementById('mcp-name').value.trim();
    const command = document.getElementById('mcp-command').value.trim();
    const argsRaw = document.getElementById('mcp-args').value.trim();
    if (!name || !command) { alert('Name and command are required.'); return; }
    const args = argsRaw ? argsRaw.split(/\s+/) : [];
    try {
        await api('/api/mcp/servers', {
            method: 'POST',
            body: JSON.stringify({ name, command, args, enabled: true }),
        });
        document.getElementById('mcp-name').value = '';
        document.getElementById('mcp-command').value = '';
        document.getElementById('mcp-args').value = '';
        loadMcpList();
    } catch (e) {
        alert('Could not add server: ' + e.message);
    }
}

async function toggleMcpServer(name, enabled) {
    await api(`/api/mcp/servers/${encodeURIComponent(name)}/enable`, {
        method: 'POST',
        body: JSON.stringify({ enabled }),
    });
    loadMcpList();
}

async function deleteMcpServer(name) {
    if (!confirm('Delete MCP server "' + name + '"?')) return;
    await api(`/api/mcp/servers/${encodeURIComponent(name)}`, { method: 'DELETE' });
    loadMcpList();
}

async function refreshMcpTools() {
    const container = document.getElementById('mcp-list');
    const marks = container.querySelectorAll('.mcp-tools');
    marks.forEach(m => m.textContent = 'tools: discovering…');
    try {
        const data = await api('/api/mcp/tools');
        for (const name of Object.keys(data)) {
            const el = document.getElementById('mcp-' + name);
            if (!el) continue;
            const mark = el.querySelector('.mcp-tools');
            const entry = data[name];
            if (entry.error) {
                mark.textContent = 'error: ' + entry.error;
            } else {
                const toolNames = (entry.tools || []).map(t => t.name).join(', ');
                mark.textContent = 'tools: ' + (toolNames || 'none');
            }
        }
    } catch (e) {
        marks.forEach(m => m.textContent = 'tools: ' + e.message);
    }
}
