// ================================================================
// Search — results as rows you can scan, not cards you have to read.
//
// The page was seven fat cards on a screen. Each one spent seventy-five pixels
// on a date, a role, a conversation title and four hundred characters of body
// text, none of it marked to show *why* it had matched — so finding the thing
// you searched for meant reading every result in full, which is the work the
// search was supposed to do. Twenty results was three screens of scrolling.
//
// The shape it wants is the one every transcript, log and result list settles
// on: **one line per hit, uniform, with a disclosure**. Twenty of those fit on
// a screen, the line carries the sentence that matched with the matched words
// marked in it, and the rest — the whole message, where it came from, the way
// in — is behind the chevron for the one or two you actually want.
//
// Three things follow from that, and each is a thing the cards did not do:
//
// * **The matched words are marked.** A result you cannot tell the reason for
//   is one you have to open, and a list where every row must be opened is a
//   list of one row.
// * **A result goes somewhere.** The cards were inert: you found the message
//   and then had to go and find it again by hand in the history list.
// * **The foot says what the search was.** `7 results · 4 conversations ·
//   keyword` — including the mode, because "keyword" and "semantic" find
//   different things and a search that quietly fell back to one of them was
//   previously indistinguishable from one that found nothing.
//
// Keyboard, because a list of rows is a thing you drive: ↑/↓ move, Enter opens
// what is selected, Space expands it in place.
// ================================================================

let searchData = null;
let searchOpen = new Set();     // which rows are expanded, by index
let searchCursor = -1;          // which row the keyboard is on

async function doSearch() {
    const box = document.getElementById('search-input');
    const query = (box?.value || '').trim();
    if (!query) return;
    const host = document.getElementById('search-results');
    const foot = document.getElementById('search-foot');
    searchOpen = new Set();
    searchCursor = -1;
    host.innerHTML = '<div class="empty">Searching…</div>';
    if (foot) foot.textContent = '';
    try {
        searchData = await api(`/api/search?q=${encodeURIComponent(query)}&limit=40`);
    } catch (e) {
        searchData = null;
        host.innerHTML = `<div class="empty">${escHtml(e.message)}</div>`;
        return;
    }
    renderSearch();
}

/** The words to mark. Short ones are dropped: marking every "a" and "of" in a
 *  result paints the whole line and says nothing about why it is there. */
