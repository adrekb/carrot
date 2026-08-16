// ===== Carrot Research and Carrot Agent =====
// Loaded after app.js and agentops.js, so `api`, `escHtml`, `authHeaders`,
// `mdToHtml` and `showApprovalPrompt` are already defined.
//
// Both views consume the same SSE trace format, so there is one reader here and
// two event handlers. Approval prompts arrive on those same streams and are
// handed straight to the shared prompt renderer — the agent's gate and the chat
// agent's gate are the same gate, and it looks the same to the user.

let researchRunId = null;
// The plan a research run is working to, which of it has landed, and which of
// it the evidence asked for after the run had started.
let researchGoals = [];
let researchDone = [];
let researchAdded = [];
let agentRunId = null;

// ---------- Shared stream reader ----------

async function streamTrace(url, body, onEvent) {
    const response = await fetch(url, {
        method: 'POST',
        headers: authHeaders(),
        body: JSON.stringify(body),
    });
    if (!response.ok) {
        const detail = (await response.json().catch(() => ({}))).detail;
        throw new Error(detail || response.statusText);
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let boundary;
        while ((boundary = buffer.indexOf('\n\n')) !== -1) {
            const raw = buffer.slice(0, boundary).trim();
            buffer = buffer.slice(boundary + 2);
            if (!raw.startsWith('data:')) continue;
            try {
                onEvent(JSON.parse(raw.slice(5).trim()));
            } catch (e) {
                console.warn('bad trace event', e);
            }
        }
    }
}

function traceLine(hostId, text, kind) {
    const host = document.getElementById(hostId);
    if (!host) return;
    const line = document.createElement('div');
    line.className = 'trace-line ' + (kind || '');
    line.textContent = text;
    host.appendChild(line);
    host.scrollTop = host.scrollHeight;
}

// ---------- Research ----------

const DEPTH_LABELS = {
    quick: 'Quick — 2 threads, one round',
    standard: 'Standard — 4 threads, two rounds',
    deep: 'Deep — 6 threads, three rounds',
    exhaustive: 'Exhaustive — 10 threads, five rounds (cloud model)',
};

// Exhaustive only appears when research is routed to a hosted model: an
// on-device model is the bottleneck at that volume, not the evidence.
async function loadResearchDepths() {
    const select = document.getElementById('research-depth');
    if (!select) return;
    let info;
    try { info = await api('/api/research/depths'); } catch (_) { return; }
    const current = select.value;
    select.innerHTML = (info.depths || []).map(d =>
        `<option value="${d}">${escHtml(DEPTH_LABELS[d] || d)}</option>`).join('');
    select.value = (info.depths || []).includes(current) ? current : (info.default || 'standard');
    const note = document.getElementById('research-depth-note');
    if (note) {
        note.textContent = info.local
            ? `Running on ${info.model} — assign Research to a cloud provider in Settings for Exhaustive.`
            : `Running on ${info.model} — Exhaustive available.`;
    }
}

async function loadResearch() {
    loadResearchDepths();
    try {
        const { runs } = await api('/api/research');
        renderResearchRuns(runs);
    } catch (e) {
        console.warn('research history failed', e);
    }
}

function renderResearchRuns(runs) {
    const host = document.getElementById('research-runs');
    if (!host) return;
    if (!runs.length) {
        host.innerHTML = '<div class="empty">No research runs yet.</div>';
        return;
    }
    host.innerHTML = runs.map(run => `
        <div class="list-item">
            <div class="list-main" onclick="openResearchRun('${escHtml(run.id)}')">
                <div class="list-title">${escHtml(run.question)}</div>
                <div class="list-sub">
                    ${escHtml(run.status)} · ${escHtml(run.depth)} ·
                    ${run.sources} source${run.sources === 1 ? '' : 's'} ·
                    ${escHtml((run.created_at || '').slice(0, 16).replace('T', ' '))}
                </div>
            </div>
            <button class="btn btn-ghost small" onclick="deleteResearchRun('${escHtml(run.id)}')">Delete</button>
        </div>`).join('');
}

async function openResearchRun(runId) {
    const run = await api(`/api/research/${runId}`);
    document.getElementById('research-report').innerHTML = mdToHtml(run.report || '_No report._');
    renderResearchSources(run.sources.map(s => ({
        id: s.id, kind: s.kind, title: s.title, locator: s.locator, tainted: s.tainted,
        tier: s.tier, tier_reason: s.tier_reason,
    })));
    const trace = document.getElementById('research-trace');
    trace.innerHTML = '';
    // A follow-up says what made it necessary. Reopened, the plan is a flat
    // list and a sub-question the evidence forced looks exactly like one that
    // was planned from the question — which loses the only part of it that
    // explains why the report goes where it does.
    (run.plan || []).forEach(p => traceLine('research-trace',
        p.added
            ? `follow-up: ${p.question} — because ${p.prompted_by || 'of what the first wave found'}`
            : 'sub-question: ' + p.question,
        'intent'));
    (run.findings || []).forEach(f =>
        traceLine('research-trace', `${f.verdict}: ${f.claim}`, f.verdict === 'supported' ? 'ok' : 'stage'));
}

