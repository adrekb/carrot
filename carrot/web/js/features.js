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
    // Read the note's own @/to and @/file lines now rather than waiting for a
    // keystroke — opening a note you wrote last week should already show where
    // it goes, which is the whole point of writing the destination into it.
    if (typeof resetDocDestination === 'function') resetDocDestination();
    if (typeof refreshDocReferences === 'function') refreshDocReferences();
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
    // Label the open button for the editor actually installed.
    try {
        const ed = await api('/api/files/editors');
        const btn = document.getElementById('open-editor-btn');
        if (btn && (ed.editors || []).length) {
            btn.textContent = ed.editors[0] === 'cursor' ? 'Open in Cursor' : 'Open in VS Code';
        }
    } catch (_) {}
    wireTerminal();
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

// The directory the next "New file"/"New folder" lands in: whatever is
// selected in the tree, or the workspace root.
let selectedDir = '';

function renderTreeEntries(parent, entries, depth) {
    for (const entry of entries) {
        const row = document.createElement('div');
        row.className = 'tree-row' + (entry.is_dir ? ' dir' : ' file');
        row.style.paddingLeft = (8 + depth * 14) + 'px';
        row.dataset.path = entry.path;
        row.dataset.isDir = entry.is_dir ? '1' : '';
        row.draggable = true;
        row.innerHTML = `<span class="tree-caret">${entry.is_dir ? '▸' : ''}</span><span class="tree-name">${escHtml(entry.name)}</span>`;
        parent.appendChild(row);

        row.addEventListener('contextmenu', (e) => {
            e.preventDefault();
            e.stopPropagation();
            showTreeMenu(e.clientX, e.clientY, entry);
        });
        wireTreeDrag(row, entry);

        if (entry.is_dir) {
            const childBox = document.createElement('div');
            childBox.className = 'tree-children hidden';
            parent.appendChild(childBox);
            row.dataset.depth = depth;
            let loaded = false;
            row.onclick = async () => {
                selectTreeRow(row, entry);
                const nowHidden = childBox.classList.toggle('hidden');
                row.querySelector('.tree-caret').textContent = nowHidden ? '▸' : '▾';
                if (!loaded && !nowHidden) {
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
            row.onclick = () => { selectTreeRow(row, entry); openFile(entry.path); };
        }
    }
}

function selectTreeRow(row, entry) {
    document.querySelectorAll('#code-tree .tree-row.selected')
        .forEach(el => el.classList.remove('selected'));
    row.classList.add('selected');
    // New items go inside a selected folder, or beside a selected file.
    selectedDir = entry.is_dir ? entry.path
        : entry.path.split('/').slice(0, -1).join('/');
}

// ---------- create / rename / delete ----------

async function createEntry(isDir, parentPath) {
    const where = parentPath !== undefined ? parentPath : selectedDir;
    const name = (await inlineTextPrompt({
        title: isDir ? 'New folder' : 'New file',
        placeholder: isDir ? 'folder name' : 'name.ext',
        action: 'Create',
    })).trim();
    if (!name) return;
    try {
        const r = await api('/api/files/create', {
            method: 'POST',
            body: JSON.stringify({ path: where, name, is_dir: isDir }),
        });
        await loadCodeTree();
        if (!isDir) openFile(r.path);
        setCodeStatus(`created ${r.path}`);
        // Say up front if the language cannot run here. Discovering that
        // after writing a program is a bad order to learn it in.
        const tc = r.toolchain || {};
        if (tc.language && !tc.available) warnMissingToolchain(tc);
    } catch (e) {
        setCodeStatus('could not create: ' + e.message);
    }
}

async function renameEntry(entry) {
    const name = (await inlineTextPrompt({
        title: `Rename ${entry.name}`, value: entry.name,
        placeholder: 'new name', action: 'Rename',
    })).trim();
    if (!name || name === entry.name) return;
    try {
        const r = await api('/api/files/rename', {
            method: 'POST',
            body: JSON.stringify({ path: entry.path, new_name: name }),
        });
        // An open tab still points at the old path; move it with the file.
        if (openFiles[entry.path]) {
            openFiles[r.path] = openFiles[entry.path];
            delete openFiles[entry.path];
            if (activeFilePath === entry.path) activeFilePath = r.path;
            if (dirtyFiles.has(entry.path)) {
                dirtyFiles.delete(entry.path);
                dirtyFiles.add(r.path);
            }
            renderCodeTabs();
        }
        await loadCodeTree();
        setCodeStatus(`renamed to ${name}`);
    } catch (e) {
        setCodeStatus('could not rename: ' + e.message);
    }
}

async function deleteEntry(entry) {
    const what = entry.is_dir ? `folder "${entry.name}" and everything in it` : `"${entry.name}"`;
    if (!confirm(`Delete ${what}? This cannot be undone.`)) return;
    try {
        await api('/api/files/delete', {
            method: 'POST', body: JSON.stringify({ path: entry.path }),
        });
        // Close any tab whose file just went away, discarding its model.
        for (const open of Object.keys(openFiles)) {
            if (open === entry.path || open.startsWith(entry.path + '/')) closeFile(open, true);
        }
        await loadCodeTree();
        setCodeStatus(`deleted ${entry.name}`);
    } catch (e) {
        setCodeStatus('could not delete: ' + e.message);
    }
}

// ---------- drag to move ----------

let dragPath = null;

function wireTreeDrag(row, entry) {
    row.addEventListener('dragstart', (e) => {
        dragPath = entry.path;
        e.dataTransfer.effectAllowed = 'move';
        // Firefox refuses to start a drag without data set.
        try { e.dataTransfer.setData('text/plain', entry.path); } catch (_) {}
        e.stopPropagation();
    });
    row.addEventListener('dragend', () => {
        dragPath = null;
        document.querySelectorAll('#code-tree .drop-target')
            .forEach(el => el.classList.remove('drop-target'));
    });
    if (!entry.is_dir) return;                 // only folders accept a drop
    row.addEventListener('dragover', (e) => {
        if (!dragPath || dragPath === entry.path) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = 'move';
        row.classList.add('drop-target');
    });
    row.addEventListener('dragleave', () => row.classList.remove('drop-target'));
    row.addEventListener('drop', async (e) => {
        e.preventDefault();
        e.stopPropagation();
        row.classList.remove('drop-target');
        const moving = dragPath;
        dragPath = null;
        if (!moving || moving === entry.path) return;
        await moveEntry(moving, entry.path);
    });
}

async function moveEntry(path, destDir) {
    try {
        const r = await api('/api/files/move', {
            method: 'POST', body: JSON.stringify({ path, dest_dir: destDir }),
        });
        if (openFiles[path]) {
            openFiles[r.path] = openFiles[path];
            delete openFiles[path];
            if (activeFilePath === path) activeFilePath = r.path;
            renderCodeTabs();
        }
        await loadCodeTree();
        setCodeStatus(`moved to ${destDir || 'workspace root'}`);
    } catch (e) {
        setCodeStatus('could not move: ' + e.message);
    }
}

// ---------- context menu ----------

function showTreeMenu(x, y, entry) {
    closeTreeMenu();
    const menu = document.createElement('div');
    menu.className = 'tree-menu';
    menu.id = 'tree-menu';
    const items = [];
    if (entry.is_dir) {
        items.push(['New file', () => createEntry(false, entry.path)]);
        items.push(['New folder', () => createEntry(true, entry.path)]);
    } else {
        items.push(['Open', () => openFile(entry.path)]);
    }
    items.push(['Rename…', () => renameEntry(entry)]);
    items.push(['Delete', () => deleteEntry(entry)]);

    for (const [label, action] of items) {
        const item = document.createElement('button');
        item.type = 'button';
        item.className = 'tree-menu-item' + (label === 'Delete' ? ' danger' : '');
        item.textContent = label;
        item.onclick = () => { closeTreeMenu(); action(); };
        menu.appendChild(item);
    }
    document.body.appendChild(menu);
    // Keep it on screen when right-clicking near an edge.
    const box = menu.getBoundingClientRect();
    menu.style.left = Math.min(x, window.innerWidth - box.width - 8) + 'px';
    menu.style.top = Math.min(y, window.innerHeight - box.height - 8) + 'px';
    setTimeout(() => document.addEventListener('click', closeTreeMenu, { once: true }), 0);
}

function closeTreeMenu() {
    const existing = document.getElementById('tree-menu');
    if (existing) existing.remove();
}

// ---------- find in files ----------

let codeSearchTimer = null;

function toggleCodeSearch() {
    const box = document.getElementById('code-search');
    const hidden = box.classList.toggle('hidden');
    if (!hidden) {
        const input = document.getElementById('code-search-input');
        input.focus();
        input.select();
        if (!input.dataset.wired) {
            input.dataset.wired = '1';
            input.addEventListener('input', () => {
                clearTimeout(codeSearchTimer);
                // Debounced: every keystroke would otherwise walk the tree.
                codeSearchTimer = setTimeout(runCodeSearch, 250);
            });
            input.addEventListener('keydown', (e) => {
                if (e.key === 'Escape') toggleCodeSearch();
            });
        }
    }
}

async function runCodeSearch() {
    const query = document.getElementById('code-search-input').value.trim();
    const out = document.getElementById('code-search-results');
    if (!query) { out.innerHTML = ''; return; }
    out.innerHTML = '<div class="empty" style="padding:6px">Searching…</div>';
    try {
        const data = await api(`/api/files/search?q=${encodeURIComponent(query)}`);
        if (!data.hits.length) {
            out.innerHTML = '<div class="empty" style="padding:6px">No matches.</div>';
            return;
        }
        out.innerHTML = '';
        for (const hit of data.hits) {
            const row = document.createElement('div');
            row.className = 'code-hit';
            row.innerHTML = `<div class="code-hit-path">${escHtml(hit.path)}:${hit.line}</div>`
                          + `<div class="code-hit-text">${escHtml(hit.text.trim())}</div>`;
            row.onclick = () => openFile(hit.path, hit.line);
            out.appendChild(row);
        }
        if (data.truncated) {
            const note = document.createElement('div');
            note.className = 'empty';
            note.style.padding = '6px';
            note.textContent = 'More matches than shown — narrow the search.';
            out.appendChild(note);
        }
    } catch (e) {
        out.innerHTML = `<div class="empty" style="padding:6px">${escHtml(e.message)}</div>`;
    }
}

// Files with unsaved edits. Shown as a dot on the tab, and checked before
// anything closes one.
const dirtyFiles = new Set();

async function openFile(path, line) {
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
        // Mark dirty on every edit so the tab dot and the close guard are
        // driven by the editor itself rather than a timer.
        model.onDidChangeContent(() => {
            if (!dirtyFiles.has(path)) {
                dirtyFiles.add(path);
                renderCodeTabs();
            }
        });
        openFiles[path] = model;
    }
    activeFilePath = path;
    monacoEditor.setModel(model);
    document.getElementById('code-empty').classList.add('hidden');
    document.getElementById('code-editor-host').classList.remove('hidden');
    if (line) {
        try {
            monacoEditor.revealLineInCenter(line);
            monacoEditor.setPosition({ lineNumber: line, column: 1 });
            monacoEditor.focus();
        } catch (_) { /* model not laid out yet; not worth failing the open */ }
    }
    renderCodeTabs();
    setCodeStatus('');
}

// Monaco paints its own chrome, so it does not inherit the page's theme —
// left alone it stays dark on a light page, which is the one part of the UI
// that would not follow the appearance setting.
function monacoThemeName() {
    return document.documentElement.getAttribute('data-theme') === 'light'
        ? 'carrot-light' : 'carrot-dark';
}

function defineMonacoThemes(monaco) {
    const accent = getComputedStyle(document.documentElement)
        .getPropertyValue('--accent').trim() || '#f4813f';
    monaco.editor.defineTheme('carrot-dark', {
        base: 'vs-dark',
        inherit: true,
        rules: [],
        colors: {
            'editor.background': '#16181e',
            'editor.foreground': '#eceef4',
            'editorLineNumber.foreground': '#69707f',
            'editorCursor.foreground': accent,
            'editor.selectionBackground': '#2b3040',
        },
    });
    monaco.editor.defineTheme('carrot-light', {
        base: 'vs',
        inherit: true,
        rules: [],
        colors: {
            'editor.background': '#fbfaf8',
            'editor.foreground': '#1c1a18',
            'editorLineNumber.foreground': '#a8a29a',
            'editorCursor.foreground': accent,
            'editor.selectionBackground': '#f0e5da',
        },
    });
}

function ensureMonacoEditor(monaco) {
    if (monacoEditor) return;
    defineMonacoThemes(monaco);
    monacoEditor = monaco.editor.create(document.getElementById('code-editor-host'), {
        model: null,
        theme: monacoThemeName(),
        automaticLayout: true,
        fontSize: 13,
        minimap: { enabled: false },
        scrollBeyondLastLine: false,
    });
    monacoEditor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, saveCurrentFile);
}

