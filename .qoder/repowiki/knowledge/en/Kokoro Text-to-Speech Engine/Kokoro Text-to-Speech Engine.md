---
kind: external_dependency
name: Kokoro Text-to-Speech Engine
slug: kokoro-tts
category: external_dependency
category_hints:
    - vendor_identity
    - sdk_real_api
scope:
    - '**'
source_files:
    - carrot/speech/kokoro_tts.py
---

### Identity & Role
Kokoro is a compact text-to-speech engine using ONNX models for generating realistic, human-like speech output in Carrot.

### Integration Points
- `carrot/speech/kokoro_tts.py` uses the `kokoro-onnx` Python package and downloads models from HuggingFace Hub (`hexgrad/Kokoro-82M`).
- Model files stored at `~/.cache/carrot/kokoro-v1.0.onnx` and voices configuration at `~/.cache/carrot/voices.json`.
- Built-in voice styles include `us_rabbit`, `us_calm`, `us_energetic` with different voice profiles and speeds.

### Usage Model
- Auto-installs dependencies (`kokoro-onnx`, `sounddevice`, `numpy`) if missing.
- Downloads ONNX model and voice configurations from HuggingFace on first use.
- Uses `KokoroOnnx` class with voice names like `af_heart`, `bf_qlwn`, `af_blswy`.
- Audio playback handled via sounddevice library.

### Configuration
- Voice styles support adjustable speed and volume parameters.
- Model versioning through `kokoro-v1.0.onnx` filename.