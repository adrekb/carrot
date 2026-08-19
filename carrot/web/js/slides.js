// ================================================================
// Slides — a deck you edit by pointing at it
// ================================================================
//
// This was Markdown with `---` between slides, which is a fine way to write a
// document and a poor way to make a deck: you cannot put a thing where you
// want it, and you cannot say what it should look like when it gets there.
//
// No library does the editing half. DeckDeckGo, reveal.js and Impress are all
// presenters — they render a deck somebody else authored, which is the part
// already solved. So the editor is ours and the presenting stays reveal.js,
// which is vendored and good at exactly that.
//
// The format is JSON: slides of absolutely-placed elements on a 1280x720
// stage. Everything is stored in stage coordinates and scaled at the last
// moment, so one deck is correct in the editor, in the thumbnails, on a
// projector and in an export.

const SLIDE_W = 1280;
const SLIDE_H = 720;

// Offline, so these are families that exist on the machine plus the app's own.
const SLIDE_FONTS = [
    ['var(--sans)', 'Carrot Sans'],
    ['var(--mono)', 'Carrot Mono'],
    ['Georgia, serif', 'Georgia'],
    ['"Times New Roman", serif', 'Times'],
    ['Arial, Helvetica, sans-serif', 'Arial'],
    ['"Courier New", monospace', 'Courier'],
];

// Named so a deck stays readable when the accent changes, with a spread that
// works on both light and dark slides.
const SLIDE_COLORS = [
    ['var(--text)', 'Ink'],
    ['var(--muted)', 'Muted'],
    ['var(--accent)', 'Accent'],
    ['#e5484d', 'Red'],
    ['#f5a623', 'Amber'],
    ['#30a46c', 'Green'],
    ['#0091ff', 'Blue'],
    ['#8e4ec6', 'Purple'],
];

let slidesDoc = null;
let slidesActive = 0;
// The element the format bar is pointed at. Still a single id, because every
// control that sets a font or a colour acts on one thing.
let slidesSelected = null;
// Everything currently picked, which is the same thing when one is picked and
// the reason align and distribute can exist when more are. Kept as a set
// beside `slidesSelected` rather than replacing it, so nothing that already
// reads the single selection has to change.
let slidesPicked = new Set();
let slidesSaveTimer = null;
let slidesDrag = null;
let revealInstance = null;

function newId(prefix) { return prefix + Math.random().toString(36).slice(2, 9); }

function blankSlide() {
    return { id: newId('s'), background: '', elements: [], notes: '' };
}

function makeElement(type, opts = {}) {
    return {
        id: newId('e'),
        type,
        x: opts.x ?? 160, y: opts.y ?? 160,
        w: opts.w ?? 480, h: opts.h ?? 120,
        text: opts.text ?? '',
        font: opts.font ?? 'var(--sans)',
        size: opts.size ?? 32,
        color: opts.color ?? 'var(--text)',
        fill: opts.fill ?? 'transparent',
        align: opts.align ?? 'left',
        bold: !!opts.bold,
        italic: !!opts.italic,
    };
}

function titleSlide() {
    const slide = blankSlide();
    slide.elements = [
        makeElement('text', { text: 'Your title', x: 120, y: 250, w: 1040, h: 130,
                              size: 72, align: 'center', bold: true }),
        makeElement('text', { text: 'A subtitle, maybe', x: 120, y: 390, w: 1040, h: 70,
                              size: 30, align: 'center', color: 'var(--muted)' }),
    ];
    return slide;
}

// ================================================================
// Reading a deck
// ================================================================
// The markdown a deck was converted from, kept for exactly as long as it takes
// to write it back into the saved file.
//
// Opening an old deck converts it, and the first autosave — 700ms after any
// edit, including none you meant to make — writes JSON over the markdown. The
// conversion is lossy: reveal's vertical stacks (`--`), fragments and inline
// formatting have nowhere to go in a stage of positioned boxes. Overwriting
// somebody's source with a lossy read of it and no copy is not a migration, it
// is losing their file, so the original rides along in the saved JSON.
let slidesConvertedFrom = '';

function parseDeck(body) {
    slidesConvertedFrom = '';
    if (!body || !body.trim()) return [titleSlide()];
    try {
        const data = JSON.parse(body);
        if (Array.isArray(data.slides) && data.slides.length) {
            // Already converted once: carry the original forward rather than
            // dropping it on the second save.
            slidesConvertedFrom = data.converted_from_markdown || '';
            return data.slides;
        }
        return [titleSlide()];
    } catch (_) {
        slidesConvertedFrom = body;
        return convertMarkdownDeck(body);
    }
}

