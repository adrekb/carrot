// ===== The nav rail's living half =====
//
// Backed by /api/activity: what is running now, and what you were last doing.
// See carrot/activity.py for why those two things are in one payload and why
// they are treated so unequally here.
//
// The design rule this file follows: running work announces itself, recents do
// not. A job you started and cannot see is a job you assume died, so live work
// is drawn at full weight the moment it exists and the section disappears
// entirely when it does not — no "Nothing running" card, because a panel that
// is right about nothing most of the time trains people to stop reading it.
// Recents get the opposite treatment: present, collapsed, quiet, and only
// expanded if you ask.

let activityData = { running: [], recent: [], any_running: false };
let activityTimer = null;
let activityRecentsOpen = false;
// Whether /api/activity has ever answered. Distinct from "has any activity":
// an empty rail and an unloaded one look the same and mean opposite things.
let activityLoaded = false;

// Polling, tuned to what is actually changing.
//
// Watching a live job wants a few seconds; an idle app wants to be left alone.
// Polling at the fast rate all the time is how a sidebar becomes the reason
// the machine's fan is on, and this is an app whose argument is that it runs
// on your machine.
const ACTIVITY_POLL_BUSY_MS = 4000;
const ACTIVITY_POLL_IDLE_MS = 45000;

const ACTIVITY_ICON = {
    agent: 'i-terminal',
    research: 'i-search',
    index: 'i-folder',
    conversation: 'i-chat',
    code: 'i-terminal',
};

// Where each kind of thing opens.
//
// The same three steps the history menu takes (app.js, openHistoryItem): the
// tab, then the chat mode, then the loader. Going straight to the loader without
// setting the mode leaves an agent run drawn into a chat transcript with its
// plan and step list still hidden, which reads as a run that produced nothing.
//
// Kept as data so a kind nobody can open is a missing entry here rather than a
// row that silently does nothing when clicked — which is the bug this session
// already fixed once on the Workspaces page.
const ACTIVITY_OPEN = {
    agent: (id) => {
        switchTab('workspace'); setChatMode('agent');
        if (typeof openAgentRun === 'function') openAgentRun(id);
    },
    research: (id) => {
        switchTab('workspace');
        if (typeof openResearchRun === 'function') openResearchRun(id);
    },
    conversation: (id) => {
        switchTab('workspace'); setChatMode('chat');
        if (typeof openConversation === 'function') openConversation(id);
    },
    // A coding session opens where it was had. Sending it to chat would render
    // it as a transcript in a tab with no file tree, no diff and no agent —
    // technically the same conversation, and not the thing you clicked.
    code: (id) => {
        switchTab('code');
        if (typeof openCodeSession === 'function') openCodeSession(id);
    },
    index: () => switchTab('settings'),
};

async function loadActivity() {
    try {
        activityData = await api('/api/activity');
        activityLoaded = true;
    } catch (e) {
        // The rail is decoration around the app, not the app. A failed poll
        // leaves the last good answer on screen rather than blanking it.
        console.warn('activity poll failed', e);
        return scheduleActivity();
    }
    renderActivity();
    scheduleActivity();
}

function scheduleActivity() {
    clearTimeout(activityTimer);
    // A hidden window is not watching anything. The visibilitychange listener
    // below brings it straight back, so this loses nothing but the wakeups.
    if (document.hidden) return;
    activityTimer = setTimeout(loadActivity,
        activityData.any_running ? ACTIVITY_POLL_BUSY_MS : ACTIVITY_POLL_IDLE_MS);
}

// How many of each the rail shows before it stops and offers the rest.
//
// A rail is a glance, not a list. Three workspaces and five recents is about
// what the eye takes in without reading, and everything past that is a trip to
// the place that is actually built for looking — which is what "see more" is
// for. Capping also fixes the thing that made the rail unusable as it filled
// up: an unbounded recents section pushed "in progress" off the bottom.
const RAIL_RECENTS = 5;

