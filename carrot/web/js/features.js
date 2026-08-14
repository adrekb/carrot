// ===== Carrot AI — editors & extensions (Notes/Code/Extensions) =====
// Loaded before app.js so its function definitions (loadNotes, loadCodeTab,
// loadExtensions, mdToHtml, ...) are available to the tab loader map.

// ===== Markdown rendering =====
// Renders markdown via the vendored `marked`, then sanitizes: drop dangerous
// tags/attributes and force links to open safely in a new tab.
// ===== Maths =====
//
// Models write LaTeX constantly — a physics question, anything from the
// Academia pack, half of what a maths answer contains — and every surface
// except the notes editor printed the source. `$\nabla \cdot B = 0$` arrived
// as literal dollars and backslashes.
//
// Extracted *before* markdown, put back after. This ordering is the whole
// difficulty: `marked` treats `_` as emphasis and `*` as a list, so
// `$a_1 * b_2$` reaches KaTeX as `$a<em>1 </em> b<em>2</em>$` if it is parsed
// first — mangled beyond recovery, and silently, because what comes out is
// still valid HTML. So each expression is lifted out, replaced by an inert
// placeholder that contains no markdown-significant characters, and restored
// once the parser has finished.
//
// Rendered by KaTeX rather than left to the browser: it is already bundled
// for the notes editor, and rendering the same expression with two different
// engines in two panels of one app is how a subtle difference in spacing
// becomes a bug report nobody can reproduce.

// Display first, so `$$…$$` is never read as two empty inline spans. `\[…\]`
// and `\(…\)` are included because models emit them about as often as dollars.
const MATH_PATTERNS = [
    { re: /\$\$([\s\S]+?)\$\$/g, display: true },
    { re: /\\\[([\s\S]+?)\\\]/g, display: true },
    { re: /\\\(([\s\S]+?)\\\)/g, display: false },
    // Inline dollars, with the two guards that stop prices becoming maths:
    // no space directly inside the delimiters, and no digit immediately after
    // the closing one — so "$5 and $10" is left alone while "$x=1$" is not.
    { re: /\$(?!\s)((?:[^$\n\\]|\\.)+?)(?<!\s)\$(?!\d)/g, display: false },
];

function extractMath(text, store) {
    let out = String(text);
    for (const { re, display } of MATH_PATTERNS) {
        out = out.replace(re, (whole, body) => {
            // A placeholder with nothing markdown cares about. Letters and
            // digits only — an underscore here would itself become emphasis.
            const token = `KTXMATH${store.length}KTXEND`;
            store.push({ body, display });
            return token;
        });
    }
    return out;
}

function restoreMath(root, store) {
    if (!store.length) return;
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const hits = [];
    while (walker.nextNode()) {
        if (/KTXMATH\d+KTXEND/.test(walker.currentNode.nodeValue)) hits.push(walker.currentNode);
    }
    for (const node of hits) {
        const frag = document.createDocumentFragment();
        const parts = node.nodeValue.split(/(KTXMATH\d+KTXEND)/);
        for (const part of parts) {
            const match = /^KTXMATH(\d+)KTXEND$/.exec(part);
            if (!match) {
                if (part) frag.appendChild(document.createTextNode(part));
                continue;
            }
            const item = store[Number(match[1])];
            const span = document.createElement('span');
            try {
                window.katex.render(item.body, span, {
                    displayMode: item.display,
                    // Malformed LaTeX shows as the source in red rather than
                    // throwing — a model that writes a broken expression
                    // should cost the reader that expression, not the answer
                    // it was part of.
                    throwOnError: false,
                    // `\href` in model-authored LaTeX is a link the user did
                    // not ask for, in a place no sanitiser is looking.
                    trust: false,
                    strict: false,
                });
            } catch (_) {
                span.textContent = item.display ? `$$${item.body}$$` : `$${item.body}$`;
            }
            frag.appendChild(span);
        }
        node.parentNode.replaceChild(frag, node);
    }
}

