// ================================================================
// Canvas — Excalidraw, with a navigator
// ================================================================
//
// This was a hand-rolled box mover: rectangles you could drag, name and join
// with straight lines. It worked, and it was never going to be good — a real
// canvas is freehand, arrows that stay attached, images, text at any angle,
// selection, grouping, undo that survives all of it. That is a library.
//
// Excalidraw (MIT) is that library. tldraw is the better SDK and could not be
// used: its licence permits development environments only, requires a paid
// business licence to ship, and enforces a watermark — terms Carrot cannot
// meet by being distributed software.
//
// What is kept is the navigator, because it is the part that is not in any
// library: on a plane with no edges, the way back to something is its name.
// Excalidraw's frames have names, so the navigator lists frames, and falls
// back to text on the canvas when a document has none.

let canvasDoc = null;        // {id, title}
let canvasHandle = null;     // the mounted editor
let canvasSaveTimer = null;
let canvasSceneVersion = null;

async function ensureCanvasLib() {
    if (window.CarrotCanvas) return window.CarrotCanvas;
    if (!_vendorLoaded.excalidraw) {
        _loadCss('/vendor/excalidraw.css');
        _vendorLoaded.excalidraw = _loadScript('/vendor/excalidraw.js');
    }
    await _vendorLoaded.excalidraw;
    return window.CarrotCanvas;
}

function blankCanvas() { return { elements: [], appState: {} }; }

// Read a canvas document body.
//
// Two formats exist: Excalidraw scenes, and the `{nodes, edges}` the old
// hand-rolled canvas wrote. The second is converted rather than discarded —
// somebody's boxes are their work, and "we changed the editor" is not a reason
// to lose them.
function parseCanvasBody(body) {
    if (!body || !body.trim()) return blankCanvas();
    const data = JSON.parse(body);
    if (Array.isArray(data.elements)) {
        return { elements: data.elements, appState: data.appState || {}, files: data.files || {} };
    }
    if (Array.isArray(data.nodes)) return convertLegacyCanvas(data);
    return blankCanvas();
}

// Old boxes become a rectangle with its title as bound text, which is the
// nearest thing Excalidraw has to what they were.
function convertLegacyCanvas(data) {
    const elements = [];
    const base = (id, extra) => ({
        id, version: 1, versionNonce: 1, isDeleted: false, seed: 1,
        strokeColor: '#1e1e1e', backgroundColor: 'transparent', fillStyle: 'solid',
        strokeWidth: 1, strokeStyle: 'solid', roughness: 1, opacity: 100,
        angle: 0, groupIds: [], frameId: null, roundness: null, boundElements: [],
        updated: Date.now(), link: null, locked: false, ...extra,
    });
    for (const node of data.nodes || []) {
        const w = node.w || 220, h = node.h || 120;
        elements.push(base(node.id, { type: 'rectangle', x: node.x || 0, y: node.y || 0,
                                      width: w, height: h, roundness: { type: 3 } }));
        const label = [node.title, node.text].filter(Boolean).join('\n');
        if (label) {
            elements.push(base(node.id + '-t', {
                type: 'text', x: (node.x || 0) + 12, y: (node.y || 0) + 12,
                width: w - 24, height: 24, text: label, originalText: label,
                fontSize: 16, fontFamily: 1, textAlign: 'left', verticalAlign: 'top',
                containerId: null, lineHeight: 1.25,
            }));
        }
    }
    return { elements, appState: {}, files: {} };
}

async function openCanvasDoc(note) {
    let scene;
    try {
        scene = parseCanvasBody(note.body || '');
    } catch (_) {
        alert('This canvas file could not be read, so it has not been opened — '
            + 'nothing has been changed. Its contents are still on disk.');
        return;
    }

    const lib = await ensureCanvasLib();
    if (!lib) { alert('The canvas editor could not be loaded.'); return; }

    canvasDoc = { id: note.id, title: note.title || 'Untitled canvas' };
    showWriteMode('canvas');
    document.getElementById('canvas-title').value = canvasDoc.title;
    if (typeof currentNoteId !== 'undefined') currentNoteId = note.id;

    const host = document.getElementById('canvas-stage');
    if (canvasHandle) { try { canvasHandle.destroy(); } catch (_) {} canvasHandle = null; }
    host.innerHTML = '';

    canvasSceneVersion = null;
    canvasHandle = lib.mount(host, {
        initialData: { elements: scene.elements, appState: scene.appState, files: scene.files,
                       scrollToContent: true },
        theme: document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark',
        onChange: ({ elements }) => {
            // onChange fires on pointer moves and selection too. Saving on
            // every one of those would be a request a second for nothing; the
            // scene version only moves when the drawing actually changes.
            const version = canvasHandle.sceneVersion(elements);
            if (version === canvasSceneVersion) return;
            canvasSceneVersion = version;
            renderCanvasNavigator();
            scheduleCanvasSave();
        },
    });
    renderCanvasNavigator();
}

