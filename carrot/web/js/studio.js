// ===== Sign-in modes and media generation =====
//
// Two settings panels that answer the same question from different ends: how
// does Carrot reach a provider, and what does it get back. Both are built the
// same way — read state, draw it, and never claim something is configured
// when it is not, because the failure that follows is unreadable.

// ---------- How you sign in ----------

async function loadAuthPanel() {
    const host = document.getElementById('auth-panel');
    if (!host) return;
    let providers = [];
    try {
        providers = (await api('/api/auth/status')).providers || [];
    } catch (e) {
        host.innerHTML = `<div class="empty error">${escHtml(e.message)}</div>`;
        return;
    }
    // Only providers that actually have a consumer plan get the switch. Showing
    // a disabled "Subscription" button next to Groq would just raise a question
    // with no answer.
    const relevant = providers.filter(p => p.subscription_supported);
    if (!relevant.length) {
        host.innerHTML = '<div class="empty">No provider here offers a consumer subscription.</div>';
        return;
    }
    host.innerHTML = '';
    for (const provider of relevant) host.appendChild(authRow(provider));
}

function authRow(provider) {
    const row = document.createElement('div');
    row.className = 'auth-row';
    const usable = provider.usable;
    const detail = provider.mode === 'subscription'
        ? (provider.signed_in
            ? `signed in with ${provider.plan_label}`
            : provider.oauth_configured
                ? 'not signed in yet'
                : 'needs this install’s OAuth client details')
        : (provider.key_set ? 'API key configured' : 'no API key yet');

    row.innerHTML = `
      <div class="auth-main">
        <span class="dot ${usable ? 'ok' : 'warn'}"></span>
        <span class="provider-name">${escHtml(provider.provider)}</span>
        <span class="auth-detail">${escHtml(detail)}</span>
      </div>
      <div class="mode-switch">
        <button class="mode-opt ${provider.mode === 'api_key' ? 'on' : ''}"
                data-mode="api_key">API key</button>
        <button class="mode-opt ${provider.mode === 'subscription' ? 'on' : ''}"
                data-mode="subscription">Subscription</button>
      </div>`;

    row.querySelectorAll('.mode-opt').forEach(button => {
        button.onclick = () => setAuthMode(provider.provider, button.dataset.mode);
    });

    if (provider.mode === 'subscription') {
        const action = document.createElement('button');
        action.className = 'btn btn-ghost';
        action.textContent = provider.signed_in ? 'Sign out' : 'Sign in';
        action.onclick = () => provider.signed_in
            ? signOutProvider(provider.provider)
            : startSignIn(provider.provider);
        row.appendChild(action);
        if (!provider.oauth_configured) row.appendChild(oauthDetails(provider.provider));
    }
    return row;
}

// Carrot ships the shape of the OAuth flow, not someone else's client
// credentials — so an installation supplies its own, here.
function oauthDetails(providerId) {
    const wrap = document.createElement('details');
    wrap.className = 'oauth-details';
    wrap.innerHTML = `
      <summary>OAuth client details</summary>
      <div class="settings-row">
        <input type="text" placeholder="client id" spellcheck="false" data-field="client_id">
        <input type="text" placeholder="https://…/authorize" spellcheck="false" data-field="authorize_url">
      </div>
      <div class="settings-row">
        <input type="text" placeholder="https://…/token" spellcheck="false" data-field="token_url">
        <button class="btn btn-ghost">Save</button>
      </div>`;
    wrap.querySelector('button').onclick = async () => {
        const body = {};
        wrap.querySelectorAll('input').forEach(input => {
            if (input.value.trim()) body[input.dataset.field] = input.value.trim();
        });
        try {
            await api(`/api/auth/oauth/${encodeURIComponent(providerId)}`,
                { method: 'PUT', body: JSON.stringify(body) });
            loadAuthPanel();
        } catch (e) {
            alert('Could not save: ' + (e.detail || e.message));
        }
    };
    return wrap;
}

async function setAuthMode(providerId, mode) {
    try {
        await api(`/api/auth/mode/${encodeURIComponent(providerId)}`,
            { method: 'PUT', body: JSON.stringify({ mode }) });
    } catch (e) {
        alert('Could not switch: ' + (e.detail || e.message));
        return;
    }
    loadAuthPanel();
    if (typeof loadRouting === 'function') loadRouting();
}

