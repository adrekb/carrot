---
kind: build_system
name: Carrot Desktop Build System — Python + Electron + Vite Monorepo
category: build_system
scope:
    - '**'
source_files:
    - build.bat
    - pyproject.toml
    - gui/package.json
    - gui/vite.config.js
    - gui/main.js
---

The Carrot project uses a hybrid build system that combines a Python package (setuptools) with an Electron desktop shell built via Vite and electron-builder. The monorepo is split into two build targets: the `carrot` Python core and the `gui` Electron application, orchestrated by a single Windows batch script.

**Build tools and frameworks**
- Python packaging: setuptools (via `pyproject.toml`) with PEP 517 backend `setuptools.backends._legacy:_Backend`. Version is `0.2.0`, requires Python >=3.10.
- Node.js/Electron: Vite for frontend asset bundling, React plugin, and dev server proxying to the FastAPI backend on port 8181. Electron 30.x as the runtime, electron-builder 24.x for packaging into a Windows NSIS installer.
- Orchestration: a top-level `build.bat` script that runs the full pipeline in order.

**Key files**
- `pyproject.toml` — declares Python dependencies (FastAPI, uvicorn, httpx, pydantic, etc.), entry point `carrot = "carrot.main:main"`, and optional dev deps (`pytest`, `pylint`).
- `build.bat` — sequential 5-step builder: install Python deps (`pip install -e .`), install Node deps (`npm install`), build frontend (`npm run build`), package Electron app (`npm run package`), then prints usage and output path (`gui\dist\Carrot Setup.exe`).
- `gui/package.json` — defines scripts (`start`, `dev`, `build`, `package`, `dist`), Electron/electron-builder config targeting Windows NSIS with output directory `dist`, and app metadata (`appId: com.carrot.ai`, `productName: Carrot`).
- `gui/vite.config.js` — builds assets into `gui/public/`, proxies `/api` requests to `http://127.0.0.1:8181` during development, serves dev server on port 3000.
- `gui/main.js` — Electron main process that spawns the Python FastAPI server (`python -m carrot.app`), creates the main window and an always-on-top overlay window, registers global shortcuts (`Alt+Space`, `Alt+Q`), and bridges renderer IPC calls over HTTP to the local FastAPI API.

**Architecture and conventions**
- Two-tier runtime: the Electron shell is purely a UI wrapper; all business logic lives in the Python FastAPI server started as a child process. The GUI communicates with the backend exclusively via HTTP (`http://127.0.0.1:8181/api/*`), not through native IPC.
- Frontend assets are emitted into `gui/public/` so they can be served directly by Electron's `loadFile`. The dev server proxies API calls to avoid CORS issues during development.
- Packaging targets Windows only (NSIS installer); cross-platform builds are not configured.
- Version numbers are synchronized manually between `pyproject.toml` and `gui/package.json` (both at `0.2.0`).

**Conventions and constraints**
- Development workflow: run `build.bat` from the repo root to install both Python and Node dependencies, build the React frontend, and produce a distributable installer under `gui/dist/`.
- Runtime dependency: the Electron app expects the `carrot` Python package to be installed in editable mode (`pip install -e .`) so `python -m carrot.app` resolves correctly.
- Port contract: the FastAPI server must listen on `127.0.0.1:8181`; the Electron preload and vite dev proxy both hardcode this address.
- No CI/Docker/Makefile: there is no automated CI pipeline, Docker image, or Unix shell script present in the repository — building is manual via `build.bat`.