async function deleteResearchRun(runId) {
    if (!confirm('Delete this research run and its sources?')) return;
    await api(`/api/research/${runId}`, { method: 'DELETE' });
    loadResearch();
}

// Who is speaking, in the words the report uses. The backend decides the tier;
// this only decides how loud it looks. Keeping the wording identical to
// carrot/websearch.py's TIER_MEANING matters — a legend in the panel that
// disagrees with the legend in the report teaches the reader to trust neither.
const TIER_MEANING = {
    'first-party': 'published by the party the claim is about',
    'official': 'government, standards body or official record',
    'reputable': 'established outlet with editorial accountability',
    'unknown': 'no signal either way',
    'low': 'shape of a content farm — likely rewording another source',
};
const TIER_ORDER = ['first-party', 'official', 'reputable', 'unknown', 'low'];

function renderResearchSources(sources) {
    const host = document.getElementById('research-sources');
    if (!host) return;
    if (!sources.length) {
        host.innerHTML = '<div class="empty">No sources read yet.</div>';
        return;
    }
    // Strongest first, matching the report's appendix. Read order is an
    // argument about what matters, and the old insertion order was making
    // that argument on behalf of whichever page the search backend returned
    // first — which is the one thing about a result that carries no meaning.
    const ranked = sources.slice().sort((a, b) => {
        const ai = TIER_ORDER.indexOf(a.tier || 'unknown');
        const bi = TIER_ORDER.indexOf(b.tier || 'unknown');
        return (ai < 0 ? 99 : ai) - (bi < 0 ? 99 : bi);
    });
    host.innerHTML = ranked.map(source => {
        const tier = source.tier || 'unknown';
        const why = source.tier_reason || TIER_MEANING[tier] || '';
        return `
        <div class="source-item tier-${escHtml(tier)}${source.tainted ? ' tainted' : ''}">
            <span class="source-id">${escHtml(source.id)}</span>
            <span class="source-tier" title="${escHtml(why)}">${escHtml(tier)}</span>
            <span class="source-kind">${escHtml(source.kind)}</span>
            <span class="source-title">${escHtml(source.title || source.locator)}</span>
            ${source.tainted ? '<span class="source-flag" title="This page tried to give the agent instructions">flagged</span>' : ''}
        </div>`;
    }).join('');
}

// A run can start from the question box or from a note sent here. Both produce
// the same trace, so the panes, the handler and the teardown are shared and
// only the endpoint differs.

function prepareResearchPanes(heading) {
    // A plan left over from the last run would tick against this one.
    researchGoals = [];
    researchDone = [];
    researchAdded = [];
    document.getElementById('research-plan-host').innerHTML = '';
    document.getElementById('research-trace').innerHTML = '';
    document.getElementById('research-sources').innerHTML = '';
    document.getElementById('research-report').innerHTML = '';
    document.getElementById('research-run-btn').classList.add('hidden');
    document.getElementById('research-stop-btn').classList.remove('hidden');
    if (heading) traceLine('research-trace', 'from note: ' + heading, 'intent');
}

