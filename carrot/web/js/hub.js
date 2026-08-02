// ===== Model Hub — hardware-aware model catalog =====
// Backed by /api/hub: detected specs, the fit-annotated catalog, and
// per-role picks. Live results come from the public Hugging Face API.

let hubData = null;
let hubUseCase = '';
let hubModalities = new Set(); // required input modalities (image/audio/video)
let hubSort = 'trending';

const HUB_SORTS = [
    ['trending', 'Top choice'],
    ['popular', 'Popular'],
    ['recent', 'Recent releases'],
];

const FIT_LABEL = {
    great: 'Runs great',
    good: 'Runs well',
    tight: 'Tight fit — slower',
    too_big: 'Too big for this machine',
};

async function loadHub() {
    try {
        hubData = await api('/api/hub');
    } catch (e) {
        document.getElementById('hub-grid').innerHTML =
            `<div class="empty">Could not load the hub: ${escHtml(e.message)}</div>`;
        return;
    }
    document.getElementById('hub-site-link').href = hubData.browse_url || hubData.hub_url;
    renderHubSpecs();
    renderHubChips();
    renderHubMeta();
    await renderHubModels();
    runHubSearch(); // live HF results load alongside the curated picks
    renderHubStorage();
}

// ===== Live thin-client search =====
// specs read locally -> live HF fetch -> local quant planning -> ranked grid.

async function runHubSearch() {
    const grid = document.getElementById('hub-live-grid');
    const meta = document.getElementById('hub-live-meta');
    grid.innerHTML = '<div class="empty">Searching Hugging Face…</div>';
    const workload = document.getElementById('hub-workload').value.trim();
    const qs = new URLSearchParams({ workload, sort: hubSort });
    for (const m of hubModalities) qs.set(m, 'true');
    let data;
    try {
        data = await api('/api/hub/search?' + qs.toString());
    } catch (e) {
        grid.innerHTML = `<div class="empty">Search failed: ${escHtml(e.message)}</div>`;
        return;
    }
    if (data.source === 'offline') {
        meta.textContent = '';
        grid.innerHTML = `<div class="empty">${escHtml(data.detail || 'Hugging Face unreachable.')}</div>`;
        return;
    }
    const prof = data.profile || {};
    const understood = [...(prof.use_cases || []), ...(prof.modalities || [])];
    meta.textContent = 'Live from Hugging Face'
        + (understood.length ? ` · matching: ${understood.join(', ')}` : '');
    const { installed, active } = await hubInstalledSet();
    grid.innerHTML = '';
    if (!(data.results || []).length) {
        grid.innerHTML = '<div class="empty">Nothing on Hugging Face fits this machine and filter.</div>';
        return;
    }
    data.results.forEach((m, i) => grid.appendChild(hubCard(m, installed, active, i === 0 ? 'Top match' : null)));
}

function hubSpecLine(s) {
    const parts = [`${s.ram_gb} GB RAM`];
    if (s.backend === 'cuda') parts.push(`${s.vram_gb} GB VRAM (${(s.gpu || 'GPU').split(';')[0].trim()})`);
    else if (s.backend === 'metal') parts.push('Apple Silicon unified memory');
    else parts.push('CPU inference');
    return parts.join(' · ');
}

function renderHubSpecs() {
    const s = hubData.specs;
    document.getElementById('hub-specs').innerHTML = `
        <div class="hub-spec-card">
          <div><span class="muted small">Your machine</span><br><strong>${escHtml(hubSpecLine(s))}</strong></div>
          <div><span class="muted small">Model budget</span><br><strong>${s.model_budget_gb} GB</strong>
            <span class="muted small">memory Carrot plans models into</span></div>
        </div>`;
}

function renderHubChips() {
    const wrap = document.getElementById('hub-usecase-chips');
    wrap.innerHTML = '';
    const all = ['', ...(hubData.use_cases || [])];
    for (const uc of all) {
        const b = document.createElement('button');
        b.className = 'chip' + (hubUseCase === uc ? ' active' : '');
        b.textContent = uc === '' ? 'All' : uc;
        b.onclick = () => { hubUseCase = uc; renderHubChips(); renderHubModels(); };
        wrap.appendChild(b);
    }
    // Sort modes for the live search.
    const swrap = document.getElementById('hub-sort-chips');
    swrap.innerHTML = '';
    for (const [key, label] of HUB_SORTS) {
        const b = document.createElement('button');
        b.className = 'chip' + (hubSort === key ? ' active' : '');
        b.textContent = label;
        b.onclick = () => { hubSort = key; renderHubChips(); runHubSearch(); };
        swrap.appendChild(b);
    }
    // Y/N toggles for extra input modalities — on means "must support it".
    const mwrap = document.getElementById('hub-modality-toggles');
    mwrap.innerHTML = '';
    for (const mod of hubData.modalities || []) {
        const on = hubModalities.has(mod);
        const b = document.createElement('button');
        b.className = 'chip mod-chip' + (on ? ' active' : '');
        b.textContent = `${mod}: ${on ? 'Y' : 'N'}`;
        b.onclick = () => {
            if (hubModalities.has(mod)) hubModalities.delete(mod); else hubModalities.add(mod);
            renderHubChips();
            renderHubModels();
            runHubSearch();
        };
        mwrap.appendChild(b);
    }
}

