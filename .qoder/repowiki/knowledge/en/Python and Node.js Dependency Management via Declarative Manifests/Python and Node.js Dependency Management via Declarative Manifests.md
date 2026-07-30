---
kind: dependency_management
name: Python and Node.js Dependency Management via Declarative Manifests
category: dependency_management
scope:
    - '**'
source_files:
    - pyproject.toml
    - gui/package.json
    - gui/vite.config.js
---

This repository manages dependencies for two distinct runtime environments using declarative manifest files, with no lockfiles or vendoring present.

**Python (Carrot core)**
- Dependencies are declared in `pyproject.toml` under the `[project]` section using PEP 621 format. The build system uses setuptools (`setuptools>=68.0`) as the backend.
- Runtime dependencies include FastAPI, Uvicorn, Pydantic v2, httpx, requests, feedparser, Jinja2, DuckDuckGo search, BeautifulSoup4, and NumPy — all pinned with minimum version constraints (`>=`).
- Optional development dependencies are grouped under `[project.optional-dependencies] dev`, listing pytest, httpx, and pylint.
- No `requirements.txt`, `Pipfile`, `poetry.lock`, or `pipenv.lock` exists; dependency resolution is left to the installer at install time.
- No Python package vendoring directory is present.

**Node.js (Electron GUI)**
- Dependencies are declared in `gui/package.json`. Production dependencies include React 18, ReactDOM, and Axios. Development dependencies include Electron 30, electron-builder 24, Vite 5, and the React plugin for Vite.
- Version ranges use caret (`^`) semantics, allowing compatible minor/patch updates.
- No `package-lock.json`, `yarn.lock`, or `pnpm-lock.yaml` is committed; dependency resolution is not locked in version control.
- No `node_modules` directory is tracked by Git (consistent with a `.gitignore` that excludes it).
- Build and packaging are configured via `electron-builder` in the `build` field of `package.json`, targeting Windows NSIS installers.

**Build and tooling integration**
- The Python entry point is registered as a console script `carrot = "carrot.main:main"` in `pyproject.toml`.
- The Electron app exposes npm scripts for start, dev, build, package, and dist commands, with Vite proxying `/api` requests to the local FastAPI server on port 8181 during development (`gui/vite.config.js`).
- A `build.bat` file exists at the repository root but its contents were not examined.

**Conventions and constraints**
- Both manifests use minimum-version pinning rather than exact versions, prioritizing compatibility over reproducibility.
- There is no private registry configuration, no `PIP_INDEX_URL`/`PYPI_MIRROR` setup, and no `npmrc`/`.npmrc` customization visible.
- Lockfiles are absent from version control, meaning dependency resolution is non-deterministic across machines unless an external lockfile strategy is applied outside this repo.