function makeResearchHandler() {
    const sources = [];
    let report = '';

    return event => {
        if (event.run_id) researchRunId = event.run_id;
        if (event.stage) traceLine('research-trace', `${event.stage}: ${event.detail || ''}`, 'stage');
        // The same checklist chat uses. Sub-questions run in parallel, so
        // they tick out of order as each thread lands — which is honest about
        // what is actually happening and, with four threads and three rounds,
        // is most of what there is to watch.
        if (event.plan) {
            researchGoals = event.plan.map(item => item.question);
            researchDone = [];
            researchAdded = [];
            renderPlan(document.getElementById('research-plan-host'),
                       { goals: researchGoals, done: researchDone,
                         added: researchAdded });
        }
        // Sub-questions the evidence asked for after the run started. Appended
        // rather than replacing the plan, because by now the list has ticks on
        // it and re-sending the whole plan would clear them — and marked new,
        // since a checklist that silently grows between glances reads as one
        // you miscounted the first time.
        if (event.plan_added) {
            for (const item of event.plan_added) {
                if (researchGoals.includes(item.question)) continue;
                researchGoals.push(item.question);
                researchAdded.push(item.question);
            }
            renderPlan(document.getElementById('research-plan-host'),
                       { goals: researchGoals, done: researchDone,
                         added: researchAdded });
        }
        if (event.plan_progress) {
            if (!researchDone.includes(event.plan_progress.question)) {
                researchDone.push(event.plan_progress.question);
            }
            renderPlan(document.getElementById('research-plan-host'),
                       { goals: researchGoals, done: researchDone,
                         added: researchAdded });
            traceLine('research-trace',
                      `${event.plan_progress.outcome}: ${event.plan_progress.question}`,
                      'stage');
        }
        if (event.source) {
            sources.push(event.source);
            renderResearchSources(sources);
            const tier = event.source.tier || 'unknown';
            traceLine('research-trace',
                `read [${event.source.id}] (${tier}) ${event.source.title}`,
                // A content farm entering the evidence pool is worth seeing as
                // it happens, not only in the appendix once the report is
                // already written around it.
                tier === 'low' ? 'warn' : 'search');
        }
        if (event.finding) {
            traceLine('research-trace',
                `finding ${event.finding.sources.join('')} ${event.finding.claim}`, 'finding');
        }
        if (event.verdict) {
            traceLine('research-trace',
                `${event.verdict.verdict}: ${event.verdict.claim}`,
                event.verdict.verdict === 'supported' ? 'ok' : 'warn');
        }
        // Two claims that each checked out and cannot both be true. Worth
        // seeing in the trace rather than only as a line in the report: it is
        // the case where the report is least able to give you a straight
        // answer, and knowing that early is the point.
        // A claim resting on a page much older than the rest of the evidence.
        // The one that motivated this said a contract award was "imminent"
        // from a page two years old — every other check passed, because none
        // of them are about time.
        if (event.stale) {
            traceLine('research-trace',
                `dated ${event.stale.source_date}, `
                + `${event.stale.days_behind} days behind the newest source: `
                + event.stale.claim.slice(0, 120), 'warn');
        }
        if (event.conflict) {
            const tiers = (event.conflict.tiers || []).join(' vs ');
            traceLine('research-trace',
                `sources disagree (${tiers}) on ${event.conflict.subject}`, 'warn');
            (event.conflict.claims || []).forEach(claim =>
                traceLine('research-trace', `  · ${claim}`, 'stage'));
        }
        if (event.injection_warning) {
            traceLine('research-trace',
                `a source tried to give Carrot instructions (${event.injection_warning.origin}) — it was not obeyed`,
                'err');
        }
        if (event.token) {
            report += event.token;
            document.getElementById('research-report').innerHTML = mdToHtml(report);
        }
        if (event.error) traceLine('research-trace', event.error, 'err');
        if (event.done) {
            traceLine('research-trace',
                `done — ${event.sources} sources, ${event.findings} findings, `
                + `${event.rejected} rejected`
                + (event.conflicts ? `, ${event.conflicts} unresolved disagreement(s)` : ''),
                'ok');
            document.getElementById('research-report').innerHTML = mdToHtml(event.report);
        }
    };
}

async function runResearchStream(url, payload) {
    try {
        await streamTrace(url, payload, makeResearchHandler());
    } catch (e) {
        traceLine('research-trace', 'failed: ' + e.message, 'err');
    } finally {
        researchRunId = null;
        document.getElementById('research-run-btn').classList.remove('hidden');
        document.getElementById('research-stop-btn').classList.add('hidden');
        loadResearch();
    }
}

async function startResearch() {
    const question = document.getElementById('research-question').value.trim();
    if (!question) return;
    const depth = document.getElementById('research-depth').value;
    prepareResearchPanes('');
    await runResearchStream('/api/research/run', { question, depth });
}

// Entry point for a note whose @/to says research. The note is the question and
// its cited files arrive as evidence, so the trace opens with them already read.
async function streamResearchInto(payload) {
    await runResearchStream('/api/doc/send', payload);
}

async function stopResearch() {
    if (!researchRunId) return;
    await api(`/api/research/${researchRunId}/cancel`, { method: 'POST' });
    traceLine('research-trace', 'stopping…', 'warn');
}

// ---------- Agent ----------

async function loadAgent() {
    try {
        const status = await api('/api/agent/status');
        renderAgentAvailability(status);
        renderPolicy(status.policy);
    } catch (e) {
        console.warn('agent status failed', e);
    }
    try {
        renderAgentRuns((await api('/api/agent/runs')).runs);
    } catch (e) {
        console.warn('agent runs failed', e);
    }
}

