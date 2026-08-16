// ================================================================
// Slides — a deck written in Markdown, presented with reveal.js
// ================================================================
//
// The source of truth is Markdown, not a slide object model. `---` on its own
// line starts a new slide, `--` starts one below it in reveal's vertical
// stack. That means a deck stays a text file you can grep, diff and paste into
// a chat, and it means writing one is the thing you already know how to do.
//
// reveal.js (MIT, vendored offline at /vendor/reveal.js) does the presenting:
// transitions, keyboard control, fragments, the overview grid, fullscreen. It
// is loaded lazily — most documents are not decks, and a deck is not usually
// the first thing opened in a session.

let slidesDoc = null;
let revealInstance = null;
let slidesSaveTimer = null;
let slidesActive = 0;

async function ensureReveal() {
    if (window.Reveal) return window.Reveal;
    if (!_vendorLoaded.reveal) {
        _loadCss('/vendor/reveal.css');
        _vendorLoaded.reveal = _loadScript('/vendor/reveal.js');
    }
    await _vendorLoaded.reveal;
    return window.Reveal;
}

// Split a deck into slides.
//
// Done on raw text rather than through a Markdown parser because the separator
// is a line, not a construct — and `---` is also valid Markdown for a rule and
// for frontmatter. Splitting on the line means a deck is exactly as
// predictable as it looks.
function parseDeck(source) {
    const slides = [];
    let current = { md: [], notes: [] };
    let inNotes = false;
    let inFence = false;

    const flush = () => {
        slides.push({ md: current.md.join('\n').trim(), notes: current.notes.join('\n').trim() });
        current = { md: [], notes: [] };
        inNotes = false;
    };

    for (const line of (source || '').split('\n')) {
        // A `---` inside a code fence is code, not a slide break. Without this,
        // a deck showing YAML in a code block silently splits in half.
        if (/^\s*```/.test(line)) inFence = !inFence;
        if (!inFence && /^---\s*$/.test(line)) { flush(); continue; }
        if (!inFence && /^\s*Notes?:\s*$/i.test(line)) { inNotes = true; continue; }
        (inNotes ? current.notes : current.md).push(line);
    }
    flush();
    // A trailing separator should not produce a final empty slide, but a deck
    // that is genuinely empty still needs one slide to render into.
    const kept = slides.filter(s => s.md || s.notes);
    return kept.length ? kept : [{ md: '', notes: '' }];
}

function slideTitle(slide, index) {
    const heading = (slide.md.match(/^#{1,6}\s+(.+)$/m) || [])[1];
    if (heading) return heading.trim();
    const firstLine = slide.md.split('\n').find(l => l.trim());
    return firstLine ? firstLine.replace(/[#*`>_-]/g, '').trim().slice(0, 40) || `Slide ${index + 1}`
                     : `Slide ${index + 1}`;
}

async function openSlidesDoc(note) {
    slidesDoc = { id: note.id, title: note.title || 'Untitled deck', source: note.body || '' };
    slidesActive = 0;
    showWriteMode('slides');
    document.getElementById('slides-title').value = slidesDoc.title;
    document.getElementById('slides-source').value = slidesDoc.source;
    if (typeof currentNoteId !== 'undefined') currentNoteId = note.id;
    renderSlidesPreview();
    renderSlidesFilm();
    bindSlidesEvents();
}

function renderSlidesFilm() {
    const film = document.getElementById('slides-film');
    if (!film || !slidesDoc) return;
    const slides = parseDeck(slidesDoc.source);
    film.innerHTML = slides.map((s, i) => `
        <div class="slide-thumb${i === slidesActive ? ' active' : ''}" onclick="gotoSlide(${i})">
          <span class="slide-thumb-n">${i + 1}</span>
          <span class="slide-thumb-t">${escHtml(slideTitle(s, i))}</span>
        </div>`).join('');
    const count = document.getElementById('slides-count');
    if (count) count.textContent = slides.length + (slides.length === 1 ? ' slide' : ' slides');
}

// The editing preview is one slide, rendered as HTML — not a running reveal
// instance. Booting reveal on every keystroke is slow and it steals the
// keyboard, which makes writing impossible. Reveal is for presenting.
function renderSlidesPreview() {
    const host = document.getElementById('slides-preview');
    if (!host || !slidesDoc) return;
    const slides = parseDeck(slidesDoc.source);
    slidesActive = Math.min(slidesActive, slides.length - 1);
    const slide = slides[slidesActive];
    const html = (window.marked ? marked.parse(slide.md || '') : escHtml(slide.md || ''));
    host.innerHTML = `<div class="slide-surface md">${html}</div>`
        + (slide.notes ? `<div class="slide-notes"><b>Notes</b> ${escHtml(slide.notes)}</div>` : '');
    if (window.renderMathInElement) {
        try { renderMathInElement(host, { delimiters: [
            { left: '$$', right: '$$', display: true },
            { left: '$', right: '$', display: false }] }); } catch (_) {}
    }
}

