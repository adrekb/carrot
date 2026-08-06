const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('carrotAPI', {
  // Accepts a string or { message, attachments, workspace_id }.
  sendCommand: (command) => ipcRenderer.invoke('send-command', command),
  pickAttachments: () => ipcRenderer.invoke('pick-attachments'),
  readAttachment: (filePath) => ipcRenderer.invoke('read-attachment', filePath),
  listWorkspaces: () => ipcRenderer.invoke('list-workspaces'),
  getStatus: () => ipcRenderer.invoke('get-status'),
  onSpeechResult: (callback) => ipcRenderer.on('speech-result', (event, data) => callback(data)),
  onSpeechError: (callback) => ipcRenderer.on('speech-error', (event, data) => callback(data)),
  // Quick-ask overlay: it grows to fit its reply and closes on Escape.
  resizeOverlay: (height) => ipcRenderer.invoke('resize-overlay', height),
  hideOverlay: () => ipcRenderer.invoke('hide-overlay'),
  onOverlayShown: (callback) => ipcRenderer.on('overlay-shown', () => callback()),
  // Native folder chooser — window.prompt() is disabled in Electron.
  pickDirectory: (opts) => ipcRenderer.invoke('pick-directory', opts || {}),
  // Lets the next launch open on the current theme instead of flashing dark,
  // and keeps the quick-ask overlay in the same palette as the app.
  setAppearance: (a) => ipcRenderer.invoke('set-appearance', a || {}),
  onAppearance: (callback) => ipcRenderer.on('appearance', (event, a) => callback(a)),
});

// The web UI raises proactive notifications; the desktop shell turns them into
// native OS toasts so they reach the user even when Carrot is not focused.
contextBridge.exposeInMainWorld('carrot', {
  notify: (title, body) => ipcRenderer.invoke('notify', { title, body }),
  // A provider's sign-in page must open in the real browser: that is where the
  // user is already logged in, and an embedded window asking for their
  // password is indistinguishable from a phishing page.
  openExternal: (url) => ipcRenderer.invoke('open-external', url),
});