function renderAgentAvailability(status) {
    const host = document.getElementById('agent-availability');
    if (!host) return;
    if (status.browser.available) {
        host.classList.add('hidden');
        return;
    }
    host.classList.remove('hidden');
    host.innerHTML = `
        <strong>Browser control is unavailable.</strong>
        ${escHtml(status.browser.reason)}.
        <pre>${escHtml(status.browser.hint)}</pre>`;
}

function renderPolicy(policy) {
    const domains = document.getElementById('policy-domains');
    if (domains) {
        domains.innerHTML = policy.allowed_domains.length
            ? policy.allowed_domains.map(domain => `
                <span class="chip">${escHtml(domain)}
                    <button onclick="removeAllowedDomain('${escHtml(domain)}')" title="Remove">×</button>
                </span>`).join('')
            : '<span class="muted small">No sites allowed yet — Carrot will ask before each one.</span>';
    }

    const secrets = document.getElementById('policy-secrets');
    if (secrets) {
        secrets.innerHTML = policy.secrets.length
            ? policy.secrets.map(name => `
                <span class="chip">${escHtml(name)}
                    <button onclick="removeSecret('${escHtml(name)}')" title="Delete">×</button>
                </span>`).join('')
            : '<span class="muted small">No stored credentials.</span>';
    }

    const budget = document.getElementById('policy-budget');
    if (budget) {
        budget.textContent =
            `A run stops after ${policy.budget.max_steps} steps, ${policy.budget.max_seconds}s, ` +
            `${policy.budget.max_navigations} navigations, or ${policy.budget.max_domains} distinct sites.`;
    }

    const desktopToggle = document.getElementById('policy-desktop-control');
    if (desktopToggle) desktopToggle.checked = !!policy.desktop_control_enabled;
    const criticalToggle = document.getElementById('policy-critical');
    if (criticalToggle) criticalToggle.checked = !!policy.critical_actions_enabled;
}

async function setPolicyFlag(key, value) {
    await api(`/api/config/${key}`, { method: 'PUT', body: JSON.stringify(value) });
    loadAgent();
}

async function addAllowedDomain() {
    const input = document.getElementById('policy-domain');
    const domain = input.value.trim();
    if (!domain) return;
    try {
        await api('/api/policy/domains', { method: 'POST', body: JSON.stringify({ domain }) });
        input.value = '';
        loadAgent();
    } catch (e) {
        alert(e.message);
    }
}

async function removeAllowedDomain(domain) {
    await api(`/api/policy/domains/${encodeURIComponent(domain)}`, { method: 'DELETE' });
    loadAgent();
}

async function addSecret() {
    const nameInput = document.getElementById('policy-secret-name');
    const valueInput = document.getElementById('policy-secret-value');
    if (!nameInput.value.trim() || !valueInput.value) return;
    try {
        await api('/api/policy/secrets', {
            method: 'POST',
            body: JSON.stringify({ name: nameInput.value.trim(), value: valueInput.value }),
        });
        nameInput.value = '';
        valueInput.value = '';
        loadAgent();
    } catch (e) {
        alert(e.message);
    }
}

async function removeSecret(name) {
    if (!confirm(`Delete the stored credential "${name}"?`)) return;
    await api(`/api/policy/secrets/${encodeURIComponent(name)}`, { method: 'DELETE' });
    loadAgent();
}

function agentStep(html, kind) {
    agentLogHeader();
    const host = document.getElementById('agent-log-body')
              || document.getElementById('agent-steps');
    const step = document.createElement('div');
    step.className = 'agent-step ' + (kind || '');
    step.innerHTML = html;
    host.appendChild(step);
    step.scrollIntoView({ block: 'nearest' });
}

// ===== The work log =====
//
// A run is a long silence with occasional lines in it. What makes that bearable
// is knowing it is still going and roughly how long it has been — so the log
// opens with a header that counts, and collapses to that header once the run
// is over. Finished work is a summary you can expand, not a wall you scroll
// past to reach the answer.

let agentLogTimer = null;
let agentLogStarted = 0;

function agentLogHeader() {
    let head = document.getElementById('agent-log-head');
    if (head) return head;
    const host = document.getElementById('agent-steps');
    const details = document.createElement('details');
    details.id = 'agent-log';
    details.className = 'agent-log';
    details.open = true;
    details.innerHTML = '<summary id="agent-log-head" class="agent-log-head">'
        + '<span class="agent-log-time">Working</span>'
        + '<svg class="ico agent-log-chev"><use href="#i-chevron"/></svg></summary>'
        + '<div id="agent-log-body" class="agent-log-body"></div>';
    host.appendChild(details);
    return details.querySelector('#agent-log-head');
}

