---
kind: frontend_style
name: Carrot Web UI — CSS Variables + Vanilla JS Frontend
category: frontend_style
scope:
    - '**'
source_files:
    - carrot/web/css/style.css
    - carrot/web/index.html
    - carrot/web/js/app.js
    - carrot/web/js/search.js
    - gui/package.json
    - gui/vite.config.js
---

The Carrot project ships two distinct frontend styling approaches:

1. **Built-in web UI** (`carrot/web/`) — a self-contained, vanilla-HTML/CSS/JS interface served by the Python FastAPI core.
2. **Electron desktop shell** (`gui/`) — an Electron app built with React and Vite that can optionally render the same content.

### What system/approach is used
- **CSS methodology**: Pure CSS with a single global stylesheet (`carrot/web/css/style.css`). No preprocessors, no CSS-in-JS, no component-scoped stylesheets.
- **Design tokens**: A `:root` block defines all colors, fonts, and visual constants as CSS custom properties (e.g. `--bg`, `--surface`, `--accent`, `--carrot`, `--text`, `--border`, `--editor-bg`, `--success`, `--error`, `--font-mono`, `--font-sans`, `--glass`). All components reference these variables rather than hard-coded values.
- **Layout strategy**: Flexbox for the overall page layout (`#app` → `#sidebar` + `#main`) and within each view; CSS Grid is used sparingly for status cards and leaderboard entries.
- **Responsive approach**: A single `@media (max-width: 768px)` breakpoint collapses the sidebar to icon-only mode and hides text labels.
- **Build tooling**: The Electron GUI uses Vite (`vite.config.js`) with the React plugin, proxying `/api` requests to the local FastAPI server at `http://127.0.0.1:8181`. Styles in the Electron side are not present in this snapshot — the GUI currently relies on the bundled HTML/CSS from the core or external assets.

### Key files and packages
- `carrot/web/css/style.css` — the single source of truth for all visual styling, design tokens, and responsive rules.
- `carrot/web/index.html` — the HTML shell that wires views (`view-chat`, `view-editor`, `view-search`, etc.) and includes the CSS and JS scripts.
- `carrot/web/js/app.js`, `carrot/web/js/search.js` — vanilla JavaScript that drives view switching, DOM updates, and API calls.
- `gui/package.json` — declares React 18, Axios, Vite 5, and Electron 30 as dependencies; build scripts use `vite build` and `electron-builder`.
- `gui/vite.config.js` — configures the React plugin, output directory (`public/`), dev server port (3000), and API proxy.

### Architecture and conventions
- **View-based routing**: Each feature lives in its own `<div id="view-*" class="view">` container; visibility is toggled via a `.active` class set by `switchView()` in `app.js`. There is no client-side router.
- **ID-driven DOM selection**: Elements are addressed by `id` attributes throughout the HTML and referenced directly in JS (`document.getElementById(...)`). No component framework abstracts this.
- **Token-first styling**: Every color, spacing, and font choice goes through CSS variables defined in `:root`. Adding a new theme would only require redefining those variables.
- **Consistent card/list pattern**: Reusable patterns like `.note-card`, `.goal-item`, `.reminder-item`, `.search-result`, and `.status-card` share the same border/background/border-radius/shadow treatment, keeping the UI visually uniform across features.
- **Glassmorphism accent**: A `.glass` utility applies `backdrop-filter: blur(20px)` with a semi-transparent background for overlay elements.
- **Dark theme default**: The entire palette is dark-mode oriented (`--bg: #1a1a2e`, `--surface: #16213e`, `--card: #0f3460`); no light-theme toggle exists.

### Conventions and constraints
- **Single stylesheet rule**: All styles must be added to `carrot/web/css/style.css`; there is no per-component CSS splitting in the vanilla web UI.
- **CSS variable usage**: Colors and fonts should be referenced via `var(--name)` rather than literal hex values, ensuring consistency and easy theming.
- **View structure**: New features should follow the existing `<div id="view-<name>" class="view">` pattern and be wired into the sidebar navigation in `index.html`.
- **Responsive baseline**: The only breakpoint is `768px`; additional breakpoints should be added consistently if needed.
- **Electron vs. web UI**: The Electron shell (`gui/`) is a separate React/Vite application that proxies API calls to the same FastAPI backend; it does not import `style.css` directly in this snapshot, so changes to `carrot/web/css/style.css` do not automatically affect the Electron renderer unless explicitly linked.