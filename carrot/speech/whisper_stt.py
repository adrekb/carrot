import subprocess
import json
import os
import base64
import tempfile
from typing import Optional, Callable


WHISPER_MODEL = "small"
WHISPER_BIN = os.path.expanduser("~/.local/bin/whisper.cpp/main")
WHISPER_MODELS_DIR = os.path.expanduser("~/.cache/whisper.cpp")


def ensure_whisper_installed() -> bool:
    if os.path.exists(WHISPER_BIN):
        return True
    os.makedirs(WHISPER_MODELS_DIR, exist_ok=True)
    print("[Carrot Speech] Whisper.cpp not found. Installing...")
    print("[Carrot Speech] Run: git clone https://github.com/ggerganov/whisper.cpp.git && cd whisper.cpp && make && make install")
    return False


def download_whisper_model(model_name: str = WHISPER_MODEL) -> str:
    model_path = os.path.join(WHISPER_MODELS_DIR, f"ggml-{model_name}.bin")
    if os.path.exists(model_path):
        return model_path

    if not ensure_whisper_installed():
        return ""

    print(f"[Carrot Speech] Downloading whisper model: {model_name}")
    try:
        subprocess.run(
            [WHISPER_BIN, "--download-model", model_name],
            capture_output=True,
            timeout=120,
        )
    except Exception as e:
        print(f"[Carrot Speech] Failed to download model: {e}")

    return model_path if os.path.exists(model_path) else ""


def transcribe_audio(
    audio_path: str = None,
    model: str = WHISPER_MODEL,
    language: str = "en",
    on_progress: Optional[Callable] = None,
) -> dict:
    if not ensure_whisper_installed():
        return {"success": False, "error": "Whisper.cpp not installed", "text": ""}

    model_path = download_whisper_model(model)
    if not model_path:
        return {"success": False, "error": "Failed to download whisper model", "text": ""}

    cmd = [
        WHISPER_BIN,
        "-m", model_path,
        "-f", audio_path if audio_path else "pipe:0",
        "--language", language,
        "--output-format", "json",
        "--no-timestamps",
        "--print-special",
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return {"success": False, "error": result.stderr.strip(), "text": ""}

        output = result.stdout.strip()
        data = json.loads(output) if output else {}

        text = data.get("text", "").strip()
        if not text and "segments" in data:
            text = " ".join(seg.get("text", "") for seg in data["segments"])

        return {
            "success": True,
            "text": text,
            "language": language,
            "model": model,
            "segments": data.get("segments", []),
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Transcription timed out", "text": ""}
    except json.JSONDecodeError:
        return {"success": False, "error": "Failed to parse whisper output", "text": ""}
    except Exception as e:
        return {"success": False, "error": str(e), "text": ""}


def transcribe_base64(audio_base64: str, model: str = WHISPER_MODEL, language: str = "en") -> dict:
    """Decode base64 audio (WAV) to a temp file and transcribe it."""
    try:
        audio_bytes = base64.b64decode(audio_base64)
    except Exception as e:
        return {"success": False, "error": f"Invalid base64 audio: {e}", "text": ""}

    audio_path = os.path.join(tempfile.gettempdir(), "carrot_speech_upload.wav")
    with open(audio_path, "wb") as f:
        f.write(audio_bytes)

    return transcribe_audio(audio_path, model=model, language=language)


def record_and_transcribe(
    duration_seconds: int = 5,
    model: str = WHISPER_MODEL,
    language: str = "en",
) -> dict:
    try:
        import sounddevice as sd
        import numpy as np
        import wave
    except ImportError:
        return {"success": False, "error": "sounddevice/numpy not installed", "text": ""}

    sample_rate = 16000
    channels = 1
    dtype = "int16"

    print(f"[Carrot Speech] Recording for {duration_seconds} seconds...")

    audio_data = sd.rec(
        int(sample_rate * duration_seconds),
        samplerate=sample_rate,
        channels=channels,
        dtype=dtype,
    )
    sd.wait()

    tmp_dir = tempfile.gettempdir()
    audio_path = os.path.join(tmp_dir, "carrot_speech_input.wav")

    audio_flat = np.asarray(audio_data, dtype=np.int16).flatten()

    with wave.open(audio_path, "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(audio_flat.tobytes())

    print(f"[Carrot Speech] Recorded audio saved to: {audio_path}")
    return transcribe_audio(audio_path, model=model, language=language)