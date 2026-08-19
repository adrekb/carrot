// Mermaid, exposed as window.mermaid.
//
// A mermaid artifact was stored, listed, downloadable and editable, and shown
// as a <pre> full of `graph TD; A-->B`. Which is to say the one kind of
// artifact whose entire purpose is to be a picture was the only one rendered
// as its source — the model would answer "here is the flow" and hand over the
// instructions for drawing it.
//
// Bundled offline like everything else here rather than pulled from a CDN. The
// argument of this app is that it runs on your machine, and a diagram that
// only draws when the network is up would be the one part of a reply that
// depends on somebody else's server.
//
// `startOnLoad: false` because nothing here is a page of static diagrams: each
// one is rendered on demand into an element that already exists, by
// `renderMermaid` in features.js.
import mermaid from 'mermaid';

mermaid.initialize({
    startOnLoad: false,
    // The app is dark by default and the theme is picked per render anyway —
    // see `mermaidTheme` — but a sensible default matters for the first paint.
    theme: 'dark',
    securityLevel: 'strict',
    fontFamily: 'inherit',
});

window.mermaid = mermaid;