function renderHubMeta() {
    const el = document.getElementById('hub-catalog-meta');
    const src = hubData.catalog_source;
    if (src === 'bundled') el.textContent = "Carrot's built-in list";
    else el.textContent = `Custom catalog · updated ${hubData.catalog_fetched_at ? hubData.catalog_fetched_at.slice(0, 10) : ''}`;
}

async function hubInstalledSet() {
    try {
        const data = await api('/api/models');
        return { installed: new Set(data.installed.map(m => m.name)), active: data.active_model };
    } catch (_) {
        return { installed: new Set(), active: null };
    }
}

async function renderHubModels() {
    const grid = document.getElementById('hub-grid');
    const recsEl = document.getElementById('hub-recs');
    const { installed, active } = await hubInstalledSet();
    const recs = hubData.recommendations || {};
    const bestId = recs.best ? recs.best.id : null;

    // Headline picks: best overall, light, and the current use-case pick.
    recsEl.innerHTML = '';
    if (recs.best) {
        const picks = [];
        picks.push({ role: 'Recommended for you', m: recs.best });
        if (recs.light && recs.light.id !== recs.best.id) picks.push({ role: 'Light & fast', m: recs.light });
        const uc = hubUseCase && recs.by_use_case ? recs.by_use_case[hubUseCase] : null;
        if (uc && !picks.some(p => p.m.id === uc.id)) picks.push({ role: `Best for ${hubUseCase}`, m: uc });
        for (const p of picks) recsEl.appendChild(hubCard(p.m, installed, active, p.role));
    }

    let models = hubData.models || [];
    if (hubUseCase) models = models.filter(m => (m.use_cases || []).includes(hubUseCase));
    if (hubModalities.size) {
        models = models.filter(m =>
            [...hubModalities].every(mod => (m.modalities || []).includes(mod)));
    }
    // Fits-first, then by size.
    const fitOrder = { great: 0, good: 1, tight: 2, too_big: 3 };
    models = [...models].sort((a, b) =>
        (fitOrder[a.fit] - fitOrder[b.fit]) || (a.min_mem_gb - b.min_mem_gb));

    grid.innerHTML = '';
    if (!models.length) {
        grid.innerHTML = '<div class="empty">No models match this filter.</div>';
    }
    for (const m of models) grid.appendChild(hubCard(m, installed, active, m.id === bestId ? 'Recommended' : null));
}

function hubCard(m, installed, active, role) {
    const card = document.createElement('div');
    card.className = `hub-card fit-${m.fit}` + (role ? ' pick' : '');
    const isInstalled = installed.has(m.id) || [...installed].some(n => n.startsWith(m.id + ':'));
    const isActive = active === m.id;
    card.innerHTML = `
        ${role ? `<div class="hub-role">${escHtml(role)}</div>` : ''}
        <div class="hub-card-head">
          <strong>${escHtml(m.label || m.id)}</strong>
          <span class="fit-badge fit-${m.fit}">${FIT_LABEL[m.fit] || m.fit}</span>
        </div>
        <div class="muted small">${escHtml(m.id)} · ${escHtml(m.quant || '')} · ${m.download_gb} GB download · needs ~${m.min_mem_gb} GB${m.est_tps ? ` · ~${m.est_tps} tok/s` : ''}</div>
        ${m.quant_reason ? `<div class="muted small quant-note">${escHtml(m.quant_reason)}</div>` : ''}
        <div class="hub-blurb">${escHtml(m.blurb || '')}${m.hf_url ? ` <a href="${escHtml(m.hf_url)}" target="_blank" rel="noopener">View on HF ↗</a>` : ''}</div>
        <div class="hub-tags">${(m.use_cases || []).map(u => `<span class="tag">${escHtml(u)}</span>`).join('')}${(m.modalities || []).map(u => `<span class="tag mod">${escHtml(u)}</span>`).join('')}</div>
        <div class="hub-card-actions"></div>`;
    const actions = card.querySelector('.hub-card-actions');
    if (isActive) {
        actions.innerHTML = '<span class="tag active-tag">Active model</span>';
    } else if (isInstalled) {
        const useBtn = document.createElement('button');
        useBtn.className = 'btn btn-primary small';
        useBtn.textContent = 'Use this model';
        useBtn.onclick = () => hubChoose(m.id);
        actions.appendChild(useBtn);
    } else if (m.fit !== 'too_big') {
        const btn = document.createElement('button');
        btn.className = 'btn btn-primary small';
        btn.innerHTML = '<svg class="ico"><use href="#i-download"/></svg>Install';
        btn.onclick = () => hubInstall(m.id, card);
        actions.appendChild(btn);
    } else {
        actions.innerHTML = '<span class="muted small">Needs more memory than this machine has.</span>';
    }
    return card;
}

