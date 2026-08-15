"""Carrot bootstrap: ensures Ollama and the default model are ready before first use.

Works on all three platforms, without admin rights where possible:
  - Windows: run the official installer silently (bundled with the app or
    downloaded). The installer ships CUDA and ROCm support, so NVIDIA and
    AMD both work out of the box.
  - macOS: download the official ``ollama-darwin.tgz`` binary into Carrot's
    own data directory and run it from there. Metal acceleration is built
    in — Apple Silicon and Intel both covered by the universal binary.
  - Linux: download ``ollama-linux-{amd64,arm64}.tgz`` into the data
    directory (CUDA runners included); when an AMD GPU is detected, also
    fetch the ROCm add-on archive so Radeon cards accelerate too.
"""
import os
import json
import platform
import tarfile
import time
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Optional, Callable

import requests

from carrot.config import CARROT_DIR, get_config, set_config

# The floor, not the default. There is no longer one model that is right for
# every machine — `hub.default_model()` picks that from the memory the machine
# actually has — and this is only what is left when hardware detection itself
# fails. Small on purpose: too small is slow-witted, too big does not run at
# all, and only one of those gets the user to the screen where they can choose.
DEFAULT_MODEL = "llama3.2:3b"
OLLAMA_PORT = 11434
OLLAMA_DOWNLOAD_URL = "https://ollama.com/download/OllamaSetup.exe"
OLLAMA_RELEASE_BASE = "https://github.com/ollama/ollama/releases/latest/download"
BOOTSTRAP_STATE_PATH = os.path.join(CARROT_DIR, "config", "bootstrap.json")
# Where the tarball-based install lands on macOS/Linux (user-writable, no sudo).
MANAGED_OLLAMA_DIR = os.path.join(CARROT_DIR, "ollama")


def ollama_artifact_url() -> str:
    """The right Ollama artifact for this OS and architecture."""
    system = platform.system()
    if system == "Windows":
        return OLLAMA_DOWNLOAD_URL
    if system == "Darwin":
        return f"{OLLAMA_RELEASE_BASE}/ollama-darwin.tgz"
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "amd64"
    return f"{OLLAMA_RELEASE_BASE}/ollama-linux-{arch}.tgz"


def ollama_rocm_addon_url() -> Optional[str]:
    """The ROCm add-on archive, only meaningful on x86-64 Linux."""
    if platform.system() == "Linux" and platform.machine().lower() not in ("arm64", "aarch64"):
        return f"{OLLAMA_RELEASE_BASE}/ollama-linux-amd64-rocm.tgz"
    return None


def _now_iso():
    return __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()


