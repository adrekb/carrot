# Third-party notices

Carrot itself is MIT-licensed — see [LICENSE](LICENSE).

`carrot/web/vendor/` holds **pre-built bundles of other people's code**, committed
so that a clone runs fully offline without a Node toolchain. They are not Carrot's
work and they keep their own licenses. The bundlers strip comments, so the
notices those licenses require live here instead.

| Bundle | Upstream | License |
|---|---|---|
| `monaco.js`, `monaco.css`, `workers/*.js` | [microsoft/monaco-editor](https://github.com/microsoft/monaco-editor) | MIT — Copyright (c) Microsoft Corporation |
| `milkdown.js`, `milkdown.css` | [Milkdown Crepe](https://github.com/Milkdown/milkdown) | MIT — Copyright (c) Mirone |
| `katex.js`, `katex.css`, `KaTeX_*` fonts | [KaTeX](https://github.com/KaTeX/KaTeX) | MIT — Copyright (c) Khan Academy and contributors |
| `marked.js` | [marked](https://github.com/markedjs/marked) | MIT — Copyright (c) Christopher Jeffrey |

Sources and exact versions are pinned in [`webvendor/package.json`](webvendor/package.json);
`node webvendor/build.mjs` regenerates the directory. `esbuild` does the bundling
(MIT, Copyright (c) Evan Wallace) but ships no code into the output.

The typefaces in `carrot/web/assets/fonts/` are under the SIL Open Font License,
each with its full license text alongside it: DM Mono, Plus Jakarta Sans,
Space Grotesk, Stack Sans Text.

Runtime Python dependencies are declared in `pyproject.toml` and installed from
PyPI rather than vendored; their licenses come with them.
