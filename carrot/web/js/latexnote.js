// ===== LaTeX Notes =====
//
// The renderer was never missing: `$…$` and `$$…$$` have gone through KaTeX
// anywhere markdown is shown in this app since notes existed. What was missing
// was the place to write — a pane where the source and the result are both in
// front of you, because checking a formula against what it renders to is the
// entire loop of writing one, and doing that by saving and switching views is
// how you stop writing mathematics in a note-taking app.
//
// The editing idea here is the one worth taking from the current crop of LaTeX
// editors: the selection is the prompt. You highlight a formula and say what
// you want done to it, and the reply replaces exactly what was highlighted.
// Describing a fragment to a chat window on the other side of the screen and
// pasting the answer back is the same operation with four extra steps and a
// good chance of pasting it into the wrong place.

let latexDirty = false;
let latexMode = 'split';
// The selection at the moment the AI button was pressed. Read then, not later:
// clicking a button moves focus out of the textarea, and on some browsers the
// selection is gone by the time the handler asks for it.
let latexSelection = null;

function latexChanged() {
    latexDirty = true;
    renderLatexPreview();
    renderLatexOutline();
}

// LaTeX sectioning, as markdown headings — for the preview only.
//
// The outline reads both notations, so a document using \section had headings
// in the sidebar and the literal string "\section{Consequences}" sitting in
// the middle of the rendered page. Two answers to "is this a heading" on one
// screen, and the wrong one was the bigger.
//
// The source is untouched: what the user typed is what gets saved and
// exported. This is the render pass agreeing with the outline.
const LATEX_SECTIONS = [
    [/^\\chapter\*?\{([^}]*)\}\s*$/gm, '# $1'],
    [/^\\section\*?\{([^}]*)\}\s*$/gm, '## $1'],
    [/^\\subsection\*?\{([^}]*)\}\s*$/gm, '### $1'],
    [/^\\subsubsection\*?\{([^}]*)\}\s*$/gm, '#### $1'],
];

// And figures, for the same reason. The bracketed options are a typesetting
// instruction for a real TeX run and mean nothing to a browser, so they are
// dropped rather than half-honoured.
const LATEX_FIGURES = [
    [/\\includegraphics(?:\[[^\]]*\])?\{([^}]*)\}/g, '![]($1)'],
];

function latexToMarkdown(text) {
    let out = String(text);
    for (const [pattern, replacement] of [...LATEX_SECTIONS, ...LATEX_FIGURES]) {
        out = out.replace(pattern, replacement);
    }
    return out;
}

function renderLatexPreview() {
    const source = document.getElementById('latex-source');
    const preview = document.getElementById('latex-preview');
    if (!source || !preview) return;
    // Through the same mdToHtml every other surface uses, so a document reads
    // here exactly as it will read in a note, a chat reply or an export.
    preview.innerHTML = mdToHtml(latexToMarkdown(source.value));
}

// ---------- Outline and statistics ----------
//
// Both computed in the browser rather than round-tripping to the pack's tools.
// The tools exist for the agent, which is asked about documents it cannot see;
// the panel is looking at the text as it is typed, and a network call per
// keystroke to learn how many words there are would be a worse version of
// something that costs nothing here.

