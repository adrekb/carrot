// ===== The turn rail =====
//
// A long conversation is a scrollbar and nothing else. You know the thing you
// want is "somewhere around when I asked about friction", and the only way
// back to it is to drag and read, drag and read — which is worse the longer
// the answers are, and Carrot's answers are long.
//
// So: one tick per question, down the right edge. Collapsed it is a column of
// dashes, which is enough to see how many turns there are and where you are in
// them. Hovered it becomes the questions themselves.
//
// Ticks rather than a proportional minimap. A minimap makes a three-line
// question and a two-screen answer different sizes, so the thing you are
// looking for is the smallest mark on the rail — exactly backwards. Every
// question gets the same tick, because every question is equally a place you
// might want to go back to.
//
// It follows the *questions*, not every message: an answer is where the turn
// went, and you navigate by what you asked.

let turnRailEl = null;
let turnRailFrame = null;
let turnRailActive = -1;

const TURN_RAIL_MIN = 2;      // one tick is not navigation

function turnRailHost() {
    return document.getElementById('chat-messages');
}

function ensureTurnRail() {
    if (turnRailEl && turnRailEl.isConnected) return turnRailEl;
    const scroller = turnRailHost();
    if (!scroller || !scroller.parentElement) return null;
    turnRailEl = document.createElement('nav');
    turnRailEl.className = 'turn-rail';
    turnRailEl.setAttribute('aria-label', 'Questions in this conversation');
    // A sibling of the transcript rather than a child of it: inside, it would
    // scroll away with the text it is there to navigate.
    scroller.parentElement.appendChild(turnRailEl);
    return turnRailEl;
}

function turnRailQuestions() {
    const scroller = turnRailHost();
    if (!scroller) return [];
    return [...scroller.querySelectorAll('.message.user')];
}

function renderTurnRail() {
    const rail = ensureTurnRail();
    if (!rail) return;
    const questions = turnRailQuestions();
    if (questions.length < TURN_RAIL_MIN) {
        rail.classList.add('hidden');
        rail.innerHTML = '';
        return;
    }
    rail.classList.remove('hidden');
    rail.innerHTML = questions.map((el, index) => {
        const text = (el.dataset.raw || el.textContent || '').trim();
        const label = text.split('\n')[0].slice(0, 80) || 'Untitled question';
        return `
          <button class="turn-tick" data-index="${index}" title="${escHtml(text.slice(0, 300))}">
            <span class="turn-tick-label">${escHtml(label)}</span>
            <span class="turn-tick-mark" aria-hidden="true"></span>
          </button>`;
    }).join('');
    for (const tick of rail.querySelectorAll('.turn-tick')) {
        tick.onclick = () => scrollToTurn(Number(tick.dataset.index));
    }
    turnRailActive = -1;
    markTurnRailActive();
}

// Scroll the transcript to one question.
//
// `scrollIntoView({behavior: 'smooth'})` is a silent no-op on this container —
// measured: `auto` moves it 2278px and `smooth` moves it zero, while
// `scroller.scrollTo({behavior: 'smooth'})` works fine. So the offset is
// computed and the container is scrolled directly, which also buys the thing
// scrollIntoView could not give: a little air above the question, instead of
// pinning it flush to the top edge where it reads as clipped.
const TURN_RAIL_HEADROOM = 14;

function scrollToTurn(index) {
    const scroller = turnRailHost();
    const target = turnRailQuestions()[index];
    if (!scroller || !target) return;
    const top = scroller.scrollTop
        + target.getBoundingClientRect().top
        - scroller.getBoundingClientRect().top
        - TURN_RAIL_HEADROOM;
    scroller.scrollTo({ top: Math.max(0, top), behavior: 'smooth' });
}

// Which question you are currently reading the answer to.
//
// The last one whose top is above the middle of the viewport — not the nearest
// one, which flickers between two ticks as a long answer crosses the midpoint,
// and not the first one visible, which jumps to the *next* question the moment
// its first line appears while you are still reading the previous answer.
function markTurnRailActive() {
    const rail = turnRailEl;
    const scroller = turnRailHost();
    if (!rail || !scroller || rail.classList.contains('hidden')) return;
    const questions = turnRailQuestions();
    if (!questions.length) return;
    const middle = scroller.getBoundingClientRect().top + scroller.clientHeight / 2;
    let active = 0;
    questions.forEach((el, index) => {
        if (el.getBoundingClientRect().top <= middle) active = index;
    });
    if (active === turnRailActive) return;
    turnRailActive = active;
    rail.querySelectorAll('.turn-tick').forEach((tick, index) => {
        tick.classList.toggle('on', index === active);
    });
}

function scheduleTurnRailActive() {
    if (turnRailFrame) return;
    turnRailFrame = requestAnimationFrame(() => {
        turnRailFrame = null;
        markTurnRailActive();
    });
}

// Rebuilt when the transcript changes, which is streaming, opening a
// conversation, and starting a new one. A MutationObserver rather than a call
// at each of those sites: there are a dozen of them and the one that gets
// forgotten is the one that leaves a rail pointing at a conversation you have
// left.
function watchTurnRail() {
    const scroller = turnRailHost();
    if (!scroller || scroller.dataset.railWatched) return;
    scroller.dataset.railWatched = '1';

    let pending = null;
    const observer = new MutationObserver(() => {
        // Coalesced: a streaming answer mutates its own node many times a
        // second, and none of those change the list of questions.
        clearTimeout(pending);
        pending = setTimeout(renderTurnRail, 200);
    });
    observer.observe(scroller, { childList: true });
    scroller.addEventListener('scroll', scheduleTurnRailActive, { passive: true });
    renderTurnRail();
}

window.addEventListener('DOMContentLoaded', watchTurnRail);
window.addEventListener('resize', scheduleTurnRailActive);