def load_bootstrap_state() -> dict:
    if os.path.exists(BOOTSTRAP_STATE_PATH):
        try:
            with open(BOOTSTRAP_STATE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {"ollama_installed": False, "model_pulled": False, "model_pulling": False}


def save_bootstrap_state(state: dict):
    os.makedirs(os.path.dirname(BOOTSTRAP_STATE_PATH), exist_ok=True)
    with open(BOOTSTRAP_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def get_ollama_executable() -> Optional[str]:
    """Return the path to the Ollama CLI, or None if not found."""
    ollama_exe = shutil.which("ollama")
    if ollama_exe:
        return ollama_exe
    candidates = [
        # Carrot's own managed install (macOS/Linux tarball layout).
        os.path.join(MANAGED_OLLAMA_DIR, "bin", "ollama"),
        os.path.join(MANAGED_OLLAMA_DIR, "ollama"),
        # Common Windows install locations.
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Ollama\ollama.exe"),
        os.path.expandvars(r"%ProgramFiles%\Ollama\ollama.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Ollama\ollama.exe"),
        # Common macOS/Linux locations.
        "/usr/local/bin/ollama",
        "/opt/homebrew/bin/ollama",
        "/usr/bin/ollama",
        "/Applications/Ollama.app/Contents/Resources/ollama",
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    return None


def is_ollama_running() -> bool:
    try:
        resp = requests.get(f"http://127.0.0.1:{OLLAMA_PORT}/api/tags", timeout=2)
        return resp.status_code == 200
    except Exception:
        return False


def ensure_ollama_running(timeout: int = 60) -> bool:
    """Start the Ollama service if it is installed but not running."""
    if is_ollama_running():
        return True
    exe = get_ollama_executable()
    if not exe:
        return False
    try:
        subprocess.Popen([exe, "serve"], creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if is_ollama_running():
                return True
            time.sleep(0.5)
    except Exception:
        return False
    return False


def list_local_models() -> list:
    if not is_ollama_running():
        return []
    try:
        resp = requests.get(f"http://127.0.0.1:{OLLAMA_PORT}/api/tags", timeout=5)
        data = resp.json()
        return [m.get("name", m.get("model", "")) for m in data.get("models", [])]
    except Exception:
        return []


def is_model_available(model: str = DEFAULT_MODEL) -> bool:
    models = list_local_models()
    return any(m == model or m.startswith(f"{model}:") for m in models)


def find_bundled_installer() -> Optional[str]:
    """Locate a bundled Ollama installer shipped with the app."""
    candidates = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "ollama-setup.exe"),
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "assets", "ollama-setup.exe"),
        os.path.join(os.path.expandvars(r"%LOCALAPPDATA%\Carrot\assets"), "ollama-setup.exe"),
    ]
    # The Electron shell passes its resources dir so a packaged app finds
    # the installer regardless of where it was installed to.
    resources = os.environ.get("CARROT_RESOURCES")
    if resources:
        candidates.insert(0, os.path.join(resources, "assets", "ollama-setup.exe"))
    # When running from Electron asar, the unpacked extraFiles land near exe
    if getattr(__import__("sys"), "frozen", False):
        candidates.insert(0, os.path.join(os.path.dirname(__import__("sys").executable), "assets", "ollama-setup.exe"))
    for c in candidates:
        if os.path.exists(c):
            return os.path.abspath(c)
    return None


def download_installer(destination: str, progress_cb: Optional[Callable] = None,
                       url: str = OLLAMA_DOWNLOAD_URL) -> bool:
    """Download an Ollama artifact with optional progress callback."""
    try:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            downloaded = 0
            with open(destination, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        downloaded += len(chunk)
                        if progress_cb:
                            progress_cb({"type": "download", "downloaded": downloaded, "total": total})
        return os.path.exists(destination)
    except Exception as e:
        if progress_cb:
            progress_cb({"type": "error", "message": f"Failed to download Ollama installer: {e}"})
        return False


def install_ollama(installer_path: str, progress_cb: Optional[Callable] = None) -> bool:
    """Run the Ollama installer silently on Windows."""
    if not os.path.exists(installer_path):
        if progress_cb:
            progress_cb({"type": "error", "message": "Ollama installer not found"})
        return False
    try:
        if progress_cb:
            progress_cb({"type": "install", "message": "Installing Ollama..."})
        proc = subprocess.run(
            [installer_path, "/S"],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if proc.returncode != 0:
            if progress_cb:
                progress_cb({"type": "error", "message": proc.stderr or "Ollama installer failed"})
            return False
        return True
    except Exception as e:
        if progress_cb:
            progress_cb({"type": "error", "message": f"Ollama install error: {e}"})
        return False


def _machine_has_amd_gpu() -> bool:
    try:
        from carrot.hub import _detect_amd_vram_gb
        return _detect_amd_vram_gb() >= 2
    except Exception:
        return False


def install_ollama_unix(progress_cb: Optional[Callable] = None) -> bool:
    """Install Ollama on macOS/Linux from the official release tarball.

    Extracts into Carrot's data directory — no sudo, no shell pipe to a
    remote script. On Linux with an AMD GPU the ROCm add-on archive is
    layered on top so Radeon acceleration works.
    """
    urls = [ollama_artifact_url()]
    rocm = ollama_rocm_addon_url()
    if rocm and _machine_has_amd_gpu():
        urls.append(rocm)
    os.makedirs(MANAGED_OLLAMA_DIR, exist_ok=True)
    for url in urls:
        name = url.rsplit("/", 1)[-1]
        archive = os.path.join(tempfile.gettempdir(), f"carrot_{name}")
        if progress_cb:
            progress_cb({"type": "status", "message": f"Downloading {name}…"})
        if not download_installer(archive, progress_cb, url=url):
            # The ROCm add-on is an enhancement; the base archive is not.
            if url == urls[0]:
                return False
            continue
        try:
            with tarfile.open(archive, "r:gz") as tf:
                try:
                    tf.extractall(MANAGED_OLLAMA_DIR, filter="data")
                except TypeError:  # Python < 3.10.12: no filter parameter
                    tf.extractall(MANAGED_OLLAMA_DIR)
        except Exception as e:
            if progress_cb:
                progress_cb({"type": "error", "message": f"Could not extract {name}: {e}"})
            if url == urls[0]:
                return False
        finally:
            try:
                os.remove(archive)
            except OSError:
                pass
    for candidate in (os.path.join(MANAGED_OLLAMA_DIR, "bin", "ollama"),
                      os.path.join(MANAGED_OLLAMA_DIR, "ollama")):
        if os.path.exists(candidate):
            os.chmod(candidate, 0o755)
            return True
    if progress_cb:
        progress_cb({"type": "error", "message": "Ollama binary missing after extraction"})
    return False


def pull_model(model: str = DEFAULT_MODEL, progress_cb: Optional[Callable] = None) -> bool:
    """Pull a model, reporting real byte-level progress.

    Uses Ollama's HTTP API rather than the CLI. The CLI renders progress
    with carriage returns (so line-buffered reads stall) and has to be
    found on PATH, which a frozen app launched before Ollama was
    installed may not see. The HTTP endpoint has neither problem and
    reports exact completed/total bytes for the progress bar.
    """
    try:
        with requests.post(
            f"http://127.0.0.1:{OLLAMA_PORT}/api/pull",
            json={"model": model, "name": model, "stream": True},
            stream=True,
            timeout=(10, 1800),
        ) as resp:
            if resp.status_code != 200:
                detail = resp.text[:300].strip() or f"HTTP {resp.status_code}"
                if progress_cb:
                    progress_cb({"type": "error", "message": f"Ollama refused the pull: {detail}"})
                return False
            last_error = None
            for raw in resp.iter_lines(decode_unicode=True):
                if not raw:
                    continue
                try:
                    update = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if update.get("error"):
                    last_error = update["error"]
                    if progress_cb:
                        progress_cb({"type": "error", "message": last_error})
                    continue
                if progress_cb:
                    progress_cb({
                        "type": "pull",
                        "model": model,
                        "status": update.get("status", ""),
                        "completed": update.get("completed"),
                        "total": update.get("total"),
                    })
            if last_error:
                return False
    except Exception as e:
        if progress_cb:
            progress_cb({"type": "error", "message": f"Could not reach Ollama to pull {model}: {e}"})
        return False
    # Trust the tag list rather than the stream ending politely.
    return is_model_available(model)


def get_target_model() -> str:
    """The model bootstrap should ensure: the user's choice, else the default.

    Bootstrap can run before the config database exists on a first launch,
    so a missing/uninitialized config means the stock default.
    """
    try:
        from carrot import hub as hub_mod

        return hub_mod.configured_or_default_model()
    except Exception:
        return DEFAULT_MODEL


def run_bootstrap(progress_cb: Optional[Callable] = None, model: Optional[str] = None) -> dict:
    """Run the full bootstrap flow and return final status.

    ``model`` is the tag chosen on the setup splash (from the Hub's
    hardware-based recommendations); omitted, it falls back to whatever
    is configured and finally to DEFAULT_MODEL.
    """
    state = load_bootstrap_state()
    target = model or get_target_model()
    result = {"ollama_installed": False, "model_pulled": False, "model": target, "error": None}

    # Keep the last real error so a failure reports *why*, not just "failed".
    last_error = {"message": None}
    caller_cb = progress_cb

    def emit(event):
        if event.get("type") == "error" and event.get("message"):
            last_error["message"] = event["message"]
        if caller_cb:
            caller_cb(event)

    progress_cb = emit
    progress_cb({"type": "status", "message": "Checking Ollama..."})

    # The service answering on the port is what actually matters; the CLI
    # may be installed but invisible to a frozen app's PATH.
    if not get_ollama_executable() and not is_ollama_running():
        if platform.system() == "Windows":
            installer = find_bundled_installer()
            if not installer:
                progress_cb({"type": "status", "message": "Downloading Ollama installer..."})
                installer = os.path.join(tempfile.gettempdir(), "carrot_ollama_setup.exe")
                if not download_installer(installer, progress_cb):
                    result["error"] = "Could not download Ollama installer"
                    return result
            if not install_ollama(installer, progress_cb):
                result["error"] = "Ollama installation failed"
                return result
        else:
            if not install_ollama_unix(progress_cb):
                result["error"] = "Ollama installation failed"
                return result

    state["ollama_installed"] = True
    result["ollama_installed"] = True
    save_bootstrap_state(state)

    if not is_ollama_running():
        progress_cb({"type": "status", "message": "Starting Ollama service..."})
        if not ensure_ollama_running(timeout=90):
            result["error"] = ("Ollama installed but its service did not start. "
                               "Open the Ollama app once, then press Retry.")
            return result

    if not is_model_available(target):
        state["model_pulling"] = True
        save_bootstrap_state(state)
        progress_cb({"type": "status", "message": f"Downloading {target}…"})
        if pull_model(target, progress_cb):
            state["model_pulled"] = True
        else:
            result["error"] = last_error["message"] or f"Failed to pull {target}"
    else:
        state["model_pulled"] = True

    state["model_pulling"] = False
    save_bootstrap_state(state)
    result["model_pulled"] = state["model_pulled"]

    # Persist the chosen model in config
    set_config("ollama_model", target)
    set_config("ollama_model_recap", target)
    set_config("ollama_model_search", target)

    return result


def bootstrap_status() -> dict:
    """Return the current bootstrap/Ollama state for the UI splash screen."""
    state = load_bootstrap_state()
    target = get_target_model()
    ollama_installed = get_ollama_executable() is not None
    model_pulled = is_model_available(target)
    return {
        "ollama_installed": ollama_installed,
        "ollama_running": is_ollama_running(),
        "model_pulled": model_pulled,
        "model_pulling": state.get("model_pulling", False),
        "default_model": target,
        # Complete when Ollama and the model are actually present, regardless of
        # whether Carrot's bootstrap ran (e.g. Ollama installed externally).
        "bootstrap_complete": ollama_installed and model_pulled,
    }
