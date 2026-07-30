---
kind: external_dependency
name: Whisper.cpp Speech-to-Text Engine
slug: whisper-cpp
category: external_dependency
category_hints:
    - vendor_identity
    - framework_behavior
scope:
    - '**'
source_files:
    - carrot/speech/whisper_stt.py
---

### Identity & Role
whisper.cpp is a C++ implementation of OpenAI's Whisper used for offline speech-to-text transcription in Carrot's voice input pipeline.

### Integration Points
- `carrot/speech/whisper_stt.py` invokes the whisper.cpp binary located at `~/.local/bin/whisper.cpp/main`.
- Models are downloaded to `~/.cache/whisper.cpp/` directory.
- Uses subprocess calls with parameters for audio input, language detection, and JSON output format.

### Usage Model
- Binary must be built and installed separately (`git clone && make && make install`).
- Models are downloaded on-demand using `--download-model` flag.
- Audio can be provided via file path or stdin pipe for live recording.
- Output is parsed from JSON format containing text and optional segments.

### Dependencies
- Requires sounddevice, numpy for audio recording functionality.
- Optional dependency - transcription fails gracefully if whisper.cpp is not installed.