function mdToHtml(md) {
    if (md == null) return '';
    if (!window.marked) {
        const div = document.createElement('div');
        div.textContent = String(md);
        return div.innerHTML.replace(/\n/g, '<br>');
    }
    // Lifted out before the parser sees them; see extractMath.
    const math = [];
    const source = window.katex ? extractMath(md, math) : String(md);
    let html;
    try {
        html = window.marked.parse(source, { breaks: true, gfm: true });
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
    markCitations(tpl.content);
    // After sanitising, so KaTeX's own markup is not stripped by the attribute
    // pass — and after citations, which only look at links.
    if (window.katex) restoreMath(tpl.content, math);
    return tpl.innerHTML;
}

// Inline citations as chips.
//
// A source link written mid-sentence renders as ordinary blue text, so a
// paragraph carrying five of them reads as a paragraph with five interruptions
// in it — and when the model writes the link flush against the last word you
// get "architectural interestsAl Jazeera". A chip is the shape people already
// read as "this is where that came from": small, quiet, and skimmable past.
//
// No favicons. Fetching one means asking every cited domain for an image on
// every render, which tells those sites what you are reading — the one thing
// this app exists not to do.
const CITE_MAX_CHARS = 34;

function markCitations(root) {
    for (const link of root.querySelectorAll('a[href]')) {
        const href = link.getAttribute('href') || '';
        if (!/^https?:/i.test(href)) continue;
        const text = (link.textContent || '').trim();
        // A citation names its source. A link whose text is a sentence is the
        // author linking a phrase, and turning that into a chip would be
        // rewriting their prose.
        if (!text || text.length > CITE_MAX_CHARS || text.split(/\s+/).length > 4) continue;
        // A bare URL as the text is not a name either.
        if (/^https?:/i.test(text)) continue;
        // Only mid-flow links: one that is the whole of its own line is a
        // list of sources, which already reads correctly.
        const parent = link.parentElement;
        if (parent && parent.childNodes.length === 1) continue;

        link.classList.add('cite-chip');
        link.title = href;
    }
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

// ===== File type marks =====
//
// A file tree of identical grey rows makes you read every name to find the one
// you want. Every real IDE marks the type instead, so the shape and colour do
// the finding and the name only confirms it.
//
// Drawn rather than drawn *from* something: a letter badge in the app's mono
// face, tinted per language. No icon set to ship, no sprite to keep in sync
// with the extension list, and it scales and themes with everything else.
// The colours are the ones people already associate with these languages
// (Linguist's, broadly), because a private colour scheme would have to be
// learned before it could help.
const FILE_MARKS = {
    py: ['PY', '#3572a5'], pyi: ['PY', '#3572a5'],
    js: ['JS', '#c9a227'], mjs: ['JS', '#c9a227'], cjs: ['JS', '#c9a227'],
    jsx: ['JSX', '#c9a227'],
    ts: ['TS', '#3178c6'], tsx: ['TSX', '#3178c6'],
    json: ['{ }', '#a8a13a'],
    html: ['<>', '#e34c26'], htm: ['<>', '#e34c26'],
    css: ['CSS', '#8a63d2'], scss: ['SCS', '#c6538c'], less: ['LES', '#2b5e8f'],
    md: ['MD', '#7d8590'], markdown: ['MD', '#7d8590'],
    sh: ['SH', '#66a55a'], bash: ['SH', '#66a55a'],
    bat: ['BAT', '#66a55a'], ps1: ['PS', '#2b6cb0'],
    yml: ['YML', '#b8574a'], yaml: ['YML', '#b8574a'],
    toml: ['TML', '#8a7a5e'], ini: ['INI', '#8a7a5e'], cfg: ['CFG', '#8a7a5e'],
    env: ['ENV', '#8a7a5e'],
    xml: ['XML', '#0f6cbd'], svg: ['SVG', '#c96198'],
    sql: ['SQL', '#c48a1a'], db: ['DB', '#c48a1a'], sqlite: ['DB', '#c48a1a'],
    java: ['JV', '#b07219'], kt: ['KT', '#a97bff'],
    c: ['C', '#6f8ba4'], h: ['H', '#6f8ba4'],
    cpp: ['C++', '#f34b7d'], cc: ['C++', '#f34b7d'], hpp: ['H++', '#f34b7d'],
    cs: ['C#', '#2e8b3d'], go: ['GO', '#00add8'], rs: ['RS', '#d08770'],
    rb: ['RB', '#a4373a'], php: ['PHP', '#6f77b5'], swift: ['SW', '#f05138'],
    lua: ['LUA', '#3b6fd4'], r: ['R', '#276dc3'], pl: ['PL', '#7a8a99'],
    txt: ['TXT', '#7d8590'], log: ['LOG', '#7d8590'], csv: ['CSV', '#4a8f5a'],
    pdf: ['PDF', '#c0392b'],
    png: ['IMG', '#b678c4'], jpg: ['IMG', '#b678c4'], jpeg: ['IMG', '#b678c4'],
    gif: ['IMG', '#b678c4'], webp: ['IMG', '#b678c4'], ico: ['IMG', '#b678c4'],
    woff: ['FNT', '#8a7a9e'], woff2: ['FNT', '#8a7a9e'], ttf: ['FNT', '#8a7a9e'],
    zip: ['ZIP', '#8a8a8a'], tar: ['ZIP', '#8a8a8a'], gz: ['ZIP', '#8a8a8a'],
};

// A few files are recognised whole, because the name carries more than the
// extension does — `.gitignore` has no extension at all, and `Dockerfile`
// would otherwise read as plain text.
const FILE_MARKS_BY_NAME = {
    '.gitignore': ['GIT', '#e8734a'], '.gitattributes': ['GIT', '#e8734a'],
    'dockerfile': ['DK', '#2496ed'], 'makefile': ['MK', '#8a7a5e'],
    'license': ['LIC', '#7d8590'], 'readme.md': ['MD', '#7d8590'],
    'package.json': ['NPM', '#cb3837'], 'package-lock.json': ['NPM', '#cb3837'],
    'pyproject.toml': ['PY', '#3572a5'], 'requirements.txt': ['PY', '#3572a5'],
};

function fileMark(name) {
    const lower = (name || '').toLowerCase();
    const mark = FILE_MARKS_BY_NAME[lower]
        || FILE_MARKS[lower.includes('.') ? lower.split('.').pop() : ''];
    return mark || ['•', '#6b7280'];
}

function fileMarkHtml(name) {
    const [label, colour] = fileMark(name);
    // The tint is per file type, so it cannot live in the stylesheet without a
    // class per extension. The shape does; only the colour is inline.
    return `<span class="file-mark" style="--mark: ${colour}">${escHtml(label)}</span>`;
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
    loadCoderState();
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
        row.innerHTML = `<span class="tree-caret">${entry.is_dir ? '▸' : ''}</span>`
                      + (entry.is_dir ? '<svg class="ico tree-folder"><use href="#i-folder"/></svg>'
                                      : fileMarkHtml(entry.name))
                      + `<span class="tree-name">${escHtml(entry.name)}</span>`;
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

// The editor is Monaco — the same engine VS Code runs — but it was created
// with almost none of it switched on: no minimap, no indent guides, no bracket
// colouring, no folding affordances, and the browser's default monospace.
// That is not a smaller editor, it is the same editor with its features off,
// and next to a real IDE it read as a textarea with syntax colours.
//
// Everything below is Monaco configuration. Options it does not recognise are
// ignored, so this degrades quietly on an older vendor bundle rather than
// throwing during editor construction and leaving the Code tab blank.

function editorOptions() {
    return {
        model: null,
        theme: monacoThemeName(),
        automaticLayout: true,
        // The app's mono face, so code in the editor, the terminal and a chat
        // code block are all the same typeface. DM Mono has no ligatures to
        // disable, which is the right default for an editor anyway: `!=` and
        // `=>` should be the characters that are actually in the file.
        fontFamily: 'DMMono, "Cascadia Code", "JetBrains Mono", Consolas, monospace',
        fontSize: 13,
        lineHeight: 20,
        fontLigatures: false,
        // JetBrains shows the map, the guides and the scope. Each one answers
        // a "where am I" question that otherwise costs a scroll.
        minimap: { enabled: true, renderCharacters: false, maxColumn: 90 },
        stickyScroll: { enabled: true },
        guides: {
            indentation: true,
            highlightActiveIndentation: true,
            bracketPairs: true,
        },
        bracketPairColorization: { enabled: true },
        matchBrackets: 'always',
        folding: true,
        showFoldingControls: 'mouseover',
        renderLineHighlight: 'all',
        renderWhitespace: 'selection',
        cursorBlinking: 'smooth',
        cursorSmoothCaretAnimation: 'on',
        smoothScrolling: true,
        scrollBeyondLastLine: false,
        // A ruler where lines start getting long, which is a convention this
        // codebase already follows in its own source.
        rulers: [88],
        tabSize: 4,
        detectIndentation: true,
        trimAutoWhitespace: true,
        formatOnPaste: true,
        suggestSelection: 'first',
        quickSuggestions: { other: true, comments: false, strings: false },
        occurrencesHighlight: 'singleFile',
        selectionHighlight: true,
        linkedEditing: true,
        autoClosingBrackets: 'languageDefined',
        autoSurround: 'languageDefined',
        multiCursorModifier: 'alt',
        find: { seedSearchStringFromSelection: 'selection', autoFindInSelection: 'multiline' },
        padding: { top: 8, bottom: 120 },
    };
}

// The handful of JetBrains bindings whose muscle memory actually hurts when
// it fails. Monaco keeps its own defaults as well, so this adds an alias
// rather than taking anything away — Ctrl+D still duplicates in JetBrains and
// still adds a cursor in VS Code.
function jetbrainsKeymap(monaco) {
    const K = monaco.KeyMod, C = monaco.KeyCode;
    return [
        ['editor.action.copyLinesDownAction', K.CtrlCmd | C.KeyD],
        ['editor.action.deleteLines', K.CtrlCmd | C.KeyY],
        ['editor.action.moveLinesUpAction', K.CtrlCmd | K.Shift | C.UpArrow],
        ['editor.action.moveLinesDownAction', K.CtrlCmd | K.Shift | C.DownArrow],
        ['editor.action.formatDocument', K.CtrlCmd | K.Alt | C.KeyL],
        ['editor.action.quickCommand', K.CtrlCmd | K.Shift | C.KeyA],
        ['editor.action.gotoLine', K.CtrlCmd | C.KeyG],
        ['editor.action.smartSelect.expand', K.CtrlCmd | C.KeyW],
        ['editor.action.smartSelect.shrink', K.CtrlCmd | K.Shift | C.KeyW],
        ['editor.action.commentLine', K.CtrlCmd | C.Slash],
        ['editor.action.rename', C.F2],
    ];
}

function ensureMonacoEditor(monaco) {
    if (monacoEditor) return;
    defineMonacoThemes(monaco);
    monacoEditor = monaco.editor.create(
        document.getElementById('code-editor-host'), editorOptions());
    monacoEditor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, saveCurrentFile);

    for (const [id, binding] of jetbrainsKeymap(monaco)) {
        // `addAction` on an id Monaco does not have would create a dead menu
        // entry, so the existing action is looked up and re-bound instead.
        // A missing one is skipped: the bundle decides which exist, not this.
        const action = monacoEditor.getAction(id);
        if (!action) continue;
        monacoEditor.addCommand(binding, () => action.run());
    }
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
        tab.innerHTML = fileMarkHtml(name)
                      + `<span class="ct-name">${escHtml(name)}</span>`
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
    ['output', 'terminal', 'git', 'checkpoints'].forEach(name => {
        document.getElementById('panel-' + name)?.classList.toggle('hidden', which !== name);
    });
    toggleCodePanel(true);
    if (which === 'terminal') document.getElementById('term-input')?.focus();
    if (which === 'git') loadGitPanel();
    if (which === 'checkpoints') loadCheckpoints();
}

function clearCodePanel() {
    if (codePanelTab === 'output') { document.getElementById('run-output').textContent = ''; clearRunOffer(); }
    else if (codePanelTab === 'terminal') document.getElementById('term-log').innerHTML = '';
    else if (codePanelTab === 'git') document.getElementById('git-diff').textContent = '';
    document.getElementById('panel-status').textContent = '';
}

// ---------- The coding agent: plan/act, git, checkpoints ----------
//
// Plan mode is enforced server-side by removing the write tools, not by asking
// the model to hold back. This is just the switch and its readout — but the
// readout matters: someone who thinks they are in Plan and is actually in Act
// is exactly the person who gets a surprise commit.

let coderMode = 'act';

async function loadCoderState() {
    let state;
    try {
        state = await api('/api/coder/state');
    } catch (_) { return; }
    coderMode = state.mode;
    document.getElementById('mode-plan')?.classList.toggle('on', state.mode === 'plan');
    document.getElementById('mode-act')?.classList.toggle('on', state.mode === 'act');
    // One status word above the editor, saying what the agent may do. It is
    // deliberately a sentence rather than a mode name: "act" told the user
    // nothing about whether their files were at risk.
    const status = document.getElementById('mode-status');
    if (status) {
        status.textContent = state.mode === 'plan'
            ? 'Plan — the agent reads and proposes, nothing on disk moves'
            : 'Act — the agent can create, edit, move and delete files, and run commands';
        status.classList.toggle('is-act', state.mode === 'act');
    }
    const rules = document.getElementById('rules-chip');
    if (rules) {
        rules.classList.toggle('hidden', !state.has_rules);
        rules.title = `Project rules in effect (${state.rules_chars} characters)`;
    }
    loadAgentModelPicker();

    const rootHint = document.getElementById('agent-root-hint');
    if (rootHint && state.root) rootHint.textContent = state.root;
    // The startup panel is built from this state, so it is drawn when the
    // state arrives rather than on a timer — and only while it is still the
    // startup panel, since redrawing it over a conversation in progress would
    // delete the conversation.
    if (document.querySelector('#agent-log .agent-hello')) renderAgentHello();
    // Which checkout this is. Refreshed with the rest of the coder state
    // rather than once at load, because switching workspace folder changes
    // the answer and so does creating a worktree from anywhere else.
    loadWorktrees();
    const chip = document.getElementById('git-chip');
    if (chip) {
        const git = state.git || {};
        chip.classList.toggle('hidden', !git.repo);
        if (git.repo) {
            const n = (git.changes || []).length;
            chip.textContent = `${git.branch || 'HEAD'}${n ? ` · ${n}` : ''}`;
        }
    }
}

async function setCoderMode(mode) {
    let result;
    if (mode === 'act') setCodeStatus('compacting the plan…');
    try {
        result = await api('/api/coder/mode', {
            method: 'PUT',
            // Plan -> Act compacts the planning conversation into an
            // implementation brief, so Act starts from the decisions rather
            // than from the transcript that produced them.
            //
            // It has to be the *agent panel's* conversation. These buttons sit
            // in the Code tab and govern the panel, but this sent
            // currentConversationId — which belongs to the chat tab. So the
            // plan you had just written was never the thing compacted: Act
            // started from an unrelated chat, or from nothing at all, and the
            // brief came back empty. Fall back to the chat only when the panel
            // has not been used yet, which is the one case where there is no
            // plan of its own to carry.
            body: JSON.stringify({
                mode,
                conversation_id: agentConversationId || currentConversationId,
            }),
        });
    } catch (e) {
        setCodeStatus('could not switch mode: ' + e.message);
        return;
    }
    loadCoderState();
    if (result.compacted) {
        setCodeStatus('Act mode — plan compacted into a brief');
        showBrief(result.brief);
    } else {
        setCodeStatus(mode === 'plan' ? 'Plan mode — writes are off' : 'Act mode — writes are on');
    }
}

// The brief is what Act will actually work from, so it is shown rather than
// applied invisibly: a compaction that dropped something important is only
// catchable if you can see it.
function showBrief(text) {
    if (!text) return;
    showCodePanel('output');
    clearRunOffer();
    document.getElementById('run-output').textContent =
        'Implementation brief (this is what Act works from):\n\n' + text;
}

async function takeCheckpoint() {
    try {
        const made = await api('/api/coder/checkpoints', {
            method: 'POST',
            body: JSON.stringify({ label: activeFilePath ? `before editing ${activeFilePath}` : '' }),
        });
        setCodeStatus(`checkpoint saved (${made.files} files)`);
        if (codePanelTab === 'checkpoints') loadCheckpoints();
    } catch (e) {
        setCodeStatus('could not checkpoint: ' + e.message);
    }
}

async function loadCheckpoints() {
    const host = document.getElementById('checkpoint-list');
    if (!host) return;
    let items = [];
    try {
        items = (await api('/api/coder/checkpoints')).checkpoints || [];
    } catch (e) {
        host.innerHTML = `<div class="empty">Could not load checkpoints: ${escHtml(e.message)}</div>`;
        return;
    }
    if (!items.length) {
        host.innerHTML = '<div class="empty">No checkpoints yet. Take one before a big change '
            + 'and you can undo the whole thing in one step.</div>';
        return;
    }
    host.innerHTML = '';
    for (const item of items) {
        const row = document.createElement('div');
        row.className = 'checkpoint-row';
        // A git-backed checkpoint restores atomically and purges ghost files;
        // a copied one is bounded. Saying which is which is honest about it.
        const backed = item.tree ? 'git' : 'snapshot';
        row.innerHTML = `
            <span class="cp-label">${escHtml(item.label || 'checkpoint')}</span>
            <span class="tag">${backed}</span>
            <span class="cp-when">${escHtml((item.created_at || '').replace('T', ' ').slice(0, 16))}</span>`;
        const restore = document.createElement('button');
        restore.className = 'btn btn-ghost';
        restore.textContent = 'Restore';
        restore.onclick = () => restoreCheckpoint(item.id, item.label);
        row.appendChild(restore);
        host.appendChild(row);
    }
}

async function restoreCheckpoint(id, label) {
    // Restoring throws away work that came after it, so it asks first — and
    // says what it will do, not just "are you sure".
    if (!confirm(`Restore "${label || id}"?\n\nEvery change made after this point `
        + `is undone, and files created since are deleted.`)) return;
    try {
        const result = await api(`/api/coder/checkpoints/${encodeURIComponent(id)}/restore`,
            { method: 'POST' });
        setCodeStatus(result.purged
            ? `restored ${result.restored.length} file(s), workspace purged to that point`
            : `restored ${result.restored.length} file(s), removed ${result.removed.length}`);
        loadCodeTree();
        // Keep the panel honest the moment the workspace moves under it.
        loadCheckpoints();
        loadCoderState();
        if (activeFilePath) openFile(activeFilePath);
    } catch (e) {
        setCodeStatus('could not restore: ' + e.message);
    }
}

async function loadGitPanel() {
    const changes = document.getElementById('git-changes');
    const branch = document.getElementById('git-branch');
    if (!changes) return;
    let state;
    try {
        state = await api('/api/coder/git/status');
    } catch (e) {
        branch.textContent = '';
        changes.innerHTML = `<div class="empty">${escHtml(e.detail || e.message)}</div>`;
        return;
    }
    branch.textContent = state.branch
        + (state.ahead ? ` ↑${state.ahead}` : '') + (state.behind ? ` ↓${state.behind}` : '');
    if (state.clean) {
        changes.innerHTML = '<div class="empty">Working tree clean.</div>';
        document.getElementById('git-diff').textContent = '';
        return;
    }
    changes.innerHTML = '';
    for (const change of state.changes) {
        const row = document.createElement('div');
        row.className = 'git-change';
        row.innerHTML = `<span class="git-code">${escHtml(change.code)}</span>
                         <span class="git-path">${escHtml(change.path)}</span>`;
        row.onclick = () => showGitDiff(change.path);
        changes.appendChild(row);
    }
    showGitDiff('');
}

async function showGitDiff(path) {
    const host = document.getElementById('git-diff');
    try {
        const body = await api('/api/coder/git/diff?path=' + encodeURIComponent(path || ''));
        host.textContent = body.diff;
    } catch (e) {
        host.textContent = e.detail || e.message;
    }
}

async function commitChanges() {
    const input = document.getElementById('git-message');
    const message = input.value.trim();
    if (!message) { setCodeStatus('a commit needs a message'); return; }
    try {
        const result = await api('/api/coder/git/commit', {
            method: 'POST', body: JSON.stringify({ message }),
        });
        input.value = '';
        setCodeStatus(`committed ${result.head ? result.head.sha : ''}`);
        loadGitPanel();
        loadCoderState();
    } catch (e) {
        setCodeStatus('could not commit: ' + (e.detail || e.message));
    }
}

// ---------- Run ----------

async function runCurrentFile() {
    if (!activeFilePath) { setCodeStatus('open a file first'); return; }
    // Running stale bytes is the classic way to debug the wrong program.
    if (dirtyFiles.has(activeFilePath)) await saveCurrentFile();

    const out = document.getElementById('run-output');
    const status = document.getElementById('panel-status');
    showCodePanel('output');
    clearRunOffer();
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
        // The one line that matters is usually buried in the traceback, so it
        // gets lifted out and given a button.
        if (r.missing_tool) showToolchainOffer(r);
        else if (r.missing_package) showPackageOffer(r.missing_package);
    } catch (e) {
        out.textContent = 'Could not run: ' + e.message;
        status.textContent = 'error';
    } finally {
        if (runBtn) runBtn.disabled = false;
    }
}

// ---------- "you're missing something" offers ----------
//
// The interesting line of a failed run is one line in fifty, and the fix is
// usually one command. Both get lifted out of the output and given a button.
// Nothing installs itself: the command is shown next to the button, because
// running an installer on someone's machine without saying which one is not a
// convenience, it is a surprise.

function clearRunOffer() {
    const host = document.getElementById('run-offer');
    if (!host) return;
    host.innerHTML = '';
    host.classList.add('hidden');
}

function runOfferHost() {
    const host = document.getElementById('run-offer');
    host.innerHTML = '';
    host.classList.remove('hidden');
    return host;
}

// A missing language is not something Carrot can install for you — that is an
// installer with a licence screen — so this offers the official download page.
function showToolchainOffer(result) {
    const host = runOfferHost();
    const title = document.createElement('div');
    title.className = 'offer-title';
    title.textContent = `${result.language || 'That language'} is not installed on this computer`;
    host.appendChild(title);

    const body = document.createElement('div');
    body.className = 'offer-body';
    body.textContent = `Run needs ${result.missing_tool}. Install it, then press Run again — `
        + `your file is saved and waiting.`;
    host.appendChild(body);

    if (result.help_url) {
        const link = document.createElement('button');
        link.className = 'btn btn-primary';
        link.textContent = `Get ${result.missing_tool}`;
        link.onclick = () => {
            if (window.carrot?.openExternal) window.carrot.openExternal(result.help_url);
            else window.open(result.help_url, '_blank', 'noopener');
        };
        host.appendChild(link);
        const url = document.createElement('code');
        url.className = 'offer-cmd';
        url.textContent = result.help_url;
        host.appendChild(url);
    }
}

function showPackageOffer(offer) {
    const host = runOfferHost();
    const title = document.createElement('div');
    title.className = 'offer-title';
    title.textContent = offer.installable
        ? `${offer.missing} is not installed`
        : `${offer.missing} is missing`;
    host.appendChild(title);

    const body = document.createElement('div');
    body.className = 'offer-body';
    // `import cv2` needing `opencv-python` is the single most useful sentence
    // in this whole feature, so it goes above the button.
    body.textContent = offer.note ? `${offer.message} ${offer.note}` : offer.message;
    host.appendChild(body);

    if (!offer.installable) return;
    if (!offer.available) {
        const why = document.createElement('div');
        why.className = 'offer-body';
        why.textContent = `${offer.manager_label} is not on this computer, so Carrot cannot `
            + `install it for you.`;
        host.appendChild(why);
        return;
    }

    const button = document.createElement('button');
    button.className = 'btn btn-primary';
    button.textContent = `Install ${offer.package}`;
    button.onclick = () => installMissingPackage(offer, button);
    host.appendChild(button);

    const cmd = document.createElement('code');
    cmd.className = 'offer-cmd';
    cmd.textContent = offer.command;
    host.appendChild(cmd);
}

async function installMissingPackage(offer, button) {
    const out = document.getElementById('run-output');
    button.disabled = true;
    button.textContent = `Installing ${offer.package}…`;
    let result;
    try {
        result = await api('/api/files/install', {
            method: 'POST',
            body: JSON.stringify({ package: offer.package, manager: offer.manager }),
        });
    } catch (e) {
        button.disabled = false;
        button.textContent = `Install ${offer.package}`;
        out.textContent = 'Could not install: ' + (e.detail || e.message);
        return;
    }
    out.textContent = `${result.command}\n\n${result.output || ''}`;
    if (result.ok) {
        clearRunOffer();
        setCodeStatus(`installed ${offer.package} — running again`);
        // The reason anyone pressed the button was to get their program to
        // run, so finish the job rather than making them press Run again.
        runCurrentFile();
    } else {
        button.disabled = false;
        button.textContent = `Try again`;
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
    const out = document.getElementById('run-output');
    clearRunOffer();
    showToolchainOffer({ language: tc.language, missing_tool: tc.install, help_url: tc.help_url });
    out.textContent =
        `${tc.language} is not installed on this computer, so Run will not work yet.\n\n` +
        `Install ${tc.install}` + (tc.help_url ? `\n  ${tc.help_url}` : '') +
        `\n\nYou can still write and save the file — come back and press Run once it is set up.`;
    document.getElementById('panel-status').textContent = `${tc.language} missing`;
}

// ---------- The agent panel ----------
//
// A Plan/Act switch with nowhere to talk to the agent is a steering wheel with
// no car attached. This is the car: a conversation scoped to the code
// workspace, streaming the same SSE the chat tab does, showing tool calls and
// approval prompts inline so you can watch what it is doing to your files.

let agentConversationId = null;
// A skill armed for the next task in this panel, and the catalogue the
// startup list was built from.
let agentSkill = null;
let agentSkillCatalog = [];
let agentAbort = null;
let agentAttachments = [];

function toggleAgentSide(force) {
    const side = document.getElementById('agent-side');
    if (!side) return;
    const hide = force === undefined ? !side.classList.contains('hidden') : !force;
    side.classList.toggle('hidden', hide);
    if (!hide) document.getElementById('agent-input')?.focus();
}

function newAgentTask() {
    agentConversationId = null;
    agentAttachments = [];
    renderAgentTray();
    renderAgentHello();
    document.getElementById('agent-input')?.focus();
}

// ===== What the panel says before you have asked it anything =====
//
// It said "Tell me what to build, fix or explain" and named the folder. That
// is a greeting, and a greeting is the least useful thing that can occupy the
// screen you look at most often — every other coding tool uses this space to
// answer the questions you actually have when you sit down: where am I, what
// is still running from last time, and what does this thing know how to do.
//
// Three sections, all of them facts rather than encouragement, and each one
// absent when it has nothing to say. An empty state that invents content to
// fill itself is how a startup screen becomes something people click past.

async function renderAgentHello() {
    const log = document.getElementById('agent-log');
    if (!log) return;
    // The real path, not the hint element's placeholder — that element still
    // says "your workspace" until the coder state has come back, and the
    // panel renders before it.
    const root = (typeof codeRoot !== 'undefined' && codeRoot)
        || document.getElementById('agent-root-hint')?.textContent
        || 'your workspace';
    const branch = document.getElementById('git-chip')?.textContent || '';

    log.innerHTML = `
      <div class="agent-hello">
        <div class="hello-where">
          <svg class="ico"><use href="#i-folder"/></svg>
          <span class="hello-root mono" title="${escHtml(root)}">${escHtml(root)}</span>
          ${branch ? `<span class="hello-branch mono">${escHtml(branch)}</span>` : ''}
        </div>
        <div id="hello-servers" class="hello-block hidden"></div>
        <div id="hello-scheduled" class="hello-block hidden"></div>
        <div id="hello-skills" class="hello-block hidden"></div>
        <p class="hello-modes muted small">In <strong>Plan</strong> I only read and propose.
           In <strong>Act</strong> I can edit files, run commands and start servers.</p>
      </div>`;

    // Servers first: it is the only thing here that is still true from a
    // previous session, and the only one with a cost attached — a dev server
    // the user has forgotten is holding one of their ports right now.
    try {
        const { servers } = await api('/api/coder/servers');
        const live = (servers || []).filter(s => s.running);
        if (live.length) {
            const host = document.getElementById('hello-servers');
            host.classList.remove('hidden');
            host.innerHTML = '<div class="hello-title">Still running</div>'
                + live.map(s => `
                    <div class="hello-server">
                        <span class="server-dot live"></span>
                        ${s.url
                            ? `<a href="${escHtml(s.url)}" target="_blank" rel="noopener">${escHtml(s.url)}</a>`
                            : `<span class="mono">${escHtml(s.label || s.command)}</span>`}
                        <span class="spacer"></span>
                        <button class="btn btn-ghost" onclick="stopHelloServer('${escHtml(s.id)}')">Stop</button>
                    </div>`).join('');
        }
    } catch (_) {}

    // Then the standing appointments. They run whether or not this screen is
    // open, which is exactly why they belong on it: work that happens without
    // being asked has to be visible somewhere the user passes anyway, or the
    // first they hear of it is a notification about a task they had forgotten
    // making.
    try {
        const { tasks } = await api('/api/coder/scheduled');
        if ((tasks || []).length) {
            const host = document.getElementById('hello-scheduled');
            host.classList.remove('hidden');
            host.innerHTML = '<div class="hello-title">On a schedule</div>'
                + tasks.map(t => `
                    <div class="sched-row${t.enabled ? '' : ' off'}">
                        <button class="sched-toggle${t.enabled ? ' on' : ''}"
                                title="${t.enabled ? 'Pause this' : 'Switch this back on'}"
                                onclick="toggleScheduledTask('${escHtml(t.id)}', ${!t.enabled})"></button>
                        <div class="sched-body">
                            <div class="sched-prompt">${escHtml(t.prompt)}</div>
                            <div class="sched-when">${escHtml(describeSchedule(t))}${
                                t.last_status ? ' · last run ' + escHtml(t.last_status) : ''}</div>
                        </div>
                        <button class="icon-btn" title="Run it now, without waiting for its slot"
                                onclick="runScheduledTaskNow('${escHtml(t.id)}')"
                            ><svg class="ico"><use href="#i-pulse"/></svg></button>
                        <button class="icon-btn" title="Delete this scheduled task"
                                onclick="deleteScheduledTask('${escHtml(t.id)}')"
                            ><svg class="ico"><use href="#i-trash"/></svg></button>
                    </div>`).join('');
        }
    } catch (_) {}

    // Then what it has been taught. A skill nobody remembers exists is a
    // skill nobody invokes, and they are listed here because this is the
    // moment you are deciding what to ask for.
    try {
        const skills = await api('/api/skills');
        agentSkillCatalog = skills || [];
        if ((skills || []).length) {
            const host = document.getElementById('hello-skills');
            host.classList.remove('hidden');
            host.innerHTML = '<div class="hello-title">It knows how you work</div>'
                + skills.slice(0, 6).map(s => `
                    <button class="hello-skill" title="${escHtml(s.description || '')}"
                            onclick="useSkillInAgent('${escHtml(s.slug)}')">${escHtml(s.name)}</button>`
                  ).join('');
        }
    } catch (_) {}
}

// ===== Which checkout the agent is working in =====
//
// "Try this refactor" and "keep working" are the same directory otherwise:
// the agent's edits land on top of whatever you had open, and undoing them
// means undoing yours too. A worktree gives it a whole checkout on its own
// branch, sharing the object database, for the price of a directory.
//
// A picker rather than a command, because the thing you need to know is which
// one you are in *now* — the mistake this prevents is committing an
// experiment to main, and that mistake is made by not knowing where you are.

const NEW_WORKTREE = '__new__';

async function loadWorktrees() {
    const picker = document.getElementById('worktree-picker');
    if (!picker) return;
    let data;
    try {
        data = await api('/api/coder/worktrees');
    } catch (_) {
        picker.classList.add('hidden');
        return;
    }
    if (!data.repo) { picker.classList.add('hidden'); return; }
    picker.classList.remove('hidden');

    const here = (data.current || '').replace(/[\\/]+$/, '').toLowerCase();
    picker.innerHTML = (data.worktrees || []).map((w, index) => {
        const label = index === 0
            // The first one git lists is the repository proper. Calling it by
            // its branch would make it look like one experiment among
            // several, when it is the thing the others are branches of.
            ? `Main · ${w.branch || 'detached'}`
            : (w.branch || w.path.split(/[\\/]/).pop());
        const selected = w.path.replace(/[\\/]+$/, '').toLowerCase() === here ? ' selected' : '';
        return `<option value="${escHtml(w.path)}"${selected}>${escHtml(label)}</option>`;
    }).join('') + `<option value="${NEW_WORKTREE}">New worktree…</option>`;
}

async function pickWorktree(value) {
    if (value === NEW_WORKTREE) {
        const branch = await inlineTextPrompt(
            'Branch name for the new worktree', 'try/refactor');
        // Cancelled: put the picker back on where we actually are, or it
        // sits there naming a worktree that was never made.
        if (!branch) { loadWorktrees(); return; }
        try {
            const made = await api('/api/coder/worktrees', {
                method: 'POST', body: JSON.stringify({ branch, switch: true }),
            });
            setCodeStatus(`working in ${made.path}`);
        } catch (err) {
            setCodeStatus('could not make that worktree: ' + err);
            loadWorktrees();
            return;
        }
    } else {
        try {
            await api('/api/files/root', {
                method: 'POST', body: JSON.stringify({ root: value }),
            });
        } catch (err) {
            setCodeStatus('could not switch: ' + err);
            return;
        }
    }
    // Everything that reads the root has to be told. The file tree and the
    // agent's own idea of where it is were the two that mattered: a tree
    // still showing the old checkout is a tree you open files from and then
    // edit in the other one.
    await loadCodeTab();
    await loadCoderState();
    await loadWorktrees();
}

// ===== Standing appointments =====

function describeSchedule(task) {
    if (task.schedule === 'hourly') return 'Every hour';
    if (task.schedule === 'weekly') {
        const day = (task.weekday || 'monday');
        return `Weekly on ${day.charAt(0).toUpperCase()}${day.slice(1)} around ${task.at}`;
    }
    return `Daily around ${task.at}`;
}

async function toggleScheduledTask(id, enabled) {
    try {
        await api(`/api/coder/scheduled/${id}`, {
            method: 'PATCH', body: JSON.stringify({ enabled }),
        });
    } catch (_) {}
    renderAgentHello();
}

async function deleteScheduledTask(id) {
    if (!confirm('Delete this scheduled task?')) return;
    try { await api(`/api/coder/scheduled/${id}`, { method: 'DELETE' }); } catch (_) {}
    renderAgentHello();
}

// Runs it this second rather than at its slot. The only way to find out
// whether a task you have written does what you meant is to run it, and
// waiting until 09:00 tomorrow to discover it was phrased badly is not a
// feedback loop anybody uses.
async function runScheduledTaskNow(id) {
    const row = document.querySelector(`.sched-row button[onclick*="${id}"]`)?.closest('.sched-row');
    if (row) row.classList.add('running');
    try {
        const result = await api(`/api/coder/scheduled/${id}/run`, { method: 'POST' });
        agentBubble('agent', result.output || '(the run produced no output)');
    } catch (err) {
        agentBubble('agent', 'The scheduled task failed: ' + err);
    }
    if (row) row.classList.remove('running');
}

// Turns what is in the composer into a standing appointment. Written here
// rather than in a settings page because this is where the sentence already
// is — the moment you notice you have typed the same thing three mornings
// running is the moment to say "every morning", and a form on another screen
// is a form you fill in never.
async function scheduleCurrentTask() {
    const input = document.getElementById('agent-input');
    const prompt = (input?.value || '').trim();
    if (!prompt) {
        agentBubble('agent', 'Type what you want done first, then schedule it.');
        return;
    }
    const when = await inlineTextPrompt(
        'When should this run? "hourly", "daily 09:00", or "weekly monday 09:00"',
        'daily 09:00');
    if (when === null) return;

    const parts = String(when).trim().toLowerCase().split(/\s+/);
    const body = { prompt, schedule: 'daily', at: '09:00', weekday: 'monday' };
    if (parts[0] === 'hourly') body.schedule = 'hourly';
    else if (parts[0] === 'weekly') {
        body.schedule = 'weekly';
        body.weekday = parts[1] || 'monday';
        body.at = parts[2] || '09:00';
    } else {
        body.at = parts[1] || parts[0] || '09:00';
    }

    try {
        await api('/api/coder/scheduled', { method: 'POST', body: JSON.stringify(body) });
        input.value = '';
        renderAgentHello();
    } catch (err) {
        agentBubble('agent', 'Could not schedule that: ' + err);
    }
}

async function stopHelloServer(id) {
    try { await api(`/api/coder/servers/${id}/stop`, { method: 'POST' }); } catch (_) {}
    renderAgentHello();
}

// Arms the skill for the next task rather than firing one off. Clicking a
// chip that immediately starts work is a chip people stop touching, and the
// user still has to say what they want done — the skill is how, not what.
function useSkillInAgent(slug) {
    const chosen = (agentSkillCatalog || []).find(s => s.slug === slug);
    agentSkill = chosen ? { slug: chosen.slug, name: chosen.name } : { slug, name: slug };
    renderAgentSkillChip();
    document.getElementById('agent-input')?.focus();
}

function clearAgentSkill() {
    agentSkill = null;
    renderAgentSkillChip();
}

// Shown in the composer, next to the model picker, because that row is where
// everything else that changes what the next message does already lives. A
// skill armed and not visible is the same bug as a model set and not visible,
// which is the one this panel already fixed once.
function renderAgentSkillChip() {
    const row = document.querySelector('.agent-compose-row');
    if (!row) return;
    let chip = row.querySelector('.agent-skill-chip');
    if (!agentSkill) { chip?.remove(); return; }
    if (!chip) {
        chip = document.createElement('button');
        chip.className = 'agent-skill-chip';
        chip.onclick = clearAgentSkill;
        row.insertBefore(chip, row.querySelector('.spacer'));
    }
    chip.title = 'Using this skill for the next task — click to clear';
    chip.textContent = `/${agentSkill.name} ✕`;
}

// ---------- Attachments ----------
//
// A screenshot of the error, a mock of the screen, a PDF of the spec. Images
// only work with a vision model, so the tray says so rather than letting the
// send fail with a 400 the user cannot interpret.

async function addAgentAttachments(files) {
    for (const file of Array.from(files || [])) {
        if (file.size > (typeof ATTACH_MAX_BYTES !== 'undefined' ? ATTACH_MAX_BYTES : 10485760)) {
            setCodeStatus(`${file.name} is too large`);
            continue;
        }
        const data = await new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(String(reader.result).split(',')[1] || '');
            reader.onerror = reject;
            reader.readAsDataURL(file);
        });
        const isImage = (file.type || '').startsWith('image/');
        agentAttachments.push({
            name: file.name, mime: file.type, bytes: file.size, data,
            thumb: isImage ? `data:${file.type};base64,${data}` : null,
            image: isImage,
        });
    }
    renderAgentTray();
}