const HEADING_RE = /^(#{1,6})\s+(.+?)\s*$|^\\(section|subsection|subsubsection|chapter)\*?\{([^}]*)\}/gm;

function latexOutline(text) {
    const found = [];
    let match;
    HEADING_RE.lastIndex = 0;
    while ((match = HEADING_RE.exec(text)) !== null) {
        const line = text.slice(0, match.index).split('\n').length;
        if (match[1]) {
            found.push({ level: match[1].length, title: match[2], line });
        } else {
            const depth = { chapter: 1, section: 1, subsection: 2, subsubsection: 3 };
            found.push({ level: depth[match[3]] || 2, title: match[4], line });
        }
    }
    return found;
}

function renderLatexOutline() {
    const source = document.getElementById('latex-source');
    const host = document.getElementById('latex-outline');
    const stats = document.getElementById('latex-stats');
    if (!source || !host) return;
    const text = source.value;

    const headings = latexOutline(text);
    host.innerHTML = headings.length
        ? headings.map(h => `
            <div class="side-item latex-heading depth-${Math.min(h.level, 4)}"
                 onclick="jumpToLine(${h.line})">${escHtml(h.title)}</div>`).join('')
        : '<div class="empty">No headings yet.</div>';

    // The equation counts are the ones this tab is for. "1800 words" says
    // nothing about a paper that is forty displayed equations.
    const display = (text.match(/\$\$[\s\S]+?\$\$/g) || []).length;
    const inline = (text.replace(/\$\$[\s\S]+?\$\$/g, ' ')
                        .match(/(?<!\$)\$(?!\$)[\s\S]+?(?<!\$)\$(?!\$)/g) || []).length;
    const words = text.replace(/\$\$[\s\S]+?\$\$/g, ' ')
                      .replace(/\$[^$]+\$/g, ' ').split(/\s+/).filter(Boolean).length;
    stats.textContent = `${words} words · ${text.split('\n').length} lines · `
        + `${display + inline} equation${display + inline === 1 ? '' : 's'}`;
}

// Scrolls the source to a heading and puts the caret there, so clicking the
// outline moves where you are typing rather than only what you are looking at.
function jumpToLine(line) {
    const source = document.getElementById('latex-source');
    if (!source) return;
    const lines = source.value.split('\n');
    const offset = lines.slice(0, line - 1).join('\n').length + (line > 1 ? 1 : 0);
    source.focus();
    source.setSelectionRange(offset, offset);
    // Approximate, and deliberately so: exact caret geometry in a textarea
    // needs a mirror element, which is a lot of machinery for landing a
    // heading near the top of the view.
    const lineHeight = parseFloat(getComputedStyle(source).lineHeight) || 20;
    source.scrollTop = Math.max(0, (line - 2) * lineHeight);
}

// ---------- Figures ----------
//
// Embedded in the document as data URIs rather than saved beside it as files.
// A note is a row in a database that gets exported, mailed and pasted into
// other notes, and a figure that is a path is a figure that is missing the
// moment the document travels — which for a document whose whole purpose is
// to be handed to somebody is most of the time. Self-contained costs bytes
// and never costs the picture.
//
// Downscaled on the way in for the same reason it is embedded: a 12-megapixel
// phone photo is eight megabytes of base64 in a text field, and nothing in a
// paper needs more than about sixteen hundred pixels on its long edge.

const IMAGE_MAX_EDGE = 1600;
const IMAGE_MAX_BYTES = 8 * 1024 * 1024;

function downscaleImage(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onerror = () => reject(new Error('could not read that file'));
        reader.onload = () => {
            const img = new Image();
            img.onerror = () => reject(new Error('that does not look like an image'));
            img.onload = () => {
                const scale = Math.min(1, IMAGE_MAX_EDGE / Math.max(img.width, img.height));
                if (scale === 1 && String(reader.result).length < 400000) {
                    // Small enough already. Re-encoding it would only lose
                    // quality and, for a PNG diagram, usually add size.
                    resolve(String(reader.result));
                    return;
                }
                const canvas = document.createElement('canvas');
                canvas.width = Math.round(img.width * scale);
                canvas.height = Math.round(img.height * scale);
                canvas.getContext('2d').drawImage(img, 0, 0, canvas.width, canvas.height);
                // PNG for anything with transparency, JPEG otherwise: a
                // screenshot of a terminal re-encoded as JPEG is a screenshot
                // of a terminal with rings around the text.
                const type = /png|gif|webp/i.test(file.type) ? 'image/png' : 'image/jpeg';
                resolve(canvas.toDataURL(type, 0.9));
            };
            img.src = String(reader.result);
        };
        reader.readAsDataURL(file);
    });
}

async function insertLatexImage(file) {
    const source = document.getElementById('latex-source');
    const stats = document.getElementById('latex-stats');
    if (!source || !file) return;
    if (file.size > IMAGE_MAX_BYTES) {
        stats.textContent = 'That image is over 8MB — resize it first.';
        return;
    }
    let uri;
    try {
        uri = await downscaleImage(file);
    } catch (err) {
        stats.textContent = String(err.message || err);
        return;
    }
    const alt = (file.name || 'figure').replace(/\.[a-z0-9]+$/i, '').replace(/[\[\]()]/g, '');
    const markup = `\n\n![${alt}](${uri})\n\n`;
    const at = source.selectionStart;
    source.value = source.value.slice(0, at) + markup + source.value.slice(at);
    source.setSelectionRange(at + markup.length, at + markup.length);
    source.focus();
    latexChanged();
}

