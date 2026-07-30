"""Carrot bootstrap: ensures Ollama and the default model are ready before first use."""
import os
import json
import time
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Optional, Callable

import requests

from carrot.config import CARROT_DIR, get_config, set_config

DEFAULT_MODEL = "gemma4:e4b"
OLLAMA_PORT = 11434
OLLAMA_DOWNLOAD_URL = "https://ollama.com/download/OllamaSetup.exe"
BOOTSTRAP_STATE_PATH = os.path.join(CARROT_DIR, "config", "bootstrap.json")


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
    # Common Windows install locations
    candidates = [
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Ollama\ollama.exe"),
        os.path.expandvars(r"%LOCALAPPDATA%\Ollama\ollama.exe"),
        os.path.expandvars(r"%ProgramFiles%\Ollama\ollama.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Ollama\ollama.exe"),
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
    # When running from Electron asar, the unpacked extraFiles land near exe
    if getattr(__import__("sys"), "frozen", False):
        candidates.insert(0, os.path.join(os.path.dirname(__import__("sys").executable), "assets", "ollama-setup.exe"))
    for c in candidates:
        if os.path.exists(c):
            return os.path.abspath(c)
    return None


def download_installer(destination: str, progress_cb: Optional[Callable] = None) -> bool:
    """Download the Ollama Windows installer with optional progress callback."""
    try:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with requests.get(OLLAMA_DOWNLOAD_URL, stream=True, timeout=120) as r:
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


def pull_model(model: str = DEFAULT_MODEL, progress_cb: Optional[Callable] = None) -> bool:
    """Pull the given model via Ollama and stream progress."""
    exe = get_ollama_executable()
    if not exe:
        if progress_cb:
            progress_cb({"type": "error", "message": "Ollama CLI not found after install"})
        return False
    try:
        if progress_cb:
            progress_cb({"type": "pull", "message": f"Pulling {model}..."})
        proc = subprocess.Popen(
            [exe, "pull", model],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        for line in proc.stdout:
            line = line.strip()
            if line and progress_cb:
                progress_cb({"type": "pull", "message": line})
        proc.wait(timeout=1800)
        return proc.returncode == 0
    except Exception as e:
        if progress_cb:
            progress_cb({"type": "error", "message": f"Model pull error: {e}"})
        return False


def run_bootstrap(progress_cb: Optional[Callable] = None) -> dict:
    """Run the full bootstrap flow and return final status."""
    state = load_bootstrap_state()
    result = {"ollama_installed": False, "model_pulled": False, "model": DEFAULT_MODEL, "error": None}

    if progress_cb:
        progress_cb({"type": "status", "message": "Checking Ollama..."})

    if not get_ollama_executable():
        installer = find_bundled_installer()
        if not installer:
            if progress_cb:
                progress_cb({"type": "status", "message": "Downloading Ollama installer..."})
            installer = os.path.join(tempfile.gettempdir(), "carrot_ollama_setup.exe")
            if not download_installer(installer, progress_cb):
                result["error"] = "Could not download Ollama installer"
                return result
        if not install_ollama(installer, progress_cb):
            result["error"] = "Ollama installation failed"
            return result

    state["ollama_installed"] = True
    result["ollama_installed"] = True
    save_bootstrap_state(state)

    if not is_ollama_running():
        if progress_cb:
            progress_cb({"type": "status", "message": "Starting Ollama service..."})
        if not ensure_ollama_running(timeout=60):
            result["error"] = "Ollama service did not start"
            return result

    if not is_model_available(DEFAULT_MODEL):
        state["model_pulling"] = True
        save_bootstrap_state(state)
        if pull_model(DEFAULT_MODEL, progress_cb):
            state["model_pulled"] = True
        else:
            result["error"] = f"Failed to pull {DEFAULT_MODEL}"
    else:
        state["model_pulled"] = True

    state["model_pulling"] = False
    save_bootstrap_state(state)
    result["model_pulled"] = state["model_pulled"]

    # Persist default model in config
    set_config("ollama_model", DEFAULT_MODEL)
    set_config("ollama_model_recap", DEFAULT_MODEL)
    set_config("ollama_model_search", DEFAULT_MODEL)

    return result


def bootstrap_status() -> dict:
    """Return the current bootstrap/Ollama state for the UI splash screen."""
    state = load_bootstrap_state()
    ollama_installed = get_ollama_executable() is not None
    model_pulled = is_model_available(DEFAULT_MODEL)
    return {
        "ollama_installed": ollama_installed,
        "ollama_running": is_ollama_running(),
        "model_pulled": model_pulled,
        "model_pulling": state.get("model_pulling", False),
        "default_model": DEFAULT_MODEL,
        # Complete when Ollama and the model are actually present, regardless of
        # whether Carrot's bootstrap ran (e.g. Ollama installed externally).
        "bootstrap_complete": ollama_installed and model_pulled,
    }
