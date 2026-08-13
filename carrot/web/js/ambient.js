// ===== Ambient Recall =====
//
// Loaded after app.js, so `api`, `escHtml` and `switchTab` exist.
//
// The panel is ordered by the questions a person actually has about something
// that watches their screen: is it on, how do I stop it, what will it refuse
// to look at, and only then what did it find. A feature like this earns trust
// by being legible about its own state, so "not capturing, and here is which
// rule stopped it" is the most important line on the page — a feature that
// silently stops looks exactly like one that is broken, and the user's next
// move is to turn it off for good.

let ambientTimer = null;

async function loadAmbient() {
    await refreshAmbient();
    await ambientLoadTimeline();
    // Polled while the tab is open and stopped when it is not. The status
    // changes on its own — battery, a model starting up, a password field —
    // so a static panel would be lying within about a minute.
    if (ambientTimer) clearInterval(ambientTimer);
    ambientTimer = setInterval(() => {
        if (!document.getElementById('view-ambient')?.classList.contains('active')) {
            clearInterval(ambientTimer);
            ambientTimer = null;
            return;
        }
        refreshAmbient();
    }, 5000);
}

function ambientRuleLabel(rule) {
    return {
        secure_input: 'a password field has focus',
        private_window: 'a private browsing window',
        known_secret_app: 'a credential app',
        sensitive_title: 'a sensitive window title',
        excluded_app: 'an app you excluded',
        excluded_title: 'a title you excluded',
        excluded_url: 'a domain you excluded',
        model_busy: 'a model is generating',
        battery_low: 'the battery is low',
        on_battery: 'running on battery',
        memory_low: 'memory is tight',
        vram_low: 'VRAM is tight',
        not_installed: 'something is not installed',
        unchanged: 'the screen has not changed',
    }[rule] || rule || '';
}

async function refreshAmbient() {
    let state;
    try {
        state = await api('/api/ambient/status');
    } catch (_) {
        document.getElementById('ambient-status').textContent =
            'Could not read the ambient status.';
        return;
    }

    const caps = state.capabilities || {};
    const stats = state.stats || {};
    const last = state.last || {};
    const running = !!state.running;

    const button = document.getElementById('ambient-toggle');
    if (button) {
        button.textContent = running ? 'Stop' : 'Start';
        button.classList.toggle('btn-primary', !running);
    }

    // Missing pieces come first and the start button says so, because a
    // switch that appears to work and does nothing is the worst version of
    // this. Each one names the command that fixes it.
    if (!caps.ready) {
        document.getElementById('ambient-status').innerHTML =
            '<div class="ambient-blocked">'
            + '<div class="ambient-blocked-head">Not ready on this machine</div>'
            + (caps.missing || []).map(m =>
                `<div class="ambient-missing">${escHtml(m.what)}
                 <code>${escHtml(m.fix)}</code></div>`).join('')
            + '</div>';
        if (button) button.disabled = true;
        return;
    }
    if (button) button.disabled = false;

    const reason = last.reason
        ? `${last.captured ? 'Captured' : 'Not capturing'} — ${escHtml(last.reason)}`
        : (running ? 'Starting…' : 'Stopped.');

    document.getElementById('ambient-status').innerHTML = `
        <div class="ambient-line">
            <span class="ambient-dot ${running ? (last.captured ? 'on' : 'wait') : 'off'}"></span>
            <span>${reason}</span>
        </div>
        ${last.title ? `<div class="muted small">last saw: ${escHtml(last.title)}</div>` : ''}
        <div class="ambient-stats">
            <span><b>${stats.frames || 0}</b> moments</span>
            <span><b>${stats.kilobytes || 0}</b> KB of text</span>
            <span>read by <b>${escHtml(caps.ocr || 'nothing')}</b></span>
            <span><b>${state.captures || 0}</b> kept · <b>${state.skips || 0}</b> skipped</span>
        </div>
        <div class="muted small">Screenshots are never written to disk — only the
        text on them, and only on this machine.</div>`;

    renderAmbientRules(state.policy || {});
}

function renderAmbientRules(policy) {
    const host = document.getElementById('ambient-rules');
    if (!host) return;
    const rules = [
        ['skip_private_windows', 'Private browsing windows'],
        ['skip_password_fields', 'Anything with a password field focused'],
        ['skip_known_secret_apps', 'Password managers and authenticators'],
        ['skip_sensitive_titles', 'Windows titled banking, tax, medical, 2FA…'],
    ];
    host.innerHTML = rules.map(([key, label]) => `
        <label class="switch-row">
            <input type="checkbox" ${policy[key] ? 'checked' : ''}
                   onchange="ambientSetRule('${key}', this.checked)">
            <span>${escHtml(label)}</span>
        </label>`).join('');

    const custom = document.getElementById('ambient-exclusions');
    const all = [
        ...(policy.excluded_apps || []).map(v => ['app', v]),
        ...(policy.excluded_titles || []).map(v => ['title', v]),
        ...(policy.excluded_urls || []).map(v => ['url', v]),
    ];
    custom.innerHTML = all.length
        ? all.map(([kind, value]) => `
            <div class="ambient-excl">
                <span class="badge">${escHtml(kind)}</span>
                <span>${escHtml(value)}</span>
                <button class="btn btn-ghost btn-sm"
                        onclick="ambientRemoveExclusion('${escHtml(kind)}', '${escHtml(value)}')">
                  Remove</button>
            </div>`).join('')
        : '<div class="muted small">Nothing added beyond the defaults above.</div>';
}

async function ambientSetRule(key, value) {
    await api('/api/ambient/policy', {
        method: 'PUT', body: JSON.stringify({ policy: { [key]: value } }),
    });
    refreshAmbient();
}