function removeAgentAttachment(index) {
    agentAttachments.splice(index, 1);
    renderAgentTray();
}

function renderAgentTray() {
    const tray = document.getElementById('agent-attach-tray');
    if (!tray) return;
    tray.classList.toggle('hidden', !agentAttachments.length);
    tray.innerHTML = agentAttachments.map((a, i) => `
        <div class="attach-chip">
          ${a.thumb ? `<img src="${a.thumb}" alt="">` : '<span class="attach-doc"></span>'}
          <span class="attach-name">${escHtml(a.name)}</span>
          <button class="attach-x" onclick="removeAgentAttachment(${i})">&times;</button>
        </div>`).join('');
    warnIfNoVision();
}

// Checking before sending turns "400: gemma cannot read images" into a
// sentence that appears while there is still time to switch models.
async function warnIfNoVision() {
    const hint = document.getElementById('agent-context-hint');
    if (!hint) return;
    if (!agentAttachments.some(a => a.image)) { hint.textContent = ''; return; }
    try {
        const data = await api('/api/models');
        const model = data.chat_local === false ? data.chat_model : data.active_model;
        const vision = /vision|llava|moondream|gemma|qwen2?-?vl|pixtral|gpt-4|claude|minicpm/i;
        hint.textContent = vision.test(model || '')
            ? '' : `${model} probably cannot read images — pick a vision model.`;
    } catch (_) { hint.textContent = ''; }
}

