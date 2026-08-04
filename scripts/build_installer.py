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
# Playwright's Chromium, downloaded at build time and shipped inside the
# installer so the Agent tab works with nothing for the user to install.
PW_BROWSERS_DIST = os.path.join(ROOT, "dist", "pw-browsers")
GUI_DIR = os.path.join(ROOT, "gui")
OLLAMA_INSTALLER_PATH = os.path.join(ASSETS_DIR, "ollama-setup.exe")
OLLAMA_DOWNLOAD_URL = "https://ollama.com/download/OllamaSetup.exe"
DEFAULT_MODEL = "gemma4:e4b"

IS_WINDOWS = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"
NPM = "npm.cmd" if IS_WINDOWS else "npm"


def log(msg):
    print(f"[build] {msg}", flush=True)


def write_build_stamp():
    """Record version and commit so the running app can identify itself."""
    version = "unknown"
    try:
        import tomllib
        with open(os.path.join(ROOT, "pyproject.toml"), "rb") as handle:
            version = tomllib.load(handle)["project"]["version"]
    except Exception:
        pass
    commit = ""
    try:
        commit = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT,
            stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        pass
    path = os.path.join(ROOT, "carrot", "_build.py")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(f'"""Generated at build time."""\nVERSION = "{version}"\nCOMMIT = "{commit}"\n')
    log(f"Build stamp: {version}+{commit}")


def build_backend():
    """Freeze the FastAPI backend into dist/backend/carrot-backend."""
    write_build_stamp()
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
    ]
    cmd += optional_dependency_flags()
    cmd.append(os.path.join(ROOT, "scripts", "backend_entry.py"))
    subprocess.run(cmd, check=True, cwd=ROOT)
    exe = os.path.join(BACKEND_DIST, "carrot-backend",
                       "carrot-backend.exe" if IS_WINDOWS else "carrot-backend")
    if not os.path.exists(exe):
        raise RuntimeError(f"PyInstaller did not produce {exe}")
    log(f"Backend frozen -> {exe}")
    return exe


# Optional dependencies, and why each needs to be named explicitly.
#
# Every one of these is imported lazily, inside a function and behind a
# try/except, so that a source checkout without them still runs. That is the
# right runtime behaviour and it is invisible to PyInstaller: static analysis
# walks module-level imports, sees no reference, and silently ships a build
# without them. The feature then reports itself as "not installed" on a
# machine where it was, in fact, installed at build time.
#
# --collect-all rather than --hidden-import because none of these are pure
# module trees: playwright carries a Node driver, sqlite-vec and onnxruntime
# carry native libraries, anthropic carries package data.
OPTIONAL_COLLECTS = [
    ("playwright", "browser control for the Agent tab"),
    ("anthropic", "Claude cloud routing"),
    ("sqlite_vec", "ANN vector search"),
    ("kokoro_onnx", "speech synthesis"),
    ("onnxruntime", "speech synthesis runtime"),
    ("sounddevice", "audio output"),
]


def optional_dependency_flags():
    """--collect-all for each optional dependency that is actually installed.

    Naming one that is absent makes PyInstaller fail the build, so this asks
    first and reports what is going in. A missing entry is not fatal — it just
    means that feature is unavailable in the resulting installer, which is
    worth saying out loud in the build log.
    """
    import importlib.util
    flags, present, missing = [], [], []
    for module, purpose in OPTIONAL_COLLECTS:
        try:
            found = importlib.util.find_spec(module) is not None
        except (ImportError, ValueError):
            found = False
        if found:
            flags += ["--collect-all", module]
            present.append(module)
        else:
            missing.append(f"{module} ({purpose})")
    if present:
        log(f"Bundling optional dependencies: {', '.join(present)}")
    for item in missing:
        log(f"NOT bundled, feature will be unavailable: {item}")
    return flags


def install_playwright_browser():
    """Download Chromium into dist/pw-browsers so it can ride in the installer.

    Playwright normally caches browsers per-user, which is no good for a
    packaged app: the user has no Python to run ``playwright install`` with.
    PLAYWRIGHT_BROWSERS_PATH redirects the download into the build tree, and
    electron-builder copies it in as a resource. carrot/browser.py points
    Playwright back at it at runtime.

    The agent runs headful by default — the user watches it work — so the
    headless shell is dead weight. `playwright install` pulls it alongside
    Chromium unless told not to, and on the first CI run that was 115 MB of
    download and a few hundred MB in the installer for a binary nothing can
    reach. --no-shell suppresses it, with a fallback for older Playwright
    versions that predate the flag.
    """
    try:
        import playwright  # noqa: F401
    except ImportError:
        log("playwright not installed — skipping browser bundle. "
            "The Agent tab will be unavailable in this build. "
            "Install with: pip install -e '.[browser]'")
        return False
    if os.path.isdir(PW_BROWSERS_DIST) and os.listdir(PW_BROWSERS_DIST):
        log(f"Chromium already downloaded -> {PW_BROWSERS_DIST}")
        return True
    os.makedirs(PW_BROWSERS_DIST, exist_ok=True)
    log("Downloading Chromium for the Agent tab (a few hundred MB)…")
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=PW_BROWSERS_DIST)
    base = [sys.executable, "-m", "playwright", "install"]
    result = subprocess.run(base + ["--no-shell", "chromium"], cwd=ROOT, env=env)
    if result.returncode != 0:
        log("--no-shell not supported by this Playwright; "
            "installing Chromium with the headless shell as well.")
        subprocess.run(base + ["chromium"], check=True, cwd=ROOT, env=env)
    size_mb = sum(
        os.path.getsize(os.path.join(dirpath, name))
        for dirpath, _, names in os.walk(PW_BROWSERS_DIST) for name in names
    ) / (1024 * 1024)
    log(f"Chromium bundled -> {PW_BROWSERS_DIST} ({size_mb:.0f} MB)")
    return True


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
    parser.add_argument("--no-browser", action="store_true",
                        help="Skip bundling Chromium; the Agent tab will be unavailable")
    args = parser.parse_args()

    log(f"=== Carrot build on {platform.system()} {platform.machine()} ===")
    build_backend()
    if args.backend_only:
        return 0
    write_model_manifest()
    if not args.no_browser:
        install_playwright_browser()
    if IS_WINDOWS and not args.no_ollama:
        download_ollama_installer()
    build_webvendor()
    build_electron()
    log("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
