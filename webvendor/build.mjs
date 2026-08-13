// Bundles vendor libraries into carrot/web/vendor for fully-offline serving.
import * as esbuild from 'esbuild';
import { mkdirSync } from 'fs';
import { fileURLToPath } from 'url';
import path from 'path';

const here = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.join(here, '..', 'carrot', 'web', 'vendor');
mkdirSync(outDir, { recursive: true });

const common = {
  bundle: true,
  minify: true,
  format: 'iife',
  logLevel: 'info',
  loader: {
    '.ttf': 'file',
    '.woff': 'file',
    '.woff2': 'file',
    '.svg': 'dataurl',
  },
};

// 1. marked — exposed as window.marked
await esbuild.build({
  ...common,
  entryPoints: [path.join(here, 'src', 'marked-entry.js')],
  outfile: path.join(outDir, 'marked.js'),
});

// 2. Milkdown Crepe — exposed as window.CarrotCrepe (+ milkdown.css)
await esbuild.build({
  ...common,
  entryPoints: [path.join(here, 'src', 'milkdown-entry.js')],
  outfile: path.join(outDir, 'milkdown.js'),
});

// 3. KaTeX — exposed as window.katex (+ katex.css + fonts)
//
// Its fonts were already being emitted here as a side effect of the Milkdown
// bundle, so the app shipped every glyph and could render maths in exactly one
// place. This makes the library itself reachable, which is what chat, Research
// and the Code tab needed.
await esbuild.build({
  ...common,
  entryPoints: [path.join(here, 'src', 'katex-entry.js')],
  outfile: path.join(outDir, 'katex.js'),
  assetNames: '[name]-[hash]',
});

// 4. Monaco editor — exposed as window.monaco (+ monaco.css + fonts)
await esbuild.build({
  ...common,
  entryPoints: [path.join(here, 'src', 'monaco-entry.js')],
  outfile: path.join(outDir, 'monaco.js'),
  assetNames: '[name]',
});

// 5. Monaco web workers (served from /vendor/workers/)
const workers = {
  'editor.worker': 'monaco-editor/esm/vs/editor/editor.worker.js',
  'json.worker': 'monaco-editor/esm/vs/language/json/json.worker.js',
  'css.worker': 'monaco-editor/esm/vs/language/css/css.worker.js',
  'html.worker': 'monaco-editor/esm/vs/language/html/html.worker.js',
  'ts.worker': 'monaco-editor/esm/vs/language/typescript/ts.worker.js',
};
for (const [name, entry] of Object.entries(workers)) {
  await esbuild.build({
    ...common,
    entryPoints: [entry],
    outfile: path.join(outDir, 'workers', `${name}.js`),
  });
}

console.log('vendor bundles written to', outDir);