function agentBubble(role, text) {
    const log = document.getElementById('agent-log');
    log.querySelector('.agent-hello')?.remove();
    const wrap = document.createElement('div');
    wrap.className = 'agent-msg ' + role;
    const body = document.createElement('div');
    // `md` carries the heading, list and code-block styling the chat bubbles
    // already have; without it the rendered markdown is unstyled run-on text.
    body.className = 'agent-body md';
    body.textContent = text || '';
    wrap.appendChild(body);
    log.appendChild(wrap);
    log.scrollTop = log.scrollHeight;
    return { wrap, body };
}

// The same set the backend gates ACT mode on, so "changed" means here what it
// means there.
const WRITE_TOOLS = new Set([
    'write_file', 'edit_file', 'create_file', 'delete_file', 'move_file',
    'git_commit', 'git_checkout', 'restore_checkpoint',
]);

// Say when it has stopped, and what it did.
//
// A finished turn used to just stop: the caret disappeared and the prose sat
// there, often ending with a question, so there was no way to tell a turn that
// had finished from one still thinking. And the answer describes intentions —
// the files are what actually happened, which is the thing ACT mode exists to
// make true.
function agentFinished(wrap, touched, commandsRun, elapsedMs, failure) {
    if (!wrap || wrap.querySelector('.agent-done')) return;
    const parts = [];
    if (touched.size) {
        parts.push(`${touched.size} file${touched.size === 1 ? '' : 's'} changed`);
    }
    if (commandsRun) {
        parts.push(`${commandsRun} command${commandsRun === 1 ? '' : 's'} run`);
    }
    // "Nothing changed" is information, not an absence of it — in ACT mode it
    // is the whole complaint, and in Plan mode it is the correct outcome.
    if (!parts.length) parts.push('nothing changed on disk');

    const row = document.createElement('div');
    // A turn the provider stopped is not a turn that finished, and saying
    // "Done" over the top of a rate limit is how you end up staring at a
    // file that was never written wondering what you did wrong. The counts
    // stay: what it managed before it stopped is exactly what you need to
    // know to decide whether to run it again.
    row.className = 'agent-done' + (failure ? ' failed' : '');
    row.innerHTML = `<svg class="ico"><use href="#i-${failure ? 'stop' : 'check'}"/></svg>`
        + `<span>${failure ? 'Stopped' : 'Done'} — ${escHtml(parts.join(', '))}`
        + (failure ? ` · ${escHtml(String(failure).slice(0, 160))}` : '')
        + `<span class="agent-done-time"> · ${Math.round(elapsedMs / 1000)}s</span></span>`;
    if (touched.size) {
        const list = document.createElement('div');
        list.className = 'agent-done-files';
        list.innerHTML = [...touched]
            .map(p => `<code>${escHtml(p)}</code>`).join('');
        row.appendChild(list);
    }
    wrap.appendChild(row);
    document.getElementById('agent-log').scrollTop = 1e9;
}

