const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('carrotAPI', {
  sendCommand: (command) => ipcRenderer.invoke('send-command', command),
  getStatus: () => ipcRenderer.invoke('get-status'),
  onSpeechResult: (callback) => ipcRenderer.on('speech-result', (event, data) => callback(data)),
  onSpeechError: (callback) => ipcRenderer.on('speech-error', (event, data) => callback(data)),
});