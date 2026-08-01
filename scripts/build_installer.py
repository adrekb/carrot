"""Build the Carrot one-click installer for the current platform.

Produces a real, double-clickable app on all three OSes:
  - Windows: one-click NSIS installer (``Carrot Setup <version>.exe``) with
    the official Ollama installer bundled so first launch works offline-ish.
  - macOS: ``.dmg`` (Apple Silicon or Intel, matching the build machine).
  - Linux: ``.AppImage`` and ``.deb``.

The pipeline, same on every OS:
  1. Freeze the Python backend with PyInstaller -> ``dist/backend/carrot-backend``
     (end users never need Python installed).
  2. Build the offline editor bundles (webvendor) if npm is available.
  3. Package with electron-builder for the host platform; the frozen
     backend rides along as an extraResource.

GPU support needs no per-vendor builds: Ollama's own artifacts carry
CUDA + ROCm on Windows/Linux and Metal on macOS, and the runtime
bootstrap (carrot/bootstrap.py) fetches the right one per machine.

Usage:
    python scripts/build_installer.py                 # full build for this OS
    python scripts/build_installer.py --backend-only  # just freeze the backend
    python scripts/build_installer.py --no-ollama     # skip bundling OllamaSetup.exe (Windows)
"""
import os
import sys
import json
import shutil
import platform
import subprocess
import argparse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(ROOT, "assets")
BACKEND_DIST = os.path.join(ROOT, "dist", "backend")
GUI_DIR = os.path.join(ROOT, "gui")
OLLAMA_INSTALLER_PATH = os.path.join(ASSETS_DIR, "ollama-setup.exe")
OLLAMA_DOWNLOAD_URL = "https://ollama.com/download/OllamaSetup.exe"
DEFAULT_MODEL = "gemma4:e4b"

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
NPM = "npm.cmd" if IS_WINDOWS else "npm"


def log(msg):
    print(f"[build] {msg}", flush=True)


def build_backend():
    """Freeze the FastAPI backend into dist/backend/carrot-backend."""
    log("Freezing Python backend with PyInstaller…")
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        log("PyInstaller missing — installing…")
        subprocess.run([sys.executable, "-m", "pip", "install", "pyinstaller"], check=True)

    sep = ";" if IS_WINDOWS else ":"
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", "carrot-backend",
        "--onedir", "--noconfirm", "--clean",
        "--noconsole" if IS_WINDOWS else "--console",
        "--distpath", BACKEND_DIST,
        "--workpath", os.path.join(ROOT, "dist", "pyi-build"),
        "--specpath", os.path.join(ROOT, "dist"),
        "--paths", ROOT,
        # Package data the server reads at runtime (web UI, packs).
        "--add-data", f"{os.path.join(ROOT, 'carrot', 'web')}{sep}carrot/web",
        "--add-data", f"{os.path.join(ROOT, 'carrot', 'packs')}{sep}carrot/packs",
        # Uvicorn's workers/loops are imported by string name.
        "--collect-submodules", "uvicorn",
        "--hidden-import", "carrot.app",
        # Not in Carrot's dependency tree; PyInstaller's analysis can trip
        # over a system-wide copy's Rust bindings, so keep it out explicitly.
        "--exclude-module", "cryptography",
        os.path.join(ROOT, "scripts", "backend_entry.py"),
    ]
    subprocess.run(cmd, check=True, cwd=ROOT)
    exe = os.path.join(BACKEND_DIST, "carrot-backend",
                       "carrot-backend.exe" if IS_WINDOWS else "carrot-backend")
    if not os.path.exists(exe):
        raise RuntimeError(f"PyInstaller did not produce {exe}")
    log(f"Backend frozen -> {exe}")
    return exe


def download_ollama_installer():
    """Windows only: bundle the official Ollama installer inside ours."""
    os.makedirs(ASSETS_DIR, exist_ok=True)
    if os.path.exists(OLLAMA_INSTALLER_PATH):
        log(f"Ollama installer already present: {OLLAMA_INSTALLER_PATH}")
        return
    log(f"Downloading Ollama installer from {OLLAMA_DOWNLOAD_URL}…")
    urllib.request.urlretrieve(OLLAMA_DOWNLOAD_URL, OLLAMA_INSTALLER_PATH)
    log(f"Saved -> {OLLAMA_INSTALLER_PATH}")


def write_model_manifest():
    os.makedirs(ASSETS_DIR, exist_ok=True)
    manifest_path = os.path.join(ASSETS_DIR, "bootstrap-manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump({"default_model": DEFAULT_MODEL}, f, indent=2)
    log(f"Wrote model manifest -> {manifest_path}")


def build_webvendor():
    """Offline Monaco/Milkdown bundles; skipped if already built or no npm."""
    vendor_dir = os.path.join(ROOT, "webvendor")
    built_marker = os.path.join(ROOT, "carrot", "web", "vendor", "monaco")
    if os.path.exists(built_marker):
        log("Editor bundles already built — skipping webvendor.")
        return
    if not shutil.which(NPM):
        log("npm not found — skipping webvendor build (editor bundles may be stale).")
        return
    log("Building offline editor bundles…")
    subprocess.run([NPM, "install", "--quiet"], cwd=vendor_dir, check=True)
    subprocess.run([NPM, "run", "build"], cwd=vendor_dir, check=True)


def build_electron():
    """Package the desktop app for the host platform."""
    if not os.path.exists(os.path.join(GUI_DIR, "node_modules")):
        log("Installing Electron dependencies…")
        subprocess.run([NPM, "install", "--quiet"], cwd=GUI_DIR, check=True)
    log("Packaging with electron-builder…")
    subprocess.run([NPM, "run", "dist"], cwd=GUI_DIR, check=True)
    log(f"Installer output in {os.path.join(GUI_DIR, 'dist')}")


def main():
    parser = argparse.ArgumentParser(description="Carrot one-click installer build")
    parser.add_argument("--backend-only", action="store_true",
                        help="Freeze the backend and stop")
    parser.add_argument("--no-ollama", action="store_true",
                        help="Windows: skip bundling OllamaSetup.exe (bootstrap downloads it instead)")
    args = parser.parse_args()

    log(f"=== Carrot build on {platform.system()} {platform.machine()} ===")
    build_backend()
    if args.backend_only:
        return 0
    write_model_manifest()
    if IS_WINDOWS and not args.no_ollama:
        download_ollama_installer()
    build_webvendor()
    build_electron()
    log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
