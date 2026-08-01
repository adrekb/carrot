// ===== Model Hub — hardware-aware model catalog =====
// Backed by /api/hub: detected specs, the fit-annotated catalog (bundled,
// refreshed daily from the Carrot Hub website), and per-role picks.

let hubData = null;
let hubUseCase = '';
let hubModalities = new Set(); // required input modalities (image/audio/video)

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
    document.getElementById('hub-site-link').href = hubData.hub_url;
    renderHubSpecs();
    renderHubChips();
    renderHubMeta();
    await renderHubModels();
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
        };
        mwrap.appendChild(b);
    }
}

function renderHubMeta() {
    const el = document.getElementById('hub-catalog-meta');
    const src = hubData.catalog_source;
    if (src === 'bundled') el.textContent = 'Bundled catalog (Carrot Hub not reached yet)';
    else el.textContent = `Catalog from Carrot Hub · updated ${hubData.catalog_fetched_at ? hubData.catalog_fetched_at.slice(0, 10) : ''}`;
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

    // Trending from the public Hugging Face API — self-updating.
    const trendEl = document.getElementById('hub-trending');
    trendEl.innerHTML = '';
    let trending = hubData.trending || [];
    if (hubModalities.size) {
        trending = trending.filter(m =>
            [...hubModalities].every(mod => (m.modalities || []).includes(mod)));
    }
    if (!trending.length) {
        trendEl.innerHTML = '<div class="empty">Hugging Face not reached yet — check your connection or hit Refresh.</div>';
    }
    for (const m of trending) trendEl.appendChild(hubCard(m, installed, active, null));
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