function pickLatexImage() {
    const input = document.getElementById('latex-image-input');
    if (input) input.click();
}

// Paste and drop, because those are how a figure actually arrives: a
// screenshot in the clipboard, or a plot dragged out of a folder. A file
// picker alone means going and finding something you are already holding.
function wireLatexImages() {
    const source = document.getElementById('latex-source');
    if (!source || source._imagesWired) return;
    source._imagesWired = true;

    source.addEventListener('paste', event => {
        const item = [...(event.clipboardData?.items || [])]
            .find(i => i.type.startsWith('image/'));
        if (!item) return;   // ordinary text paste, left alone
        event.preventDefault();
        insertLatexImage(item.getAsFile());
    });

    source.addEventListener('dragover', event => {
        if ([...(event.dataTransfer?.types || [])].includes('Files')) {
            event.preventDefault();
            source.classList.add('dropping');
        }
    });
    source.addEventListener('dragleave', () => source.classList.remove('dropping'));
    source.addEventListener('drop', event => {
        const file = event.dataTransfer?.files?.[0];
        source.classList.remove('dropping');
        if (!file || !file.type.startsWith('image/')) return;
        event.preventDefault();
        insertLatexImage(file);
    });
}

// ---------- Modes ----------

function setLatexMode(mode) {
    latexMode = mode === 'lite' ? 'lite' : 'split';
    document.getElementById('latex-panes')?.classList.toggle('lite', latexMode === 'lite');
    document.getElementById('latex-mode-split')?.classList.toggle('on', latexMode === 'split');
    document.getElementById('latex-mode-lite')?.classList.toggle('on', latexMode === 'lite');
    if (latexMode === 'lite') renderLatexPreview();
    try { localStorage.setItem('carrot-latex-mode', latexMode); } catch (_) {}
}

// ---------- The selection is the prompt ----------

function latexSelectionChanged() {
    const source = document.getElementById('latex-source');
    if (!source) return;
    const has = source.selectionEnd > source.selectionStart;
    const button = document.getElementById('latex-ai-btn');
    if (button) button.disabled = !has;
}

async function editSelectionWithAI() {
    const source = document.getElementById('latex-source');
    const bar = document.getElementById('latex-ai-bar');
    if (!source || !bar) return;
    const start = source.selectionStart;
    const end = source.selectionEnd;
    if (end <= start) {
        bar.classList.remove('hidden');
        bar.innerHTML = '<div class="ai-bar-note">Select the formula or passage you want '
                      + 'changed first — what you highlight is what gets replaced.</div>';
        return;
    }
    latexSelection = { start, end, text: source.value.slice(start, end) };

    const instruction = await inlineTextPrompt(
        'What should change about the selected text?', 'simplify this');
    if (!instruction) return;

    bar.classList.remove('hidden');
    bar.innerHTML = '<div class="ai-bar-note">Rewriting the selection…</div>';

    // The surrounding lines go along as context but are marked as context: the
    // model is told what is around the fragment so it can match the notation,
    // and told plainly that only the fragment may come back.
    const before = source.value.slice(Math.max(0, start - 600), start);
    const after = source.value.slice(end, end + 600);
    const prompt = `Rewrite only the SELECTED fragment of this document.\n\n`
        + `Instruction: ${instruction}\n\n`
        + `--- text before (context, do not return) ---\n${before}\n`
        + `--- SELECTED (return a replacement for exactly this) ---\n${latexSelection.text}\n`
        + `--- text after (context, do not return) ---\n${after}\n\n`
        + `Return only the replacement for the selected fragment: no explanation, `
        + `no code fence, no surrounding text. Keep the notation it already uses, `
        + `and keep every \\label, \\ref and citation key unless the instruction is `
        + `about them.`;

    let reply = '';
    try {
        const response = await fetch('/api/chat/stream', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', ...authHeaders() },
            body: JSON.stringify({ message: prompt, search_mode: 'off' }),
        });
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
                try {
                    const payload = JSON.parse(line.slice(6));
                    if (payload.chunk) reply += payload.chunk;
                } catch (_) {}
            }
        }
    } catch (err) {
        bar.innerHTML = `<div class="ai-bar-note">That did not work: ${escHtml(String(err))}</div>`;
        return;
    }

    const replacement = stripFence(reply.trim());
    if (!replacement) {
        bar.innerHTML = '<div class="ai-bar-note">It came back empty. The selection is unchanged.</div>';
        return;
    }
    // Shown before it is applied, and applied only on a click. A rewrite that
    // lands in the document the moment it arrives is one you have to undo to
    // read, and undo in a textarea is a coin toss.
    bar.innerHTML = `
        <div class="ai-bar-head">Replace the selection with this?</div>
        <pre class="ai-bar-diff"><del>${escHtml(latexSelection.text)}</del></pre>
        <pre class="ai-bar-diff"><ins>${escHtml(replacement)}</ins></pre>
        <div class="ai-bar-row">
            <button class="btn btn-primary" onclick="applyLatexEdit()">Replace</button>
            <button class="btn btn-ghost" onclick="dismissLatexEdit()">Keep mine</button>
        </div>`;
    bar._replacement = replacement;
}

