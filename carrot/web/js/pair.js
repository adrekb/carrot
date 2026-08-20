// ================================================================
// The first screen a phone ever sees.
//
// A device that is not this computer gets the app shell with no session token
// in it (see `index` in app.py). That is deliberate and it is the whole
// security model: the page loads, and then it has to ask.
//
// So this runs before anything else does. If there is no token of either kind,
// nothing else in the app should start — every module here opens with a fetch,
// and a screen full of failed requests behind a login prompt is how people
// conclude the thing is broken rather than locked.
//
// It is not a password box. There is no account, no server, nobody to be a
// user of: the credential is a six-character code showing on the machine you
// are asking to get into, which means the person pairing is the person sitting
// at that machine. That is the trust being granted and it is worth being
// literal about, because what is behind this screen is a terminal.
// ================================================================

function needsPairing() {
    return !CARROT_TOKEN && !deviceToken();
}

function showPairGate(message) {
    const gate = document.getElementById('pair-gate');
    if (!gate) return;
    gate.classList.remove('hidden');
    // Covering the app is only half of it: its modules poll on their own
    // timers and would sit behind this firing requests that cannot succeed.
    // `api()` refuses outright while unpaired, which stops them at the source
    // rather than asking twenty files to know about this screen.
    const note = document.getElementById('pair-note');
    if (note) {
        note.textContent = message || '';
        note.classList.toggle('hidden', !message);
    }
    // Suggest a name it can actually be recognised by later. "A phone" in a
    // list of three phones is the reason revocation lists get ignored.
    const name = document.getElementById('pair-name');
    if (name && !name.value) name.value = guessDeviceName();
}

function hidePairGate() {
    document.getElementById('pair-gate')?.classList.add('hidden');
}

// From the user agent, which is a poor source for almost everything and a
// perfectly good one for "is this an iPhone".
//
// CrOS is checked before Android: a Chromebook's user agent says X11 and
// Linux and, on some builds, Android as well — and a device list that calls
// somebody's laptop an Android tablet is a list they stop trusting to tell
// them which thing to sign out.
function guessDeviceName() {
    const ua = navigator.userAgent || '';
    if (/CrOS/i.test(ua)) return 'Chromebook';
    if (/iPhone/i.test(ua)) return 'iPhone';
    if (/iPad/i.test(ua)) return 'iPad';
    if (/Android/i.test(ua)) return /Mobile/i.test(ua) ? 'Android phone' : 'Android tablet';
    if (/Macintosh/i.test(ua)) return 'Mac';
    if (/Windows/i.test(ua)) return 'Windows PC';
    return 'A device';
}

async function submitPairingCode() {
    const input = document.getElementById('pair-code');
    const button = document.getElementById('pair-submit');
    const code = (input?.value || '').trim().toUpperCase();
    if (code.length < 4) {
        showPairGate('Type the six characters showing on your computer.');
        return;
    }
    if (button) button.disabled = true;
    let answer;
    try {
        // Not `api()`: that attaches a token, and the entire point of this
        // request is that there is not one yet.
        const resp = await fetch('/api/pair', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                code,
                name: (document.getElementById('pair-name')?.value || '').trim(),
                username: (document.getElementById('pair-username')?.value || '').trim(),
                password: document.getElementById('pair-password')?.value || '',
            }),
        });
        answer = await resp.json();
        if (!resp.ok) throw new Error(answer.detail || 'Pairing failed');
        // The password field is cleared whatever happens next. It is the one
        // value on this screen that must not survive on a device that has just
        // failed to prove it should be holding it.
        const pw = document.getElementById('pair-password');
        if (pw) pw.value = '';
    } catch (e) {
        if (button) button.disabled = false;
        showPairGate(e.message || String(e));
        if (input) { input.value = ''; input.focus(); }
        return;
    }
    setDeviceToken(answer.token);
    // A reload rather than starting the app in place. Every module here reads
    // its credential once at load, and a session that began unauthenticated
    // would be carrying that decision around all day.
    location.reload();
}

// Enter submits, and the code is upper-cased as it is typed: the alphabet has
// no lowercase in it, and a field that silently rejects what somebody just
// typed correctly is a field that gets sworn at.
document.addEventListener('DOMContentLoaded', () => {
    const input = document.getElementById('pair-code');
    if (!input) return;
    input.addEventListener('input', () => {
        input.value = input.value.toUpperCase().replace(/[^A-Z0-9]/g, '');
    });
    input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') submitPairingCode();
    });
});