function startAgentLog() {
    agentLogStarted = Date.now();
    agentLogHeader();
    clearInterval(agentLogTimer);
    const tick = () => {
        const el = document.querySelector('#agent-log-head .agent-log-time');
        if (!el) return;
        el.textContent = 'Working for ' + Math.round((Date.now() - agentLogStarted) / 1000) + 's';
    };
    tick();
    agentLogTimer = setInterval(tick, 1000);
}

// The run is over: stop counting, say how long it took, and fold. The steps
// stay one click away rather than being thrown out — that record is the only
// place what the agent actually did is written down.
function endAgentLog() {
    clearInterval(agentLogTimer);
    agentLogTimer = null;
    const el = document.querySelector('#agent-log-head .agent-log-time');
    if (el) el.textContent = 'Worked for ' + Math.round((Date.now() - agentLogStarted) / 1000) + 's';
    document.getElementById('agent-log')?.removeAttribute('open');
}

function prepareAgentPanes() {
    document.getElementById('agent-steps').innerHTML = '';
    document.getElementById('agent-plan').classList.add('hidden');
    document.getElementById('agent-result').classList.add('hidden');
    // Run and Stop were buttons on the agent's own page. The composer's send
    // and stop do both jobs now, so these are optional — and optional rather
    // than deleted because `getElementById(...).classList` on a missing
    // element throws, which would take the whole run down before it started.
    agentBusy(true);
    startAgentLog();
}

// The composer's own send/stop pair, driven by the run. Same control the chat
// uses, because from the outside a long agent run and a long answer are the
// same thing: something is happening and you may want it to stop.
function agentBusy(busy) {
    document.getElementById('send-btn')?.classList.toggle('hidden', busy);
    document.getElementById('stop-btn')?.classList.toggle('hidden', !busy);
}

async function runAgentStream(url, payload) {
    const startedAt = Date.now();
    let ending = '';
    try {
        await streamTrace(url, payload,
            event => {
                if (event.run_id) agentRunId = event.run_id;
                if (event.plan) {
                    const plan = document.getElementById('agent-plan');
                    plan.classList.remove('hidden');
                    plan.innerHTML = `<h4>Plan</h4><pre>${escHtml(event.plan)}</pre>`;
                }
                // The plan grew mid-run. The `plan` event above has already
                // redrawn the panel with the new lines in it, but the panel is
                // above the step list and the user is watching the bottom of
                // the run — so the growth is also announced where they are
                // looking. A plan that changes silently is one the user stops
                // trusting as a description of what is happening.
                if (event.plan_added) {
                    event.plan_added.forEach(step =>
                        agentStep(`<strong>Added to the plan:</strong> ${escHtml(step)}`, 'thought'));
                }
                if (event.approval_request) showApprovalPrompt(event.approval_request);
                if (event.approval_waiting) noteApprovalWaiting(event.approval_waiting);
                if (event.approval_resolved) dismissApprovalPrompt(event.approval_resolved.id);
                if (event.thought) agentStep(`<span class="agent-thought">${escHtml(event.thought)}</span>`, 'thought');
                if (event.action) {
                    agentStep(
                        `<span class="agent-action">${escHtml(event.action.action)}</span>` +
                        `<span class="agent-args">${escHtml(event.action.label || JSON.stringify(event.action.arguments))}</span>`,
                        'action');
                }
                if (event.denied) {
                    agentStep(
                        `<strong>Refused:</strong> ${escHtml(event.denied.action)} — ${escHtml(event.denied.reason)}`,
                        'denied');
                }
                if (event.observation) {
                    agentStep(`<span class="agent-observation">${escHtml(event.observation.result)}</span>`, 'observation');
                }
                if (event.injection_warning) {
                    agentStep(
                        `<strong>A page tried to give Carrot instructions</strong> ` +
                        `(${escHtml(event.injection_warning.origin)}). It was not obeyed, and every ` +
                        `action from here on will be confirmed individually.`,
                        'injection');
                }
                if (event.screenshot) {
                    agentStep(`<span class="agent-observation">screenshot saved: ${escHtml(event.screenshot)}</span>`, 'observation');
                }
                if (event.question) agentStep(`<strong>Carrot asks:</strong> ${escHtml(event.question)}`, 'question');
                if (event.error) agentStep(escHtml(event.error), 'denied');
                if (event.done) {
                    ending = `${event.status.replace('_', ' ')} — `
                           + (event.result || event.error || '');
                    const result = document.getElementById('agent-result');
                    result.classList.remove('hidden');
                    result.className = 'agent-result status-' + escHtml(event.status);
                    result.innerHTML =
                        `<h4>${escHtml(event.status.replace('_', ' '))}</h4>` +
                        `<div>${escHtml(event.result || event.error || '')}</div>` +
                        `<div class="muted small">${event.steps} step${event.steps === 1 ? '' : 's'} used</div>`;
                }
            });
    } catch (e) {
        agentStep('failed: ' + escHtml(e.message), 'denied');
    } finally {
        agentRunId = null;
        agentBusy(false);
        endAgentLog();
        // An agent run is the longest thing in the app and the least likely to
        // be watched all the way through. The status is in the toast, so a run
        // that ended in "needs input" is distinguishable from one that worked
        // without coming back to the window to find out.
        if (typeof notifyWhenLongRunFinishes === 'function') {
            notifyWhenLongRunFinishes(startedAt, 'Carrot Agent finished',
                                      ending || 'The run has ended.');
        }
        loadAgent();
    }
}