// ================================================================
// The navigator — the point of the whole thing
// ================================================================
//
// Frames are what Excalidraw calls a named region, so a frame is a navigator
// entry. A canvas with no frames falls back to its text, because somebody who
// has written "Lecture 1" on the page has named that part of it whether or not
// they knew about frames.
function canvasNavItems() {
    const api = canvasHandle && canvasHandle.api;
    if (!api) return [];
    const elements = api.getSceneElements().filter(el => !el.isDeleted);
    const frames = elements
        .filter(el => el.type === 'frame' || el.type === 'magicframe')
        .map(el => ({ id: el.id, name: (el.name || '').trim() || 'Untitled frame', kind: 'frame' }));
    if (frames.length) return frames;
    return elements
        .filter(el => el.type === 'text' && (el.text || '').trim())
        .map(el => ({ id: el.id, name: el.text.trim().split('\n')[0].slice(0, 40), kind: 'text' }));
}

function renderCanvasNavigator() {
    const list = document.getElementById('canvas-nav-list');
    if (!list) return;
    const filter = (document.getElementById('canvas-nav-filter')?.value || '').toLowerCase();
    const all = canvasNavItems();
    const items = all.filter(n => !filter || n.name.toLowerCase().includes(filter));

    if (!all.length) {
        list.innerHTML = '<div class="canvas-nav-empty">Nothing named yet. Add a frame, '
                       + 'or write on the canvas, and it appears here.</div>';
        return;
    }
    if (!items.length) {
        list.innerHTML = '<div class="canvas-nav-empty">Nothing matches.</div>';
        return;
    }
    list.innerHTML = items.map(n => `
        <div class="canvas-nav-item" onclick="flyToCanvasBox('${escHtml(n.id)}')">
          <span class="canvas-nav-dot"></span>
          <span class="canvas-nav-name">${escHtml(n.name)}</span>
        </div>`).join('');
}

function filterCanvasNav() { renderCanvasNavigator(); }

// Fly the viewport to something by name. Animated and framed by the library,
// which knows where everything is far better than an offset computed here.
function flyToCanvasBox(elementId) {
    const api = canvasHandle && canvasHandle.api;
    if (!api) return;
    const target = api.getSceneElements().find(el => el.id === elementId);
    if (!target) return;
    api.scrollToContent(target, { fitToContent: true, animate: true, duration: 300 });
}

function fitCanvasToContent() {
    const api = canvasHandle && canvasHandle.api;
    if (!api) return;
    api.scrollToContent(api.getSceneElements(), { fitToContent: true, animate: true });
}

// ================================================================
// Saving
// ================================================================
function scheduleCanvasSave() {
    clearTimeout(canvasSaveTimer);
    setCanvasStatus('editing…');
    canvasSaveTimer = setTimeout(saveCanvasNow, 900);
}

async function saveCanvasNow() {
    if (!canvasDoc || !canvasHandle || !canvasHandle.api) return;
    const api = canvasHandle.api;
    const title = document.getElementById('canvas-title').value.trim() || 'Untitled canvas';
    const appState = api.getAppState();
    const body = JSON.stringify({
        type: 'excalidraw',
        version: 2,
        elements: api.getSceneElements(),
        // Only what should come back on reopening. The whole appState carries
        // cursor position, selection and which tool was last held, none of
        // which is the document.
        appState: { viewBackgroundColor: appState.viewBackgroundColor, gridSize: appState.gridSize },
        files: api.getFiles(),
    });
    try {
        await api_put(`/api/notes/${canvasDoc.id}`, { content: body, title });
        canvasDoc.title = title;
        setCanvasStatus('saved');
        if (typeof loadNotes === 'function') loadNotes();
    } catch (_) {
        setCanvasStatus('save failed');
    }
}

// `api` is Excalidraw's instance inside this file, so the app's own fetch
// helper needs a name that does not collide with it.
function api_put(url, body) {
    return window.api(url, { method: 'PUT', body: JSON.stringify(body) });
}

function setCanvasStatus(msg) {
    const el = document.getElementById('canvas-status');
    if (el) el.textContent = msg;
}

async function newCanvasDoc() {
    const created = await window.api('/api/notes', {
        method: 'POST',
        body: JSON.stringify({ title: 'Untitled canvas', content: JSON.stringify(blankCanvas()),
                               format: 'canvas' }),
    });
    await loadNotes();
    openNote(created.id);
}