// What this computer asks for, fetched before the form is filled in. The
// endpoint is public because a device with no token cannot ask any other way,
// and it says only *whether* a sign-in is wanted and under what name.
async function loadPairingRequirements() {
    let needs = { credentials_required: false, username: '' };
    try {
        needs = await (await fetch('/api/pair/requirements')).json();
    } catch (_) {}
    const box = document.getElementById('pair-credentials');
    if (box) box.classList.toggle('hidden', !needs.credentials_required);
    // The name is not the secret and is a nuisance to type on a phone, so it
    // is filled in; the password is the half that has to be known.
    const user = document.getElementById('pair-username');
    if (user && needs.username && !user.value) user.value = needs.username;
}

// Before anything else. This file is loaded first for that reason.
if (needsPairing()) {
    document.addEventListener('DOMContentLoaded', () => {
        showPairGate('');
        loadPairingRequirements();
        document.getElementById('pair-code')?.focus();
    });
}

// ===== Host and client are not the same app =====
//
// A paired device is *using* Carrot; this computer *administers* it. The
// controls that decide who may connect belong to the machine being connected
// to — the server enforces that (`require_host`), and this keeps the client
// from rendering doors it will only be refused at.
//
// Marked in the markup rather than listed here, so a panel added later is
// covered by having the attribute rather than by somebody remembering to come
// back and add it to a list in a different file.
function applyClientChrome() {
    if (CARROT_TOKEN) return;          // this is the host
    document.body.classList.add('is-client');
    for (const el of document.querySelectorAll('[data-host-only]')) {
        el.classList.add('hidden');
    }
}

document.addEventListener('DOMContentLoaded', applyClientChrome);

// A device token that has been revoked on the desktop looks exactly like a
// broken app from here: every request 401s and nothing says why. So the first
// 401 against a stored device token throws it away and asks to pair again,
// which is both the truth and the thing to do about it.
function handleDeviceTokenRejected() {
    if (CARROT_TOKEN || !deviceToken()) return false;
    setDeviceToken('');
    showPairGate('This device was signed out on the computer. Pair it again to continue.');
    return true;
}


// ================================================================
// The other end of it, on the computer.
//
// Two switches that look like one and are not: whether Carrot is *listening*
// beyond this machine, and which devices are *allowed* once it is. Conflating
// them is how "I turned it on for a minute" becomes a phone that still works
// six months later, or a revoked phone that still gets in because the port is
// open to everything.
// ================================================================

let remoteState = null;
let pairPoll = null;

// The QR is fetched once per address, not once per poll. This panel redraws
// every second while a code is showing, and re-encoding the same URL sixty
// times a minute to draw the same square would be work done purely to arrive
// at what is already on screen.
let qrAddress = '';
let qrSvg = '';
let qrUrl = '';

async function showQrFor(address) {
    if (!address || address === qrAddress) return;
    qrAddress = address;
    qrSvg = '';
    qrUrl = '';
    try {
        const data = await api('/api/pair/qr?address=' + encodeURIComponent(address));
        // Only if the answer is still for the address being asked about: a
        // slow response for one address must not overwrite a fast one for
        // another that was clicked meanwhile.
        if (qrAddress === address) { qrSvg = data.svg; qrUrl = data.url; }
    } catch (_) {}
    renderRemoteAccess();
}

async function loadRemoteAccess() {
    try {
        remoteState = await api('/api/pair/state');
    } catch (_) {
        remoteState = null;
    }
    renderRemoteAccess();
}

function renderRemoteAccess() {
    const host = document.getElementById('remote-access-panel');
    if (!host) return;
    if (!remoteState) {
        host.innerHTML = '<div class="empty">Could not read the connection settings.</div>';
        return;
    }
    const remote = remoteState.remote || {};
    const on = remote.enabled;
    const tailnet = remote.tailnet;
    host.innerHTML =
        '<label class="switch-row' + (tailnet ? '' : ' is-blocked') + '">'
      +   '<input type="checkbox" ' + (on ? 'checked' : '')
      +     (tailnet || on ? '' : ' disabled')
      +     ' onchange="setRemoteAccess(this.checked)">'
      +   '<span>Let my devices connect over Tailscale</span>'
      + '</label>'
      // The one thing this panel must never get wrong is claiming to be in a
      // state it is not in. Three of them, in the order they stop being true.
      + (tailnet ? '' : tailscaleMissingHtml())
      + (remote.fallback_reason
          ? '<div class="notice small">' + escHtml(remote.fallback_reason) + '</div>'
          : '')
      + (remote.restart_required
          ? '<div class="notice small">Restart Carrot for this to take effect.</div>'
          : '')
      + credentialHtml()
      + (on ? remoteAddressesHtml(remote) + pairingHtml() : '')
      + devicesHtml();
}