// Markdown decks predate this editor and are converted rather than refused —
// one slide per `---`, the first heading a title and the rest a body, which is
// what the markdown meant.
function convertMarkdownDeck(source) {
    const out = [];
    let current = [], notes = [], inNotes = false, inFence = false;
    const flush = () => {
        const text = current.join('\n').trim();
        const slide = blankSlide();
        slide.notes = notes.join('\n').trim();
        if (text) {
            const lines = text.split('\n');
            const at = lines.findIndex(l => /^#{1,6}\s+/.test(l));
            const heading = at >= 0 ? lines[at].replace(/^#{1,6}\s+/, '') : '';
            const rest = lines.filter((_, i) => i !== at).join('\n').trim();
            if (heading) {
                slide.elements.push(makeElement('text', { text: heading, x: 96, y: 96,
                                                          w: 1088, h: 120, size: 56, bold: true }));
            }
            if (rest) {
                slide.elements.push(makeElement('text', { text: rest, x: 96, y: heading ? 250 : 96,
                                                          w: 1088, h: 380, size: 30 }));
            }
        }
        out.push(slide);
        current = []; notes = []; inNotes = false;
    };
    for (const line of source.split('\n')) {
        if (/^\s*```/.test(line)) inFence = !inFence;
        if (!inFence && /^---\s*$/.test(line)) { flush(); continue; }
        if (!inFence && /^\s*Notes?:\s*$/i.test(line)) { inNotes = true; continue; }
        (inNotes ? notes : current).push(line);
    }
    flush();
    const kept = out.filter(s => s.elements.length || s.notes);
    return kept.length ? kept : [titleSlide()];
}


// ================================================================
// Undo
// ================================================================
//
// Prose has Milkdown's history, LaTeX has the textarea's, and the canvas has
// Excalidraw's. Slides had none: this editor mutates the deck object directly,
// so every drag, delete and colour change was permanent. A deck is small
// enough that whole-state snapshots are simpler and more reliable than a diff
// log — there is no action whose inverse has to be worked out, because the
// previous state *is* the inverse.
const SLIDES_HISTORY_MAX = 60;
let slidesPast = [];
let slidesFuture = [];
let slidesLastPush = 0;

function snapshotSlides() {
    return JSON.stringify({ slides: slidesDoc.slides, active: slidesActive });
}

// `coalesce` is for the changes that arrive in a stream — typing, or dragging
// a slider. Without it every keystroke is its own undo step and getting back
// past a sentence takes forty of them.
function pushSlidesHistory({ coalesce = false } = {}) {
    if (!slidesDoc) return;
    const now = Date.now();
    if (coalesce && now - slidesLastPush < 600) return;
    slidesLastPush = now;
    slidesPast.push(snapshotSlides());
    if (slidesPast.length > SLIDES_HISTORY_MAX) slidesPast.shift();
    // A new edit is a new branch: whatever was undone is no longer reachable.
    slidesFuture = [];
}

function applySlidesSnapshot(json) {
    const state = JSON.parse(json);
    slidesDoc.slides = state.slides;
    slidesActive = Math.min(state.active || 0, slidesDoc.slides.length - 1);
    pickOnly(null);
    renderSlideStage();
    renderSlidesFilm();
    renderSlideFormatBar();
    scheduleSlidesSave();
}

function undoSlides() {
    if (!slidesDoc || !slidesPast.length) return;
    slidesFuture.push(snapshotSlides());
    applySlidesSnapshot(slidesPast.pop());
    setSlidesStatus('undone');
}

function redoSlides() {
    if (!slidesDoc || !slidesFuture.length) return;
    slidesPast.push(snapshotSlides());
    applySlidesSnapshot(slidesFuture.pop());
    setSlidesStatus('redone');
}

async function openSlidesDoc(note) {
    slidesDoc = { id: note.id, title: note.title || 'Untitled deck',
                  slides: parseDeck(note.body || '') };
    slidesActive = 0;
    pickOnly(null);
    slidesPast = [];
    slidesFuture = [];
    showWriteMode('slides');
    document.getElementById('slides-title').value = slidesDoc.title;
    if (typeof currentNoteId !== 'undefined') currentNoteId = note.id;
    renderSlideStage();
    renderSlidesFilm();
    renderSlideFormatBar();
    bindSlidesEvents();
    bindSlideReorder();
}

// ================================================================
// The stage
// ================================================================
function slideScale() {
    const host = document.getElementById('slides-preview');
    if (!host) return 1;
    const box = host.getBoundingClientRect();
    const pad = 40;
    return Math.max(0.1, Math.min((box.width - pad) / SLIDE_W, (box.height - pad) / SLIDE_H));
}

// The shapes, as clip paths.
//
// Not an SVG each: a clipped div takes a background, and a background can be a
// gradient — which is the thing that was actually missing. The outline is a
// second clipped div behind the first, inset by the stroke width, because a
// border on a clipped element is clipped away with everything else.
// The shape library.
//
// Eleven shapes is enough to draw a box and an arrow and nothing else — you
// reach for a cylinder for a database, a callout for a remark, a left arrow
// for a flow that goes back, and there is a rectangle. This is the set a deck
// actually needs, grouped the way every slide editor groups it, because forty
// shapes in one flat grid is a worse eleven.
//
// All `clip-path` polygons, which is what makes the size affordable: no SVG
// assets, no export path to teach, they scale to any box, and they already
// work in the thumbnails and the presentation because those are the same DOM.
// `radius` is for the two that are rounded rather than clipped, and `line` is
// the one that is neither.
const SLIDE_SHAPE_GROUPS = ['Shapes', 'Arrows', 'Callouts'];

const SLIDE_SHAPES = {
    // ---- Shapes ----
    rect:      { label: 'Rectangle',  group: 'Shapes', clip: '' },
    rounded:   { label: 'Rounded',    group: 'Shapes', clip: '', radius: 18, swatch: '5px' },
    pill:      { label: 'Pill',       group: 'Shapes', clip: '', radius: 999, swatch: '999px' },
    ellipse:   { label: 'Ellipse',    group: 'Shapes', clip: 'ellipse(50% 50%)' },
    triangle:  { label: 'Triangle',   group: 'Shapes', clip: 'polygon(50% 0%, 100% 100%, 0% 100%)' },
    rtriangle: { label: 'Right triangle', group: 'Shapes', clip: 'polygon(0% 0%, 0% 100%, 100% 100%)' },
    parallel:  { label: 'Parallelogram', group: 'Shapes', clip: 'polygon(22% 0%, 100% 0%, 78% 100%, 0% 100%)' },
    trapezoid: { label: 'Trapezoid',  group: 'Shapes', clip: 'polygon(22% 0%, 78% 0%, 100% 100%, 0% 100%)' },
    diamond:   { label: 'Diamond',    group: 'Shapes', clip: 'polygon(50% 0%, 100% 50%, 50% 100%, 0% 50%)' },
    pentagon:  { label: 'Pentagon',   group: 'Shapes', clip: 'polygon(50% 0%, 100% 38%, 82% 100%, 18% 100%, 0% 38%)' },
    hexagon:   { label: 'Hexagon',    group: 'Shapes', clip: 'polygon(25% 0%, 75% 0%, 100% 50%, 75% 100%, 25% 100%, 0% 50%)' },
    octagon:   { label: 'Octagon',    group: 'Shapes', clip: 'polygon(30% 0%, 70% 0%, 100% 30%, 100% 70%, 70% 100%, 30% 100%, 0% 70%, 0% 30%)' },
    star:      { label: 'Star',       group: 'Shapes', clip: 'polygon(50% 0%, 61% 35%, 98% 35%, 68% 57%, 79% 91%, 50% 70%, 21% 91%, 32% 57%, 2% 35%, 39% 35%)' },
    star4:     { label: 'Four-point star', group: 'Shapes', clip: 'polygon(50% 0%, 62% 38%, 100% 50%, 62% 62%, 50% 100%, 38% 62%, 0% 50%, 38% 38%)' },
    cross:     { label: 'Cross',      group: 'Shapes', clip: 'polygon(33% 0%, 67% 0%, 67% 33%, 100% 33%, 100% 67%, 67% 67%, 67% 100%, 33% 100%, 33% 67%, 0% 67%, 0% 33%, 33% 33%)' },
    chevron:   { label: 'Chevron',    group: 'Shapes', clip: 'polygon(0% 0%, 72% 0%, 100% 50%, 72% 100%, 0% 100%, 28% 50%)' },
    // A database, a step, a corner. The three that turn up in every diagram
    // and cannot be faked with a rectangle.
    cylinder:  { label: 'Cylinder',   group: 'Shapes', clip: 'polygon(0% 12%, 8% 4%, 25% 0%, 50% 0%, 75% 0%, 92% 4%, 100% 12%, 100% 88%, 92% 96%, 75% 100%, 50% 100%, 25% 100%, 8% 96%, 0% 88%)' },
    step:      { label: 'Step',       group: 'Shapes', clip: 'polygon(0% 0%, 50% 0%, 50% 50%, 100% 50%, 100% 100%, 0% 100%)' },
    corner:    { label: 'L-shape',    group: 'Shapes', clip: 'polygon(0% 0%, 38% 0%, 38% 62%, 100% 62%, 100% 100%, 0% 100%)' },
    heart:     { label: 'Heart',      group: 'Shapes', clip: 'polygon(50% 100%, 15% 65%, 0% 35%, 12% 8%, 32% 4%, 50% 22%, 68% 4%, 88% 8%, 100% 35%, 85% 65%)' },
    line:      { label: 'Line',       group: 'Shapes', clip: '' },

    // ---- Arrows ----
    arrow:      { label: 'Right arrow', group: 'Arrows', clip: 'polygon(0% 30%, 60% 30%, 60% 0%, 100% 50%, 60% 100%, 60% 70%, 0% 70%)' },
    arrowleft:  { label: 'Left arrow',  group: 'Arrows', clip: 'polygon(100% 30%, 40% 30%, 40% 0%, 0% 50%, 40% 100%, 40% 70%, 100% 70%)' },
    arrowup:    { label: 'Up arrow',    group: 'Arrows', clip: 'polygon(30% 100%, 30% 40%, 0% 40%, 50% 0%, 100% 40%, 70% 40%, 70% 100%)' },
    arrowdown:  { label: 'Down arrow',  group: 'Arrows', clip: 'polygon(30% 0%, 30% 60%, 0% 60%, 50% 100%, 100% 60%, 70% 60%, 70% 0%)' },
    arrowlr:    { label: 'Left-right',  group: 'Arrows', clip: 'polygon(0% 50%, 25% 0%, 25% 30%, 75% 30%, 75% 0%, 100% 50%, 75% 100%, 75% 70%, 25% 70%, 25% 100%)' },
    arrowud:    { label: 'Up-down',     group: 'Arrows', clip: 'polygon(50% 0%, 100% 25%, 70% 25%, 70% 75%, 100% 75%, 50% 100%, 0% 75%, 30% 75%, 30% 25%, 0% 25%)' },
    arrowbent:  { label: 'Bent arrow',  group: 'Arrows', clip: 'polygon(0% 70%, 0% 100%, 70% 100%, 70% 100%, 70% 30%, 55% 30%, 80% 0%, 100% 30%, 85% 30%, 85% 70%)' },
    arrownotch: { label: 'Notched',     group: 'Arrows', clip: 'polygon(0% 30%, 60% 30%, 60% 0%, 100% 50%, 60% 100%, 60% 70%, 0% 70%, 14% 50%)' },
    arrowquad:  { label: 'Four-way',    group: 'Arrows', clip: 'polygon(50% 0%, 68% 22%, 57% 22%, 57% 43%, 78% 43%, 78% 32%, 100% 50%, 78% 68%, 78% 57%, 57% 57%, 57% 78%, 68% 78%, 50% 100%, 32% 78%, 43% 78%, 43% 57%, 22% 57%, 22% 68%, 0% 50%, 22% 32%, 22% 43%, 43% 43%, 43% 22%, 32% 22%)' },

    // ---- Callouts ----
    // The tail is part of the clip, so it scales with the box and needs no
    // second element to keep in step with the first.
    speech:     { label: 'Speech',       group: 'Callouts', clip: 'polygon(0% 0%, 100% 0%, 100% 75%, 32% 75%, 14% 100%, 16% 75%, 0% 75%)' },
    speechleft: { label: 'Speech left',  group: 'Callouts', clip: 'polygon(0% 0%, 100% 0%, 100% 75%, 86% 75%, 88% 100%, 68% 75%, 0% 75%)' },
    speechup:   { label: 'Speech above', group: 'Callouts', clip: 'polygon(0% 25%, 16% 25%, 14% 0%, 32% 25%, 100% 25%, 100% 100%, 0% 100%)' },
    banner:     { label: 'Banner',       group: 'Callouts', clip: 'polygon(0% 0%, 100% 0%, 100% 100%, 50% 78%, 0% 100%)' },
};
function isShape(type) { return Object.prototype.hasOwnProperty.call(SLIDE_SHAPES, type); }

// The filters an image can carry. All CSS, so they cost nothing to apply, work
// in the thumbnails and the presentation unchanged, and survive an export.
const IMAGE_ADJUST = [
    { key: 'brightness', label: 'Brightness', min: 0,  max: 200, def: 100, unit: '%' },
    { key: 'contrast',   label: 'Contrast',   min: 0,  max: 200, def: 100, unit: '%' },
    { key: 'saturate',   label: 'Saturation', min: 0,  max: 200, def: 100, unit: '%' },
    { key: 'blur',       label: 'Blur',       min: 0,  max: 20,  def: 0,   unit: 'px' },
    { key: 'opacity',    label: 'Opacity',    min: 10, max: 100, def: 100, unit: '%' },
];

function imageFilter(el) {
    const parts = IMAGE_ADJUST
        .filter(a => a.key !== 'opacity')
        .map(a => {
            const v = el[a.key] ?? a.def;
            return v === a.def ? '' : a.key + '(' + v + a.unit + ')';
        })
        .filter(Boolean);
    return parts.length ? 'filter:' + parts.join(' ') + ';' : '';
}

function elementStyle(el) {
    const base = 'left:' + el.x + 'px;top:' + el.y + 'px;'
        + 'width:' + el.w + 'px;height:' + el.h + 'px;'
        + (el.rotation ? 'transform:rotate(' + el.rotation + 'deg);' : '');

    if (el.type === 'text') {
        return base
            + 'font-family:' + el.font + ';font-size:' + el.size + 'px;'
            + 'color:' + el.color + ';text-align:' + el.align + ';'
            + (el.bold ? 'font-weight:700;' : '')
            + (el.italic ? 'font-style:italic;' : '')
            + (el.underline ? 'text-decoration:underline;' : '')
            + (el.fill && el.fill !== 'transparent' ? 'background:' + el.fill + ';' : '');
    }
    if (el.type === 'image') {
        return base + imageFilter(el)
            + 'opacity:' + ((el.opacity ?? 100) / 100) + ';'
            + 'border-radius:' + (el.radius || 0) + 'px;'
            + 'object-fit:' + (el.fit || 'cover') + ';';
    }
    if (el.type === 'line') {
        return base + 'height:0;border-top:' + (el.stroke || 2) + 'px solid ' + el.color + ';';
    }
    // A shape is the fill; the outline is drawn by the wrapper behind it.
    const shape = SLIDE_SHAPES[el.type] || SLIDE_SHAPES.rect;
    return base
        + 'background:' + (el.fill || 'transparent') + ';'
        + (shape.clip ? 'clip-path:' + shape.clip + ';' : '')
        + (shape.radius ? 'border-radius:' + shape.radius + 'px;' : '')
        + 'opacity:' + ((el.opacity ?? 100) / 100) + ';';
}

// The outline layer: the same clip, the stroke colour, sitting one stroke-width
// out on every side. A border cannot do this on a clipped element — it is
// clipped along with the rest of the box.
function shapeOutlineStyle(el) {
    const w = el.stroke ?? 2;
    if (!w || !el.color || el.color === 'transparent') return null;
    const shape = SLIDE_SHAPES[el.type] || SLIDE_SHAPES.rect;
    return 'left:' + (el.x - w) + 'px;top:' + (el.y - w) + 'px;'
        + 'width:' + (el.w + w * 2) + 'px;height:' + (el.h + w * 2) + 'px;'
        + (el.rotation ? 'transform:rotate(' + el.rotation + 'deg);' : '')
        + 'background:' + el.color + ';'
        + (shape.clip ? 'clip-path:' + shape.clip + ';' : '')
        + (shape.radius ? 'border-radius:' + (shape.radius + w) + 'px;' : '')
        + 'opacity:' + ((el.opacity ?? 100) / 100) + ';';
}

// One element, as markup. Shared by the stage, the thumbnails, the
// presentation and the export, so a slide is the same object everywhere.
function elementHtml(el, opts = {}) {
    const sel = opts.selected ? ' selected' : '';
    const id = opts.interactive ? ' data-id="' + escHtml(el.id) + '"' : '';
    const handle = opts.interactive ? '<span class="slide-el-resize"></span>' : '';
    const styler = opts.resolve || ((s) => s);

    if (el.type === 'image') {
        return '<img class="slide-el slide-el-image' + sel + '"' + id
            + ' style="' + styler(elementStyle(el)) + '" src="' + el.src + '">' + handle;
    }
    if (el.type === 'text') {
        return '<div class="slide-el slide-el-text-box' + sel + '"' + id
            + ' style="' + styler(elementStyle(el)) + '">'
            + (opts.interactive
                ? '<div class="slide-el-text" contenteditable="true">' + escHtml(el.text) + '</div>'
                : escHtml(el.text))
            + handle + '</div>';
    }
    const outline = shapeOutlineStyle(el);
    return (outline && el.type !== 'line'
                ? '<div class="slide-el slide-el-outline" style="' + styler(outline) + '"></div>' : '')
        + '<div class="slide-el slide-el-shape' + sel + '"' + id
        + ' style="' + styler(elementStyle(el)) + '">' + handle + '</div>';
}

function renderSlideStage() {
    const host = document.getElementById('slides-preview');
    if (!host || !slidesDoc) return;
    const slide = slidesDoc.slides[slidesActive];
    if (!slide) return;
    const scale = slideScale();

    host.innerHTML =
        '<div class="slide-stage paper" style="width:' + SLIDE_W + 'px;height:' + SLIDE_H + 'px;'
        + 'transform:scale(' + scale + ');'
        + (slide.background ? 'background:' + slide.background + ';' : '') + '">'
        + slide.elements.map(el => elementHtml(el, {
            interactive: true, selected: slidesPicked.has(el.id) })).join('')
        // Above the elements and below nothing: the guides have to be visible
        // over whatever is being dragged past them.
        + '<div id="slide-guides" class="slide-guides"></div>'
        + '</div>';

    const notes = document.getElementById('slides-notes');
    if (notes && notes.value !== slide.notes) notes.value = slide.notes || '';
}

function renderSlidesFilm() {
    const film = document.getElementById('slides-film');
    if (!film || !slidesDoc) return;
    const scale = 132 / SLIDE_W;
    film.innerHTML = slidesDoc.slides.map((s, i) =>
        '<div class="slide-thumb' + (i === slidesActive ? ' active' : '') + '"'
        + ' draggable="true" data-index="' + i + '" onclick="gotoSlide(' + i + ')">'
        + '<span class="slide-thumb-n">' + (i + 1) + '</span>'
        + '<span class="slide-thumb-box">'
        + '<span class="slide-stage paper" style="width:' + SLIDE_W + 'px;height:' + SLIDE_H + 'px;'
        + 'transform:scale(' + scale + ');'
        + (s.background ? 'background:' + s.background + ';' : '') + '">'
        + (s.elements || []).map(el => elementHtml(el)).join('')
        + '</span></span></div>').join('');
    const count = document.getElementById('slides-count');
    if (count) {
        const n = slidesDoc.slides.length;
        count.textContent = n + (n === 1 ? ' slide' : ' slides');
    }
}


// ================================================================
// Reordering
// ================================================================
//
// Native HTML drag rather than pointer maths: the rail is a single column of
// fixed-size items, which is the one case the built-in gives for free — and it
// brings the drag image, the escape key and the cursor with it.
let slideDragFrom = null;

function bindSlideReorder() {
    const film = document.getElementById('slides-film');
    if (!film || film.dataset.reorder) return;
    film.dataset.reorder = '1';

    film.addEventListener('dragstart', (e) => {
        const thumb = e.target.closest('.slide-thumb');
        if (!thumb) return;
        slideDragFrom = +thumb.dataset.index;
        thumb.classList.add('dragging');
        e.dataTransfer.effectAllowed = 'move';
        // Firefox will not start a drag without data set on it.
        e.dataTransfer.setData('text/plain', String(slideDragFrom));
    });

    film.addEventListener('dragover', (e) => {
        if (slideDragFrom === null) return;
        e.preventDefault();
        const thumb = e.target.closest('.slide-thumb');
        for (const el of film.querySelectorAll('.slide-thumb')) el.classList.remove('drop-before', 'drop-after');
        if (!thumb) return;
        // Which half of the thumbnail the pointer is over decides whether the
        // slide lands before or after it, so dropping on the last one can mean
        // "at the end".
        const box = thumb.getBoundingClientRect();
        thumb.classList.add(e.clientY < box.top + box.height / 2 ? 'drop-before' : 'drop-after');
    });

    film.addEventListener('drop', (e) => {
        if (slideDragFrom === null) return;
        e.preventDefault();
        const thumb = e.target.closest('.slide-thumb');
        let to = slidesDoc.slides.length - 1;
        if (thumb) {
            const box = thumb.getBoundingClientRect();
            to = +thumb.dataset.index + (e.clientY < box.top + box.height / 2 ? 0 : 1);
            // Removing the slide first shifts everything after it down one.
            if (to > slideDragFrom) to -= 1;
        }
        moveSlide(slideDragFrom, to);
        endSlideDrag();
    });

    film.addEventListener('dragend', endSlideDrag);
}

function endSlideDrag() {
    slideDragFrom = null;
    for (const el of document.querySelectorAll('.slide-thumb')) {
        el.classList.remove('dragging', 'drop-before', 'drop-after');
    }
}

function moveSlide(from, to) {
    if (!slidesDoc || from === to || from == null) return;
    pushSlidesHistory();
    const slides = slidesDoc.slides;
    if (to < 0 || to >= slides.length) return;
    const [moved] = slides.splice(from, 1);
    slides.splice(to, 0, moved);
    // Follow the slide rather than the position: you dragged this one, so this
    // one is still the one you are looking at.
    slidesActive = to;
    renderSlideStage();
    renderSlidesFilm();
    scheduleSlidesSave();
}

function gotoSlide(index) {
    if (!slidesDoc) return;
    slidesActive = Math.max(0, Math.min(index, slidesDoc.slides.length - 1));
    pickOnly(null);
    renderSlideStage();
    renderSlidesFilm();
    renderSlideFormatBar();
}

// ================================================================
// The format bar
// ================================================================
//
// Shown only with something selected. A row of font and colour controls
// pointed at nothing is a row of controls that do nothing when you use them.
// ================================================================
// Picking more than one
// ================================================================
//
// Everything above works on one element, which is enough to put a thing on a
// slide and not enough to make the slide tidy. Three boxes that should share
// an edge get dragged until they look close, and "looks close" is what makes a
// deck read as homemade — the eye catches a four-pixel disagreement between
// two headings faster than it reads either of them.
//
// So: a set, the operations that only mean anything over a set, and snapping,
// which is the one that stops the problem being created in the first place.

function pickOnly(id) {
    slidesPicked = new Set(id ? [id] : []);
    slidesSelected = id || null;
}

function togglePicked(id) {
    if (slidesPicked.has(id) && slidesPicked.size > 1) {
        slidesPicked.delete(id);
        // The format bar has to keep pointing at something that is still on.
        if (slidesSelected === id) slidesSelected = [...slidesPicked][0] || null;
        return;
    }
    slidesPicked.add(id);
    slidesSelected = id;
}

function pickedElements() {
    const slide = currentSlide();
    if (!slide) return [];
    // In slide order rather than click order, so "distribute" spaces them the
    // way they are stacked rather than the way they happened to be selected.
    return slide.elements.filter(el => slidesPicked.has(el.id));
}

// Align moves; it never resizes. Stretching a heading to match a box is the
// version of this that quietly ruins type.
const SLIDE_ALIGNMENTS = {
    left:    (els, box) => els.forEach(el => { el.x = box.left; }),
    centre:  (els, box) => els.forEach(el => { el.x = Math.round(box.cx - el.w / 2); }),
    right:   (els, box) => els.forEach(el => { el.x = box.right - el.w; }),
    top:     (els, box) => els.forEach(el => { el.y = box.top; }),
    middle:  (els, box) => els.forEach(el => { el.y = Math.round(box.cy - el.h / 2); }),
    bottom:  (els, box) => els.forEach(el => { el.y = box.bottom - el.h; }),
};

function pickedBounds(els) {
    const left = Math.min(...els.map(e => e.x));
    const top = Math.min(...els.map(e => e.y));
    const right = Math.max(...els.map(e => e.x + e.w));
    const bottom = Math.max(...els.map(e => e.y + e.h));
    return { left, top, right, bottom, cx: (left + right) / 2, cy: (top + bottom) / 2 };
}

// Two or more align to each other; one aligns to the slide.
//
// Both readings are what somebody means. With a row of boxes selected they
// want the row tidy; with one box selected there is nothing to be tidy against
// except the slide, and refusing would be a button that does nothing.
function alignPicked(how) {
    const els = pickedElements();
    if (!els.length || !SLIDE_ALIGNMENTS[how]) return;
    pushSlidesHistory();
    const box = els.length > 1
        ? pickedBounds(els)
        : { left: 0, top: 0, right: SLIDE_W, bottom: SLIDE_H, cx: SLIDE_W / 2, cy: SLIDE_H / 2 };
    SLIDE_ALIGNMENTS[how](els, box);
    renderSlideStage();
    renderSlidesFilm();
    scheduleSlidesSave();
}

// Equal gaps, not equal centres.
//
// Spacing centres evenly is the easier sum and the wrong one: with a wide box
// between two narrow ones it leaves visibly different gaps. The ends stay put
// and the free space is shared between the elements in the middle.
function distributePicked(axis) {
    const els = pickedElements();
    if (els.length < 3) return;
    const horizontal = axis === 'x';
    const size = (el) => (horizontal ? el.w : el.h);
    const pos = (el) => (horizontal ? el.x : el.y);

    const order = [...els].sort((a, b) => pos(a) - pos(b));
    const first = order[0], last = order[order.length - 1];
    const span = (pos(last) + size(last)) - pos(first);
    const used = order.reduce((sum, el) => sum + size(el), 0);
    const gap = (span - used) / (order.length - 1);

    pushSlidesHistory();
    let cursor = pos(first);
    for (const el of order) {
        if (horizontal) el.x = Math.round(cursor); else el.y = Math.round(cursor);
        cursor += size(el) + gap;
    }
    renderSlideStage();
    renderSlidesFilm();
    scheduleSlidesSave();
}

// ================================================================
// Snapping
// ================================================================
//
// The lines a dragged element is allowed to agree with: the slide's own edges
// and centre, and every edge and centre of everything not being dragged. The
// centre of the slide matters most — a title that is nearly centred is the
// single most common way a deck looks wrong.

const SNAP_DISTANCE = 7;

function snapTargets(moving) {
    const ignore = new Set(moving.map(el => el.id));
    const x = [0, SLIDE_W / 2, SLIDE_W];
    const y = [0, SLIDE_H / 2, SLIDE_H];
    for (const el of (currentSlide()?.elements || [])) {
        if (ignore.has(el.id)) continue;
        x.push(el.x, el.x + el.w / 2, el.x + el.w);
        y.push(el.y, el.y + el.h / 2, el.y + el.h);
    }
    return { x, y };
}

// Returns the correction to apply, and the lines to draw for it. The offer is
// made from all three of an element's own edges — left, centre, right — so a
// box snaps by whichever of its sides is nearest something, not only by the
// corner being dragged.
function snapOffset(box, targets, scale) {
    // Constant on screen rather than in stage units: at 40% zoom a 7px stage
    // threshold is 3 real pixels, which is not a snap anyone can feel.
    const reach = SNAP_DISTANCE / (scale || 1);
    const best = { dx: 0, dy: 0, gx: null, gy: null, bestX: reach, bestY: reach };
    for (const [mine, axis] of [[box.left, 'x'], [box.cx, 'x'], [box.right, 'x'],
                                [box.top, 'y'], [box.cy, 'y'], [box.bottom, 'y']]) {
        for (const target of targets[axis]) {
            const delta = target - mine;
            if (axis === 'x' && Math.abs(delta) < best.bestX) {
                best.bestX = Math.abs(delta); best.dx = delta; best.gx = target;
            } else if (axis === 'y' && Math.abs(delta) < best.bestY) {
                best.bestY = Math.abs(delta); best.dy = delta; best.gy = target;
            }
        }
    }
    return best;
}

function renderSnapGuides(gx, gy) {
    const layer = document.getElementById('slide-guides');
    if (!layer) return;
    let html = '';
    if (gx !== null && gx !== undefined) {
        html += '<span class="slide-guide slide-guide-v" style="left:' + gx + 'px"></span>';
    }
    if (gy !== null && gy !== undefined) {
        html += '<span class="slide-guide slide-guide-h" style="top:' + gy + 'px"></span>';
    }
    layer.innerHTML = html;
}

function selectedElement() {
    const slide = currentSlide();
    return slide && slide.elements.find(e => e.id === slidesSelected);
}

function currentSlide() { return slidesDoc && slidesDoc.slides[slidesActive]; }

function renderSlideFormatBar() {
    const bar = document.getElementById('slides-format');
    if (!bar) return;
    const el = selectedElement();
    bar.classList.toggle('hidden', !el);
    if (!el) return;

    let html = '';

    // Arrange comes first, because with several things picked it is the only
    // row that applies to all of them — every control after this one sets a
    // font or a colour on the single element the bar is aimed at.
    const picked = pickedElements();
    html += '<div class="fmt-group fmt-arrange">'
        + [['left', '⭰', picked.length > 1 ? 'Align left edges' : 'Align to the left of the slide'],
           ['centre', '⭼', picked.length > 1 ? 'Align centres' : 'Centre on the slide'],
           ['right', '⭲', picked.length > 1 ? 'Align right edges' : 'Align to the right of the slide'],
           ['top', '⭱', picked.length > 1 ? 'Align tops' : 'Align to the top of the slide'],
           ['middle', '⭶', picked.length > 1 ? 'Align middles' : 'Centre vertically on the slide'],
           ['bottom', '⭳', picked.length > 1 ? 'Align bottoms' : 'Align to the bottom of the slide']]
            .map(([how, glyph, title]) =>
                '<button class="fmt-btn" title="' + title + '"'
                + ' onclick="alignPicked(\'' + how + '\')">' + glyph + '</button>').join('')
        // Distribute needs three: with two there is one gap, and one gap is
        // already even. Shown disabled rather than hidden, so the row does not
        // change width as the selection grows.
        + ['x', 'y'].map(axis =>
            '<button class="fmt-btn" title="Space evenly ' + (axis === 'x' ? 'across' : 'down')
            + (picked.length < 3 ? ' (needs three or more)' : '') + '"'
            + (picked.length < 3 ? ' disabled' : '')
            + ' onclick="distributePicked(\'' + axis + '\')">'
            + (axis === 'x' ? '⇹' : '⇳') + '</button>').join('')
        + '</div>';

    if (picked.length > 1) {
        html += '<span class="fmt-count">' + picked.length + ' selected</span>';
    }

    if (el.type === 'text') {
        html += '<select class="fmt-select" onchange="setSlideProp(\'font\', this.value)">'
            + SLIDE_FONTS.map(([v, n]) => '<option value="' + v + '"'
                + (el.font === v ? ' selected' : '') + '>' + n + '</option>').join('')
            + '</select>'
            + '<select class="fmt-select fmt-size" onchange="setSlideProp(\'size\', +this.value)">'
            + [12, 16, 20, 24, 28, 32, 40, 48, 56, 72, 96, 128].map(s => '<option value="' + s + '"'
                + (el.size === s ? ' selected' : '') + '>' + s + '</option>').join('')
            + '</select>'
            + fmtToggle('bold', el.bold, '<b>B</b>')
            + fmtToggle('italic', el.italic, '<i>I</i>')
            + fmtToggle('underline', el.underline, '<u>U</u>')
            + ['left', 'center', 'right'].map(a =>
                '<button class="fmt-btn' + (el.align === a ? ' on' : '') + '" title="Align ' + a
                + '" onclick="setSlideProp(\'align\', \'' + a + '\')">'
                + (a === 'left' ? '⭰' : a === 'center' ? '⭶' : '⭲') + '</button>').join('');
    }

    if (isShape(el.type) && el.type !== 'line') {
        html += '<select class="fmt-select" onchange="setSlideProp(\'type\', this.value)">'
            + Object.entries(SLIDE_SHAPES).filter(([k]) => k !== 'line').map(([k, s]) =>
                '<option value="' + k + '"' + (el.type === k ? ' selected' : '') + '>'
                + s.label + '</option>').join('')
            + '</select>';
    }

    if (el.type === 'image') {
        html += IMAGE_ADJUST.map(a =>
            '<label class="fmt-slider" title="' + a.label + '">'
            + '<span>' + a.label + '</span>'
            + '<input type="range" min="' + a.min + '" max="' + a.max + '"'
            + ' value="' + (el[a.key] ?? a.def) + '"'
            + ' oninput="setSlideProp(\'' + a.key + '\', +this.value)"></label>').join('')
            + '<label class="fmt-slider" title="Rounded corners"><span>Corners</span>'
            + '<input type="range" min="0" max="120" value="' + (el.radius || 0) + '"'
            + ' oninput="setSlideProp(\'radius\', +this.value)"></label>'
            + '<button class="fmt-btn" onclick="resetImageAdjustments()">Reset</button>';
    } else {
        // Colour, then fill. A picker beside the swatches rather than instead
        // of them: the swatches are the deck's palette and stay right when the
        // theme changes, the picker is for the one colour that is not in it.
        html += '<span class="fmt-label">' + (el.type === 'text' ? 'Text' : 'Line') + '</span>'
            + swatches('color', el.color)
            + colorPicker('color', el.color);
        if (el.type !== 'line') {
            html += '<span class="fmt-label">Fill</span>'
                + swatches('fill', el.fill, true)
                + colorPicker('fill', el.fill)
                + '<button class="fmt-btn" onclick="openGradientEditor()" title="Gradient fill">▤</button>';
        }
    }

    html += '<span class="fmt-gap"></span>'
        + '<label class="fmt-slider" title="Rotation"><span>Turn</span>'
        + '<input type="range" min="-180" max="180" value="' + (el.rotation || 0) + '"'
        + ' oninput="setSlideProp(\'rotation\', +this.value)"></label>'
        + '<button class="fmt-btn" onclick="raiseSlideElement(1)" title="Bring forward">↑</button>'
        + '<button class="fmt-btn" onclick="raiseSlideElement(-1)" title="Send back">↓</button>'
        + '<button class="fmt-btn" onclick="duplicateSlideElement()">Duplicate</button>'
        + '<button class="fmt-btn danger" onclick="deleteSlideElement()">Delete</button>';
    bar.innerHTML = html;
}

function fmtToggle(prop, on, label) {
    return '<button class="fmt-btn' + (on ? ' on' : '') + '"'
        + ' onclick="setSlideProp(\'' + prop + '\', ' + (on ? 'false' : 'true') + ')">'
        + label + '</button>';
}

// Any colour at all. The value has to be a hex literal for the native control,
// so a token is resolved first — otherwise the picker opens on black whatever
// the element actually is.
function colorPicker(prop, current) {
    let value = current || '#000000';
    if (value.startsWith('var(') || value === 'transparent') {
        const resolved = resolveTokens(value === 'transparent' ? 'var(--card)' : value);
        value = /^#[0-9a-f]{6}$/i.test(resolved) ? resolved : '#888888';
    }
    return '<input type="color" class="fmt-picker" value="' + value + '"'
        + ' title="Any colour" oninput="setSlideProp(\'' + prop + '\', this.value)">';
}

function swatches(prop, current, withNone) {
    return '<span class="fmt-swatches">'
        + (withNone
            ? '<button class="fmt-swatch none' + (current === 'transparent' ? ' on' : '') + '"'
              + ' title="None" onclick="setSlideProp(\'' + prop + '\', \'transparent\')"></button>'
            : '')
        + SLIDE_COLORS.map(([v, n]) =>
            '<button class="fmt-swatch' + (current === v ? ' on' : '') + '"'
            + ' title="' + n + '" style="background:' + v + '"'
            + ' onclick="setSlideProp(\'' + prop + '\', \'' + v + '\')"></button>').join('')
        + '</span>';
}

// ================================================================
// Gradients
// ================================================================
//
// Stored as the CSS value itself rather than as stops, so everything that
// already draws a fill — stage, thumbnail, presentation, export — draws a
// gradient with no further knowledge of what one is.
function openGradientEditor() {
    const el = selectedElement();
    if (!el) return;
    const existing = parseGradient(el.fill);
    const host = document.createElement('div');
    host.className = 'grad-editor';
    host.innerHTML =
        '<div class="grad-card">'
        + '<div class="grad-title">Gradient fill</div>'
        + '<label>From <input type="color" id="grad-a" value="' + existing.from + '"></label>'
        + '<label>To <input type="color" id="grad-b" value="' + existing.to + '"></label>'
        + '<label>Angle <input type="range" id="grad-angle" min="0" max="360" value="'
        + existing.angle + '"></label>'
        + '<div id="grad-preview" class="grad-preview"></div>'
        + '<div class="row"><button class="btn btn-primary" id="grad-ok">Apply</button>'
        + '<button class="btn btn-ghost" id="grad-cancel">Cancel</button></div></div>';
    document.body.appendChild(host);

    const read = () => 'linear-gradient(' + host.querySelector('#grad-angle').value + 'deg, '
        + host.querySelector('#grad-a').value + ', ' + host.querySelector('#grad-b').value + ')';
    const paint = () => { host.querySelector('#grad-preview').style.background = read(); };
    for (const input of host.querySelectorAll('input')) input.oninput = paint;
    paint();
    host.querySelector('#grad-ok').onclick = () => { setSlideProp('fill', read()); host.remove(); };
    host.querySelector('#grad-cancel').onclick = () => host.remove();
    host.onclick = (e) => { if (e.target === host) host.remove(); };
}

function parseGradient(fill) {
    const m = /linear-gradient\((\d+)deg,\s*(#[0-9a-f]{3,8}),\s*(#[0-9a-f]{3,8})\)/i.exec(fill || '');
    return m ? { angle: m[1], from: m[2], to: m[3] }
             : { angle: '135', from: '#2f6bff', to: '#8e4ec6' };
}

function resetImageAdjustments() {
    const el = selectedElement();
    if (!el) return;
    for (const a of IMAGE_ADJUST) delete el[a.key];
    delete el.radius;
    renderSlideStage(); renderSlidesFilm(); renderSlideFormatBar(); scheduleSlidesSave();
}

// Order in the array is order on the slide, so moving an element forward is
// moving it later in the list.
function raiseSlideElement(direction) {
    const slide = currentSlide();
    const el = selectedElement();
    if (!slide || !el) return;
    const at = slide.elements.indexOf(el);
    const to = at + direction;
    if (to < 0 || to >= slide.elements.length) return;
    slide.elements.splice(at, 1);
    slide.elements.splice(to, 0, el);
    renderSlideStage(); renderSlidesFilm(); scheduleSlidesSave();
}

function setSlideProp(prop, value) {
    const el = selectedElement();
    if (!el) return;
    // Coalesced: sliders and colour pickers fire continuously.
    pushSlidesHistory({ coalesce: true });
    el[prop] = value;
    renderSlideStage();
    renderSlidesFilm();
    renderSlideFormatBar();
    scheduleSlidesSave();
}

function setSlideBackground(color) {
    const slide = currentSlide();
    if (!slide) return;
    slide.background = color;
    renderSlideStage();
    renderSlidesFilm();
    scheduleSlidesSave();
}

// ================================================================
// Editing
// ================================================================
function addSlide() {
    if (!slidesDoc) return;
    pushSlidesHistory();
    slidesDoc.slides.splice(slidesActive + 1, 0, blankSlide());
    gotoSlide(slidesActive + 1);
    scheduleSlidesSave();
}

function duplicateSlide() {
    const slide = currentSlide();
    if (!slide) return;
    pushSlidesHistory();
    const copy = JSON.parse(JSON.stringify(slide));
    copy.id = newId('s');
    copy.elements.forEach(e => { e.id = newId('e'); });
    slidesDoc.slides.splice(slidesActive + 1, 0, copy);
    gotoSlide(slidesActive + 1);
    scheduleSlidesSave();
}

function deleteSlide() {
    if (!slidesDoc || slidesDoc.slides.length <= 1) return;
    pushSlidesHistory();
    slidesDoc.slides.splice(slidesActive, 1);
    gotoSlide(Math.min(slidesActive, slidesDoc.slides.length - 1));
    scheduleSlidesSave();
}


// Every shape drawn as itself — a grid of names makes you read ten words to
// find the triangle — and under a heading, because thirty-four in one flat
// grid is a worse eleven.
//
// The name moved from under each swatch into the tooltip and the aria-label.
// At eleven shapes the labels helped; at thirty-four they were most of the
// menu, and a drawn square does not need the word "square" beneath it.
function shapeMenuHtml() {
    return SLIDE_SHAPE_GROUPS.map(group => {
        const items = Object.entries(SLIDE_SHAPES).filter(([, s]) => s.group === group);
        if (!items.length) return '';
        return '<div class="shape-group">' + escHtml(group) + '</div>'
            + '<div class="shape-grid">' + items.map(([key, s]) =>
                '<button class="shape-item" title="' + escHtml(s.label) + '"'
                + ' aria-label="' + escHtml(s.label) + '"'
                + ' onclick="addSlideElement(\'' + key + '\'); toggleShapeMenu()">'
                + '<span class="shape-swatch" data-shape="' + key + '" style="'
                + (s.clip ? 'clip-path:' + s.clip + ';' : '')
                // The swatch is 22px and the shape is 200. A radius that reads
                // as "slightly rounded" on the slide is a circle at this size,
                // which is why `rounded`, `pill` and `ellipse` all drew as the
                // same dot. `swatch` is the radius for the drawing rather than
                // for the shape; where a shape does not set one, its own is
                // already small enough to survive.
                + (s.swatch !== undefined ? 'border-radius:' + s.swatch + ';'
                   : s.radius ? 'border-radius:' + s.radius + 'px;' : '')
                + (key === 'line' ? 'height:2px;margin:10px 0;' : '')
                + '"></span></button>').join('')
            + '</div>';
    }).join('');
}

function toggleShapeMenu() {
    const pop = document.getElementById('slides-shape-pop');
    if (!pop) return;
    if (pop.classList.contains('hidden')) pop.innerHTML = shapeMenuHtml();
    pop.classList.toggle('hidden');
}

document.addEventListener('mousedown', (e) => {
    if (!e.target.closest('#slides-shapes')) {
        document.getElementById('slides-shape-pop')?.classList.add('hidden');
    }
    if (!e.target.closest('#slides-export')) {
        document.getElementById('slides-export-pop')?.classList.add('hidden');
    }
});

function addSlideElement(type) {
    const slide = currentSlide();
    if (!slide) return;
    pushSlidesHistory();
    const n = slide.elements.length;
    const opts = { x: 160 + (n % 4) * 24, y: 200 + (n % 4) * 24 };
    let el;
    if (type === 'text') {
        el = makeElement('text', { ...opts, text: 'Text', w: 560, h: 90, size: 36 });
    } else if (type === 'line') {
        el = makeElement('line', { ...opts, w: 400, h: 0, color: 'var(--text)' });
    } else {
        el = makeElement(type, { ...opts, w: 320, h: 240,
                                 fill: 'var(--accent-soft)', color: 'var(--accent)' });
        el.stroke = 2;
    }
    slide.elements.push(el);
    pickOnly(el.id);
    renderSlideStage();
    renderSlidesFilm();
    renderSlideFormatBar();
    scheduleSlidesSave();
    if (type === 'text') {
        requestAnimationFrame(() => {
            document.querySelector('.slide-el[data-id="' + el.id + '"] .slide-el-text')?.focus();
        });
    }
}

// An image becomes a data URI, so a deck stays one file that presents with no
// network and survives being copied somewhere else.
function pickSlideImage() {
    document.getElementById('slides-image-input')?.click();
}

function addSlideImage(file) {
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
        const slide = currentSlide();
        if (!slide) return;
        const el = makeElement('image', { x: 200, y: 160, w: 560, h: 360 });
        el.src = reader.result;
        slide.elements.push(el);
        pickOnly(el.id);
        renderSlideStage();
        renderSlidesFilm();
        scheduleSlidesSave();
    };
    reader.readAsDataURL(file);
}

function duplicateSlideElement() {
    const el = selectedElement();
    if (!el) return;
    pushSlidesHistory();
    const copy = { ...el, id: newId('e'), x: el.x + 24, y: el.y + 24 };
    currentSlide().elements.push(copy);
    pickOnly(copy.id);
    renderSlideStage();
    renderSlidesFilm();
    scheduleSlidesSave();
}

function deleteSlideElement() {
    const slide = currentSlide();
    if (!slide || !slidesPicked.size) return;
    pushSlidesHistory();
    // All of them. Pressing Delete with four boxes ticked and losing one is
    // the kind of half-obeyed instruction you have to undo and redo by hand.
    slide.elements = slide.elements.filter(e => !slidesPicked.has(e.id));
    pickOnly(null);
    renderSlideStage();
    renderSlidesFilm();
    renderSlideFormatBar();
    scheduleSlidesSave();
}

function bindSlidesEvents() {
    const host = document.getElementById('slides-preview');
    if (!host || host.dataset.bound) return;
    host.dataset.bound = '1';

    host.addEventListener('mousedown', (e) => {
        const box = e.target.closest('.slide-el');
        if (!box) { pickOnly(null); renderSlideStage(); renderSlideFormatBar(); return; }
        const el = currentSlide().elements.find(x => x.id === box.dataset.id);
        if (!el) return;

        // Shift or the platform's own modifier adds to the selection, which is
        // what every editor with a canvas in it does.
        const adding = e.shiftKey || e.metaKey || e.ctrlKey;
        const changed = adding || !slidesPicked.has(el.id);
        // Pressing on something already in the group keeps the group, because
        // that is how you pick a row up by one of its members. But if the
        // press turns out to be a click and not a drag, it meant "just this
        // one" — settled on mouseup, when which of the two it was is known.
        let collapseTo = null;
        if (adding) togglePicked(el.id);
        else if (!slidesPicked.has(el.id)) pickOnly(el.id);
        else { slidesSelected = el.id; collapseTo = slidesPicked.size > 1 ? el.id : null; }

        if (e.target.isContentEditable) {
            if (changed) { renderSlideStage(); renderSlideFormatBar(); }
            return;
        }
        // Resize stays a one-element operation. Scaling a mixed selection by a
        // corner needs a decision per element about text size and aspect that
        // nothing here can make well, and guessing it wrong ruins the slide.
        const resizing = e.target.classList.contains('slide-el-resize');
        const moving = resizing ? [el] : pickedElements();
        slidesDrag = {
            kind: resizing ? 'resize' : 'move',
            el, moving,
            // Where each of them started, so the whole group tracks one pointer.
            origins: moving.map(m => ({ el: m, x0: m.x, y0: m.y })),
            targets: snapTargets(moving),
            scale: slideScale(), startX: e.clientX, startY: e.clientY,
            x0: el.x, y0: el.y, w0: el.w, h0: el.h,
            moved: false, collapseTo,
        };
        e.preventDefault();
        renderSlideStage();
        renderSlideFormatBar();
    });

    window.addEventListener('mousemove', (e) => {
        if (!slidesDrag) return;
        // Divided by the stage scale, so a box tracks the pointer whatever
        // size the slide is being shown at.
        let dx = (e.clientX - slidesDrag.startX) / slidesDrag.scale;
        let dy = (e.clientY - slidesDrag.startY) / slidesDrag.scale;
        const el = slidesDrag.el;
        slidesDrag.moved = true;

        if (slidesDrag.kind === 'move') {
            // Snap the group as a whole, from where it would land — so the
            // outer edges of a multiple selection are what agree with the
            // slide, and the elements keep their spacing relative to each
            // other while it happens.
            const at = slidesDrag.origins.map(o => ({
                x: o.x0 + dx, y: o.y0 + dy, w: o.el.w, h: o.el.h }));
            const left = Math.min(...at.map(p => p.x));
            const top = Math.min(...at.map(p => p.y));
            const right = Math.max(...at.map(p => p.x + p.w));
            const bottom = Math.max(...at.map(p => p.y + p.h));
            const snap = snapOffset(
                { left, top, right, bottom, cx: (left + right) / 2, cy: (top + bottom) / 2 },
                slidesDrag.targets, slidesDrag.scale);
            // Held down, the pointer wins: a snap you cannot escape is worse
            // than no snap when the thing you want is deliberately off-grid.
            if (!e.altKey) { dx += snap.dx; dy += snap.dy; }
            renderSnapGuides(e.altKey ? null : snap.gx, e.altKey ? null : snap.gy);

            for (const origin of slidesDrag.origins) {
                origin.el.x = Math.round(origin.x0 + dx);
                origin.el.y = Math.round(origin.y0 + dy);
            }
        } else {
            el.w = Math.max(40, Math.round(slidesDrag.w0 + dx));
            el.h = Math.max(el.type === 'line' ? 0 : 30, Math.round(slidesDrag.h0 + dy));
        }

        for (const moved of (slidesDrag.kind === 'move' ? slidesDrag.moving : [el])) {
            const node = document.querySelector('.slide-el[data-id="' + moved.id + '"]');
            if (node) node.setAttribute('style', elementStyle(moved));
        }
    });

    window.addEventListener('mouseup', () => {
        if (!slidesDrag) return;
        const { moved, collapseTo } = slidesDrag;
        slidesDrag = null;
        renderSnapGuides(null, null);
        if (!moved) {
            // A press on a member of the group that never became a drag: it
            // meant that one, so drop the rest. Without this there is no way
            // back to a single element except clicking empty space first.
            if (collapseTo) {
                pickOnly(collapseTo);
                renderSlideStage();
                renderSlideFormatBar();
            }
            // Selecting is not a change; filing it as one puts an empty step
            // in the undo history.
            return;
        }
        renderSlidesFilm();
        scheduleSlidesSave();
    });

    host.addEventListener('input', (e) => {
        if (!e.target.classList.contains('slide-el-text')) return;
        const el = currentSlide()?.elements.find(x => x.id === e.target.closest('.slide-el').dataset.id);
        if (!el) return;
        el.text = e.target.textContent;
        renderSlidesFilm();
        scheduleSlidesSave();
    });

    const notes = document.getElementById('slides-notes');
    if (notes) {
        notes.addEventListener('input', () => {
            const slide = currentSlide();
            if (!slide) return;
            slide.notes = notes.value;
            scheduleSlidesSave();
        });
    }

    document.addEventListener('keydown', (e) => {
        if (!slidesDoc || !isWriteMode('slides')) return;
        if (e.target.isContentEditable || e.target.tagName === 'INPUT'
            || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') return;
        if ((e.key === 'Delete' || e.key === 'Backspace') && slidesPicked.size) {
            e.preventDefault(); deleteSlideElement();
        } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') {
            // Everything on this slide, which is what makes "align these" one
            // gesture on a slide somebody else laid out.
            e.preventDefault();
            const slide = currentSlide();
            if (!slide) return;
            slidesPicked = new Set(slide.elements.map(el => el.id));
            slidesSelected = slide.elements.length
                ? slide.elements[slide.elements.length - 1].id : null;
            renderSlideStage();
            renderSlideFormatBar();
        } else if (e.key === 'Escape' && slidesPicked.size) {
            pickOnly(null); renderSlideStage(); renderSlideFormatBar();
        } else if (e.key === 'ArrowRight') gotoSlide(slidesActive + 1);
        else if (e.key === 'ArrowLeft') gotoSlide(slidesActive - 1);
    });

    window.addEventListener('resize', () => { if (isWriteMode('slides')) renderSlideStage(); });
}

// ================================================================
// Presenting
// ================================================================
async function ensureReveal() {
    if (window.Reveal) return window.Reveal;
    if (!_vendorLoaded.reveal) {
        _loadCss('/vendor/reveal.css');
        _vendorLoaded.reveal = _loadScript('/vendor/reveal.js');
    }
    await _vendorLoaded.reveal;
    return window.Reveal;
}

function slideHtml(slide) {
    return '<div class="slide-stage paper" style="width:' + SLIDE_W + 'px;height:' + SLIDE_H + 'px;'
        + (slide.background ? 'background:' + slide.background + ';' : '') + '">'
        + (slide.elements || []).map(el => elementHtml(el)).join('')
        + '</div>';
}

async function presentDeck() {
    const Reveal = await ensureReveal();
    if (!Reveal) { alert('The presentation engine could not be loaded.'); return; }
    const overlay = document.getElementById('slides-present');
    overlay.querySelector('.reveal .slides').innerHTML = slidesDoc.slides.map(s =>
        '<section>' + slideHtml(s)
        + (s.notes ? '<aside class="notes">' + escHtml(s.notes) + '</aside>' : '')
        + '</section>').join('');

    overlay.classList.remove('hidden');
    if (revealInstance) { try { revealInstance.destroy(); } catch (_) {} }
    revealInstance = new Reveal(overlay.querySelector('.reveal'), {
        embedded: true, hash: false, keyboard: true, controls: true, progress: true,
        transition: 'slide', slideNumber: 'c/t', width: SLIDE_W, height: SLIDE_H,
    });
    await revealInstance.initialize();
    revealInstance.slide(slidesActive);
}

function exitPresent() {
    const overlay = document.getElementById('slides-present');
    if (!overlay || overlay.classList.contains('hidden')) return;
    if (revealInstance) {
        try { slidesActive = revealInstance.getState().indexh || 0; } catch (_) {}
        try { revealInstance.destroy(); } catch (_) {}
        revealInstance = null;
    }
    overlay.classList.add('hidden');
    renderSlideStage();
    renderSlidesFilm();
}

document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    const overlay = document.getElementById('slides-present');
    if (overlay && !overlay.classList.contains('hidden')) { e.stopPropagation(); exitPresent(); }
}, true);

// ================================================================
// Export
// ================================================================
//
// One self-contained HTML file: every slide, every image inlined as a data
// URI, and the accent and text colours resolved to literals — a deck that
// still needs Carrot's stylesheet to look right is not a deck you can send
// anybody. Printing that file is the route to PDF, which every browser has
// and none of them needs a library for.
function resolveTokens(css) {
    const style = getComputedStyle(document.documentElement);
    return css.replace(/var\((--[a-z0-9-]+)\)/gi, (_m, name) =>
        (style.getPropertyValue(name) || '').trim() || '#888');
}

// Resolved font stacks contain double quotes — `"Inter", "Segoe UI"` —
// and this goes into a `style="…"` attribute, where the first of them ends the
// attribute and the rest of the declaration becomes stray markup. The export
// still downloads; it is simply wrong, in a way that only shows up when
// somebody opens the file somewhere else.
function styleAttr(css) {
    return resolveTokens(css).replace(/"/g, '&quot;');
}

function exportDeckHtml(forPrint) {
    if (!slidesDoc) return;
    const bg = resolveTokens('var(--bg)');
    const ink = resolveTokens('var(--text)');
    const body = slidesDoc.slides.map(s =>
        '<section class="deck-slide" style="width:' + SLIDE_W + 'px;height:' + SLIDE_H + 'px;'
        + (s.background ? 'background:' + styleAttr(s.background) + ';' : '') + '">'
        + (s.elements || []).map(el => elementHtml(el, { resolve: styleAttr })).join('')
        + '</section>').join('\n');

    const html = '<!doctype html><html><head><meta charset="utf-8">'
        + '<title>' + escHtml(slidesDoc.title) + '</title><style>'
        + 'body{margin:0;background:' + bg + ';color:' + ink + ';'
        + 'font-family:system-ui,sans-serif;display:flex;flex-direction:column;'
        + 'align-items:center;gap:24px;padding:24px}'
        + '.deck-slide{position:relative;overflow:hidden;flex:0 0 auto;'
        + 'box-shadow:0 2px 18px rgba(0,0,0,.25)}'
        + '@page{size:' + SLIDE_W + 'px ' + SLIDE_H + 'px;margin:0}'
        + '@media print{body{gap:0;padding:0;background:#fff}'
        + '.deck-slide{box-shadow:none;page-break-after:always}}'
        + '</style></head><body>' + body + '</body></html>';

    if (forPrint) {
        // Printed from a window rather than downloaded, because "save as PDF"
        // is a thing the print dialogue already does well.
        const w = window.open('', '_blank');
        if (!w) { alert('Allow pop-ups to print this deck.'); return; }
        w.document.write(html);
        w.document.close();
        w.focus();
        setTimeout(() => w.print(), 400);
        return;
    }
    const blob = new Blob([html], { type: 'text/html' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = (slidesDoc.title || 'deck').replace(/[^\w.-]+/g, '-') + '.html';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

function toggleSlidesExport() {
    document.getElementById('slides-export-pop')?.classList.toggle('hidden');
}

// ================================================================
// Saving
// ================================================================
function scheduleSlidesSave() {
    clearTimeout(slidesSaveTimer);
    setSlidesStatus('editing…');
    slidesSaveTimer = setTimeout(saveSlidesNow, 700);
}

async function saveSlidesNow() {
    if (!slidesDoc) return;
    const title = document.getElementById('slides-title').value.trim() || 'Untitled deck';
    const deck = {
        type: 'carrot-slides', version: 1,
        size: { w: SLIDE_W, h: SLIDE_H },
        slides: slidesDoc.slides,
    };
    if (slidesConvertedFrom) deck.converted_from_markdown = slidesConvertedFrom;
    const body = JSON.stringify(deck, null, 2);
    try {
        await api('/api/notes/' + slidesDoc.id, {
            method: 'PUT', body: JSON.stringify({ content: body, title }),
        });
        slidesDoc.title = title;
        setSlidesStatus('saved');
        if (typeof loadNotes === 'function') loadNotes();
    } catch (_) {
        setSlidesStatus('save failed');
    }
}

function setSlidesStatus(msg) {
    const el = document.getElementById('slides-status');
    if (el) el.textContent = msg;
}

async function newSlidesDoc() {
    const created = await api('/api/notes', {
        method: 'POST',
        body: JSON.stringify({
            title: 'Untitled deck',
            content: JSON.stringify({ type: 'carrot-slides', version: 1,
                                      size: { w: SLIDE_W, h: SLIDE_H }, slides: [titleSlide()] }),
            format: 'slides',
        }),
    });
    await loadNotes();
    openNote(created.id);
}