function renderActivity() {
    const host = document.getElementById('nav-activity');
    if (!host) return;
    const running = activityData.running || [];
    const recent = activityData.recent || [];
    host.innerHTML = '';

    // In progress, first, and always — even with nothing in it.
    //
    // It moved to the foot on the argument that a moving thing is not missed
    // there, and moved back because the section that replaced it should not
    // have existed: the top bar already has a workspace picker, and a second
    // list of the same workspaces in the rail was two controls for one thing.
    //
    // Kept on screen when empty, which is the opposite of what this section
    // used to do. An empty state that says "nothing running" trains people to
    // stop reading a panel — unless the panel is *where you look* to find out,
    // and disappearing entirely is what made "is that research still going?"
    // unanswerable without opening a tab.
    const box = document.createElement('div');
    box.className = 'nav-running';
    // Two counts, two questions. The accent one is work somebody is waiting
    // on — chats, research, agent runs. The plain one is work that runs
    // without anybody, which you check rather than watch, and clicking it
    // opens what is on the schedule and when each last ran.
    const scheduled = (typeof scheduledTasks !== 'undefined' ? scheduledTasks : []);
    const activeScheduled = scheduled.filter(t => t.enabled).length;
    box.innerHTML = `<div class="nav-sec-head">`
        + `<span>${running.length && !running.some(j => j.status === 'running')
            ? 'Needs a look' : 'In progress'}</span>`
        + `<span class="nav-counts">`
        +   `<button class="nav-count nav-count-live" data-zero="${running.length ? 0 : 1}"`
        +     ` title="Running now — you are waiting on these">${running.length}</button>`
        +   `<button class="nav-count nav-count-sched" data-zero="${activeScheduled ? 0 : 1}"`
        +     ` onclick="toggleRailScheduled()"`
        +     ` title="On a schedule — these run without you">${activeScheduled}</button>`
        + `</span></div>`;
    if (running.length) {
        for (const job of running) box.appendChild(activityJobRow(job));
    }
    // A line that says what is actually true, in place of the one that used to
    // say "nothing for now" while four tasks sat on the schedule. Nothing is
    // running *and* nothing is scheduled is the only case where there is
    // genuinely nothing, and only then does it say so.
    const summary = document.createElement('div');
    summary.className = 'nav-idle';
    summary.textContent = activitySummaryLine(running, activeScheduled);
    if (summary.textContent) box.appendChild(summary);
    if (railScheduledOpen) box.appendChild(railScheduledList());
    host.appendChild(box);

    if (recent.length) {
        const details = document.createElement('details');
        details.className = 'nav-recents';
        details.open = activityRecentsOpen;
        details.addEventListener('toggle', () => { activityRecentsOpen = details.open; });
        details.innerHTML = '<summary class="nav-sec-head">Recent</summary>';
        for (const item of recent.slice(0, RAIL_RECENTS)) {
            details.appendChild(activityRecentRow(item));
        }
        // Always offered, not only when the rail is full: the rail holds the
        // last five and the history holds everything, so "see more" is true
        // even at four — there are older ones, they are just not from today.
        details.appendChild(activityMoreRow('see more', () => {
            if (typeof toggleHistoryMenu === 'function') toggleHistoryMenu();
        }));
        host.appendChild(details);
    }

    renderChatResume();
}

// The row at the foot of a capped section. A row rather than a link, because
// it is in a column of rows and the hand is already there.
function activityMoreRow(label, onClick) {
    const row = document.createElement('button');
    row.className = 'nav-recent nav-seemore';
    row.innerHTML = `<span class="nav-recent-label">${escHtml(label)}</span>`;
    row.onclick = onClick;
    return row;
}

// The one line on the blank chat screen that is about you rather than about
// Carrot.
//
// Running work wins over a finished thing: "research is going" is a reason to
// wait or watch, and "you were reading X" is only a way back. Both are one
// sentence and one button, because this sits under the question and the
// question is what the screen is for — anything taller starts competing with
// the thing you came here to type.
function renderChatResume() {
    const host = document.getElementById('chat-resume');
    if (!host) return;
    const running = (activityData.running || []).filter(j => j.status === 'running');
    const recent = (activityData.recent || [])[0];
    let what = null;
    if (running.length) {
        const job = running[0];
        what = {
            lead: 'In progress',
            label: job.label || job.kind,
            action: 'Open',
            kind: job.kind,
            id: job.id,
            live: true,
        };
    } else if (recent) {
        what = {
            lead: recent.kind === 'code' ? 'Last code session' : 'Last conversation',
            label: recent.label,
            action: 'Resume',
            kind: recent.kind,
            id: recent.id,
            live: false,
        };
    }
    if (!what) { host.classList.add('hidden'); host.innerHTML = ''; return; }
    host.classList.remove('hidden');
    host.innerHTML = `
        ${what.live ? '<span class="chat-resume-pulse" aria-hidden="true"></span>' : ''}
        <span class="chat-resume-lead">${escHtml(what.lead)}</span>
        <span class="chat-resume-label">${escHtml(what.label)}</span>
        <button class="chat-resume-go">${escHtml(what.action)}</button>`;
    host.querySelector('.chat-resume-go').onclick =
        () => (ACTIVITY_OPEN[what.kind] || (() => {}))(what.id);
}