async function hubChoose(modelId) {
    try {
        await api('/api/hub/choose', { method: 'POST', body: JSON.stringify({ model: modelId }) });
        await renderHubModels();
        refreshStatus();
        loadModels();
    } catch (e) {
        alert('Could not switch model: ' + e.message);
    }
}

async function hubInstall(modelId, card) {
    const actions = card.querySelector('.hub-card-actions');
    actions.innerHTML = '<div class="hub-pull"><div class="hub-pull-bar"></div><span class="muted small">starting…</span></div>';
    const bar = actions.querySelector('.hub-pull-bar');
    const label = actions.querySelector('span');
    try {
        const resp = await fetch('/api/models/pull', {
            method: 'POST',
            headers: authHeaders(),
            body: JSON.stringify({ model: modelId }),
        });
        if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail || resp.statusText);
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = '';
        while (true) {
            const { done, value } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let idx;
            while ((idx = buffer.indexOf('\n\n')) !== -1) {
                const raw = buffer.slice(0, idx).trim();
                buffer = buffer.slice(idx + 2);
                if (!raw.startsWith('data:')) continue;
                const p = JSON.parse(raw.slice(5).trim());
                if (p.error) throw new Error(p.error);
                if (p.total && p.completed != null) {
                    const pct = Math.round((p.completed / p.total) * 100);
                    bar.style.width = pct + '%';
                    label.textContent = `${p.status} ${pct}%`;
                } else if (p.status) {
                    label.textContent = p.status;
                }
            }
        }
        await renderHubModels();
        loadModels();
    } catch (e) {
        actions.innerHTML = `<span class="muted small">failed: ${escHtml(e.message)}</span>`;
    }
}

async function refreshHubCatalog() {
    const meta = document.getElementById('hub-catalog-meta');
    meta.textContent = 'Refreshing…';
    try {
        const r = await api('/api/hub/refresh', { method: 'POST' });
        if (!r.refreshed) meta.textContent = r.detail;
        await loadHub();
    } catch (e) {
        meta.textContent = 'Refresh failed: ' + e.message;
    }
}

// ===== Storage & cleanup =====

async function renderHubStorage() {
    const wrap = document.getElementById('hub-storage');
    let s;
    try { s = await api('/api/hub/storage'); } catch (e) {
        wrap.innerHTML = `<div class="empty">${escHtml(e.message)}</div>`;
        return;
    }
    if (!(s.models || []).length) {
        wrap.innerHTML = '<div class="empty">No models installed yet.</div>';
        return;
    }
    const usedPct = Math.min(100, Math.round((s.disk_total_bytes - s.disk_free_bytes) / s.disk_total_bytes * 100));
    const modelsPct = Math.min(100, Math.round(s.models_total_bytes / s.disk_total_bytes * 100));
    wrap.innerHTML = `
        <div class="storage-summary">
          <div class="storage-bar" title="Orange: your models · Grey: everything else on disk">
            <div class="storage-bar-other" style="width:${usedPct}%"></div>
            <div class="storage-bar-models" style="width:${Math.max(modelsPct, 1)}%"></div>
          </div>
          <span class="muted small">
            Models: <b>${fmtBytes(s.models_total_bytes)}</b> · Disk free: <b>${fmtBytes(s.disk_free_bytes)}</b> of ${fmtBytes(s.disk_total_bytes)}
          </span>
        </div>
        <div id="storage-rows" class="list"></div>`;
    const rows = wrap.querySelector('#storage-rows');
    for (const m of s.models) {
        const row = document.createElement('div');
        row.className = 'list-item storage-row';
        row.innerHTML = `
            <strong>${escHtml(m.name)}</strong>
            ${m.active ? '<span class="tag active-tag">Active</span>' : ''}
            <span class="tag">${fmtBytes(m.size)}</span>
            <span class="muted small">${escHtml((m.modified_at || '').slice(0, 10))}</span>`;
        const btn = document.createElement('button');
        btn.className = 'btn btn-ghost small storage-del';
        btn.textContent = m.active ? 'In use' : 'Delete';
        btn.disabled = !!m.active;
        btn.title = m.active ? 'Switch to another model first' : `Free ${fmtBytes(m.size)}`;
        btn.onclick = async () => {
            if (!confirm(`Delete ${m.name}? This frees ${fmtBytes(m.size)}. You can re-download it anytime.`)) return;
            btn.disabled = true; btn.textContent = 'Deleting…';
            try {
                await api('/api/models/delete', { method: 'POST', body: JSON.stringify({ model: m.name }) });
                renderHubStorage();
                loadModels();
            } catch (e) { alert(e.message); btn.disabled = false; btn.textContent = 'Delete'; }
        };
        row.appendChild(btn);
        rows.appendChild(row);
    }
}
