const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('carrotAPI', {
  sendCommand: (command) => ipcRenderer.invoke('send-command', command),
  getStatus: () => ipcRenderer.invoke('get-status'),
  onSpeechResult: (callback) => ipcRenderer.on('speech-result', (event, data) => callback(data)),
  onSpeechError: (callback) => ipcRenderer.on('speech-error', (event, data) => callback(data)),
});

// The web UI raises proactive notifications; the desktop shell turns them into
// native OS toasts so they reach the user even when Carrot is not focused.
contextBridge.exposeInMainWorld('carrot', {
  notify: (title, body) => ipcRenderer.invoke('notify', { title, body }),
});