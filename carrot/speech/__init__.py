from carrot.speech.whisper_stt import (
    ensure_whisper_installed,
    download_whisper_model,
    transcribe_audio,
    record_and_transcribe,
)
from carrot.speech.kokoro_tts import (
    ensure_kokoro_installed,
    download_kokoro_model,
    speak_text,
    speak_and_record_response,
    VOICE_STYLES,
)

__all__ = [
    "ensure_whisper_installed",
    "download_whisper_model",
    "transcribe_audio",
    "record_and_transcribe",
    "ensure_kokoro_installed",
    "download_kokoro_model",
    "speak_text",
    "speak_and_record_response",
    "VOICE_STYLES",
]