function agentTrace(wrap, text, cls) {
    let trace = wrap.querySelector('.agent-trace');
    if (!trace) {
        trace = document.createElement('div');
        trace.className = 'agent-trace';
        wrap.insertBefore(trace, wrap.firstChild);
    }
    const line = document.createElement('div');
    line.className = 'agent-trace-line' + (cls ? ' ' + cls : '');
    line.textContent = text;
    trace.appendChild(line);
    trace.scrollTop = trace.scrollHeight;
    document.getElementById('agent-log').scrollTop = 1e9;
}

// ===== How much room is left =====
//
// The turn runs until the context window fills rather than to a round count,
// which is the right unit and an invisible one. A bar makes it the same kind
// of fact as a battery: you do not read it, you notice it.
//
// One per turn, updated in place, and it does not appear at all until the
// window is worth thinking about — a meter at 3% is decoration, and a panel
// that decorates every turn is one people stop reading.

const CONTEXT_METER_FROM = 0.25;

function agentContextMeter(wrap, context) {
    const fraction = Math.max(0, Math.min(1, context.fraction || 0));
    // Looked up where it actually lives. Searching `wrap` for it never found
    // the one already on screen — it is a sibling of the composer, not of the
    // message — so every round built another, and the panel filled with
    // stacked bars each frozen at the reading it was born with.
    let meter = document.getElementById('agent-ctx-meter');
    if (!meter) {
        if (fraction < CONTEXT_METER_FROM) return;
        meter = document.createElement('div');
        meter.className = 'ctx-meter';
        meter.id = 'agent-ctx-meter';
        meter.innerHTML = '<div class="ctx-bar"><span></span></div><span class="ctx-text"></span>';
        // Above the composer rather than in the transcript: it describes the
        // turn as a whole, and a bar that scrolled away with the round it was
        // emitted in would be a history of how full the window used to be.
        const compose = document.querySelector('.agent-compose');
        compose?.parentElement.insertBefore(meter, compose);
    }
    const percent = Math.round(fraction * 100);
    meter.querySelector('.ctx-bar > span').style.width = percent + '%';
    meter.classList.toggle('tight', fraction > 0.7);
    meter.classList.toggle('full', fraction > 0.85);
    const thousands = n => n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
    meter.querySelector('.ctx-text').textContent =
        `${thousands(context.used)} / ${thousands(context.window)} context · ${percent}%`;
}

// ===== What the agent did, as cards rather than as a log =====
//
// Every tool call rendered as two lines of trace: `→ edit_file(path=…,
// edits=<<<<<<< SEARCH…)` and `← ok`. That is a transcript of the machinery,
// and it has the two properties you least want in the thing you read while
// deciding whether to trust a change: the file that was edited is buried in
// the middle of a line, and the edit itself is truncated at sixty characters.
// Six edits in a row were six identical-looking lines.
//
// The tools worth a card are the ones whose result you would want to check —
// a file changed, a command run. Everything else stays a trace line, because
// a panel where everything is a card is a panel where nothing stands out.
//
// Pairing a card with its result is by order, not by id: the backend runs one
// tool at a time and emits `tool` then `tool_result` around it, so the card
// waiting for a result is always the last one made.

const CARD_TOOLS = new Set([
    'run_command', 'start_server', 'write_file', 'create_file',
    'edit_file', 'delete_file', 'move_file',
]);

// A SEARCH/REPLACE block, as +/- lines. The block format is already a diff —
// this only turns its markers into the colours anyone reading a diff expects.
function parseEditBlocks(text) {
    const lines = String(text || '').split('\n');
    const out = [];
    let side = null;   // 'old' while inside SEARCH, 'new' after the divider
    for (const line of lines) {
        if (/^(<{5,9}|-{5,9}) SEARCH\s*$/.test(line)) { side = 'old'; continue; }
        if (/^={5,9}\s*$/.test(line) && side === 'old') { side = 'new'; continue; }
        if (/^(>{5,9}|\+{5,9}) REPLACE\s*$/.test(line)) { side = null; continue; }
        if (side === 'old') out.push({ kind: 'del', text: line });
        else if (side === 'new') out.push({ kind: 'add', text: line });
        else if (line.trim()) out.push({ kind: 'ctx', text: line });
    }
    return out;
}

function diffLinesFor(tool, args) {
    if (tool === 'edit_file') {
        return parseEditBlocks(args.edits != null ? args.edits : args.diff);
    }
    if (tool === 'write_file' || tool === 'create_file') {
        // A new file is all additions. An overwrite is too, as far as this
        // panel can tell — the old content is not in the event, and inventing
        // a diff against a file nobody sent would be a guess wearing the
        // clothes of a fact.
        return String(args.content || '').split('\n').map(text => ({ kind: 'add', text }));
    }
    if (tool === 'delete_file') return [{ kind: 'del', text: args.path || '' }];
    if (tool === 'move_file') {
        return [{ kind: 'del', text: args.path || '' }, { kind: 'add', text: args.to || '' }];
    }
    return [];
}

const CARD_MAX_DIFF_LINES = 120;