// The task comes from the chat composer now. The dedicated box it used to
// read is gone with the page that held it.
async function startAgentRun(task) {
    task = (task || '').trim();
    if (!task) return;
    prepareAgentPanes();
    await runAgentStream('/api/agent/run', {
        task,
        surface: document.getElementById('agent-surface').value,
        require_plan_approval: document.getElementById('agent-approve-plan').checked,
    });
}

// Entry point for a note whose @/to says agent: the note is the task, and its
// cited files ride along as background.
async function streamAgentInto(payload) {
    await runAgentStream('/api/doc/send', payload);
}

async function stopAgentRun() {
    if (!agentRunId) return;
    await api(`/api/agent/runs/${agentRunId}/stop`, { method: 'POST' });
    agentStep('stopping — the run ends before its next action', 'denied');
}

function renderAgentRuns(runs) {
    const host = document.getElementById('agent-runs');
    if (!host) return;
    if (!runs.length) {
        host.innerHTML = '<div class="empty">No agent runs yet.</div>';
        return;
    }
    host.innerHTML = runs.map(run => `
        <div class="list-item">
            <div class="list-main" onclick="openAgentRun('${escHtml(run.id)}')">
                <div class="list-title">${escHtml(run.task)}</div>
                <div class="list-sub">
                    ${escHtml(run.status)} · ${escHtml(run.surface)} ·
                    ${run.steps_used} step${run.steps_used === 1 ? '' : 's'} ·
                    ${escHtml((run.created_at || '').slice(0, 16).replace('T', ' '))}
                </div>
            </div>
        </div>`).join('');
}

async function openAgentRun(runId) {
    const run = await api(`/api/agent/runs/${runId}`);
    const host = document.getElementById('agent-steps');
    host.innerHTML = '';

    const plan = document.getElementById('agent-plan');
    plan.classList.remove('hidden');
    plan.innerHTML = `<h4>Plan</h4><pre>${escHtml(run.plan || '(none recorded)')}</pre>`;

    run.steps.forEach(step => {
        agentStep(
            `<span class="agent-action">${escHtml(step.action)}</span>` +
            `<span class="agent-args">${escHtml(JSON.stringify(step.arguments))}</span>` +
            `<div class="agent-verdict">${escHtml(step.decision)} · ${escHtml(step.risk)} risk` +
            (step.decision_reason ? ` — ${escHtml(step.decision_reason)}` : '') + `</div>` +
            `<div class="agent-observation">${escHtml((step.observation || '').slice(0, 600))}</div>`,
            step.decision === 'deny' ? 'denied' : 'action');
    });

    const result = document.getElementById('agent-result');
    result.classList.remove('hidden');
    result.className = 'agent-result status-' + escHtml(run.status);
    result.innerHTML =
        `<h4>${escHtml(String(run.status).replace('_', ' '))}</h4>` +
        `<div>${escHtml(run.result || run.error || '')}</div>`;
}

// ===== Extension packs =====
// A pack ships tools, skills, settings and a list of external programs it would
// like to have. The last of those is the honest part: the pack is listed
// whether or not LaTeX is installed, and says which is which, so a tool that
// cannot run says so here rather than failing partway through a task.

let packsExpanded = {};

async function loadPacks() {
    try {
        renderPacks((await api('/api/extensions')).extensions || []);
    } catch (e) {
        console.warn('packs failed', e);
    }
    // The shelf: everything that exists, minus what has already been added.
    try {
        const all = (await api('/api/extensions/catalog')).extensions || [];
        renderShelf(all.filter(pack => !pack.installed));
    } catch (e) {
        console.warn('catalog failed', e);
    }
}

