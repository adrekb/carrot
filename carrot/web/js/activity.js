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

// Three questions, three sections.
//
// This was one box called "In progress" holding all of it, with two counters
// in the header doing the separating. That reads as one list of
// things-going-on, and it is not: a research run that is working, a run that
// died when Carrot was last killed, and a brief that fires on Friday are three
// different demands on you — watch it, fix it, forget about it. Mixed
// together, the failed one is the one you miss, because it looks exactly like
// the scheduled one sitting next to it.
//
// So each gets a heading and its own count:
//
//   Running          — happening now, you are waiting on it
//   Needs attention  — stopped, or failed on its own; the only section that
//                      asks anything of you, and absent whenever nothing is
//                      wrong
//   Scheduled        — will happen without you; the calmest thing here
//
// Running stays on screen when empty, because it is *where you look* to find
// out and an absent section answers nothing. The other two are absent when
// empty: a permanent "nothing has failed" is a box that is right about
// nothing, and people stop reading those.
function renderActivity() {
    const host = document.getElementById('nav-activity');
    if (!host) return;
    const jobs = activityData.running || [];
    const recent = activityData.recent || [];
    const running = jobs.filter(j => j.status === 'running');
    const stalled = jobs.filter(j => j.status !== 'running');
    const scheduled = (typeof scheduledTasks !== 'undefined' ? scheduledTasks : []);
    const active = scheduled.filter(t => t.enabled);
    const failed = scheduled.filter(t => t.last_status === 'failed');
    host.innerHTML = '';

    // ----- Running -----
    const live = document.createElement('div');
    live.className = 'nav-running';
    live.appendChild(railSectionHead('Running', running.length, {
        title: 'Happening now — you are waiting on these',
        accent: true,
    }));
    for (const job of running) live.appendChild(activityJobRow(job));
    if (!running.length) {
        const idle = document.createElement('div');
        idle.className = 'nav-idle';
        idle.textContent = 'Nothing running';
        live.appendChild(idle);
    }
    host.appendChild(live);

    // ----- Needs attention -----
    //
    // Two unrelated failures in one section on purpose: a job that died when
    // Carrot was killed and a scheduled run that threw are the same sentence
    // to the person reading — something did not finish and nobody said so.
    if (stalled.length || failed.length) {
        const attention = document.createElement('div');
        attention.className = 'nav-running nav-attention';
        attention.appendChild(railSectionHead(
            'Needs attention', stalled.length + failed.length,
            { title: 'Stopped or failed — these did not finish', warn: true }));
        for (const job of stalled) attention.appendChild(activityJobRow(job));
        for (const task of failed) attention.appendChild(railFailedScheduledRow(task));
        host.appendChild(attention);
    }

    // ----- Scheduled -----
    if (scheduled.length) {
        const box = document.createElement('div');
        box.className = 'nav-running nav-scheduled';
        box.appendChild(railSectionHead('Scheduled', active.length, {
            title: 'Runs without you — open it to read what they said',
            onClick: toggleRailScheduled,
            open: railScheduledOpen,
        }));
        if (railScheduledOpen) {
            box.appendChild(railScheduledList());
            box.appendChild(activityMoreRow('edit these…', () => switchTab('scheduled')));
        } else {
            const line = document.createElement('div');
            line.className = 'nav-idle';
            line.textContent = railScheduledSummary(scheduled);
            box.appendChild(line);
        }
        host.appendChild(box);
    }

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

// One heading, one number. The number sits on the heading rather than in a
// strip of counters at the top, because a count kept away from the thing it
// counts is a number you have to be taught to read.
function railSectionHead(label, count, opts = {}) {
    const head = document.createElement(opts.onClick ? 'button' : 'div');
    head.className = 'nav-sec-head'
        + (opts.onClick ? ' is-toggle' : '')
        + (opts.open ? ' is-open' : '');
    if (opts.title) head.title = opts.title;
    head.innerHTML = '<span>' + escHtml(label) + '</span>'
        + '<span class="nav-count'
        + (opts.accent ? ' nav-count-live' : '')
        + (opts.warn ? ' nav-count-warn' : '')
        + '" data-zero="' + (count ? 0 : 1) + '">' + count + '</span>';
    if (opts.onClick) head.onclick = opts.onClick;
    return head;
}

// A scheduled run that threw, sitting among the jobs that stopped — the same
// shape as those, because it is the same news.
//
// It opens the task itself, like every other row in this rail: a row naming
// one thing that failed should land on that thing. It used to expand the
// Scheduled section instead, which answered a question nobody had asked — you
// clicked a specific failure and got handed the list it was in, with the
// finding still to do by eye.
function railFailedScheduledRow(task) {
    const row = document.createElement('button');
    row.className = 'nav-job stalled';
    row.title = task.prompt + ' — this scheduled run failed. Click to open it.';
    row.innerHTML = `
        <svg class="ico"><use href="#i-clock"/></svg>
        <span class="nav-job-text">
          <span class="nav-job-label">${escHtml(task.prompt)}</span>
          <span class="nav-job-sub">scheduled run failed</span>
        </span>`;
    row.onclick = () => openScheduledTask(task.id);
    return row;
}

// The collapsed line. Not the count — that is in the heading beside it, and
// saying it twice tells you nothing the second time.
function railScheduledSummary(tasks) {
    const active = tasks.filter(t => t.enabled);
    if (!active.length) return 'All paused';
    const ran = active.filter(t => t.last_run).length;
    return ran ? active.length + ' waiting · ' + ran + ' have run'
               : active.length + ' waiting · none have run yet';
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
    const standing = chatStandingLine();
    if (!what && !standing) { host.classList.add('hidden'); host.innerHTML = ''; return; }
    host.classList.remove('hidden');
    // "Brief me" only where there is a transcript to reconstruct. Resuming a
    // *running* job is going to watch it happen — there is nothing to be caught
    // up on, and a button offering to summarise something that has not
    // finished would be summarising the first half of it.
    const canBrief = what && !what.live
                     && (what.kind === 'conversation' || what.kind === 'code');
    host.innerHTML = (what ? `
        <div class="chat-resume-row">
          ${what.live ? '<span class="chat-resume-pulse" aria-hidden="true"></span>' : ''}
          <span class="chat-resume-lead">${escHtml(what.lead)}</span>
          <span class="chat-resume-label">${escHtml(what.label)}</span>
          ${canBrief ? '<button class="chat-resume-brief">Brief me</button>' : ''}
          <button class="chat-resume-go">${escHtml(what.action)}</button>
        </div>` : '')
        + (standing ? `<button class="chat-standing">${escHtml(standing)}</button>` : '')
        + '<div class="chat-brief hidden" id="chat-brief"></div>';
    if (what) {
        host.querySelector('.chat-resume-go').onclick =
            () => (ACTIVITY_OPEN[what.kind] || (() => {}))(what.id);
    }
    if (canBrief) host.querySelector('.chat-resume-brief').onclick = () => briefMe(what);
    if (standing) host.querySelector('.chat-standing').onclick = () => switchTab('scheduled');
}

// The one quiet line under it: what is waiting on you, whether or not you were
// in the middle of anything.
//
// The blank screen knew what you were last *doing* and nothing about what is
// standing — so three scheduled tasks and a failed run were invisible from the
// exact screen you sit on while deciding what to do next. Counts only, and
// only when they are non-zero: this sits under the composer, and a line that
// is always there is a line nobody reads.
function chatStandingLine() {
    const scheduled = (typeof scheduledTasks !== 'undefined' ? scheduledTasks : []);
    const active = scheduled.filter(t => t.enabled).length;
    const failed = scheduled.filter(t => t.last_status === 'failed').length
                 + (activityData.running || []).filter(j => j.status !== 'running').length;
    const parts = [];
    if (active) parts.push(active + ' scheduled task' + (active === 1 ? '' : 's'));
    if (failed) parts.push(failed === 1 ? '1 thing needs attention'
                                        : failed + ' things need attention');
    return parts.join(' · ');
}

// Where you left off, before you go back in.
//
// Resume puts you back in the room; it does not tell you what was being said
// in it. The reconstruction is written server-side from the transcript — see
// /api/resume/brief — and lands in place rather than in a dialog, because it
// is three sentences and a dialog would be a thing to dismiss.
async function briefMe(what) {
    const box = document.getElementById('chat-brief');
    const button = document.querySelector('.chat-resume-brief');
    if (!box) return;
    box.classList.remove('hidden');
    box.textContent = 'Reading back through it…';
    if (button) button.disabled = true;
    try {
        const data = await api('/api/resume/brief', {
            method: 'POST',
            body: JSON.stringify({ conversation_id: what.id }),
        });
        box.innerHTML = '<p>' + escHtml(data.brief) + '</p>'
            // Said, not hidden: a catch-up assembled from the transcript
            // because no model was there to write one is a different thing
            // from a summary, and reading it as a summary is the mistake.
            + (data.written_by === 'transcript'
                ? '<span class="chat-brief-note">from the transcript — the local model was not available to summarise it</span>'
                : '');
    } catch (e) {
        box.textContent = 'Could not read it back: ' + (e.message || e);
    }
    if (button) button.disabled = false;
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

// The one-line summary that used to sit under the counters is gone with them.
// It existed to say "2 chat, 1 research · 3 scheduled" because one box held
// all of it and the shape had to be described in words; three headed sections
// with their own counts are the same sentence, drawn.
