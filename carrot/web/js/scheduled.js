// ================================================================
// Agent work that runs with nobody at the keyboard.
//
// The engine has been there all along — `scheduled.py` creates, lists, updates
// and deletes, and `start_scheduler()` has run at boot this whole time. There
// was simply no way to put a task into it.
//
// It lives in the Agent mode switch rather than in Settings because a
// scheduled task *is* an agent task; the only thing that differs is when it
// starts. So the choice reads as "now, or on a schedule" in the place you were
// already going to describe the work, rather than as a separate feature you
// have to know exists.
//
// You cannot turn a chat into an agent run, but the task you write here is the
// same task either way — which is why "when" sits beside the Chat/Agent switch
// and not inside a dialog of its own.
// ================================================================

let agentWhen = 'now';
let scheduledTasks = [];

const WEEKDAY_NAMES = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday',
                       'saturday', 'sunday'];
const titleCase = word => word.charAt(0).toUpperCase() + word.slice(1);

// Its own line rather than a global toast: there isn't one, and a message
// about the schedule belongs next to the schedule.
function scheduleNote(message) {
    const note = document.getElementById('agent-schedule-note');
    if (!note) return;
    note.textContent = message || '';
    note.classList.toggle('hidden', !message);
}


function setAgentWhen(when) {
    agentWhen = when === 'schedule' ? 'schedule' : 'now';
    document.getElementById('agent-when-now')?.classList.toggle('on', agentWhen === 'now');
    document.getElementById('agent-when-schedule')?.classList.toggle('on', agentWhen === 'schedule');
    const panel = document.getElementById('agent-schedule');
    if (panel) panel.classList.toggle('hidden', agentWhen !== 'schedule');
    if (agentWhen === 'schedule') {
        renderScheduleFields();
        loadScheduledTasks();
    }
}

// Called by setChatMode: the "when" question only exists in Agent mode, and a
// scheduled panel left showing over the chat composer would be a control for a
// mode you are no longer in.
function syncAgentWhenVisibility(mode) {
    const strip = document.getElementById('agent-when');
    if (strip) strip.classList.toggle('hidden', mode !== 'agent');
    if (mode !== 'agent') {
        document.getElementById('agent-schedule')?.classList.add('hidden');
    } else if (agentWhen === 'schedule') {
        document.getElementById('agent-schedule')?.classList.remove('hidden');
    }
}

// Hourly needs no time and no day; weekly needs both. Showing all three always
// invites you to set a weekday on an hourly task and then wonder why it did
// not take.
function renderScheduleFields() {
    const every = document.getElementById('agent-schedule-every')?.value || 'daily';
    const show = (id, on) => {
        const el = document.getElementById(id);
        if (el) el.classList.toggle('hidden', !on);
    };
    show('agent-schedule-weekday', every === 'weekly');
    show('agent-schedule-weekday-label', every === 'weekly');
    show('agent-schedule-at', every !== 'hourly');
    show('agent-schedule-at-label', every !== 'hourly');
}

// The choices come from the engine with the tasks, and the forms are built
// from them rather than from a copy in the markup — a schedule the engine
// stops offering stops being offerable everywhere at once.
let scheduledMeta = { schedules: ['hourly', 'daily', 'weekly'], weekdays: WEEKDAY_NAMES };

async function loadScheduledTasks() {
    try {
        const data = await api('/api/scheduled');
        scheduledTasks = data.tasks || [];
        if (data.schedules) scheduledMeta.schedules = data.schedules;
        if (data.weekdays) scheduledMeta.weekdays = data.weekdays;
    } catch (e) {
        scheduledTasks = [];
    }
    renderScheduledTasks();
    renderScheduledPage();
}

function describeSchedule(task) {
    if (task.schedule === 'hourly') return 'Every hour';
    if (task.schedule === 'weekly') {
        const day = (task.weekday || 'monday');
        return 'Every ' + day.charAt(0).toUpperCase() + day.slice(1) + ' at ' + task.at;
    }
    return 'Every day at ' + task.at;
}

function renderScheduledTasks() {
    const host = document.getElementById('agent-schedule-list');
    if (!host) return;
    if (!scheduledTasks.length) {
        // Says what the thing is for, not that a list is empty.
        host.innerHTML = '<div class="agent-schedule-empty">Nothing scheduled yet. '
                       + 'Write a task above and it will run on its own from then on.</div>';
        return;
    }
    host.innerHTML = scheduledTasks.map(task => {
        const off = !task.enabled;
        return '<div class="agent-schedule-item' + (off ? ' is-off' : '') + '">'
             + '<div class="agent-schedule-what">'
             +   '<div class="agent-schedule-prompt">' + escHtml(task.prompt) + '</div>'
             +   '<div class="agent-schedule-when">' + escHtml(describeSchedule(task))
             +     (off ? ' · paused' : '') + '</div>'
             + '</div>'
             + '<button class="btn-ghost" onclick="toggleScheduledTask(\'' + task.id + '\')">'
             +   (off ? 'Resume' : 'Pause') + '</button>'
             + '<button class="btn-ghost" onclick="removeScheduledTask(\'' + task.id + '\')">'
             +   'Delete</button>'
             + '</div>';
    }).join('');
}