// The half of this that is a secret rather than a network.
//
// Tailscale says the two machines are on the same private network and the
// pairing code says somebody is standing here — both are facts about a place.
// This is the one that is about a person, and it is the same name and password
// typed on both ends.
function credentialHtml() {
    const cred = (remoteState.credential || {});
    if (!cred.required) {
        return '<div class="cred-block">'
          + '<div class="cred-head">Require a sign-in</div>'
          + '<div class="cred-sub">A name and password that must also be typed on '
          +   'the device. Without one, anything on your tailnet that can see this '
          +   'screen can pair.</div>'
          + credentialFormHtml('Set it')
          + '</div>';
    }
    return '<div class="cred-block is-set">'
      + '<div class="cred-head">Sign-in required · <code>' + escHtml(cred.username) + '</code></div>'
      + '<div class="cred-sub">Devices must type this name and password to pair.</div>'
      + '<button class="btn btn-ghost small" onclick="editCredential()">Change</button>'
      + '<button class="btn btn-ghost small danger" onclick="clearCredential()">Remove</button>'
      + '<div id="cred-form" class="hidden">' + credentialFormHtml('Save') + '</div>'
      + '</div>';
}

function credentialFormHtml(action) {
    return '<div class="cred-form">'
      + '<input type="text" id="cred-user" placeholder="Name" autocomplete="off"'
      +   ' autocapitalize="none" spellcheck="false">'
      + '<input type="password" id="cred-pass" placeholder="Password" autocomplete="new-password">'
      + '<button class="btn btn-ghost small" onclick="saveCredential()">' + escHtml(action) + '</button>'
      + '<span id="cred-note" class="cred-note"></span>'
      + '</div>';
}

function editCredential() {
    document.getElementById('cred-form')?.classList.remove('hidden');
    document.getElementById('cred-user')?.focus();
}

async function saveCredential() {
    const note = document.getElementById('cred-note');
    const username = (document.getElementById('cred-user')?.value || '').trim();
    const password = document.getElementById('cred-pass')?.value || '';
    try {
        await api('/api/pair/credentials', {
            method: 'POST', body: JSON.stringify({ username, password }),
        });
    } catch (e) {
        if (note) note.textContent = e.message || String(e);
        return;
    }
    await loadRemoteAccess();
}

// Changing or removing the sign-in does not sign anything out. A device that
// is already paired holds its own key, and this is the gate for getting one —
// so if the reason for removing it is that somebody knows it, the thing to do
// is sign that device out, and saying so here is better than implying this did.
async function clearCredential() {
    try {
        await api('/api/pair/credentials', {
            method: 'POST', body: JSON.stringify({ username: '', password: '' }),
        });
    } catch (_) {}
    await loadRemoteAccess();
}

// Not an error — a prerequisite, explained. The switch is off and cannot be
// turned on, and a disabled control with nothing next to it is the most
// annoying thing an interface can do.
function tailscaleMissingHtml() {
    return '<div class="notice small">'
         + 'Carrot connects your devices over <strong>Tailscale</strong>, which '
         + 'builds a private encrypted network between machines you own — no '
         + 'ports opened, nothing on the public internet, and it works from any '
         + 'network rather than only your home Wi-Fi. '
         + 'Install it on this computer and on the phone or laptop, sign in to '
         + 'the same account on both, then come back here.'
         + '</div>';
}

// One address, because there is only one way in. The LAN was offered here and
// should not have been: Carrot speaks plain HTTP, so on a local network the
// device token crosses the air in the clear, and "same Wi-Fi" is not a
// boundary at a school or an office.
function remoteAddressesHtml(remote) {
    const rows = remote.addresses || [];
    if (!rows.length) return '';
    const row = rows[0];
    return '<div class="remote-addresses">'
      + '<div class="remote-address is-private">'
      +   '<code>http://' + escHtml(row.address) + ':' + escHtml(String(remote.port)) + '</code>'
      +   '<span class="remote-note">' + escHtml(row.note) + '</span>'
      + '</div></div>';
}