function agentToolCard(wrap, tool) {
    const bare = String(tool.name || '').split('__').pop();
    const args = tool.args || {};
    // A sibling of the answer, never a child of it. `.agent-body` has its
    // innerHTML replaced on every streamed chunk, so a card put inside it
    // survives exactly until the next token arrives — the edits would have
    // vanished one at a time as the model wrote its summary of them.
    const card = document.createElement('div');
    card.className = 'tool-card' + (tool.rejected ? ' rejected' : '');

    if (bare === 'run_command' || bare === 'start_server') {
        card.classList.add('is-command');
        card.innerHTML = `
            <div class="tool-head">
                <span class="tool-sigil mono">$</span>
                <span class="tool-title mono">${escHtml(args.command || '')}</span>
                <span class="spacer"></span>
                <span class="tool-state">running…</span>
            </div>
            <pre class="tool-output hidden"></pre>`;
    } else {
        const lines = diffLinesFor(bare, args);
        const added = lines.filter(l => l.kind === 'add').length;
        const removed = lines.filter(l => l.kind === 'del').length;
        const shown = lines.slice(0, CARD_MAX_DIFF_LINES);
        card.classList.add('is-diff');
        card.innerHTML = `
            <div class="tool-head">
                <span class="tool-verb">${escHtml(bare.replace('_file', ''))}</span>
                <span class="tool-title mono" title="${escHtml(args.path || '')}">${escHtml(args.path || '')}</span>
                ${added ? `<span class="tool-plus">+${added}</span>` : ''}
                ${removed ? `<span class="tool-minus">−${removed}</span>` : ''}
                <span class="spacer"></span>
                <span class="tool-state">${tool.rejected ? 'refused' : 'pending'}</span>
            </div>
            ${shown.length ? `<div class="tool-diff">${shown.map(l =>
                `<div class="dl ${l.kind}">${escHtml(l.text) || '&nbsp;'}</div>`).join('')}${
                lines.length > shown.length
                    ? `<div class="dl more">… ${lines.length - shown.length} more lines</div>` : ''
            }</div>` : ''}`;
    }
    wrap.appendChild(card);
    document.getElementById('agent-log').scrollTop = 1e9;
    return card;
}

// The result, on the card that asked for it. `run_command` answers `[ok]` or
// `[exit N]`; a write answers with prose, so anything that does not start
// with `error:` counts as done.
function agentToolCardResult(card, result) {
    if (!card) return;
    const text = String(result || '');
    const state = card.querySelector('.tool-state');
    const failed = /^\[exit |^error:/.test(text) || text.startsWith('[failed]');
    if (state) {
        state.textContent = failed
            ? (text.match(/^\[exit \d+\]/) || ['Failed'])[0]
            : (card.classList.contains('is-command') ? 'Success' : 'Applied');
        state.classList.add(failed ? 'bad' : 'good');
    }
    const out = card.querySelector('.tool-output');
    if (out) {
        // The status marker is already on the card; repeating it as the first
        // line of the output is noise.
        const body = text.replace(/^\[(ok|exit \d+|running|failed)\]\n?/, '').trim();
        out.textContent = body || '(no output)';
        out.classList.toggle('hidden', !body);
    }
}

// ===== Several investigations running at once =====
//
// One card per named investigation, spun up together and ticking off
// independently. Without them a parallel explore is the worst-looking thing
// in the panel: nothing at all for thirty seconds, then four paragraphs
// arriving as one block, which reads as a hang followed by a wall.
//
// Keyed by name because that is what the events carry and what the user
// sees. Two investigations sharing a name would share a card, which is the
// right failure — they are the same question asked twice.

function agentSubagentCard(wrap, info) {
    let host = wrap.querySelector('.subagent-set');
    if (!host) {
        host = document.createElement('div');
        host.className = 'subagent-set';
        wrap.appendChild(host);
    }
    const key = info.name || 'investigation';
    let card = host.querySelector(`.subagent-card[data-name="${CSS.escape(key)}"]`);
    if (!card) {
        card = document.createElement('div');
        card.className = 'subagent-card';
        card.dataset.name = key;
        card.innerHTML = `
            <div class="subagent-head">
                <span class="subagent-spin"></span>
                <span class="subagent-name">${escHtml(key)}</span>
                <span class="spacer"></span>
                <span class="subagent-state"></span>
            </div>
            <div class="subagent-task">${escHtml(info.task || '')}</div>
            <div class="subagent-step mono"></div>`;
        host.appendChild(card);
    }
    if (info.state) {
        card.classList.toggle('done', info.state === 'done');
        card.classList.toggle('failed', info.state === 'failed');
        card.querySelector('.subagent-state').textContent =
            info.state === 'running' ? '' : (info.state === 'done' ? '✓' : 'failed');
        // The last thing it was doing is worth nothing once it has finished,
        // and leaving it there makes a done card look like a stalled one.
        if (info.state !== 'running') card.querySelector('.subagent-step').textContent = '';
    }
    document.getElementById('agent-log').scrollTop = 1e9;
    return card;
}

function agentSubagentStep(wrap, step) {
    const card = wrap.querySelector(`.subagent-card[data-name="${CSS.escape(step.name || '')}"]`);
    if (!card) return;
    const bare = String(step.tool || '').split('__').pop();
    card.querySelector('.subagent-step').textContent =
        `${bare}${step.detail ? ' ' + step.detail : ''}`;
}

// ===== A server the agent started and left running =====
//
// The one thing a coding agent does whose result is not text. Everything else
// it does can be read in the transcript; "the app is running at
// localhost:5173" is only useful if you can click it, and only honest if you
// can also stop it — a process holding one of your ports with no visible way
// to kill it is worse than no feature.
//
// One card per server, replaced in place, so a restart updates the card
// rather than leaving a trail of dead addresses that all look live.

function agentServerCard(wrap, server) {
    let card = wrap.querySelector(`.server-card[data-server="${server.id}"]`);
    if (!card) {
        card = document.createElement('div');
        card.className = 'server-card';
        card.dataset.server = server.id;
        // Appended to the message, not to its body: the body's innerHTML is
        // rewritten on every streamed chunk, so a card inside it lives until
        // the next token.
        wrap.appendChild(card);
    }
    const running = !!server.running;
    const url = server.url || '';
    card.classList.toggle('stopped', !running);
    card.innerHTML = `
        <div class="server-head">
            <span class="server-dot ${running ? 'live' : 'dead'}"></span>
            <span class="server-label">${escHtml(server.label || server.command || 'server')}</span>
            <span class="spacer"></span>
            ${running
                ? `<button class="btn btn-ghost" onclick="stopAgentServer('${escHtml(server.id)}')">Stop</button>`
                : `<span class="server-exit">exited ${escHtml(String(server.exit_code ?? '?'))}</span>`}
        </div>
        ${url && running
            // Opened in the real browser rather than embedded. A dev server is
            // the user's app, and an iframe inside a panel would break exactly
            // the things they are trying to look at: its own devtools, its own
            // storage origin, and any header it sets to refuse being framed.
            ? `<a class="server-url" href="${escHtml(url)}" target="_blank" rel="noopener">${escHtml(url)}</a>`
            : ''}
        <div class="server-cmd mono">${escHtml(server.command || '')}</div>
        <details class="server-logs">
            <summary>Output</summary>
            <pre class="server-log" id="server-log-${escHtml(server.id)}">loading…</pre>
        </details>`;
    const logs = card.querySelector('.server-logs');
    logs.addEventListener('toggle', () => { if (logs.open) refreshServerLog(server.id); });
    document.getElementById('agent-log').scrollTop = 1e9;
    return card;
}

async function refreshServerLog(id) {
    const host = document.getElementById(`server-log-${id}`);
    if (!host) return;
    try {
        const data = await api(`/api/coder/servers/${id}/logs?lines=200`);
        host.textContent = data.log || '(no output yet)';
    } catch (err) {
        host.textContent = 'could not read the log: ' + err;
    }
}

async function stopAgentServer(id) {
    const card = document.querySelector(`.server-card[data-server="${id}"]`);
    try {
        const stopped = await api(`/api/coder/servers/${id}/stop`, { method: 'POST' });
        if (card) agentServerCard(card.parentElement, stopped);
    } catch (err) {
        // Said on the card itself. A failed stop is specifically the case
        // where the user needs to know the process is still up, and a message
        // anywhere else leaves a card sitting there showing a live dot with
        // no hint that the button did nothing.
        if (card) {
            const head = card.querySelector('.server-head');
            head.insertAdjacentHTML('beforeend',
                `<span class="server-exit">could not stop it: ${escHtml(String(err))}</span>`);
        }
    }
}

// What the agent is looking at right now. Sending the open file with the task
// is the difference between "fix this" working and needing to paste a path.
function agentContext() {
    const parts = [];
    if (typeof activeFilePath !== 'undefined' && activeFilePath) {
        parts.push(`The file currently open is ${activeFilePath}.`);
    }
    return parts.join(' ');
}

function stopAgentTask() {
    if (agentAbort) agentAbort.abort();
}

async function sendAgentTask() {
    const input = document.getElementById('agent-input');
    const task = input.value.trim();
    if ((!task && !agentAttachments.length) || agentAbort) return;
    const attachments = agentAttachments.slice();
    input.value = '';
    agentAttachments = [];
    renderAgentTray();
    agentBubble('you', (agentSkill ? `/${agentSkill.name} ` : '') + task + (attachments.length
        ? `\n[${attachments.map(a => a.name).join(', ')}]` : ''));
    // Armed for one task, like chat. A skill that stayed on would quietly
    // shape every later message in the panel with nothing on screen still
    // saying so by the time it mattered.
    const sentSkill = agentSkill;
    clearAgentSkill();

    const { wrap, body } = agentBubble('agent', '');
    body.innerHTML = '<span class="caret">&nbsp;</span>';
    // What this turn actually did, so it can say so when it stops.
    const touched = new Set();
    let commandsRun = 0;
    // The card waiting for its result. One tool runs at a time, so this is
    // always the last card made — no ids needed, and none are sent.
    let pendingCard = null;
    // Why the turn stopped, if the provider stopped it. The footer reads this
    // rather than announcing "Done" over the top of a rate limit.
    let turnFailed = '';
    const send = document.getElementById('agent-send');
    const stop = document.getElementById('agent-stop');
    send.disabled = true;
    stop.hidden = false;
    agentAbort = new AbortController();

    const context = agentContext();
    const startedAt = Date.now();
    let answer = '';
    try {
        const response = await fetch('/api/chat/stream', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...authHeaders() },
            signal: agentAbort.signal,
            body: JSON.stringify({
                message: (context ? `${task}\n\n(${context})` : task)
                    || 'What is in the attached file?',
                attachments: attachments.map(a => ({ name: a.name, mime: a.mime, data: a.data })),
                conversation_id: agentConversationId,
                // The agent's own pick, falling back to the composer's when
                // it is set to "Same as chat".
                model: agentModel ? agentModel.model
                    : (typeof currentModel !== 'undefined' ? currentModel : null),
                provider: agentModel ? agentModel.provider
                    : (typeof currentProvider !== 'undefined' ? currentProvider : null),
                // Single search, not off. "Off" removes web_search and read_url
                // from the tool list outright, so the agent could not look up
                // an API it did not know or paste an error message into a
                // search — the two things a coding agent most needs the web
                // for. Single rather than multi: it may check a fact, not go
                // researching instead of working.
                search_mode: 'single',
                // A skill armed from the startup panel. Chat has had this
                // since skills existed; the Code tab had no way to invoke one,
                // so a skill about how this project wants tests written could
                // only be used by going to the chat tab to ask about code.
                skill: sentSkill ? sentSkill.slug : null,
                // This is the coding panel, so this turn gets the plan/act
                // preamble and the workspace rules. Ordinary chat does not:
                // one global coder_mode was being applied to every message in
                // the app, so a question about the news arrived dressed as a
                // coding task.
                coder: true,
            }),
        });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);

        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            const frames = buffer.split('\n\n');
            buffer = frames.pop();
            for (const frame of frames) {
                const line = frame.split('\n').find(l => l.startsWith('data: '));
                if (!line) continue;
                let payload;
                try { payload = JSON.parse(line.slice(6)); } catch (_) { continue; }

                if (payload.conversation_id) agentConversationId = payload.conversation_id;
                if (payload.tool) {
                    // A card for the ones whose result you would want to check,
                    // a trace line for the rest. The tool name and its
                    // arguments squeezed onto one truncated line is a fine
                    // record of a search and a useless record of an edit.
                    const bareName = String(payload.tool.name).split('__').pop();
                    if (CARD_TOOLS.has(bareName)) {
                        pendingCard = agentToolCard(wrap, payload.tool);
                    } else {
                        pendingCard = null;
                        agentTrace(wrap, `→ ${payload.tool.name}(${
                            Object.entries(payload.tool.args || {})
                                .map(([k, v]) => `${k}=${String(v).slice(0, 60)}`).join(', ')})`,
                            payload.tool.rejected ? 'rejected' : '');
                    }
                    // What the turn changed, for the summary at the end. A
                    // rejected call did not happen and must not be counted.
                    if (!payload.tool.rejected) {
                        const bare = String(payload.tool.name).split('__').pop();
                        const path = (payload.tool.args || {}).path;
                        if (WRITE_TOOLS.has(bare) && path) touched.add(path);
                        else if (bare === 'run_command') commandsRun++;
                    }
                }
                if (payload.tool_result) {
                    if (pendingCard) {
                        agentToolCardResult(pendingCard, payload.tool_result.result);
                        pendingCard = null;
                    } else {
                        agentTrace(wrap, `← ${String(payload.tool_result.result).slice(0, 300)}`, 'result');
                    }
                }
                // An approval prompt has to be answerable from here, or the
                // agent silently blocks until it times out.
                //
                // It read `payload.approval`, which the server has never sent —
                // the event is `approval_request`, as the chat trace in app.js
                // has always had it. So the card was never built, the panel sat
                // with a spinner on a turn that was waiting on a click nobody
                // could make, and it ended ten minutes later at the timeout.
                if (payload.approval_request) agentApproval(wrap, payload.approval_request);
                // And clear it when it is answered from anywhere — including a
                // timeout, which otherwise leaves live-looking buttons behind.
                // The Code tab has its own card, so its own place to say it.
                if (payload.approval_waiting) agentApprovalWaiting(wrap, payload.approval_waiting);
                if (payload.approval_resolved) {
                    agentApprovalResolved(wrap, payload.approval_resolved);
                }
                if (payload.questions) {
                    agentQuestions(wrap, payload.questions, payload.blocking);
                }
                // A server that is now running. Its own card rather than a
                // line in the trace: it is the only thing in this panel that
                // is still true after the turn ends, and the only one with a
                // control on it.
                // The provider stopped the turn. Rendered here because it was
                // not: chat has shown this since the event existed and this
                // panel ignored it, so a rate limit or a timeout on a coding
                // turn ended with "Done", no error, and no hint that the
                // reason the file was not written was the model never
                // answering. Marked as failed so the footer cannot claim the
                // turn finished.
                if (payload.provider_error) {
                    turnFailed = payload.provider_error.message || 'the provider stopped the turn';
                    agentTrace(wrap, 'provider: ' + turnFailed, 'err');
                }
                // How full the window is, every round. The turn now runs until
                // the context fills rather than to a round count, so this is
                // the only thing on screen that says how much room is left —
                // and it is most useful while there is still enough of it to
                // act on, which is why it is drawn from the first round and
                // not once the turn is already doomed.
                if (payload.context) agentContextMeter(wrap, payload.context);
                if (payload.server) agentServerCard(wrap, payload.server);
                // Four agents working is four cards ticking, not one long
                // pause and then a wall of text.
                if (payload.subagent) agentSubagentCard(wrap, payload.subagent);
                if (payload.subagent_step) agentSubagentStep(wrap, payload.subagent_step);
                // The steps this turn is working to, ticking as the tools
                // actually touch them. Same component as chat and Research,
                // from the same event.
                if (payload.plan && payload.plan.goals) {
                    renderPlan(wrap, payload.plan);
                }
                // An answer that got pushed back is one we intend to replace,
                // and this panel was keeping it. `answer` only ever grew, so
                // the ACT-mode "you pasted a file instead of writing it" nudge
                // — which has been in the backend all along — rendered the
                // rejected file and then the real reply glued underneath it,
                // with nothing on screen to say why there were two. Drop the
                // discarded text and say what sent it back.
                if (payload.gate) {
                    const unmet = (payload.gate.unmet || []).length;
                    agentTrace(wrap, unmet
                        ? `↻ ${unmet} step(s) from the plan not done yet`
                        : '↻ sent back: ' + String(payload.gate.reason).split('\n')[0],
                        'rejected');
                    answer = '';
                    body.innerHTML = '<span class="caret">&nbsp;</span>';
                }
                if (payload.chunk) {
                    answer += payload.chunk;
                    // Through mdToHtml, the same renderer the chat trace uses,
                    // rather than textContent. A plan is mostly headings, lists
                    // and fenced code, and as plain text it arrived as a wall
                    // with literal ### and ``` in it — which reads as the model
                    // formatting badly when it is the panel not rendering.
                    // The questions block is machine-readable scaffolding for
                    // the form below; showing the user raw JSON as well is
                    // worse than not asking.
                    body.innerHTML = mdToHtml(stripQuestions(answer));
                    document.getElementById('agent-log').scrollTop = 1e9;
                }
                if (payload.error) agentTrace(wrap, 'error: ' + payload.error, 'rejected');
            }
        }
    } catch (e) {
        if (e.name !== 'AbortError') body.textContent = 'Failed: ' + e.message;
        else agentTrace(wrap, 'stopped', 'rejected');
    } finally {
        agentAbort = null;
        send.disabled = false;
        stop.hidden = true;
        // "(done)" over an empty answer is the panel agreeing with a turn
        // that never happened. If the provider stopped it, say that instead.
        if (!answer.trim() && body.querySelector('.caret')) {
            body.textContent = turnFailed
                ? 'The model stopped before answering: ' + turnFailed
                : '(done)';
        }
        agentFinished(wrap, touched, commandsRun, Date.now() - startedAt, turnFailed);
        // A coding turn is the kind of work people start and then go and do
        // something else during. Finishing unread costs the same time as being
        // blocked unread does.
        if (typeof notifyWhenLongRunFinishes === 'function') {
            notifyWhenLongRunFinishes(startedAt, 'Carrot finished in the Code tab',
                                      answer.trim().slice(0, 160) || 'The turn is done.');
        }
        // The agent just touched the workspace; the tree and git state are
        // stale the instant it did.
        loadCodeTree();
        loadCoderState();
    }
}

