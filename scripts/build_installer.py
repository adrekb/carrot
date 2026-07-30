"""Build-time bundler for the Carrot one-click installer.

Responsibilities:
  1. Ensure the Windows Ollama installer is present at ``assets/ollama-setup.exe``
     (download it if missing) so it can be bundled with the Electron package.
  2. Record the default model (``gemma4:e4b``) that the runtime bootstrap will
     pull on first launch.
  3. Optionally drive the Electron build (``npm run dist``) to produce the final
     ``Carrot Setup.exe`` under ``gui/dist``.

Usage:
    python scripts/build_installer.py            # download installer only
    python scripts/build_installer.py --build    # also run the Electron build
"""
import os
import sys
import json
import subprocess
import argparse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS_DIR = os.path.join(ROOT, "assets")
OLLAMA_INSTALLER_NAME = "ollama-setup.exe"
OLLAMA_INSTALLER_PATH = os.path.join(ASSETS_DIR, OLLAMA_INSTALLER_NAME)
OLLAMA_DOWNLOAD_URL = "https://ollama.com/download/OllamaSetup.exe"
DEFAULT_MODEL = "gemma4:e4b"


def download_ollama_installer() -> str:
    """Download the Ollama Windows installer into assets/ if not present."""
    os.makedirs(ASSETS_DIR, exist_ok=True)
    if os.path.exists(OLLAMA_INSTALLER_PATH):
        size_mb = os.path.getsize(OLLAMA_INSTALLER_PATH) / (1024 * 1024)
        print(f"[bundler] Ollama installer already present ({size_mb:.1f} MB): {OLLAMA_INSTALLER_PATH}")
        return OLLAMA_INSTALLER_PATH

    print(f"[bundler] Downloading Ollama installer from {OLLAMA_DOWNLOAD_URL} ...")
    try:
        def _progress(blocks, block_size, total_size):
            if total_size > 0:
                done = min(blocks * block_size, total_size)
                pct = done * 100 // total_size
                sys.stdout.write(f"\r[bundler] Downloading... {pct}%")
                sys.stdout.flush()

        urllib.request.urlretrieve(OLLAMA_DOWNLOAD_URL, OLLAMA_INSTALLER_PATH, _progress)
        sys.stdout.write("\n")
    except Exception as e:
        if os.path.exists(OLLAMA_INSTALLER_PATH):
            os.remove(OLLAMA_INSTALLER_PATH)
        raise RuntimeError(f"Failed to download Ollama installer: {e}")

    size_mb = os.path.getsize(OLLAMA_INSTALLER_PATH) / (1024 * 1024)
    print(f"[bundler] Saved Ollama installer ({size_mb:.1f} MB) -> {OLLAMA_INSTALLER_PATH}")
    return OLLAMA_INSTALLER_PATH


def write_model_manifest() -> None:
    """Persist the default model so the runtime bootstrap knows what to pull."""
    manifest = {
        "default_model": DEFAULT_MODEL,
        "ollama_installer": OLLAMA_INSTALLER_NAME,
        "ollama_download_url": OLLAMA_DOWNLOAD_URL,
    }
    manifest_path = os.path.join(ASSETS_DIR, "bootstrap-manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    print(f"[bundler] Wrote model manifest -> {manifest_path} (model={DEFAULT_MODEL})")


def run_electron_build() -> None:
    """Run the Electron packaging step to produce Carrot Setup.exe."""
    gui_dir = os.path.join(ROOT, "gui")
    if not os.path.exists(os.path.join(gui_dir, "node_modules")):
        print("[bundler] Installing Node dependencies (npm install)...")
        subprocess.run(["npm", "install"], cwd=gui_dir, check=True)

    print("[bundler] Building Electron package (npm run dist)...")
    subprocess.run(["npm", "run", "dist"], cwd=gui_dir, check=True)
    print(f"[bundler] Electron build complete. Output in: {os.path.join(gui_dir, 'dist')}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Carrot one-click installer bundler")
    parser.add_argument("--build", action="store_true", help="Also run the Electron build")
    args = parser.parse_args()

    print("[bundler] === Carrot installer bundler ===")
    download_ollama_installer()
    write_model_manifest()

    if args.build:
        run_electron_build()
    else:
        print("[bundler] Skipping Electron build (pass --build to produce Carrot Setup.exe).")

    print("[bundler] Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
