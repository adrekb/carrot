// ===== Carrot Research and Carrot Agent =====
// Loaded after app.js and agentops.js, so `api`, `escHtml`, `authHeaders`,
// `mdToHtml` and `showApprovalPrompt` are already defined.
//
// Both views consume the same SSE trace format, so there is one reader here and
// two event handlers. Approval prompts arrive on those same streams and are
// handed straight to the shared prompt renderer — the agent's gate and the chat
// agent's gate are the same gate, and it looks the same to the user.

let researchRunId = null;
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

async function loadResearch() {
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
    })));
    const trace = document.getElementById('research-trace');
    trace.innerHTML = '';
    (run.plan || []).forEach(p => traceLine('research-trace', 'sub-question: ' + p.question, 'intent'));
    (run.findings || []).forEach(f =>
        traceLine('research-trace', `${f.verdict}: ${f.claim}`, f.verdict === 'supported' ? 'ok' : 'stage'));
}

async function deleteResearchRun(runId) {
    if (!confirm('Delete this research run and its sources?')) return;
    await api(`/api/research/${runId}`, { method: 'DELETE' });
    loadResearch();
}

function renderResearchSources(sources) {
    const host = document.getElementById('research-sources');
    if (!host) return;
    if (!sources.length) {
        host.innerHTML = '<div class="empty">No sources read yet.</div>';
        return;
    }
    host.innerHTML = sources.map(source => `
        <div class="source-item${source.tainted ? ' tainted' : ''}">
            <span class="source-id">${escHtml(source.id)}</span>
            <span class="source-kind">${escHtml(source.kind)}</span>
            <span class="source-title">${escHtml(source.title || source.locator)}</span>
            ${source.tainted ? '<span class="source-flag" title="This page tried to give the agent instructions">flagged</span>' : ''}
        </div>`).join('');
}

async function startResearch() {
    const question = document.getElementById('research-question').value.trim();
    if (!question) return;

    const depth = document.getElementById('research-depth').value;
    document.getElementById('research-trace').innerHTML = '';
    document.getElementById('research-sources').innerHTML = '';
    document.getElementById('research-report').innerHTML = '';
    document.getElementById('research-run-btn').classList.add('hidden');
    document.getElementById('research-stop-btn').classList.remove('hidden');

    const sources = [];
    let report = '';

    try {
        await streamTrace('/api/research/run', { question, depth }, event => {
            if (event.run_id) researchRunId = event.run_id;
            if (event.stage) traceLine('research-trace', `${event.stage}: ${event.detail || ''}`, 'stage');
            if (event.plan) {
                event.plan.forEach(item =>
                    traceLine('research-trace', 'sub-question: ' + item.question, 'intent'));
            }
            if (event.source) {
                sources.push(event.source);
                renderResearchSources(sources);
                traceLine('research-trace', `read [${event.source.id}] ${event.source.title}`, 'search');
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
                    `done — ${event.sources} sources, ${event.findings} findings, ${event.rejected} rejected`,
                    'ok');
                document.getElementById('research-report').innerHTML = mdToHtml(event.report);
            }
        });
    } catch (e) {
        traceLine('research-trace', 'failed: ' + e.message, 'err');
    } finally {
        researchRunId = null;
        document.getElementById('research-run-btn').classList.remove('hidden');
        document.getElementById('research-stop-btn').classList.add('hidden');
        loadResearch();
    }
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
    const host = document.getElementById('agent-steps');
    const step = document.createElement('div');
    step.className = 'agent-step ' + (kind || '');
    step.innerHTML = html;
    host.appendChild(step);
    step.scrollIntoView({ block: 'nearest' });
}

async function startAgentRun() {
    const task = document.getElementById('agent-task').value.trim();
    if (!task) return;

    const surface = document.getElementById('agent-surface').value;
    const requirePlanApproval = document.getElementById('agent-approve-plan').checked;

    document.getElementById('agent-steps').innerHTML = '';
    document.getElementById('agent-plan').classList.add('hidden');
    document.getElementById('agent-result').classList.add('hidden');
    document.getElementById('agent-run-btn').classList.add('hidden');
    document.getElementById('agent-stop-btn').classList.remove('hidden');

    try {
        await streamTrace('/api/agent/run',
            { task, surface, require_plan_approval: requirePlanApproval },
            event => {
                if (event.run_id) agentRunId = event.run_id;
                if (event.plan) {
                    const plan = document.getElementById('agent-plan');
                    plan.classList.remove('hidden');
                    plan.innerHTML = `<h4>Plan</h4><pre>${escHtml(event.plan)}</pre>`;
                }
                if (event.approval_request) showApprovalPrompt(event.approval_request);
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
        document.getElementById('agent-run-btn').classList.remove('hidden');
        document.getElementById('agent-stop-btn').classList.add('hidden');
        loadAgent();
    }
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