// The clarifying questions at the end of a plan, as a form.
//
// As prose they were a dead end: answering meant retyping the request with the
// answers folded in, so most of the time nobody did, and Act guessed. Skip is
// a real answer, not silence — it takes the model's first option for each,
// which is what "just pick something sensible" means.
// Display-side twin of strip_questions in coder.py, and see that for why it
// does not match on fences: three live runs wrapped the block three different
// ways. Find the marker, find the array after it, cut from the marker's line
// to the end of that array plus any fence it was sitting in.
const QUESTIONS_MARKER = /carrot-questions/i;

function stripQuestions(text) {
    const marker = QUESTIONS_MARKER.exec(text || '');
    if (!marker) return text || '';

    const open = text.indexOf('[', marker.index + marker[0].length);
    if (open < 0) return text;

    let depth = 0, inString = false, escaped = false, end = -1;
    for (let i = open; i < text.length; i++) {
        const c = text[i];
        if (inString) {
            if (escaped) escaped = false;
            else if (c === '\\') escaped = true;
            else if (c === '"') inString = false;
            continue;
        }
        if (c === '"') inString = true;
        else if (c === '[') depth++;
        else if (c === ']' && --depth === 0) { end = i + 1; break; }
    }
    if (end < 0) end = text.length;          // reply stopped mid-block

    let head = text.slice(0, text.lastIndexOf('\n', marker.index) + 1);
    head = head.replace(/(?:^|\n)[ \t]*```[a-zA-Z]*[ \t]*\n?$/, '\n');
    const tail = text.slice(end).replace(/^\s*```[a-zA-Z]*\s*/, '');
    return (head.trimEnd() + '\n' + tail.trimStart()).trim();
}

function agentQuestions(wrap, questions, blocking) {
    if (!questions || !questions.length) return;
    if (wrap.querySelector('.agent-questions')) return;   // one form per turn

    const box = document.createElement('div');
    // `blocking` says the model asked before it planned, so there is nothing
    // above the form to read and the form has to look like the turn rather
    // than like an appendix to it. The server decides this, because it is the
    // only place that saw where the question fell in the reply.
    box.className = 'agent-questions' + (blocking ? ' blocking' : '');
    const head = document.createElement('div');
    head.className = 'questions-head';
    head.textContent = blocking
        ? 'Waiting on you — answer what matters, skip the rest.'
        : 'Before it starts — answer what matters, skip the rest.';
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
    go.textContent = 'Build it';
    go.onclick = () => submitAgentQuestions(box, questions, chosen);

    const skip = document.createElement('button');
    skip.className = 'btn btn-ghost';
    skip.textContent = 'Skip — just pick sensible defaults';
    skip.onclick = () => {
        questions.forEach((q, i) => chosen.set(i, q.options[0]));
        submitAgentQuestions(box, questions, chosen);
    };

    actions.append(go, skip);
    box.appendChild(actions);
    wrap.appendChild(box);
    document.getElementById('agent-log').scrollTop = 1e9;
}