// theme.js fires this whenever the appearance changes, including when the OS
// flips and the mode is "Match system".
window.addEventListener('carrot-theme', () => {
    if (!window.monaco || !monacoEditor) return;
    defineMonacoThemes(window.monaco);
    window.monaco.editor.setTheme(monacoThemeName());
});

function renderCodeTabs() {
    const bar = document.getElementById('code-tabs');
    bar.innerHTML = '';
    for (const path of Object.keys(openFiles)) {
        const tab = document.createElement('div');
        tab.className = 'code-tab' + (path === activeFilePath ? ' active' : '');
        const name = path.split('/').pop();
        const dirty = dirtyFiles.has(path);
        if (dirty) tab.classList.add('dirty');
        tab.title = path;
        tab.innerHTML = `<span class="ct-name">${escHtml(name)}</span>`
                      + `<span class="ct-close">${dirty ? '●' : '×'}</span>`;
        tab.querySelector('.ct-name').onclick = () => openFile(path);
        tab.querySelector('.ct-close').onclick = (e) => { e.stopPropagation(); closeFile(path); };
        bar.appendChild(tab);
    }
}

function closeFile(path, force) {
    if (!force && dirtyFiles.has(path)) {
        if (!confirm(`${path.split('/').pop()} has unsaved changes. Close it anyway?`)) return;
    }
    dirtyFiles.delete(path);
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
        dirtyFiles.delete(activeFilePath);
        renderCodeTabs();
        setCodeStatus('saved ' + activeFilePath.split('/').pop());
    } catch (e) {
        setCodeStatus('save failed: ' + e.message);
    }
}

