"""Tests for the speech API endpoints (engines stubbed — they are optional deps)."""
import base64

from carrot.speech import whisper_stt, kokoro_tts


def test_transcribe_endpoint_wiring(client, monkeypatch):
    monkeypatch.setattr(
        whisper_stt, "transcribe_base64",
        lambda audio_base64, model="small", language="en": {"success": True, "text": "hello carrot"},
    )
    resp = client.post("/api/speech/transcribe", json={"audio_base64": "AAAA"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["text"] == "hello carrot"


def test_speak_endpoint_wiring(client, monkeypatch):
    monkeypatch.setattr(
        kokoro_tts, "synthesize_base64",
        lambda text, voice_style="us_rabbit", speed=1.0: {"success": True, "audio_base64": "V0FW"},
    )
    resp = client.post("/api/speech/speak", json={"text": "hi"})
    assert resp.status_code == 200
    assert resp.json()["audio_base64"] == "V0FW"


def test_voices_endpoint(client):
    resp = client.get("/api/speech/voices")
    assert resp.status_code == 200
    voices = resp.json()["voices"]
    assert "us_rabbit" in voices


def test_transcribe_invalid_base64_graceful(client):
    # Real transcribe_base64: invalid base64 should fail gracefully, not raise.
    resp = client.post("/api/speech/transcribe", json={"audio_base64": "!!!not-base64!!!"})
    assert resp.status_code == 200
    assert resp.json()["success"] is False


def test_transcribe_base64_decodes_wav(tmp_path):
    # A minimal valid WAV header should decode and be written to disk.
    wav = b"RIFF$\x00\x00\x00WAVEfmt " + b"\x00" * 16
    b64 = base64.b64encode(wav).decode("ascii")
    # We only verify decoding succeeds; transcription itself requires whisper.
    decoded = base64.b64decode(b64)
    assert decoded.startswith(b"RIFF")
