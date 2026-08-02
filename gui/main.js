const { app, BrowserWindow, globalShortcut, ipcMain, Notification, screen, shell } = require('electron');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const http = require('http');

const BACKEND_URL = 'http://127.0.0.1:8181';
const IS_DEV = process.argv.includes('--dev');

let mainWindow = null;
let overlayWindow = null;
let fastapiProcess = null;

// ===== Backend lifecycle =====
// Packaged app: launch the frozen backend bundled in resources/ — end
// users never need Python. Dev checkout: fall back to the system Python.
function backendCommand() {
  if (app.isPackaged) {
    const exeName = process.platform === 'win32' ? 'carrot-backend.exe' : 'carrot-backend';
    const exe = path.join(process.resourcesPath, 'backend', 'carrot-backend', exeName);
    if (fs.existsSync(exe)) {
      return { cmd: exe, args: [], cwd: path.dirname(exe) };
    }
    console.error(`Bundled backend not found at ${exe}; falling back to system Python.`);
  }
  const pythonCmd = process.platform === 'win32' ? 'python' : 'python3';
  return { cmd: pythonCmd, args: ['-m', 'carrot.app'], cwd: path.join(__dirname, '..') };
}

function startFastAPI() {
  const { cmd, args, cwd } = backendCommand();
  fastapiProcess = spawn(cmd, args, {
    cwd,
    stdio: IS_DEV ? 'inherit' : 'ignore',
    windowsHide: true,
    env: { ...process.env, CARROT_RESOURCES: process.resourcesPath || '' },
  });

  fastapiProcess.on('error', (err) => {
    console.error('Failed to start FastAPI backend:', err);
  });

  fastapiProcess.on('exit', (code) => {
    if (code !== 0 && code !== null) {
      console.error(`FastAPI backend exited with code ${code}`);
    }
  });
}

function checkHealth() {
  return new Promise((resolve) => {
    const req = http.get(`${BACKEND_URL}/api/health`, { timeout: 1500 }, (res) => {
      resolve(res.statusCode === 200);
      res.resume();
    });
    req.on('error', () => resolve(false));
    req.on('timeout', () => { req.destroy(); resolve(false); });
  });
}

async function waitForBackend(timeoutMs = 30000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    if (await checkHealth()) return true;
    await new Promise((r) => setTimeout(r, 500));
  }
  return false;
}

// ===== Windows =====
function createMainWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 1024,
    minHeight: 700,
    title: 'Carrot AI',
    backgroundColor: '#0b0e1a',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
    show: false,
  });

  // The FastAPI backend serves the glassmorphism web UI at the root.
  mainWindow.loadURL(`${BACKEND_URL}/`);

  mainWindow.once('ready-to-show', () => {
    mainWindow.show();
  });

  if (IS_DEV) {
    mainWindow.webContents.openDevTools({ mode: 'detach' });
  }

  // Open external links in the system browser, not inside the app shell.
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith('http')) shell.openExternal(url);
    return { action: 'deny' };
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  return mainWindow;
}

function createOverlayWindow() {
  const primaryDisplay = screen.getPrimaryDisplay();
  const { width, height } = primaryDisplay.size;

  overlayWindow = new BrowserWindow({
    width: 620,
    height: 92,
    // Sit it in the upper third — a centred panel covers what you're reading.
    x: Math.floor(width / 2) - 310,
    y: Math.floor(height * 0.26),
    frame: false,
    alwaysOnTop: true,
    transparent: true,
    hasShadow: false,          // the panel draws its own soft shadow
    resizable: false,
    focusable: true,
    skipTaskbar: true,
    show: false,               // never flash before the page has painted
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  overlayWindow.loadFile(path.join(__dirname, 'public', 'overlay.html'));
  overlayWindow.setAlwaysOnTop(true, 'pop-up-menu');
  overlayWindow.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });

  overlayWindow.on('blur', () => {
    // Ignore the blur that fires while the window is still coming up,
    // otherwise the first Alt+Space appears to do nothing.
    if (overlayWindow && overlayWindow.isVisible() && !overlayWindow.webContents.isDevToolsFocused()) {
      overlayWindow.hide();
    }
  });

  overlayWindow.on('closed', () => {
    overlayWindow = null;
  });

  return overlayWindow;
}