// A fenced block is the model packaging its answer, not part of the answer.
// Pasted into a document it becomes three backticks in the middle of a
// paragraph.
function stripFence(text) {
    const fenced = /^```[a-zA-Z]*\n([\s\S]*?)\n?```$/.exec(text.trim());
    return fenced ? fenced[1] : text;
}

function applyLatexEdit() {
    const source = document.getElementById('latex-source');
    const bar = document.getElementById('latex-ai-bar');
    if (!source || !bar || !latexSelection) return;
    const { start, end } = latexSelection;
    source.value = source.value.slice(0, start) + bar._replacement + source.value.slice(end);
    source.focus();
    source.setSelectionRange(start, start + bar._replacement.length);
    latexChanged();
    dismissLatexEdit();
}

function dismissLatexEdit() {
    const bar = document.getElementById('latex-ai-bar');
    if (!bar) return;
    bar.classList.add('hidden');
    bar.innerHTML = '';
    latexSelection = null;
}

// ---------- Storage and export ----------

async function saveLatexDoc() {
    const title = document.getElementById('latex-title').value.trim() || 'Untitled document';
    const content = document.getElementById('latex-source').value;
    try {
        await api('/api/notes', {
            method: 'POST',
            body: JSON.stringify({ title, content, folder: '' }),
        });
        latexDirty = false;
        document.getElementById('latex-stats').textContent = 'Saved to Notes.';
        renderLatexOutline();
    } catch (err) {
        document.getElementById('latex-stats').textContent = 'Could not save: ' + err;
    }
}

function newLatexDoc() {
    if (latexDirty && !confirm('Start a new document? The current one is unsaved.')) return;
    document.getElementById('latex-title').value = '';
    document.getElementById('latex-source').value = '';
    latexDirty = false;
    latexChanged();
}

// Exported as a file the browser downloads, not through the server: the
// document is already in the page, and a round trip would put the user's text
// through a request to produce something it can make locally.
function exportLatex(format) {
    const title = document.getElementById('latex-title').value.trim() || 'document';
    const text = document.getElementById('latex-source').value;
    let body = text;
    let mime = 'text/plain';

    if (format === 'html') {
        // Self-contained: the KaTeX stylesheet and the rendered markup are
        // inlined, so the file opens correctly on a machine that has never
        // heard of Carrot. That is the whole point of exporting it.
        const rendered = mdToHtml(latexToMarkdown(text));
        const katexCss = [...document.styleSheets]
            .filter(sheet => (sheet.href || '').includes('katex'))
            .map(sheet => { try { return [...sheet.cssRules].map(r => r.cssText).join('\n'); }
                            catch (_) { return ''; } })
            .join('\n');
        body = `<!doctype html>\n<html><head><meta charset="utf-8">\n`
             + `<title>${escHtml(title)}</title>\n<style>\n${katexCss}\n`
             + `body{max-width:46rem;margin:3rem auto;padding:0 1.5rem;`
             + `font:16px/1.6 system-ui,sans-serif;color:#1a1a1a}\n</style></head>\n`
             + `<body>\n${rendered}\n</body></html>`;
        mime = 'text/html';
    }

    const blob = new Blob([body], { type: mime + ';charset=utf-8' });
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = `${title.replace(/[^\w.-]+/g, '-')}.${format}`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(link.href), 1000);
}

function loadLatexTab() {
    wireLatexImages();
    try { setLatexMode(localStorage.getItem('carrot-latex-mode') || 'split'); }
    catch (_) { setLatexMode('split'); }
    latexChanged();
    latexSelectionChanged();
}
