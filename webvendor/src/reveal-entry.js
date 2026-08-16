// reveal.js — exposed as window.Reveal (+ reveal.css)
//
// Only the core and its base stylesheet. None of reveal's own themes are
// bundled: they set their own fonts and colours, which would make a deck the
// one surface in Carrot that does not look like Carrot. The deck styling lives
// in css/style.css against the same tokens as everything else.
import Reveal from 'reveal.js';
import 'reveal.js/reveal.css';

window.Reveal = Reveal;
