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
// Workspaces change when you make one, not every four seconds, so they are
// fetched once and kept — polling them on the rail's cadence would be a
// database round trip a minute for a list that is almost always identical.
let activityWorkspaces = null;
let activityWorkspacesOpen = false;
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
const RAIL_WORKSPACES = 3;
const RAIL_RECENTS = 5;

function renderActivity() {
    const host = document.getElementById('nav-activity');
    if (!host) return;
    const running = activityData.running || [];
    const recent = activityData.recent || [];
    host.innerHTML = '';

    // Workspaces first — what you are working *inside of*, before what you were
    // working *on*. Both come before running work now: a job announces itself
    // by moving, so it does not need the top of the rail as well, and the two
    // sections you navigate with do.
    if (activityWorkspaces && activityWorkspaces.length) {
        const box = document.createElement('details');
        box.className = 'nav-recents';
        box.open = activityWorkspacesOpen;
        box.addEventListener('toggle', () => { activityWorkspacesOpen = box.open; });
        box.innerHTML = '<summary class="nav-sec-head">workspaces</summary>';
        for (const workspace of activityWorkspaces.slice(0, RAIL_WORKSPACES)) {
            box.appendChild(activityWorkspaceRow(workspace));
        }
        if (activityWorkspaces.length > RAIL_WORKSPACES) {
            box.appendChild(activityMoreRow(
                `see all ${activityWorkspaces.length}`,
                () => { if (typeof switchTab === 'function') switchTab('workspaces'); }));
        }
        host.appendChild(box);
    }

    if (recent.length) {
        const details = document.createElement('details');
        details.className = 'nav-recents';
        details.open = activityRecentsOpen;
        details.addEventListener('toggle', () => { activityRecentsOpen = details.open; });
        details.innerHTML = '<summary class="nav-sec-head">recent</summary>';
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

    // In progress, last and set apart.
    //
    // It used to sit at the top on the argument that running work is worth
    // interrupting for. It is — but it is also the only section that moves,
    // and a moving thing at the bottom of a still rail is not missed. What the
    // top buys instead is pushing the two sections you actually navigate with
    // down the page, on the days when something is running.
    renderChatResume();

    if (running.length) {
        const box = document.createElement('div');
        box.className = 'nav-running';
        // "Unfinished" described the row rather than addressing the reader,
        // and a stopped run is not a category of work — it is something that
        // wants a decision. Say what it wants.
        box.innerHTML = `<div class="nav-sec-head">${
            running.some(j => j.status === 'running') ? 'in progress' : 'needs a look'}</div>`;
        for (const job of running) box.appendChild(activityJobRow(job));
        host.appendChild(box);
    }
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

function activityWorkspaceRow(workspace) {
    const row = document.createElement('button');
    row.className = 'nav-recent';
    const count = Object.values(workspace.counts || {}).reduce((a, b) => a + b, 0);
    row.title = workspace.name + (count ? ` — ${count} item${count === 1 ? '' : 's'}` : ' — empty');
    row.innerHTML = `
        <svg class="ico"><use href="#i-folder"/></svg>
        <span class="nav-recent-label">${escHtml(workspace.name)}</span>
        ${count ? `<span class="nav-recent-count">${count}</span>` : ''}`;
    // Making it active as well as opening it: picking a workspace out of the
    // rail is a statement about what you are working on, and leaving the
    // active one behind would file the next thing you make into the last one.
    row.onclick = async () => {
        try {
            if (typeof setActiveWorkspace === 'function') await setActiveWorkspace(workspace.id);
        } catch (_) { /* opening it still works */ }
        if (typeof switchTab === 'function') switchTab('workspaces');
        if (typeof openWorkspace === 'function') openWorkspace(workspace.id);
    };
    return row;
}

async function loadActivityWorkspaces() {
    try {
        const rows = await api('/api/workspaces');
        activityWorkspaces = Array.isArray(rows) ? rows : (rows.workspaces || []);
    } catch (_) {
        activityWorkspaces = [];
    }
    // Only redraw once there is something to draw *around*. This fetch races
    // the first activity poll, and it usually wins — /api/workspaces is one
    // small table and /api/activity is four queries. Redrawing on the way past
    // rebuilds the rail from an activityData that is still empty, so for the
    // gap between the two the rail is a workspaces section and nothing else —
    // and if the activity call then fails, it stays that way, which reads
    // exactly like recents having been removed.
    if (activityLoaded) renderActivity();
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
    loadActivityWorkspaces();
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) loadActivity();
    });
});