// The row at the foot of a capped section. A row rather than a link, because
// it is in a column of rows and the hand is already there.
function activityMoreRow(label, onClick) {
    const row = document.createElement('button');
    row.className = 'nav-recent nav-seemore';
    row.innerHTML = `<span class="nav-recent-label">${escHtml(label)}</span>`;
    row.onclick = onClick;
    return row;
}

function activityJobRow(job) {
    const row = document.createElement('button');
    const stalled = job.status !== 'running';
    row.className = 'nav-job' + (stalled ? ' stalled' : '');
    row.title = stalled
        // Said plainly. These rows have been sitting in the database claiming
        // to run since whenever Carrot was last killed, and until now nothing
        // anywhere admitted it.
        ? `${job.label} — started before Carrot was last restarted, so it is not running any more.`
        : `${job.label}${job.progress ? ' — ' + job.progress : ''}`;
    row.innerHTML = `
        ${stalled ? '' : '<span class="nav-job-pulse" aria-hidden="true"></span>'}
        <svg class="ico"><use href="#${escHtml(ACTIVITY_ICON[job.kind] || 'i-clock')}"/></svg>
        <span class="nav-job-text">
          <span class="nav-job-label">${escHtml(job.label || job.kind)}</span>
          <span class="nav-job-sub">${escHtml(stalled ? 'stopped' : (job.progress || 'working'))}</span>
        </span>`;
    row.onclick = () => (ACTIVITY_OPEN[job.kind] || (() => {}))(job.id);
    return row;
}

function activityRecentRow(item) {
    const row = document.createElement('button');
    row.className = 'nav-recent';
    const isCode = item.kind === 'code';
    // `</>` rather than a second chat icon. Chat sessions and coding sessions
    // are the same kind of row from the same table, they read identically at
    // this size, and they open in different tabs — so the one that is not the
    // default says so, in the notation everybody already reads as "code".
    row.title = (isCode ? 'Code session — ' : '') + item.label;
    row.innerHTML = `
        <svg class="ico"><use href="#${escHtml(ACTIVITY_ICON[item.kind] || 'i-clock')}"/></svg>
        <span class="nav-recent-label">${escHtml(item.label)}</span>
        ${isCode ? '<span class="nav-recent-tag" aria-label="code session">&lt;/&gt;</span>' : ''}`;
    row.onclick = () => (ACTIVITY_OPEN[item.kind] || (() => {}))(item.id);
    return row;
}

window.addEventListener('DOMContentLoaded', () => {
    loadActivity();
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) loadActivity();
    });
});


// The rail's one-line answer to "what is going on". Running work is broken
// down by kind, because "3 running" does not tell you whether to wait — three
// chats and three research runs are different amounts of patience. Scheduled
// work is counted rather than broken down: it is all the same kind of thing,
// and the number is the whole answer.
const ACTIVITY_KIND_NAMES = {
    research: 'research', chat: 'chat', agent: 'agent',
    code: 'code', deep_research: 'research',
};

function activitySummaryLine(running, activeScheduled) {
    const parts = [];
    if (running.length) {
        const counts = {};
        for (const job of running) {
            const name = ACTIVITY_KIND_NAMES[job.kind] || job.kind || 'other';
            counts[name] = (counts[name] || 0) + 1;
        }
        // Most of a thing first, so the biggest number is the one you read.
        const kinds = Object.entries(counts).sort((a, b) => b[1] - a[1]);
        parts.push(kinds.map(([name, n]) => n + ' ' + name).join(', '));
    }
    if (activeScheduled) parts.push(activeScheduled + ' scheduled');
    if (!parts.length) return 'Nothing for now';
    return parts.join(' · ');
}
