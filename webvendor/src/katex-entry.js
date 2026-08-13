// KaTeX, exposed as window.katex (+ katex.css and its fonts).
//
// The fonts were already in carrot/web/vendor — Milkdown's bundle pulls KaTeX
// in for the notes editor and esbuild emitted them as a side effect. So the
// app has shipped every glyph needed to render maths since the editor landed,
// and had no way to render any of it outside that one panel: chat, Research
// and the Code tab all go through `mdToHtml`, which is `marked` with no maths
// extension, so a model that answered with $E = mc^2$ printed the dollars.
//
// Bundled from the same node_modules copy the editor uses, so the two cannot
// drift to different KaTeX versions and render the same expression differently
// in a note and in a chat reply.
import katex from 'katex';
import 'katex/dist/katex.css';

window.katex = katex;
