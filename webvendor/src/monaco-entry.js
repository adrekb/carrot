import * as monaco from 'monaco-editor';

// Workers are bundled separately and served from /vendor/workers/.
self.MonacoEnvironment = {
  getWorkerUrl(_moduleId, label) {
    if (label === 'json') return '/vendor/workers/json.worker.js';
    if (label === 'css' || label === 'scss' || label === 'less') return '/vendor/workers/css.worker.js';
    if (label === 'html' || label === 'handlebars' || label === 'razor') return '/vendor/workers/html.worker.js';
    if (label === 'typescript' || label === 'javascript') return '/vendor/workers/ts.worker.js';
    return '/vendor/workers/editor.worker.js';
  },
};

window.monaco = monaco;