function pairingHtml() {
    const open = remoteState.open;
    if (!open) {
        return '<div class="pair-strip">'
             + '<button class="btn btn-ghost small" onclick="openPairing()">Pair a device…</button>'
             + '</div>';
    }
    // Scan, then type. The camera carries the address — which is the part
    // nobody can be expected to know is even right — and the code stays
    // something a person reads off this screen and types on that one.
    return '<div class="pair-open">'
         + '<div class="pair-steps">'
         +   '<div class="pair-step">'
         +     '<span class="pair-step-n">1</span>'
         +     '<div><strong>Point the camera at this.</strong>'
         +       '<span class="pair-step-sub">Or open '
         +         '<code>' + escHtml(qrUrl || '') + '</code> on the device.</span></div>'
         +   '</div>'
         +   '<div class="pair-qr" id="pair-qr">' + (qrSvg || qrPlaceholder()) + '</div>'
         +   '<div class="pair-step">'
         +     '<span class="pair-step-n">2</span>'
         +     '<div><strong>Type this code there.</strong>'
         +       '<span class="pair-step-sub">'
         +         escHtml(String(remoteState.seconds_left)) + 's left · '
         +         escHtml(String(remoteState.attempts_left)) + ' tries left.</span></div>'
         +   '</div>'
         +   '<div class="pair-showing-code">' + escHtml(remoteState.code) + '</div>'
         + '</div>'
         + '<button class="btn btn-ghost small" onclick="closePairing()">Stop</button>'
         + '</div>';
}

// Said rather than left blank. A missing QR means the encoder is not in this
// build, not that pairing is broken — and the address underneath still works.
function qrPlaceholder() {
    return '<div class="pair-qr-none">No QR in this build — type the address above.</div>';
}

// There was a picker here, for machines with both a tailnet and a LAN. There
// is one way in now, so choosing between them is a control with one option.

function devicesHtml() {
    const devices = remoteState.devices || [];
    if (!devices.length) return '<div class="muted small">No devices are paired.</div>';
    return '<div class="device-list">'
      + devices.map(device =>
          '<div class="device-row">'
        +   '<div class="device-what">'
        +     '<div class="device-name">' + escHtml(device.name) + '</div>'
        +     '<div class="device-seen">last seen ' + escHtml(deviceSeen(device)) + '</div>'
        +   '</div>'
        +   '<button class="btn btn-ghost small danger" onclick="revokeDevice(\'' + device.id + '\')">'
        +     'Sign out</button>'
        + '</div>').join('')
      + '</div>';
}

function deviceSeen(device) {
    if (!device.last_seen) return 'never';
    const then = new Date(device.last_seen);
    if (isNaN(then)) return 'unknown';
    const mins = Math.floor((Date.now() - then.getTime()) / 60000);
    if (mins < 2) return 'just now';
    if (mins < 60) return mins + ' minutes ago';
    const hours = Math.floor(mins / 60);
    if (hours < 24) return hours + ' hours ago';
    return Math.floor(hours / 24) + ' days ago';
}

async function setRemoteAccess(enabled) {
    try {
        await api('/api/remote-access', {
            method: 'POST', body: JSON.stringify({ enabled: !!enabled }),
        });
    } catch (_) {}
    await loadRemoteAccess();
}

// While a code is showing, the panel is a countdown and a thing waiting to be
// used — so it polls, and stops the moment it is not.
async function openPairing() {
    try { await api('/api/pair/open', { method: 'POST' }); } catch (_) {}
    await loadRemoteAccess();
    // The best address this machine has, which `reachable_addresses` already
    // sorted — a tailnet ahead of the local air, for reasons that are about
    // encryption rather than convenience.
    const best = ((remoteState || {}).remote || {}).addresses || [];
    if (best.length) await showQrFor(best[0].address);
    clearInterval(pairPoll);
    pairPoll = setInterval(async () => {
        await loadRemoteAccess();
        if (!remoteState || !remoteState.open) { clearInterval(pairPoll); pairPoll = null; }
    }, 1000);
}

async function closePairing() {
    clearInterval(pairPoll);
    pairPoll = null;
    try { await api('/api/pair/close', { method: 'POST' }); } catch (_) {}
    await loadRemoteAccess();
}

async function revokeDevice(id) {
    try { await api('/api/devices/' + id, { method: 'DELETE' }); } catch (_) {}
    await loadRemoteAccess();
}

// Only on this machine. A phone has no business drawing the panel that decides
// which phones are allowed.
document.addEventListener('DOMContentLoaded', () => {
    if (CARROT_TOKEN) loadRemoteAccess();
});