async function ambientAddExclusion() {
    const kind = document.getElementById('ambient-excl-kind').value;
    const value = document.getElementById('ambient-excl-value').value.trim();
    if (!value) return;
    try {
        await api('/api/ambient/exclusions', {
            method: 'POST', body: JSON.stringify({ kind, value }),
        });
        document.getElementById('ambient-excl-value').value = '';
        refreshAmbient();
    } catch (e) { alert(e.message); }
}

async function ambientRemoveExclusion(kind, value) {
    await api('/api/ambient/exclusions/remove', {
        method: 'POST', body: JSON.stringify({ kind, value }),
    });
    refreshAmbient();
}

async function ambientToggle() {
    const button = document.getElementById('ambient-toggle');
    const starting = button.textContent === 'Start';
    button.disabled = true;
    try {
        await api(`/api/ambient/${starting ? 'start' : 'stop'}`, { method: 'POST' });
    } catch (e) {
        alert(e.message);
    } finally {
        button.disabled = false;
        refreshAmbient();
    }
}

// One frame, on request. Worth having as its own button: it is how somebody
// finds out what this feature actually keeps before agreeing to leave it on,
// and the answer is far more convincing shown than described.
async function ambientCaptureNow() {
    const host = document.getElementById('ambient-status');
    try {
        const result = await api('/api/ambient/capture', { method: 'POST' });
        const detail = result.captured
            ? `Kept ${result.chars} characters from ${escHtml(result.title || result.app || 'the screen')}.`
            : `Nothing kept — ${escHtml(result.reason || 'refused')}.`;
        host.insertAdjacentHTML('afterbegin',
            `<div class="ambient-flash">${detail}</div>`);
        setTimeout(() => host.querySelector('.ambient-flash')?.remove(), 6000);
    } catch (e) {
        alert(e.message);
    }
    refreshAmbient();
    ambientLoadTimeline();
}

async function ambientRecall() {
    const query = document.getElementById('ambient-query').value.trim();
    const host = document.getElementById('ambient-results');
    if (!query) { host.innerHTML = ''; return; }
    host.innerHTML = '<div class="muted small">Searching…</div>';
    let results;
    try {
        ({ results } = await api('/api/ambient/recall', {
            method: 'POST', body: JSON.stringify({ query, limit: 20 }),
        }));
    } catch (e) {
        host.innerHTML = `<div class="muted small">${escHtml(e.message)}</div>`;
        return;
    }
    if (!results.length) {
        host.innerHTML = '<div class="muted small">Nothing on screen matched that.</div>';
        return;
    }
    host.innerHTML = results.map(r => ambientFrameRow(r, r.snippet)).join('');
}

function ambientWhen(iso) {
    if (!iso) return '';
    const then = new Date(iso);
    if (isNaN(then)) return '';
    const mins = Math.round((Date.now() - then.getTime()) / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    if (mins < 60 * 24) return `${Math.round(mins / 60)}h ago`;
    return then.toLocaleString(undefined,
        { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function ambientFrameRow(frame, body) {
    return `
        <div class="ambient-frame" data-id="${escHtml(frame.id)}">
            <div class="ambient-frame-head">
                <span class="ambient-app">${escHtml(frame.app || 'unknown app')}</span>
                <span class="ambient-title">${escHtml(frame.title || '')}</span>
                <span class="ambient-when">${escHtml(ambientWhen(frame.captured_at))}</span>
                <button class="btn btn-ghost btn-sm"
                        onclick="ambientForgetFrame('${escHtml(frame.id)}')">Forget</button>
            </div>
            <div class="ambient-text">${escHtml(body || frame.preview || '')}</div>
        </div>`;
}

async function ambientLoadTimeline() {
    const host = document.getElementById('ambient-timeline');
    if (!host) return;
    let frames;
    try {
        ({ frames } = await api('/api/ambient/timeline?limit=40'));
    } catch (_) { return; }
    host.innerHTML = frames.length
        ? frames.map(f => ambientFrameRow(f, f.preview)).join('')
        : '<div class="muted small">Nothing captured yet.</div>';
}

async function ambientForgetFrame(id) {
    await api(`/api/ambient/frames/${id}`, { method: 'DELETE' });
    document.querySelector(`.ambient-frame[data-id="${id}"]`)?.remove();
    refreshAmbient();
}

async function ambientForgetHours(hours) {
    if (!confirm(`Forget everything captured in the last ${hours} hour(s)?`)) return;
    const since = new Date(Date.now() - hours * 3600 * 1000).toISOString();
    const { forgotten } = await api('/api/ambient/forget', {
        method: 'POST', body: JSON.stringify({ since }),
    });
    alert(`Forgot ${forgotten} moment(s).`);
    refreshAmbient();
    ambientLoadTimeline();
}

async function ambientForgetAll() {
    // Typed rather than clicked. Everything is a different decision from the
    // last hour, and the same reasoning the policy kernel applies to
    // irreversible actions applies here: a button you can hit by accident is
    // not consent.
    //
    // `inlineTextPrompt` rather than `window.prompt`, which Electron disables
    // — it returns null without showing anything, so the confirmation would
    // never appear and the button would silently do nothing in the desktop
    // app, which is where most people run this.
    const answer = await inlineTextPrompt({
        title: 'This deletes every captured moment. Type FORGET to confirm.',
        placeholder: 'FORGET',
        action: 'Forget everything',
    });
    if (answer !== 'FORGET') return;
    const { forgotten } = await api('/api/ambient/frames', { method: 'DELETE' });
    alert(`Forgot ${forgotten} moment(s).`);
    refreshAmbient();
    ambientLoadTimeline();
}