async function addScheduledTask() {
    // The composer is the task. There is no second box to write it in, because
    // a scheduled task and an immediate one are the same sentence.
    const input = document.getElementById('agent-input');
    const prompt = (input?.value || '').trim();
    if (!prompt) {
        scheduleNote('Write the task first — the box above is the task.');
        input?.focus();
        return;
    }
    const body = {
        prompt,
        schedule: document.getElementById('agent-schedule-every')?.value || 'daily',
        at: document.getElementById('agent-schedule-at')?.value || '09:00',
        weekday: document.getElementById('agent-schedule-weekday')?.value || 'monday',
    };
    try {
        await api('/api/scheduled', { method: 'POST', body: JSON.stringify(body) });
    } catch (e) {
        scheduleNote('Could not schedule that: ' + (e.message || e));
        return;
    }
    if (input) { input.value = ''; input.dispatchEvent(new Event('input')); }
    await loadScheduledTasks();
    scheduleNote('Scheduled. It will run without you.');
}

async function toggleScheduledTask(id) {
    const task = scheduledTasks.find(t => t.id === id);
    if (!task) return;
    try {
        await api('/api/scheduled/' + id,
                  { method: 'PATCH', body: JSON.stringify({ enabled: !task.enabled }) });
    } catch (e) {
        scheduleNote('Could not change that: ' + (e.message || e));
        return;
    }
    await loadScheduledTasks();
}

async function removeScheduledTask(id) {
    try {
        await api('/api/scheduled/' + id, { method: 'DELETE' });
    } catch (e) {
        scheduleNote('Could not delete that: ' + (e.message || e));
        return;
    }
    await loadScheduledTasks();
}


// ================================================================
// The rail's scheduled count, and what is behind it.
//
// Two counts sit beside "in progress" because they answer different
// questions. The accent one is work somebody is waiting on. This one is work
// that happens with nobody there — which you check rather than watch, and
// which otherwise has no surface at all: a task that ran at 17:00 on Friday
// and finished is invisible until you go looking for it.
//
// Which is also why the run's own words open here. Every run does leave a
// trace — `run_task` raises a notification carrying the first 1500 characters
// — but a notification is a thing that happened *to you at the time*: it is
// dismissed, it is buried under six others, and by Monday the Friday brief is
// gone from it. The full text has been sitting in the task's `last_output`
// this whole time with nothing in the app reading it. So "when did it last
// run" and "what did it say" are the same question, asked in the same place,
// and the notification goes back to being the nudge rather than the record.
// ================================================================

let railScheduledOpen = false;

// A row opens its report on the reader page rather than unfolding in place.
//
// Unfolding was the first shape of this and it was wrong in a 220px column: a
// morning brief is several paragraphs, so opening one pushed everything under
// it off the bottom of the rail and turned a glance into a scroll. The rail
// says *that* it ran and *how it went*; the words themselves are a page.
function openScheduledReport(id) {
    const task = scheduledTasks.find(t => t.id === id);
    if (!task) return;
    const failed = task.last_status === 'failed';
    openReaderPage({
        title: task.prompt,
        sub: describeSchedule(task) + ' · ' + scheduledLastLine(task),
        text: (task.last_output || '').trim()
              || 'This run finished without reporting anything.',
        action: {
            label: failed ? 'Open it in Scheduled' : 'Edit this task',
            onClick: () => openScheduledTask(task.id),
        },
    });
}

function toggleRailScheduled() {
    railScheduledOpen = !railScheduledOpen;
    if (railScheduledOpen) {
        // Refetch on open rather than trusting whatever was last loaded: the
        // scheduler has been running behind this the whole time, and a last-run
        // time is the thing most likely to have moved since.
        loadScheduledTasks().then(() => {
            if (typeof renderActivity === 'function') renderActivity();
        });
    }
    if (typeof renderActivity === 'function') renderActivity();
}