async function submitAgentQuestions(box, questions, chosen) {
    box.querySelectorAll('button, input').forEach(el => { el.disabled = true; });
    const pairs = questions.map((q, i) => ({ question: q.question, answer: chosen.get(i) || '' }));
    const summary = pairs.filter(p => p.answer)
        .map(p => `${p.question} — ${p.answer}`).join('; ');
    box.querySelector('.questions-head').textContent = summary || 'Using the defaults.';

    // Answering a plan is the moment Act was waiting for, so the mode switch
    // happens here rather than leaving the user to find the button. It runs
    // first: the answers are the follow-up turn, and they have to arrive with
    // the write tools already available or the model just re-plans.
    await setCoderMode('act');
    const input = document.getElementById('agent-input');
    input.value = 'Answers to your questions:\n'
        + pairs.filter(p => p.answer).map(p => `- ${p.question} — ${p.answer}`).join('\n')
        + '\n\nGo ahead on that basis.';
    sendAgentTask();
}

// The body of an approval, per tool: what you would need to read to decide.
// Long content is cut rather than paged — the point is to see what it is, and
// nobody audits nine thousand characters in a card. The head is where the
// surprises are.
const APPROVAL_PREVIEW_CHARS = 4000;

function approvalPreview(request) {
    const args = request.arguments || {};
    const clip = (text) => {
        const body = String(text);
        return body.length > APPROVAL_PREVIEW_CHARS
            ? body.slice(0, APPROVAL_PREVIEW_CHARS)
              + `\n\n… ${body.length - APPROVAL_PREVIEW_CHARS} more characters`
            : body;
    };
    switch (request.tool) {
        case 'write_file':
        case 'create_file':
            if (args.content == null) return null;
            return { label: `Show what goes into ${args.path || 'the file'}`,
                     body: clip(args.content) };
        case 'edit_file':
            // Already a search/replace block, which reads as a diff as-is.
            if (args.edits == null && args.diff == null) return null;
            return { label: `Show the edit to ${args.path || 'the file'}`,
                     body: clip(args.edits != null ? args.edits : args.diff) };
        case 'run_command':
            if (!args.command) return null;
            return { label: 'Show the full command', body: String(args.command) };
        case 'move_file':
            if (!args.path) return null;
            return { label: 'Show the move', body: `${args.path}\n  ->  ${args.to || '?'}` };
        case 'delete_file':
            if (!args.path) return null;
            return { label: 'Show what would be deleted', body: String(args.path) };
        default: {
            // Anything else — packs, MCP servers — still beats a bare summary.
            const keys = Object.keys(args);
            if (!keys.length) return null;
            return { label: 'Show the arguments', body: clip(JSON.stringify(args, null, 2)) };
        }
    }
}

function agentApproval(wrap, request) {
    const box = document.createElement('div');
    box.className = 'agent-approval';
    box.dataset.approvalId = request.id || '';
    box.innerHTML = `<div class="approval-what">${escHtml(request.summary || request.tool || 'Allow this?')}</div>`;

    // What it is actually about to do, not just how many characters of it.
    //
    // "Write 4145 characters to magnetic_field_simulator.py" is not something
    // anyone can meaningfully agree to, so seven of them in a row get seven
    // reflex clicks and the gate stops being a gate. The content is already in
    // the request — it was being thrown away at the point of asking.
    const preview = approvalPreview(request);
    if (preview) {
        const details = document.createElement('details');
        details.className = 'approval-preview';
        const summary = document.createElement('summary');
        summary.textContent = preview.label;
        const pre = document.createElement('pre');
        pre.textContent = preview.body;
        details.append(summary, pre);
        box.appendChild(details);
    }

    const row = document.createElement('div');
    row.className = 'approval-row';
    for (const [label, allow] of [['Allow', true], ['Deny', false]]) {
        const button = document.createElement('button');
        button.className = allow ? 'btn btn-primary' : 'btn btn-ghost';
        button.textContent = label;
        button.onclick = async () => {
            box.querySelectorAll('button').forEach(b => { b.disabled = true; });
            try {
                // The endpoint takes {decision: "allow"|"deny"}. This sent
                // {approved: true}, which has no `decision` at all and is
                // rejected before it reaches the gate — so the one button that
                // could have unblocked the turn could not have worked either.
                await api(`/api/agent/approvals/${encodeURIComponent(request.id)}`, {
                    method: 'POST',
                    body: JSON.stringify({ decision: allow ? 'allow' : 'deny' }),
                });
                // Both this and the approval_resolved event annotate the card,
                // and either can land first, so the wording is applied through
                // one idempotent helper rather than appended twice.
                annotateApproval(box, allow ? 'allow' : 'deny');
            } catch (e) {
                box.querySelector('.approval-what').textContent = 'Could not answer: ' + e.message;
            }
        };
        row.appendChild(button);
    }
    box.appendChild(row);
    wrap.appendChild(box);
    document.getElementById('agent-log').scrollTop = 1e9;

    // The Code tab renders its own approval card rather than reusing the
    // chat one, which meant the desktop notification — attached to the other
    // renderer — never fired here. A coding turn blocks in exactly the same
    // way and is if anything more likely to be left running while you do
    // something else, so it is the case that needed it most.
    if (typeof alertAwayFromScreen === 'function') alertAwayFromScreen(request);
}

// Says how the prompt ended, once. The button that answers it and the
// approval_resolved event that confirms it both arrive, in either order, so
// this has to be safe to call twice — it read "— allowed — allowed" when it
// was not.
function annotateApproval(box, decision) {
    if (!box || box.dataset.answered) return;
    box.dataset.answered = decision;
    box.querySelectorAll('button').forEach(b => { b.disabled = true; });
    const what = box.querySelector('.approval-what');
    if (!what) return;
    const said = { allow: 'allowed', deny: 'denied', timeout: 'not answered' };
    what.textContent += ` — ${said[decision] || decision}`;
}

// Said on the card itself, every ten seconds, for as long as the turn is
// blocked. A turn that is waiting and a turn that has died produced the same
// picture — an approval card and no further output — and the second one is
// what people reported. Now only one of them keeps moving.
function agentApprovalWaiting(wrap, waiting) {
    const box = wrap.querySelector(
        `.agent-approval[data-approval-id="${CSS.escape(waiting.id || '')}"]`);
    if (!box || box.dataset.answered) return;
    let line = box.querySelector('.approval-waiting');
    if (!line) {
        line = document.createElement('div');
        line.className = 'approval-waiting';
        box.appendChild(line);
    }
    const left = Math.round((waiting.seconds_left || 0) / 60);
    line.textContent = `Waiting for you — ${waiting.seconds}s so far`
        + (left ? `, giving up in about ${left} min` : '');
}

function agentApprovalResolved(wrap, resolved) {
    const box = wrap.querySelector(`.agent-approval[data-approval-id="${CSS.escape(resolved.id || '')}"]`);
    annotateApproval(box, resolved.decision);
}

// Paste a screenshot straight into the agent, and drop files on the panel.
// Handing an agent a picture of the error is how people actually work.
document.addEventListener('paste', (event) => {
    const side = document.getElementById('agent-side');
    if (!side || side.classList.contains('hidden')) return;
    if (!side.contains(document.activeElement)) return;
    const files = Array.from(event.clipboardData?.files || []);
    if (files.length) {
        event.preventDefault();
        addAgentAttachments(files);
    }
});

document.addEventListener('DOMContentLoaded', () => {
    const side = document.getElementById('agent-side');
    if (!side) return;
    side.addEventListener('dragover', (e) => { e.preventDefault(); side.classList.add('dropping'); });
    side.addEventListener('dragleave', () => side.classList.remove('dropping'));
    side.addEventListener('drop', (e) => {
        e.preventDefault();
        side.classList.remove('dropping');
        addAgentAttachments(e.dataTransfer?.files);
    });
});


// The status word above the editor is a pointer, not a control: clicking it
// opens the agent panel, which is where the one Plan/Act switch lives.
function revealAgentMode() {
    const side = document.getElementById('agent-side');
    if (side && side.classList.contains('hidden') && typeof toggleAgentSide === 'function') {
        toggleAgentSide();
    }
    const target = document.getElementById('mode-plan');
    if (!target) return;
    target.parentElement.classList.add('flash');
    setTimeout(() => target.parentElement.classList.remove('flash'), 1200);
}


// ---------- The agent's own model ----------
//
// It used to take whatever the chat composer happened to be set to, with
// nothing on screen saying which model that was. The coding agent is the one
// place the choice matters most — a 4B local model and a frontier model are
// not interchangeable at editing a file — and it was the one place you could
// neither see nor make it.

let agentModel = null;      // {provider, model}, or null to follow the composer

function agentModelKey(entry) {
    return `${entry.provider || 'ollama'}::${entry.model}`;
}

async function loadAgentModelPicker() {
    const select = document.getElementById('agent-model');
    if (!select) return;
    if (typeof loadAvailableModels === 'function' && !(availableModels || []).length) {
        await loadAvailableModels();
    }
    const groups = {};
    for (const entry of (typeof availableModels !== 'undefined' ? availableModels : [])) {
        (groups[entry.group] = groups[entry.group] || []).push(entry);
    }
    const saved = localStorage.getItem('carrot-agent-model') || '';
    // "Same as chat" stays the default, because that is what it did before and
    // silently changing which model runs someone's agent is not an upgrade.
    let html = '<option value="">Same as chat</option>';
    for (const [label, entries] of Object.entries(groups)) {
        html += `<optgroup label="${escHtml(label)}">`;
        for (const entry of entries) {
            const key = agentModelKey(entry);
            html += `<option value="${escHtml(key)}"${key === saved ? ' selected' : ''}>`
                  + `${escHtml(entry.model)}</option>`;
        }
        html += '</optgroup>';
    }
    select.innerHTML = html;
    applyAgentModel(saved);
}

function setAgentModel(key) {
    if (key) localStorage.setItem('carrot-agent-model', key);
    else localStorage.removeItem('carrot-agent-model');
    applyAgentModel(key);
}

function applyAgentModel(key) {
    if (!key) { agentModel = null; return; }
    const [provider, ...rest] = key.split('::');
    agentModel = { provider, model: rest.join('::') };
}
