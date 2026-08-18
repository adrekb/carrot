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
    index: () => switchTab('settings'),
};

async function loadActivity() {
    try {
        activityData = await api('/api/activity');
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

function renderActivity() {
    const host = document.getElementById('nav-activity');
    if (!host) return;
    const running = activityData.running || [];
    const recent = activityData.recent || [];
    host.innerHTML = '';

    if (running.length) {
        const box = document.createElement('div');
        box.className = 'nav-running';
        // "Unfinished" described the row rather than addressing the reader,
        // and a stopped run is not a category of work — it is something that
        // wants a decision. Say what it wants.
        box.innerHTML = `<div class="nav-sec-head">${
            running.some(j => j.status === 'running') ? 'Running now' : 'Needs a look'}</div>`;
        for (const job of running) box.appendChild(activityJobRow(job));
        host.appendChild(box);
    }

    if (!recent.length) return;
    // Recessed by default, and it stays that way between polls — re-rendering
    // a section the user opened, closed, would be the panel arguing with them
    // every four seconds.
    const details = document.createElement('details');
    details.className = 'nav-recents';
    details.open = activityRecentsOpen;
    details.addEventListener('toggle', () => { activityRecentsOpen = details.open; });
    details.innerHTML = '<summary class="nav-sec-head">Recent</summary>';
    for (const item of recent) details.appendChild(activityRecentRow(item));
    host.appendChild(details);
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
    row.title = item.label;
    row.innerHTML = `
        <svg class="ico"><use href="#${escHtml(ACTIVITY_ICON[item.kind] || 'i-clock')}"/></svg>
        <span class="nav-recent-label">${escHtml(item.label)}</span>`;
    row.onclick = () => (ACTIVITY_OPEN[item.kind] || (() => {}))(item.id);
    return row;
}

window.addEventListener('DOMContentLoaded', () => {
    loadActivity();
    document.addEventListener('visibilitychange', () => {
        if (!document.hidden) loadActivity();
    });
});