function searchTerms() {
    const query = (searchData?.query || '').toLowerCase();
    return [...new Set(query.split(/[^a-z0-9']+/i).filter(word => word.length > 2))];
}

/** Escape, then mark. In that order, because marking first would put the
 *  `<mark>` through the escaper and print it at the reader. */
function markTerms(text) {
    const escaped = escHtml(text || '');
    const terms = searchTerms();
    if (!terms.length) return escaped;
    const pattern = new RegExp('(' + terms.map(escapeForRegex).join('|') + ')', 'gi');
    return escaped.replace(pattern, '<mark>$1</mark>');
}

function escapeForRegex(text) {
    return text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/** The one line a row shows: the sentence the match is in, not the first
 *  sentence of the message.
 *
 * A four-hundred-character prefix answers "what does this message begin with",
 * which is rarely the question — the match is often three paragraphs down, and
 * the row then shows a passage with none of the searched words in it at all.
 */
function matchedLine(content) {
    const flat = String(content || '').replace(/\s+/g, ' ').trim();
    const terms = searchTerms();
    let at = -1;
    for (const term of terms) {
        const found = flat.toLowerCase().indexOf(term);
        if (found !== -1 && (at === -1 || found < at)) at = found;
    }
    if (at === -1) return flat.slice(0, 200);
    // A little before the match, so the row reads as a sentence rather than
    // starting mid-word on the term itself.
    const from = Math.max(0, at - 40);
    return (from ? '…' : '') + flat.slice(from, from + 220);
}

function searchWhen(timestamp) {
    if (!timestamp) return '';
    const then = new Date(timestamp);
    if (isNaN(then)) return '';
    const days = Math.floor((Date.now() - then.getTime()) / 86400000);
    if (days <= 0) return then.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    if (days === 1) return 'yesterday';
    if (days < 7) return `${days}d ago`;
    return then.toLocaleDateString([], { day: 'numeric', month: 'short' });
}

// What the search actually was. `mode` is the server's own word for how it
// answered — a hybrid run and one that fell back to keywords find different
// things, and until now they looked identical from here.
const SEARCH_MODE_WORDS = {
    hybrid: 'keyword and meaning',
    semantic: 'meaning',
    fts: 'keyword',
    fts_fallback: 'keyword — no embeddings yet',
    fts_only: 'keyword',
    keyword: 'keyword',
};

function renderSearch() {
    const host = document.getElementById('search-results');
    const foot = document.getElementById('search-foot');
    if (!host || !searchData) return;
    const results = searchData.results || [];

    if (!results.length) {
        host.innerHTML = '<div class="empty">Nothing matched “'
            + escHtml(searchData.query || '') + '”.</div>';
        if (foot) foot.textContent = '';
        return;
    }

    host.innerHTML = results.map((result, index) => {
        const open = searchOpen.has(index);
        return `
          <div class="s-row${open ? ' open' : ''}${index === searchCursor ? ' sel' : ''}"
               data-i="${index}">
            <button class="s-line" data-toggle="${index}">
              <svg class="ico s-chev"><use href="#i-chevron"/></svg>
              <span class="s-who">${escHtml(result.role === 'user' ? 'you' : 'carrot')}</span>
              <span class="s-snip">${markTerms(matchedLine(result.content))}</span>
              <span class="s-where">${escHtml(result.conversation_title
                                               || result.conversation_id)}</span>
              <span class="s-when">${escHtml(searchWhen(result.timestamp))}</span>
            </button>
            ${open ? `
            <div class="s-body">
              <div class="s-full">${markTerms(result.content)}</div>
              <div class="s-acts">
                <button class="s-open" data-open="${index}">Open this conversation</button>
                <span class="s-score">${escHtml(searchScoreNote(result))}</span>
              </div>
            </div>` : ''}
          </div>`;
    }).join('');

    for (const button of host.querySelectorAll('[data-toggle]')) {
        button.onclick = () => toggleSearchRow(Number(button.dataset.toggle));
    }
    for (const button of host.querySelectorAll('[data-open]')) {
        button.onclick = () => openSearchResult(Number(button.dataset.open));
    }

    if (foot) {
        const conversations = new Set(results.map(r => r.conversation_id)).size;
        const mode = SEARCH_MODE_WORDS[searchData.mode] || searchData.mode || '';
        foot.textContent = [
            `${results.length} result${results.length === 1 ? '' : 's'}`,
            `${conversations} conversation${conversations === 1 ? '' : 's'}`,
            mode,
        ].filter(Boolean).join(' · ');
    }
}

/** Why this one is here, for the row that has been opened.
 *
 * Only where there is a score to report: a keyword-only run leaves the
 * semantic score at zero for every result, and printing "0.0 semantic" under
 * all of them is noise that looks like a finding.
 */
function searchScoreNote(result) {
    const parts = [];
    if (result.semantic_score) parts.push(`${result.semantic_score.toFixed(2)} by meaning`);
    if (result.fts_score) parts.push(`${result.fts_score.toFixed(2)} by keyword`);
    return parts.join(' · ');
}

function toggleSearchRow(index) {
    if (searchOpen.has(index)) searchOpen.delete(index);
    else searchOpen.add(index);
    searchCursor = index;
    renderSearch();
}

function openSearchResult(index) {
    const result = (searchData?.results || [])[index];
    if (!result) return;
    switchTab('workspace');
    if (typeof openConversation === 'function') openConversation(result.conversation_id);
}

// ↑/↓ move, Enter opens, Space expands. Bound on the page rather than on the
// input, so it keeps working after the first click into the list — and it
// stays out of the way of anything typed into the box itself.
document.addEventListener('keydown', (event) => {
    const view = document.getElementById('view-search');
    if (!view || !view.classList.contains('active')) return;
    if (!searchData || !(searchData.results || []).length) return;
    const typing = event.target && event.target.id === 'search-input';
    const total = searchData.results.length;

    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        const step = event.key === 'ArrowDown' ? 1 : -1;
        searchCursor = Math.max(0, Math.min(total - 1, searchCursor + step));
        renderSearch();
        document.querySelector('.s-row.sel')?.scrollIntoView({ block: 'nearest' });
        return;
    }
    if (searchCursor < 0) return;
    if (event.key === 'Enter' && !typing) {
        event.preventDefault();
        openSearchResult(searchCursor);
    } else if (event.key === ' ' && !typing) {
        event.preventDefault();
        toggleSearchRow(searchCursor);
    }
});