function showOverlay() {
  if (!overlayWindow) createOverlayWindow();
  const reveal = () => {
    overlayWindow.setSize(overlayWindow.getSize()[0], 92);
    overlayWindow.show();
    overlayWindow.focus();
    overlayWindow.webContents.send('overlay-shown');
  };
  if (overlayWindow.webContents.isLoading()) {
    overlayWindow.webContents.once('did-finish-load', reveal);
  } else {
    reveal();
  }
}

function toggleOverlay() {
  if (overlayWindow && overlayWindow.isVisible()) overlayWindow.hide();
  else showOverlay();
}

// ===== App lifecycle =====
app.whenReady().then(async () => {
  startFastAPI();
  const ready = await waitForBackend();
  if (!ready) {
    console.error('Backend did not become ready; opening window anyway.');
  }
  createMainWindow();

  // Build the overlay up front so the first press is instant.
  createOverlayWindow();

  // Alt+Space is the Windows system-menu shortcut, so the OS sometimes wins
  // the race. Fall back through alternatives and report what actually bound.
  const accelerators = ['Alt+Space', 'Super+Space', 'CommandOrControl+Shift+Space'];
  const bound = accelerators.find(a => {
    try { return globalShortcut.register(a, toggleOverlay); } catch (e) { return false; }
  });
  if (bound) {
    console.log(`Quick-ask overlay bound to ${bound}`);
  } else {
    console.error('Could not bind any quick-ask shortcut; another app holds them.');
  }

  globalShortcut.register('Alt+Q', () => {
    if (mainWindow) mainWindow.close();
  });
});

app.on('before-quit', () => {
  globalShortcut.unregisterAll();
  if (fastapiProcess) {
    fastapiProcess.kill();
    fastapiProcess = null;
  }
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {
    app.quit();
  }
});

// ===== IPC =====

// The backend gates /api behind a session token it writes to disk. The renderer
// gets it injected into its HTML; the main process has to read the same file.
function sessionToken() {
  // Installed builds keep data in the per-user directory, dev checkouts keep
  // it beside the code. Try both — reading the wrong one means every API
  // call from the shell 401s.
  const candidates = [];
  if (process.platform === 'win32' && process.env.APPDATA) {
    candidates.push(path.join(process.env.APPDATA, 'Carrot', 'config', 'session.json'));
  } else if (process.platform === 'darwin') {
    candidates.push(path.join(app.getPath('home'), 'Library', 'Application Support',
                              'Carrot', 'config', 'session.json'));
  } else {
    const xdg = process.env.XDG_DATA_HOME
      || path.join(app.getPath('home'), '.local', 'share');
    candidates.push(path.join(xdg, 'carrot', 'config', 'session.json'));
  }
  candidates.push(path.join(__dirname, '..', 'carrot', 'data', 'config', 'session.json'));
  for (const tokenPath of candidates) {
    try {
      const token = JSON.parse(fs.readFileSync(tokenPath, 'utf8')).token;
      if (token) return token;
    } catch (e) { /* try the next location */ }
  }
  return '';
}

function apiHeaders() {
  const headers = { 'Content-Type': 'application/json' };
  const token = sessionToken();
  if (token) headers['X-Carrot-Token'] = token;
  return headers;
}

ipcMain.handle('send-command', async (event, command) => {
  try {
    const response = await fetch(`${BACKEND_URL}/api/chat`, {
      method: 'POST',
      headers: apiHeaders(),
      body: JSON.stringify({ message: command }),
    });
    return await response.json();
  } catch (e) {
    return { error: e.message };
  }
});

ipcMain.handle('get-status', async () => {
  try {
    const response = await fetch(`${BACKEND_URL}/api/status`, { headers: apiHeaders() });
    return await response.json();
  } catch (e) {
    return { error: e.message };
  }
});

// The quick-ask overlay is a bare floating panel: it grows to fit its reply
// and dismisses itself on Escape.
ipcMain.handle('resize-overlay', async (event, height) => {
  if (!overlayWindow) return { ok: false };
  const [width] = overlayWindow.getSize();
  overlayWindow.setSize(width, Math.max(90, Math.min(Number(height) || 90, 520)));
  return { ok: true };
});

ipcMain.handle('hide-overlay', async () => {
  if (overlayWindow) overlayWindow.hide();
  return { ok: true };
});

ipcMain.handle('notify', async (event, { title, body }) => {
  if (!Notification.isSupported()) return { shown: false };
  const notification = new Notification({ title: title || 'Carrot', body: body || '' });
  // Clicking a toast should bring the user to the thing it is about.
  notification.on('click', () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });
  notification.show();
  return { shown: true };
});