function renderShelf(available) {
    const host = document.getElementById('packs-shelf');
    if (!host) return;
    if (!available.length) {
        host.innerHTML = '<div class="empty">You have added everything there is.</div>';
        return;
    }
    host.innerHTML = available.map(pack => `
        <div class="shelf-card">
            <div class="shelf-main">
                <div class="shelf-name">${escHtml(pack.name)}</div>
                <div class="shelf-desc">${escHtml(pack.description)}</div>
                <div class="shelf-meta">
                    v${escHtml(pack.version)} ·
                    ${pack.tool_count} tool${pack.tool_count === 1 ? '' : 's'} ·
                    ${pack.skill_count} skill${pack.skill_count === 1 ? '' : 's'}${
                        pack.tabs.length ? ' · adds a tab' : ''}
                </div>
            </div>
            <button class="btn btn-primary"
                    onclick="addExtension('${escHtml(pack.id)}')">Add</button>
        </div>`).join('');
}

async function addExtension(packId) {
    try {
        await api(`/api/extensions/${packId}/install`, { method: 'POST' });
    } catch (e) {
        console.warn('install failed', e);
    }
    // Both lists, because the pack has moved from one to the other, and the
    // nav as well: a pack that adds a tab has just changed what is in the
    // sidebar, and a tab that appears on the next reload rather than now
    // reads as the Add having done nothing.
    loadPacks();
    if (typeof applyExtensionTabs === 'function') applyExtensionTabs();
}

async function removeExtension(packId) {
    if (!confirm('Remove this extension? It will be switched off and its skills deleted.')) return;
    try {
        await api(`/api/extensions/${packId}/install`, { method: 'DELETE' });
    } catch (e) {
        console.warn('uninstall failed', e);
    }
    loadPacks();
    if (typeof applyExtensionTabs === 'function') applyExtensionTabs();
}

function renderPacks(packs) {
    const host = document.getElementById('packs-list');
    if (!host) return;
    if (!packs.length) {
        host.innerHTML = '<div class="empty">No extensions added yet — '
            + 'there are some below.</div>';
        return;
    }
    host.innerHTML = packs.map(pack => `
        <div class="pack-card${pack.enabled ? ' on' : ''}">
            <div class="pack-head">
                <div class="pack-main" onclick="togglePackDetail('${escHtml(pack.id)}')">
                    <div class="pack-name">
                        ${escHtml(pack.name)}
                        <span class="pack-version">v${escHtml(pack.version)}</span>
                    </div>
                    <div class="pack-desc">${escHtml(pack.description)}</div>
                    <div class="pack-counts">
                        ${pack.tool_count} tool${pack.tool_count === 1 ? '' : 's'} ·
                        ${pack.skill_count} skill${pack.skill_count === 1 ? '' : 's'}
                    </div>
                </div>
                <label class="switch-row">
                    <input type="checkbox" ${pack.enabled ? 'checked' : ''}
                           onchange="setPackEnabled('${escHtml(pack.id)}', this.checked)">
                    <span>${pack.enabled ? 'On' : 'Off'}</span>
                </label>
                <button class="icon-btn" title="Remove this extension"
                        onclick="removeExtension('${escHtml(pack.id)}')"
                    ><svg class="ico"><use href="#i-trash"/></svg></button>
            </div>
            <div id="pack-detail-${escHtml(pack.id)}" class="pack-detail hidden"></div>
        </div>`).join('');

    // Re-open whatever was open before the refresh, so toggling a setting does
    // not collapse the panel the user is working in.
    Object.keys(packsExpanded).forEach(id => {
        if (packsExpanded[id]) loadPackDetail(id);
    });
}

async function togglePackDetail(packId) {
    const detail = document.getElementById(`pack-detail-${packId}`);
    if (!detail) return;
    if (packsExpanded[packId]) {
        packsExpanded[packId] = false;
        detail.classList.add('hidden');
        return;
    }
    packsExpanded[packId] = true;
    await loadPackDetail(packId);
}

async function loadPackDetail(packId) {
    const detail = document.getElementById(`pack-detail-${packId}`);
    if (!detail) return;
    try {
        renderPackDetail(detail, await api(`/api/extensions/${packId}`));
    } catch (e) {
        detail.innerHTML = `<div class="empty error">${escHtml(e.message)}</div>`;
        detail.classList.remove('hidden');
    }
}

