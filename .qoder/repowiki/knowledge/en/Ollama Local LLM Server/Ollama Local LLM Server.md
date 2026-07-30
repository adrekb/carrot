---
kind: external_dependency
name: Ollama Local LLM Server
slug: ollama
category: external_dependency
category_hints:
    - vendor_identity
    - auth_protocol
scope:
    - '**'
source_files:
    - carrot/ollama_client.py
    - README.md
---

### Identity & Role
Ollama is the local AI model server that Carrot uses for all generative AI features (chat, classification, embeddings). It runs on the user's machine and requires at least one pulled model.

### Integration Points
- `carrot/ollama_client.py` communicates with Ollama via HTTP to endpoints `/api/generate`, `/api/chat`, `/api/embeddings`, and `/api/tags`.
- Default host is `http://localhost:11434`; default model is `qwen2.5:7b-instruct`; classifier model is `qwen2.5:1.5b`; embedding model is `nomic-embed-text`.
- The README specifies pulling models via `ollama pull <model-tag>` before use.

### Usage Model
- All AI calls are made through the `OllamaClient` class which handles connection checking, streaming responses, and structured chat with response formats.
- Models are specified by Ollama tags (e.g., `gemma4:e4b`, `qwen2.5:7b-instruct`) — the exact tag matters as confirmed in conversation.
- Embeddings use the separate `/api/embeddings` endpoint with the `nomic-embed-text` model.

### Requirements
- Ollama must be running locally before Carrot starts.
- At least one model must be pulled via `ollama pull` command.
- No API keys or internet connection needed for AI features.