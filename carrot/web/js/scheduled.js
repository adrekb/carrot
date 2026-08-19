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

async function loadScheduledTasks() {
    try {
        const data = await api('/api/scheduled');
        scheduledTasks = data.tasks || [];
    } catch (e) {
        scheduledTasks = [];
    }
    renderScheduledTasks();
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
// ================================================================

let railScheduledOpen = false;

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
function whenLastRun(task) {
    if (!task.last_run) return 'not run yet';
    const then = new Date(task.last_run);
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
        const status = task.last_status ? ' · ' + escHtml(task.last_status) : '';
        return '<div class="nav-sched-item' + (task.enabled ? '' : ' is-off') + '">'
             + '<div class="nav-sched-what">' + escHtml(task.prompt) + '</div>'
             + '<div class="nav-sched-when">' + escHtml(describeSchedule(task))
             +   ' · ' + escHtml(whenLastRun(task)) + status
             +   (task.enabled ? '' : ' · paused') + '</div>'
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