async function startSignIn(providerId) {
    let started;
    try {
        started = await api(`/api/auth/login/${encodeURIComponent(providerId)}`, { method: 'POST' });
    } catch (e) {
        alert(e.detail || e.message);
        return;
    }
    // The provider's sign-in page has to open in the real browser: it is where
    // the user is already logged in, and an embedded view is exactly what a
    // phishing page would look like.
    if (window.carrot?.openExternal) window.carrot.openExternal(started.url);
    else window.open(started.url, '_blank', 'noopener');
    // The callback lands on the backend; poll briefly so the panel updates
    // itself rather than making the user hunt for a refresh button.
    let tries = 0;
    const timer = setInterval(async () => {
        tries += 1;
        try {
            const state = await api(`/api/auth/status/${encodeURIComponent(providerId)}`);
            if (state.signed_in || tries > 60) {
                clearInterval(timer);
                loadAuthPanel();
            }
        } catch (_) { clearInterval(timer); }
    }, 2000);
}

async function signOutProvider(providerId) {
    if (!confirm(`Sign out of ${providerId}? Carrot will stop using that subscription.`)) return;
    try {
        await api(`/api/auth/logout/${encodeURIComponent(providerId)}`, { method: 'POST' });
    } catch (_) { /* signing out locally cannot really fail */ }
    loadAuthPanel();
}

// ---------- Image and video generation ----------

let mediaState = null;

async function loadMediaPanel() {
    const host = document.getElementById('media-panel');
    if (!host) return;
    try {
        mediaState = await api('/api/media');
    } catch (e) {
        host.innerHTML = `<div class="empty error">${escHtml(e.message)}</div>`;
        return;
    }
    host.innerHTML = '';
    for (const backend of mediaState.backends) host.appendChild(mediaRow(backend));
}

function mediaRow(backend) {
    const row = document.createElement('div');
    row.className = 'media-row';
    const isDefault = mediaState.default_image === backend.id
        || mediaState.default_video === backend.id;
    row.innerHTML = `
      <div class="media-main">
        <span class="dot ${backend.configured ? 'ok' : 'warn'}"></span>
        <span class="provider-name">${escHtml(backend.label)}</span>
        ${backend.local ? '<span class="tag">on-device</span>' : ''}
        ${backend.kinds.map(k => `<span class="tag">${escHtml(k)}</span>`).join('')}
        ${isDefault ? '<span class="tag tag-accent">default</span>' : ''}
      </div>
      ${backend.note ? `<div class="muted small">${escHtml(backend.note)}</div>` : ''}`;

    const controls = document.createElement('div');
    controls.className = 'settings-row';
    if (backend.local) {
        // A local server moves — second GPU box, non-default port — so the URL
        // has to be editable without touching a config file.
        const url = document.createElement('input');
        url.type = 'text';
        url.value = backend.base_url;
        url.spellcheck = false;
        url.onchange = () => saveMediaField(backend.id, 'endpoint', { base_url: url.value.trim() });
        controls.appendChild(url);
    } else {
        const key = document.createElement('input');
        key.type = 'password';
        key.placeholder = backend.configured ? 'key configured — paste to replace' : 'API key';
        key.spellcheck = false;
        key.onchange = () => saveMediaField(backend.id, 'key', { api_key: key.value.trim() });
        controls.appendChild(key);
    }
    for (const kind of backend.kinds) {
        const use = document.createElement('button');
        use.className = 'btn btn-ghost';
        use.textContent = `Use for ${kind}`;
        use.onclick = () => setMediaDefault(backend.id, kind);
        controls.appendChild(use);
    }
    row.appendChild(controls);
    return row;
}

async function saveMediaField(backendId, field, body) {
    try {
        await api(`/api/media/backends/${encodeURIComponent(backendId)}/${field}`,
            { method: 'PUT', body: JSON.stringify(body) });
        loadMediaPanel();
    } catch (e) {
        alert('Could not save: ' + (e.detail || e.message));
    }
}

async function setMediaDefault(backendId, kind) {
    try {
        await api('/api/media/default',
            { method: 'PUT', body: JSON.stringify({ backend: backendId, kind }) });
        loadMediaPanel();
    } catch (e) {
        alert(e.detail || e.message);
    }
}

async function tryGenerate() {
    const input = document.getElementById('media-prompt');
    const preview = document.getElementById('media-preview');
    const prompt = input.value.trim();
    if (!prompt) return;
    preview.innerHTML = '<div class="muted small">Generating…</div>';
    try {
        const result = await api('/api/media/generate',
            { method: 'POST', body: JSON.stringify({ prompt }) });
        const where = result.local ? 'on this machine' : result.backend_label;
        preview.innerHTML = `<div class="muted small">${escHtml(where)} · ${result.seconds}s</div>`;
        if (result.artifact) {
            const img = document.createElement('img');
            img.src = result.artifact.content;
            img.alt = prompt;
            preview.appendChild(img);
        }
    } catch (e) {
        preview.innerHTML = `<div class="empty error">${escHtml(e.detail || e.message)}</div>`;
    }
}
