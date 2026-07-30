---
kind: external_dependency
name: Electron Desktop Application Framework
slug: electron
category: external_dependency
category_hints:
    - vendor_identity
    - framework_behavior
scope:
    - '**'
source_files:
    - gui/package.json
    - gui/main.js
    - gui/preload.js
---

### Identity & Role
Electron wraps the FastAPI backend into a native desktop application with a multi-pane dashboard interface.

### Integration Points
- `gui/` directory contains Electron app with React frontend built via Vite.
- Main process in `main.js` with preload script `preload.js` for secure IPC.
- Package configuration targets Windows NSIS installer generation.
- App ID `com.carrot.ai` with product name "Carrot".

### Usage Model
- Development: `npm start` runs Electron with hot reload.
- Build: `npm run build` creates production bundle, `npm run package` generates Windows installer.
- Output location: `gui/dist/Carrot Setup.exe`.
- Global shortcut `Alt+Space` provides overlay functionality across applications.

### Dependencies
- Node.js 18+ required for building.
- React 18.x for UI components.
- Axios for HTTP requests to backend API.