function gotoSlide(index) {
    slidesActive = index;
    renderSlidesPreview();
    renderSlidesFilm();
}

// Which slide the caret is in, so the preview follows the writing rather than
// having to be clicked. Counts separators before the caret, which is the same
// rule parseDeck uses.
function slideIndexAtCaret(textarea) {
    const before = textarea.value.slice(0, textarea.selectionStart);
    let count = 0, inFence = false;
    for (const line of before.split('\n')) {
        if (/^\s*```/.test(line)) inFence = !inFence;
        else if (!inFence && /^---\s*$/.test(line)) count++;
    }
    return count;
}

function bindSlidesEvents() {
    const source = document.getElementById('slides-source');
    if (!source || source.dataset.bound) return;
    source.dataset.bound = '1';

    source.addEventListener('input', () => {
        slidesDoc.source = source.value;
        slidesActive = slideIndexAtCaret(source);
        renderSlidesPreview();
        renderSlidesFilm();
        scheduleSlidesSave();
    });
    // Clicking around the source moves the preview too, without retyping.
    source.addEventListener('click', () => {
        const i = slideIndexAtCaret(source);
        if (i !== slidesActive) { slidesActive = i; renderSlidesPreview(); renderSlidesFilm(); }
    });
    source.addEventListener('keyup', (e) => {
        if (!e.key.startsWith('Arrow')) return;
        const i = slideIndexAtCaret(source);
        if (i !== slidesActive) { slidesActive = i; renderSlidesPreview(); renderSlidesFilm(); }
    });
}

function addSlide() {
    const source = document.getElementById('slides-source');
    const text = source.value;
    // Appended with a blank line either side so the separator is a line of its
    // own even when the deck did not end with a newline.
    source.value = text.replace(/\s*$/, '') + '\n\n---\n\n# New slide\n\n';
    slidesDoc.source = source.value;
    const slides = parseDeck(slidesDoc.source);
    slidesActive = slides.length - 1;
    source.focus();
    source.setSelectionRange(source.value.length, source.value.length);
    renderSlidesPreview();
    renderSlidesFilm();
    scheduleSlidesSave();
}

// ================================================================
// Presenting
// ================================================================
async function presentDeck() {
    const Reveal = await ensureReveal();
    if (!Reveal) { alert('The presentation engine could not be loaded.'); return; }

    const overlay = document.getElementById('slides-present');
    const container = overlay.querySelector('.reveal .slides');
    const slides = parseDeck(slidesDoc.source);
    container.innerHTML = slides.map(s => {
        const html = window.marked ? marked.parse(s.md || '') : escHtml(s.md || '');
        return `<section>${html}${s.notes ? `<aside class="notes">${escHtml(s.notes)}</aside>` : ''}</section>`;
    }).join('');

    overlay.classList.remove('hidden');
    if (revealInstance) { try { revealInstance.destroy(); } catch (_) {} }
    revealInstance = new Reveal(overlay.querySelector('.reveal'), {
        embedded: true,      // scoped to the overlay, not the whole document
        hash: false,         // a deck must not rewrite the app's URL
        keyboard: true,
        controls: true,
        progress: true,
        transition: 'slide',
        slideNumber: 'c/t',
    });
    await revealInstance.initialize();
    revealInstance.slide(slidesActive);

    if (window.renderMathInElement) {
        try { renderMathInElement(container, { delimiters: [
            { left: '$$', right: '$$', display: true },
            { left: '$', right: '$', display: false }] }); } catch (_) {}
    }
}

function exitPresent() {
    const overlay = document.getElementById('slides-present');
    if (!overlay || overlay.classList.contains('hidden')) return;
    // Leaving on the slide you presented, so exiting and re-entering does not
    // send you back to the beginning of the deck.
    if (revealInstance) {
        try { slidesActive = revealInstance.getState().indexh || 0; } catch (_) {}
        try { revealInstance.destroy(); } catch (_) {}
        revealInstance = null;
    }
    overlay.classList.add('hidden');
    renderSlidesPreview();
    renderSlidesFilm();
}

// Escape leaves the deck. Reveal binds Escape itself for the overview grid, so
// this listens in capture phase to get there first.
document.addEventListener('keydown', (e) => {
    if (e.key !== 'Escape') return;
    const overlay = document.getElementById('slides-present');
    if (overlay && !overlay.classList.contains('hidden')) { e.stopPropagation(); exitPresent(); }
}, true);

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
    try {
        await api(`/api/notes/${slidesDoc.id}`, {
            method: 'PUT',
            body: JSON.stringify({ content: slidesDoc.source, title }),
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
    const starter = '# Your deck\n\nA subtitle, maybe\n\n---\n\n## The first point\n\n'
                  + '- Written in Markdown\n- `---` on its own line starts a new slide\n'
                  + '- A line saying `Notes:` puts the rest in the speaker notes\n';
    const created = await api('/api/notes', {
        method: 'POST',
        body: JSON.stringify({ title: 'Untitled deck', content: starter, format: 'slides' }),
    });
    await loadNotes();
    openNote(created.id);
}