async function sendNoteToObsidian() {
    if (!currentNoteId) { alert('Open a note first.'); return; }
    const status = document.getElementById('note-status');
    try {
        await saveNoteNow();
        const r = await api('/api/interop/obsidian/send', {
            method: 'POST',
            body: JSON.stringify({ note_id: currentNoteId }),
        });
        if (status) status.textContent = 'saved to your vault ✓';
    } catch (e) {
        if (e.message && e.message.includes('vault')) {
            if (confirm('No Obsidian vault is set yet. Open Settings to point Carrot at your vault folder?')) {
                switchTab('settings');
            }
        } else {
            alert(e.message);
        }
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

// window.prompt() is disabled in Electron and silently returns null, which
// is why this button appeared to do nothing. Use the native folder chooser
// when the shell offers one, and an inline field in a plain browser.
async function askForFolder(current) {
    if (window.carrotAPI && window.carrotAPI.pickDirectory) {
        const picked = await window.carrotAPI.pickDirectory({
            title: 'Choose your workspace folder', defaultPath: current,
        });
        return picked && picked.path ? picked.path : '';
    }
    return await inlineTextPrompt({
        title: 'Workspace folder',
        value: current,
        placeholder: 'absolute path to a folder',
        action: 'Use folder',
    });
}

// Shared replacement for window.prompt(). Every caller in the app goes
// through here — the native dialog is unavailable in Electron.
function inlineTextPrompt({ title, value, placeholder, action } = {}) {
    return new Promise((resolve) => {
        const host = document.createElement('div');
        host.className = 'path-prompt';
        host.innerHTML = `
            <div class="path-prompt-card">
              <div class="path-prompt-title"></div>
              <input type="text" spellcheck="false">
              <div class="row">
                <button class="btn btn-primary"></button>
                <button class="btn btn-ghost">Cancel</button>
              </div>
            </div>`;
        host.querySelector('.path-prompt-title').textContent = title || 'Enter a value';
        host.querySelector('.btn-primary').textContent = action || 'OK';
        const input = host.querySelector('input');
        input.placeholder = placeholder || '';
        input.value = value || '';
        const done = (value) => { host.remove(); resolve(value); };
        host.querySelector('.btn-primary').onclick = () => done(input.value.trim());
        host.querySelector('.btn-ghost').onclick = () => done('');
        input.onkeydown = (e) => {
            if (e.key === 'Enter') done(input.value.trim());
            if (e.key === 'Escape') done('');
        };
        host.onclick = (e) => { if (e.target === host) done(''); };
        document.body.appendChild(host);
        input.focus();
        input.select();
    });
}

async function changeCodeRoot() {
    const folder = await askForFolder(codeRoot);
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
    loadPacks();
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


// ===== Code tab keyboard =====
// Ctrl+S is muscle memory; without it the browser's own save dialog appears
// over the app, which is worse than doing nothing.
document.addEventListener('keydown', (e) => {
    const codeVisible = !document.getElementById('view-code')?.classList.contains('hidden')
        && document.getElementById('view-code')?.offsetParent !== null;
    if (!codeVisible) return;
    const mod = e.ctrlKey || e.metaKey;
    if (mod && !e.shiftKey && e.key.toLowerCase() === 's') {
        e.preventDefault();
        saveCurrentFile();
    } else if (mod && e.shiftKey && e.key.toLowerCase() === 'f') {
        e.preventDefault();
        toggleCodeSearch();
    }
});

// ================================================================
// Artifacts — charts, diagrams and images shown inside the chat
// ================================================================
// The content is markup the *model* wrote. It never touches the app document:
// it goes into an iframe with `sandbox="allow-scripts"` and deliberately
// without `allow-same-origin`, which puts it in an opaque origin. Script
// inside can animate a chart; it cannot read the session token, the
// conversation, or anything in storage. srcdoc rather than a src URL so the
// artifact route does not have to be reachable without the session token.

const ARTIFACT_MARKER = /\[\[carrot:artifact:([a-f0-9]{4,32})\]\]/g;

function artifactIdsIn(text) {
    const ids = [];
    let match;
    ARTIFACT_MARKER.lastIndex = 0;
    while ((match = ARTIFACT_MARKER.exec(String(text || '')))) ids.push(match[1]);
    return ids;
}

function stripArtifactMarkers(text) {
    return String(text || '').replace(ARTIFACT_MARKER, '').trim();
}

async function renderArtifact(id, host) {
    let artifact;
    try {
        artifact = await api(`/api/artifacts/${id}`);
    } catch (e) {
        return;                       // trimmed or deleted; nothing to show
    }
    const card = document.createElement('figure');
    card.className = 'artifact';
    card.dataset.artifactId = id;

    const head = document.createElement('figcaption');
    head.className = 'artifact-head';
    head.innerHTML = `<span class="artifact-title">${escHtml(artifact.title || artifact.kind)}</span>`
                   + `<span class="artifact-kind">${escHtml(artifact.kind)}</span>`;
    const expand = document.createElement('button');
    expand.className = 'artifact-btn';
    expand.textContent = 'Open';
    expand.onclick = () => openArtifactFull(artifact);
    head.appendChild(expand);
    card.appendChild(head);

    if (artifact.kind === 'markdown') {
        // Markdown already goes through the sanitizing renderer, and it cannot
        // carry script, so it can be shown inline and pick up the app's type.
        const body = document.createElement('div');
        body.className = 'artifact-body md';
        body.innerHTML = mdToHtml(artifact.content);
        card.appendChild(body);
    } else if (artifact.kind === 'mermaid') {
        const body = document.createElement('pre');
        body.className = 'artifact-body artifact-mermaid';
        body.textContent = artifact.content;
        card.appendChild(body);
    } else {
        card.appendChild(artifactFrame(artifact));
    }
    host.appendChild(card);
}

function artifactFrame(artifact) {
    const frame = document.createElement('iframe');
    frame.className = 'artifact-frame';
    // allow-scripts WITHOUT allow-same-origin. Granting both together would
    // undo the sandbox entirely — the frame could reach into this document.
    frame.setAttribute('sandbox', 'allow-scripts');
    frame.setAttribute('referrerpolicy', 'no-referrer');
    frame.loading = 'lazy';
    frame.srcdoc = artifact.document;
    // Images and charts vary wildly in height; grow to fit rather than
    // scrolling a 200px window. Cross-origin means asking, not measuring.
    frame.style.height = artifact.kind === 'image' ? '320px' : '380px';
    return frame;
}

function openArtifactFull(artifact) {
    const host = document.createElement('div');
    host.className = 'artifact-modal';
    host.innerHTML = `
        <div class="artifact-modal-card">
          <div class="artifact-head">
            <span class="artifact-title">${escHtml(artifact.title || artifact.kind)}</span>
            <button class="artifact-btn" data-close>Close</button>
          </div>
        </div>`;
    const card = host.querySelector('.artifact-modal-card');
    if (artifact.kind === 'markdown') {
        const body = document.createElement('div');
        body.className = 'artifact-body md';
        body.innerHTML = mdToHtml(artifact.content);
        card.appendChild(body);
    } else {
        const frame = artifactFrame(artifact);
        frame.style.height = '70vh';
        card.appendChild(frame);
    }
    const close = () => host.remove();
    host.querySelector('[data-close]').onclick = close;
    host.onclick = (e) => { if (e.target === host) close(); };
    document.addEventListener('keydown', function esc(e) {
        if (e.key === 'Escape') { close(); document.removeEventListener('keydown', esc); }
    });
    document.body.appendChild(host);
}

// Called after a message finishes rendering: swap any markers the model's
// tool results left behind for the rendered thing.
async function mountArtifacts(messageEl, text) {
    const ids = artifactIdsIn(text);
    if (!ids.length) return;
    let host = messageEl.querySelector('.artifact-host');
    if (!host) {
        host = document.createElement('div');
        host.className = 'artifact-host';
        messageEl.appendChild(host);
    }
    for (const id of ids) {
        if (host.querySelector(`[data-artifact-id="${id}"]`)) continue;   // already shown
        await renderArtifact(id, host);
    }
}

// ================================================================
// Code tab — Run and terminal
// ================================================================
let codePanelTab = 'output';
const termHistory = [];
let termHistoryIndex = -1;

function toggleCodePanel(force) {
    const panel = document.getElementById('code-panel');
    if (!panel) return;
    const show = force === undefined ? panel.classList.contains('hidden') : force;
    panel.classList.toggle('hidden', !show);
    if (show && codePanelTab === 'terminal') document.getElementById('term-input')?.focus();
}

function showCodePanel(which) {
    codePanelTab = which;
    document.querySelectorAll('#code-panel .panel-tab').forEach(tab => {
        tab.classList.toggle('active', tab.dataset.panel === which);
    });
    document.getElementById('panel-output').classList.toggle('hidden', which !== 'output');
    document.getElementById('panel-terminal').classList.toggle('hidden', which !== 'terminal');
    toggleCodePanel(true);
    if (which === 'terminal') document.getElementById('term-input')?.focus();
}

function clearCodePanel() {
    if (codePanelTab === 'output') document.getElementById('panel-output').textContent = '';
    else document.getElementById('term-log').innerHTML = '';
    document.getElementById('panel-status').textContent = '';
}

// ---------- Run ----------

async function runCurrentFile() {
    if (!activeFilePath) { setCodeStatus('open a file first'); return; }
    // Running stale bytes is the classic way to debug the wrong program.
    if (dirtyFiles.has(activeFilePath)) await saveCurrentFile();

    const out = document.getElementById('panel-output');
    const status = document.getElementById('panel-status');
    showCodePanel('output');
    out.textContent = `running ${activeFilePath}…\n`;
    status.textContent = 'running';
    const runBtn = document.getElementById('run-btn');
    if (runBtn) runBtn.disabled = true;

    try {
        const r = await api('/api/files/run', {
            method: 'POST',
            body: JSON.stringify({ path: activeFilePath }),
        });
        // A compiler error is a build-stage failure, and saying which stage
        // failed is the difference between "my code is wrong" and "my
        // toolchain is wrong".
        const stage = r.stage === 'build' ? 'compile failed' : (r.ok ? 'finished' : 'exited non-zero');
        out.textContent = `${r.language || ''} · ${stage}\n\n${r.output || ''}`;
        status.textContent = r.missing_tool ? `needs ${r.missing_tool}`
            : `${r.language || ''} · ${r.ok ? 'ok' : 'failed'}`;
    } catch (e) {
        out.textContent = 'Could not run: ' + e.message;
        status.textContent = 'error';
    } finally {
        if (runBtn) runBtn.disabled = false;
    }
}

// ---------- terminal ----------

function termLine(text, kind) {
    const log = document.getElementById('term-log');
    const line = document.createElement('div');
    line.className = 'term-line' + (kind ? ' ' + kind : '');
    line.textContent = text;
    log.appendChild(line);
    log.scrollTop = log.scrollHeight;
    return line;
}

async function runTerminalCommand(command, confirmed) {
    const status = document.getElementById('panel-status');
    status.textContent = 'running';
    try {
        const r = await api('/api/terminal/execute', {
            method: 'POST',
            body: JSON.stringify({ command, cwd: codeRoot, confirm: !!confirmed }),
        });
        termLine(r.output || '(no output)', r.success ? '' : 'bad');
        status.textContent = r.success ? '' : `exit ${r.returncode}`;
    } catch (e) {
        // 428 is the backend asking for a second look at a destructive
        // command, not a failure — re-send once the user agrees.
        const detail = e.detail || {};
        if (e.status === 428 || detail.needs_confirmation) {
            const why = (detail.reasons || []).join('; ') || detail.message || 'this looks destructive';
            if (confirm(`${why}\n\nRun anyway?\n\n${command}`)) {
                return runTerminalCommand(command, true);
            }
            termLine('cancelled', 'bad');
            status.textContent = '';
            return;
        }
        termLine(e.message, 'bad');
        status.textContent = 'error';
    }
}

function wireTerminal() {
    const input = document.getElementById('term-input');
    if (!input || input.dataset.wired) return;
    input.dataset.wired = '1';
    input.addEventListener('keydown', async (e) => {
        if (e.key === 'Enter') {
            const command = input.value.trim();
            if (!command) return;
            termHistory.push(command);
            termHistoryIndex = termHistory.length;
            input.value = '';
            termLine('$ ' + command, 'cmd');
            await runTerminalCommand(command, false);
        } else if (e.key === 'ArrowUp') {
            if (!termHistory.length) return;
            e.preventDefault();
            termHistoryIndex = Math.max(0, termHistoryIndex - 1);
            input.value = termHistory[termHistoryIndex] || '';
        } else if (e.key === 'ArrowDown') {
            e.preventDefault();
            termHistoryIndex = Math.min(termHistory.length, termHistoryIndex + 1);
            input.value = termHistory[termHistoryIndex] || '';
        }
    });
}


// Shown when a new file's language has no toolchain on this machine.
function warnMissingToolchain(tc) {
    showCodePanel('output');
    const out = document.getElementById('panel-output');
    out.textContent =
        `${tc.language} is not installed on this computer, so Run will not work yet.\n\n` +
        `Install ${tc.install}` + (tc.help_url ? `\n  ${tc.help_url}` : '') +
        `\n\nYou can still write and save the file — come back and press Run once it is set up.`;
    document.getElementById('panel-status').textContent = `${tc.language} missing`;
}