// "3 minutes ago" beats a timestamp for the only question being asked here,
// which is whether it has run yet.
//
// What is stored is the *slot* a run claimed rather than a clock reading, and
// an hourly slot is written `2026-08-21T17` with no minutes on it — which
// `new Date` rejects outright. So an hourly task that had been running every
// hour for a week still read "not run yet", and the one line in the app that
// answers "did this happen" was answering it wrong for a third of the
// schedules on offer.
function whenLastRun(task) {
    if (!task.last_run) return 'not run yet';
    const claimed = /^\d{4}-\d{2}-\d{2}T\d{2}$/.test(task.last_run)
        ? task.last_run + ':00'
        : task.last_run;
    const then = new Date(claimed);
    if (isNaN(then)) return 'not run yet';
    const mins = Math.floor((Date.now() - then.getTime()) / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return mins + 'm ago';
    const hours = Math.floor(mins / 60);
    if (hours < 24) return hours + 'h ago';
    return Math.floor(hours / 24) + 'd ago';
}

function railScheduledList() {
    const wrap = document.createElement('div');
    wrap.className = 'nav-sched-list';
    if (!scheduledTasks.length) {
        wrap.innerHTML = '<div class="nav-idle">Nothing on the schedule</div>';
        return wrap;
    }
    // Active first: the paused ones are context, not the answer.
    const ordered = [...scheduledTasks].sort((a, b) => (b.enabled ? 1 : 0) - (a.enabled ? 1 : 0));
    wrap.innerHTML = ordered.map(task => {
        const failed = task.last_status === 'failed';
        // A failed run stores the exception where the report would go, so the
        // same row opens both — but it says which one it is opening, because
        // "report" over a stack trace is a small lie told every time.
        const output = (task.last_output || '').trim();
        const meta = escHtml(describeSchedule(task))
                   + ' · ' + escHtml(whenLastRun(task))
                   + (task.last_status ? ' · ' + escHtml(task.last_status) : '')
                   + (task.enabled ? '' : ' · paused')
                   + (output ? ' · <span class="nav-sched-report">'
                       + (failed ? 'error' : 'report') + '</span>' : '');
        const head = '<div class="nav-sched-what">' + escHtml(task.prompt) + '</div>'
                   + '<div class="nav-sched-when">' + meta + '</div>';
        // Only a run with something to show becomes a button. A task that has
        // never run is not a control that does nothing when you press it.
        return '<div class="nav-sched-item' + (task.enabled ? '' : ' is-off')
             +   (failed ? ' is-failed' : '') + '">'
             + (output
                 ? '<button class="nav-sched-head"'
                   + ' title="Read what this run reported"'
                   + ' onclick="openScheduledReport(\'' + task.id + '\')">'
                   + head + '</button>'
                 : head)
             + '</div>';
    }).join('');
    return wrap;
}

// The rail draws before anything asks the schedule for its count, so load it
// once at startup and redraw when it lands.
document.addEventListener('DOMContentLoaded', () => {
    loadScheduledTasks().then(() => {
        if (typeof renderActivity === 'function') renderActivity();
    });
});


// ================================================================
// The Scheduled page — where a task is changed rather than watched.
//
// Pausing and deleting were the whole of what could be done to a task that
// already existed, in a panel that is only on screen while you are writing a
// new one. So moving a daily brief from nine to eight meant deleting it and
// typing the sentence out again from memory, and getting the sentence slightly
// wrong is how you end up with a task that reports something other than what
// it used to.
//
// Every field is its own PATCH, which is what the route was built for: editing
// a scheduled task is "pause this" or "move it an hour", never "replace it".
// Saving happens on change rather than behind a Save button — there is one
// field per question here, and a button would only exist to be forgotten.
// ================================================================

// Reports open per task on this page, independently of the rail's. Two
// surfaces showing the same tasks should not fight over which ones are
// expanded.
const pageOutputOpen = new Set();

function togglePageOutput(id) {
    if (pageOutputOpen.has(id)) pageOutputOpen.delete(id);
    else pageOutputOpen.add(id);
    renderScheduledPage();
}

function loadScheduledPage() {
    loadScheduledTasks();
}

// One task, opened. This is where a row naming a specific job has to land —
// on the job, with its report already open and the page scrolled to it, so
// that reading what went wrong and changing when it runs are the same trip.
async function openScheduledTask(id) {
    pageOutputOpen.add(id);
    switchTab('scheduled');
    await loadScheduledTasks();
    const card = document.querySelector('.sched-card[data-task="' + id + '"]');
    if (!card) return;
    card.scrollIntoView({ block: 'center', behavior: 'smooth' });
    // A flash rather than a permanent mark: it answers "which of these did I
    // click" and then stops being true.
    card.classList.add('is-target');
    setTimeout(() => card.classList.remove('is-target'), 1600);
}

// Sending you to write it where every other agent task is written, with the
// schedule switch already thrown. A second composer on this page would be a
// second place to describe the same work.
function newScheduledFromComposer() {
    switchTab('workspace');
    if (typeof setChatMode === 'function') setChatMode('agent');
    setAgentWhen('schedule');
    document.getElementById('agent-input')?.focus();
}

function renderScheduledPage() {
    const host = document.getElementById('scheduled-page-list');
    if (!host) return;
    if (!scheduledTasks.length) {
        host.innerHTML = '<div class="empty">Nothing is scheduled. Anything you can '
                       + 'ask an agent to do, Carrot can do on its own — every '
                       + 'morning, every hour, or once a week.</div>';
        return;
    }
    host.innerHTML = scheduledTasks.map(scheduledPageCard).join('');
}

function scheduledPageCard(task) {
    const off = !task.enabled;
    const failed = task.last_status === 'failed';
    const output = (task.last_output || '').trim();
    const open = pageOutputOpen.has(task.id);
    const option = (value, current, label) =>
        '<option value="' + escHtml(value) + '"'
        + (value === current ? ' selected' : '') + '>' + escHtml(label) + '</option>';

    return '<div class="sched-card' + (off ? ' is-off' : '')
      +      (failed ? ' is-failed' : '') + '" data-task="' + escHtml(task.id) + '">'
      + '<div class="sched-card-top">'
      +   '<span class="sched-state' + (off ? ' is-off' : '') + '">'
      +     (off ? 'Paused' : 'Active') + '</span>'
      +   '<span class="sched-lastrun">' + escHtml(scheduledLastLine(task)) + '</span>'
      +   '<button class="btn btn-ghost small" onclick="toggleScheduledTask(\'' + task.id + '\')">'
      +     (off ? 'Resume' : 'Pause') + '</button>'
      +   '<button class="btn btn-ghost small danger" onclick="removeScheduledTask(\'' + task.id + '\')">'
      +     'Delete</button>'
      + '</div>'
      // The task itself gets the room it needs: this is a sentence somebody
      // wrote for a machine to act on unattended, and a one-line input that
      // shows eight words of it is how a typo in the other forty survives.
      + '<textarea class="sched-prompt" rows="2" spellcheck="false"'
      +   ' onchange="editScheduledTask(\'' + task.id + '\', \'prompt\', this.value)">'
      +   escHtml(task.prompt) + '</textarea>'
      + '<div class="sched-fields">'
      +   '<label>Runs<select onchange="editScheduledTask(\'' + task.id + '\', \'schedule\', this.value)">'
      +     scheduledMeta.schedules.map(s => option(s, task.schedule, SCHEDULE_WORDS[s] || s)).join('')
      +   '</select></label>'
      +   (task.schedule === 'hourly' ? '' :
          '<label>At<input type="time" value="' + escHtml(task.at || '09:00') + '"'
          + ' onchange="editScheduledTask(\'' + task.id + '\', \'at\', this.value)"></label>')
      +   (task.schedule === 'weekly' ?
          '<label>On<select onchange="editScheduledTask(\'' + task.id + '\', \'weekday\', this.value)">'
          + scheduledMeta.weekdays.map(d => option(d, task.weekday, titleCase(d))).join('')
          + '</select></label>' : '')
      + '</div>'
      + (output
          ? '<button class="sched-report-toggle' + (failed ? ' is-failed' : '') + '"'
            + ' onclick="togglePageOutput(\'' + task.id + '\')">'
            + (open ? '▾ hide' : '▸ ') + (failed ? 'what went wrong' : 'what it said last time')
            + '</button>'
          : '')
      + (open ? '<div class="sched-report">' + escHtml(output) + '</div>' : '')
      + '</div>';
}

const SCHEDULE_WORDS = {
    hourly: 'Every hour', daily: 'Every day', weekly: 'Every week',
};

function scheduledLastLine(task) {
    if (!task.last_run) return 'has not run yet';
    const status = task.last_status === 'failed' ? 'failed' : 'ran';
    return status + ' ' + whenLastRun(task);
}

// One field, one PATCH — and a reload rather than a local edit, because the
// engine normalises what it is sent (a bad time becomes 09:00) and a form
// still showing what you typed would be showing something that is not stored.
async function editScheduledTask(id, field, value) {
    const body = {};
    body[field] = value;
    try {
        await api('/api/scheduled/' + id, { method: 'PATCH', body: JSON.stringify(body) });
    } catch (e) {
        scheduleNote('Could not save that: ' + (e.message || e));
    }
    await loadScheduledTasks();
    if (typeof renderActivity === 'function') renderActivity();
}
