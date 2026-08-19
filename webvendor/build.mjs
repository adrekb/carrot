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

// 5. reveal.js — exposed as window.Reveal (+ reveal.css)
await esbuild.build({
  ...common,
  entryPoints: [path.join(here, 'src', 'reveal-entry.js')],
  outfile: path.join(outDir, 'reveal.js'),
});

// 6. Excalidraw — exposed as window.CarrotCanvas (+ excalidraw.css)
//
// React is bundled in rather than shared: it is the only thing here that wants
// it, and a global React on the page would be a second framework every other
// script could start depending on by accident.
await esbuild.build({
  ...common,
  entryPoints: [path.join(here, 'src', 'excalidraw-entry.js')],
  outfile: path.join(outDir, 'excalidraw.js'),
  // The library reads this to pick its production build; without it esbuild
  // leaves the development branch in, which is slower and far larger.
  define: { 'process.env.NODE_ENV': '"production"' },
  // Its package exports are keyed on a `development`/`production` condition
  // rather than on a plain path, so without naming one esbuild cannot resolve
  // either the entry or its stylesheet at all.
  conditions: ['production'],
  loader: { ...common.loader, '.png': 'dataurl', '.jpg': 'dataurl' },
  assetNames: '[name]-[hash]',
});

// 6b. Mermaid — exposed as window.mermaid
//
// Big, and worth it: a mermaid artifact is a diagram, and until this was here
// the app rendered the diagram's source code instead of the diagram.
await esbuild.build({
  ...common,
  entryPoints: [path.join(here, 'src', 'mermaid-entry.js')],
  outfile: path.join(outDir, 'mermaid.js'),
});

// 7. Monaco web workers (served from /vendor/workers/)
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