function renderPackDetail(host, pack) {
    const capabilities = (pack.capabilities || []).map(cap => `
        <div class="cap-row ${cap.available ? 'ok' : 'missing'}">
            <span class="cap-dot"></span>
            <span class="cap-name">${escHtml(cap.label)}</span>
            <span class="cap-state">${cap.available ? escHtml(cap.path) : 'not installed'}</span>
            <div class="cap-purpose">
                ${escHtml(cap.purpose)}
                ${cap.available ? '' : ` <em>${escHtml(cap.install_hint)}</em>`}
            </div>
        </div>`).join('');

    const settings = (pack.settings || []).map(setting => {
        const id = `pack-setting-${pack.id}-${setting.key}`;
        let field;
        if (setting.type === 'boolean') {
            field = `<input type="checkbox" id="${id}" ${setting.value ? 'checked' : ''}
                            onchange="savePackSetting('${escHtml(pack.id)}', '${escHtml(setting.key)}', this.checked)">`;
        } else if (setting.type === 'select') {
            field = `<select id="${id}"
                             onchange="savePackSetting('${escHtml(pack.id)}', '${escHtml(setting.key)}', this.value)">
                ${(setting.options || []).map(option => `
                    <option ${option === setting.value ? 'selected' : ''}>${escHtml(option)}</option>`).join('')}
            </select>`;
        } else {
            field = `<input type="text" id="${id}" value="${escHtml(setting.value || '')}"
                            placeholder="${escHtml(setting.default || '')}"
                            onchange="savePackSetting('${escHtml(pack.id)}', '${escHtml(setting.key)}', this.value)">`;
        }
        return `
            <div class="pack-setting">
                <label for="${id}">${escHtml(setting.label)}</label>
                ${field}
                <div class="pack-setting-help">${escHtml(setting.help || '')}</div>
            </div>`;
    }).join('');

    const tools = (pack.tools || []).map(tool => `
        <div class="pack-tool">
            <span class="mono">${escHtml(tool.name)}</span>
            ${tool.mutating ? '<span class="pack-tag risky">asks first</span>' : ''}
            ${tool.requires ? `<span class="pack-tag">needs ${escHtml(tool.requires)}</span>` : ''}
            <div class="pack-tool-desc">${escHtml(tool.description)}</div>
        </div>`).join('');

    const skills = (pack.skills || []).map(skill => `
        <div class="pack-tool">
            <span class="mono">/${escHtml(skill.slug)}</span>
            <div class="pack-tool-desc">${escHtml(skill.description)}</div>
        </div>`).join('');

    // First, above everything else. The tool list tells you what the pack
    // *has*; this tells you what to do with it, and knowing where to begin is
    // a different question from wanting the pack in the first place. Reading
    // "latex_compile, bib_check, matlab_run" and working out the first move
    // was left entirely to the user.
    const tutorial = (pack.tutorial || []).map((step, i) => `
        <li class="pack-step">
            <span class="pack-step-n">${i + 1}</span>
            <div>
                <div class="pack-step-title">${escHtml(step.title)}</div>
                <div class="pack-step-body">${escHtml(step.body)}</div>
            </div>
        </li>`).join('');

    host.innerHTML = `
        ${tutorial ? `<h4 class="pack-section">Getting started</h4>
            <ol class="pack-steps">${tutorial}</ol>` : ''}
        ${capabilities ? `<h4 class="pack-section">On this machine</h4>${capabilities}` : ''}
        ${settings ? `<h4 class="pack-section">Settings</h4>
            <p class="muted small">Changing these rewrites the pack's skills, so the
            instructions always name your current venue and style.</p>${settings}` : ''}
        ${tools ? `<h4 class="pack-section">Tools</h4>${tools}` : ''}
        ${skills ? `<h4 class="pack-section">Skills</h4>
            <p class="muted small">Installed into your skills when the pack is on, and
            reachable with <code>/</code> in the command bar.</p>${skills}` : ''}`;
    host.classList.remove('hidden');
}

async function setPackEnabled(packId, enabled) {
    try {
        await api(`/api/extensions/${packId}/enabled`, {
            method: 'PUT', body: JSON.stringify({ enabled }),
        });
    } catch (e) {
        alert(e.message);
    }
    await loadPacks();
    // A pack's skills land in the ordinary skills list, so refresh that too.
    if (typeof loadSkillsList === 'function') loadSkillsList();
    if (typeof loadSkillCatalog === 'function') loadSkillCatalog();
    // And its tabs appear or disappear from the sidebar. Without this the
    // switch reads as broken: you turn the Planner on, the card says enabled,
    // and the nav does not change until the next reload.
    if (typeof applyExtensionTabs === 'function') await applyExtensionTabs();
}

async function savePackSetting(packId, key, value) {
    try {
        await api(`/api/extensions/${packId}/settings/${key}`, {
            method: 'PUT', body: JSON.stringify(value),
        });
    } catch (e) {
        alert(e.message);
    }
}
