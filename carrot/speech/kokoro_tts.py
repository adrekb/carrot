import json
import os
import base64
import io
import subprocess
import tempfile
import wave
from typing import Optional


KOKORO_MODEL_PATH = os.path.expanduser("~/.cache/carrot/kokoro-v1.0.onnx")
KOKORO_VOICES_PATH = os.path.expanduser("~/.cache/carrot/voices.json")

VOICE_STYLES = {
    "us_rabbit": {"voice": "af_heart", "speed": 1.0},
    "us_calm": {"voice": "af_blswy", "speed": 0.9},
    "us_energetic": {"voice": "bf_qlwn", "speed": 1.1},
    "default": {"voice": "af_heart", "speed": 1.0},
}


def ensure_kokoro_installed() -> bool:
    try:
        import kokoro_onnx
        return True
    except ImportError:
        print("[Carrot Speech] kokoro-onnx not installed. Running: pip install kokoro-onnx sounddevice numpy")
        try:
            subprocess.run(
                ["pip", "install", "kokoro-onnx", "sounddevice", "numpy"],
                capture_output=True,
                timeout=120,
            )
            import kokoro_onnx
            return True
        except Exception:
            return False


def download_kokoro_model() -> bool:
    os.makedirs(os.path.dirname(KOKORO_MODEL_PATH), exist_ok=True)

    if os.path.exists(KOKORO_MODEL_PATH) and os.path.exists(KOKORO_VOICES_PATH):
        return True

    if not ensure_kokoro_installed():
        return False

    try:
        from kokoro_onnx import __version__ as kokoro_version
        print(f"[Carrot Speech] Kokoro TTS installed (v{kokoro_version})")

        if not os.path.exists(KOKORO_MODEL_PATH):
            print("[Carrot Speech] Downloading Kokoro voices model...")
            import huggingface_hub
            huggingface_hub.hf_hub_download(
                repo_id="hexgrad/Kokoro-82M",
                filename="kokoro-v1.0.onnx",
                local_dir=os.path.dirname(KOKORO_MODEL_PATH),
            )

        if not os.path.exists(KOKORO_VOICES_PATH):
            print("[Carrot Speech] Downloading Kokoro voices.json...")
            from pkg_resources import resource_filename
            voices_src = os.path.join(
                os.path.dirname(kokoro_onnx.__file__), "voices.json"
            )
            if os.path.exists(voices_src):
                import shutil
                shutil.copy(voices_src, KOKORO_VOICES_PATH)

        return os.path.exists(KOKORO_MODEL_PATH) and os.path.exists(KOKORO_VOICES_PATH)
    except Exception as e:
        print(f"[Carrot Speech] Failed to download Kokoro model: {e}")
        return False


def speak_text(
    text: str,
    voice_style: str = "us_rabbit",
    speed: float = 1.0,
    volume: float = 1.0,
) -> dict:
    if not ensure_kokoro_installed():
        return {"success": False, "error": "kokoro-onnx not available", "audio_played": False}

    if not download_kokoro_model():
        return {"success": False, "error": "Failed to download Kokoro model", "audio_played": False}

    try:
        from kokoro_onnx import KokoroOnnx
    except ImportError:
        return {"success": False, "error": "kokoro_onnx import failed", "audio_played": False}

    try:
        voice_config = VOICE_STYLES.get(voice_style, VOICE_STYLES["default"])
        voice_name = voice_config.get("voice", "af_heart")

        engine = KokoroOnnx(KOKORO_MODEL_PATH, KOKORO_VOICES_PATH)

        print(f"[Carrot Speech] Speaking with voice '{voice_name}' at speed {speed}")

        samples, sample_rate = engine.create(
            text,
            voice=voice_name,
            speed=speed,
        )

        import sounddevice as sd
        import numpy as np

        if volume != 1.0:
            samples = np.asarray(samples) * volume
            samples = np.clip(samples, -1.0, 1.0)

        sd.play(samples, sample_rate)
        sd.wait()

        return {
            "success": True,
            "text": text,
            "voice": voice_name,
            "speed": speed,
            "audio_played": True,
        }
    except Exception as e:
        return {"success": False, "error": str(e), "audio_played": False}


def list_voices() -> list:
    """Return the available voice style names."""
    return list(VOICE_STYLES.keys())


def synthesize_base64(text: str, voice_style: str = "us_rabbit", speed: float = 1.0) -> dict:
    """Synthesize speech and return it as base64-encoded WAV for browser playback."""
    if not ensure_kokoro_installed():
        return {"success": False, "error": "kokoro-onnx not available", "audio_base64": ""}

    if not download_kokoro_model():
        return {"success": False, "error": "Failed to download Kokoro model", "audio_base64": ""}

    try:
        from kokoro_onnx import KokoroOnnx
    except ImportError:
        return {"success": False, "error": "kokoro_onnx import failed", "audio_base64": ""}

    try:
        voice_config = VOICE_STYLES.get(voice_style, VOICE_STYLES["default"])
        voice_name = voice_config.get("voice", "af_heart")
        engine = KokoroOnnx(KOKORO_MODEL_PATH, KOKORO_VOICES_PATH)
        samples, sample_rate = engine.create(text, voice=voice_name, speed=speed)

        import numpy as np
        samples = np.asarray(samples, dtype=np.float32)
        samples = np.clip(samples, -1.0, 1.0)
        pcm = (samples * 32767).astype(np.int16)

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm.tobytes())

        return {
            "success": True,
            "text": text,
            "voice": voice_name,
            "audio_base64": base64.b64encode(buf.getvalue()).decode("ascii"),
        }
    except Exception as e:
        return {"success": False, "error": str(e), "audio_base64": ""}


def speak_and_record_response(
    user_text: str,
    ollama_response: str,
    voice_style: str = "us_rabbit",
    speed: float = 1.0,
) -> dict:
    print(f"[Carrot Speech] User said: {user_text[:50]}...")
    print(f"[Carrot Speech] Carrot responding: {ollama_response[:50]}...")

    result = speak_text(ollama_response, voice_style=voice_style, speed=speed)
    result["user_text"] = user_text
    return result