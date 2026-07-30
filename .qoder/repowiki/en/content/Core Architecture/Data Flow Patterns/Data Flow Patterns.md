# Data Flow Patterns

<cite>
**Referenced Files in This Document**
- [app.py](file://carrot/app.py)
- [main.py](file://carrot/main.py)
- [config.py](file://carrot/config.py)
- [conversation.py](file://carrot/conversation.py)
- [speech/kokoro_tts.py](file://carrot/speech/kokoro_tts.py)
- [speech/whisper_stt.py](file://carrot/speech/whisper_stt.py)
- [ollama_client.py](file://carrot/ollama_client.py)
- [database.py](file://carrot/database.py)
- [search.py](file://carrot/search.py)
- [notes.py](file://carrot/notes.py)
- [goals.py](file://carrot/goals.py)
- [reminders.py](file://carrot/reminders.py)
- [recap.py](file://carrot/recap.py)
- [computer_use.py](file://carrot/computer_use.py)
- [terminal.py](file://carrot/terminal.py)
- [web/index.html](file://carrot/web/index.html)
- [web/js/app.js](file://carrot/web/js/app.js)
- [web/js/search.js](file://carrot/web/js/search.js)
</cite>

## Table of Contents
1. [Introduction](#introduction)
2. [Project Structure](#project-structure)
3. [Core Components](#core-components)
4. [Architecture Overview](#architecture-overview)
5. [Detailed Component Analysis](#detailed-component-analysis)
6. [Dependency Analysis](#dependency-analysis)
7. [Performance Considerations](#performance-considerations)
8. [Troubleshooting Guide](#troubleshooting-guide)
9. [Conclusion](#conclusion)
10. [Appendices](#appendices)

## Introduction
This document explains the data flow patterns in the Carrot application, focusing on how user input is transformed across speech recognition, AI processing, and response generation. It covers request/response cycles, event-driven communication, asynchronous operations, data transformation pipelines, caching strategies, and state management across layers. It also includes diagrams for common flows such as voice commands, text queries, and file operations, along with error propagation, logging patterns, and performance considerations.

## Project Structure
Carrot is a Python application with a web interface and modular features:
- Core runtime and configuration: app.py, main.py, config.py
- Conversation orchestration: conversation.py
- Speech pipeline: whisper_stt.py (STT), kokoro_tts.py (TTS)
- AI integration: ollama_client.py
- Persistence and search: database.py, search.py
- Feature modules: notes.py, goals.py, reminders.py, recap.py, computer_use.py, terminal.py
- Web UI: index.html, app.js, search.js

```mermaid
graph TB
subgraph "Web UI"
HTML["index.html"]
JSApp["js/app.js"]
JSSearch["js/search.js"]
end
subgraph "Python App"
Main["main.py"]
App["app.py"]
Config["config.py"]
Conv["conversation.py"]
STT["speech/whisper_stt.py"]
TTS["speech/kokoro_tts.py"]
Ollama["ollama_client.py"]
DB["database.py"]
Search["search.py"]
Notes["notes.py"]
Goals["goals.py"]
Reminders["reminders.py"]
Recap["recap.py"]
Computer["computer_use.py"]
Terminal["terminal.py"]
end
HTML --> JSApp
JSApp --> App
JSApp --> JSSearch
App --> Main
App --> Config
App --> Conv
Conv --> STT
Conv --> TTS
Conv --> Ollama
Conv --> DB
Conv --> Search
Conv --> Notes
Conv --> Goals
Conv --> Reminders
Conv --> Recap
Conv --> Computer
Conv --> Terminal
```

**Diagram sources**
- [app.py](file://carrot/app.py)
- [main.py](file://carrot/main.py)
- [config.py](file://carrot/config.py)
- [conversation.py](file://carrot/conversation.py)
- [speech/whisper_stt.py](file://carrot/speech/whisper_stt.py)
- [speech/kokoro_tts.py](file://carrot/speech/kokoro_tts.py)
- [ollama_client.py](file://carrot/ollama_client.py)
- [database.py](file://carrot/database.py)
- [search.py](file://carrot/search.py)
- [notes.py](file://carrot/notes.py)
- [goals.py](file://carrot/goals.py)
- [reminders.py](file://carrot/reminders.py)
- [recap.py](file://carrot/recap.py)
- [computer_use.py](file://carrot/computer_use.py)
- [terminal.py](file://carrot/terminal.py)
- [web/index.html](file://carrot/web/index.html)
- [web/js/app.js](file://carrot/web/js/app.js)
- [web/js/search.js](file://carrot/web/js/search.js)

**Section sources**
- [app.py](file://carrot/app.py)
- [main.py](file://carrot/main.py)
- [config.py](file://carrot/config.py)
- [conversation.py](file://carrot/conversation.py)
- [speech/whisper_stt.py](file://carrot/speech/whisper_stt.py)
- [speech/kokoro_tts.py](file://carrot/speech/kokoro_tts.py)
- [ollama_client.py](file://carrot/ollama_client.py)
- [database.py](file://carrot/database.py)
- [search.py](file://carrot/search.py)
- [notes.py](file://carrot/notes.py)
- [goals.py](file://carrot/goals.py)
- [reminders.py](file://carrot/reminders.py)
- [recap.py](file://carrot/recap.py)
- [computer_use.py](file://carrot/computer_use.py)
- [terminal.py](file://carrot/terminal.py)
- [web/index.html](file://carrot/web/index.html)
- [web/js/app.js](file://carrot/web/js/app.js)
- [web/js/search.js](file://carrot/web/js/search.js)

## Core Components
- Application entry and lifecycle: main.py initializes the process; app.py wires routes, middleware, and background tasks.
- Configuration: config.py centralizes settings used by all components.
- Conversation orchestrator: conversation.py coordinates STT, LLM calls, tool execution, persistence, and TTS output.
- Speech layer: whisper_stt.py transcribes audio to text; kokoro_tts.py synthesizes text to audio.
- AI client: ollama_client.py handles model requests and streaming responses.
- Persistence and retrieval: database.py manages storage; search.py provides indexing and querying.
- Feature modules: notes.py, goals.py, reminders.py, recap.py, computer_use.py, terminal.py implement domain-specific logic invoked by the conversation orchestrator.
- Web frontend: index.html, app.js, and search.js capture user input and render responses.

Key responsibilities and interactions are described in subsequent sections with concrete flow diagrams.

**Section sources**
- [main.py](file://carrot/main.py)
- [app.py](file://carrot/app.py)
- [config.py](file://carrot/config.py)
- [conversation.py](file://carrot/conversation.py)
- [speech/whisper_stt.py](file://carrot/speech/whisper_stt.py)
- [speech/kokoro_tts.py](file://carrot/speech/kokoro_tts.py)
- [ollama_client.py](file://carrot/ollama_client.py)
- [database.py](file://carrot/database.py)
- [search.py](file://carrot/search.py)
- [notes.py](file://carrot/notes.py)
- [goals.py](file://carrot/goals.py)
- [reminders.py](file://carrot/reminders.py)
- [recap.py](file://carrot/recap.py)
- [computer_use.py](file://carrot/computer_use.py)
- [terminal.py](file://carrot/terminal.py)
- [web/index.html](file://carrot/web/index.html)
- [web/js/app.js](file://carrot/web/js/app.js)
- [web/js/search.js](file://carrot/web/js/search.js)

## Architecture Overview
The system follows a layered architecture:
- Presentation: Web UI captures inputs and displays outputs.
- API/Runtime: Python app exposes endpoints and manages concurrency.
- Orchestration: Conversation module sequences steps based on intent.
- Services: STT/TTS, AI client, persistence, search, and feature modules.
- Storage: Database and search indexes.

```mermaid
sequenceDiagram
participant U as "User"
participant WUI as "Web UI<br/>index.html + app.js"
participant API as "App Layer<br/>app.py"
participant CO as "Conversation Orchestrator<br/>conversation.py"
participant STT as "Speech-to-Text<br/>whisper_stt.py"
participant LLM as "AI Client<br/>ollama_client.py"
participant FEAT as "Feature Modules<br/>notes/goals/reminders/recap/computer/terminal"
participant DB as "Database<br/>database.py"
participant SRCH as "Search<br/>search.py"
participant TTS as "Text-to-Speech<br/>kokoro_tts.py"
U->>WUI : "Speak or type"
WUI->>API : "HTTP/WebSocket request"
API->>CO : "Dispatch input"
alt Voice path
CO->>STT : "Transcribe audio"
STT-->>CO : "Text transcript"
else Text path
CO-->>CO : "Use provided text"
end
CO->>LLM : "Generate response / plan actions"
LLM-->>CO : "Structured plan or text"
CO->>FEAT : "Execute tools/actions"
FEAT->>DB : "Read/Write records"
FEAT->>SRCH : "Index/query content"
CO->>TTS : "Synthesize reply (optional)"
TTS-->>CO : "Audio bytes"
CO-->>API : "Result payload"
API-->>WUI : "Streamed or final response"
WUI-->>U : "Render text/audio"
```

**Diagram sources**
- [web/index.html](file://carrot/web/index.html)
- [web/js/app.js](file://carrot/web/js/app.js)
- [app.py](file://carrot/app.py)
- [conversation.py](file://carrot/conversation.py)
- [speech/whisper_stt.py](file://carrot/speech/whisper_stt.py)
- [ollama_client.py](file://carrot/ollama_client.py)
- [notes.py](file://carrot/notes.py)
- [goals.py](file://carrot/goals.py)
- [reminders.py](file://carrot/reminders.py)
- [recap.py](file://carrot/recap.py)
- [computer_use.py](file://carrot/computer_use.py)
- [terminal.py](file://carrot/terminal.py)
- [database.py](file://carrot/database.py)
- [search.py](file://carrot/search.py)
- [speech/kokoro_tts.py](file://carrot/speech/kokoro_tts.py)

## Detailed Component Analysis

### Voice Command Flow
End-to-end flow from microphone capture to synthesized response.

```mermaid
flowchart TD
Start(["Start"]) --> Capture["Capture audio via Web UI"]
Capture --> Upload["Send audio chunk(s) to API"]
Upload --> STT["Transcribe with Whisper STT"]
STT --> Transcript{"Transcript valid?"}
Transcript --> |No| Fallback["Fallback to retry or ask user"]
Transcript --> |Yes| Plan["Conversation Orchestrator plans actions"]
Plan --> Tools["Invoke feature modules if needed"]
Tools --> Persist["Persist changes to DB/Search"]
Persist --> ReplyGen["Generate textual reply"]
ReplyGen --> TTS["Synthesize audio via Kokoro TTS"]
TTS --> Stream["Stream audio/text back to UI"]
Stream --> End(["End"])
Fallback --> End
```

**Diagram sources**
- [web/js/app.js](file://carrot/web/js/app.js)
- [app.py](file://carrot/app.py)
- [speech/whisper_stt.py](file://carrot/speech/whisper_stt.py)
- [conversation.py](file://carrot/conversation.py)
- [notes.py](file://carrot/notes.py)
- [goals.py](file://carrot/goals.py)
- [reminders.py](file://carrot/reminders.py)
- [recap.py](file://carrot/recap.py)
- [computer_use.py](file://carrot/computer_use.py)
- [terminal.py](file://carrot/terminal.py)
- [database.py](file://carrot/database.py)
- [search.py](file://carrot/search.py)
- [speech/kokoro_tts.py](file://carrot/speech/kokoro_tts.py)

**Section sources**
- [web/js/app.js](file://carrot/web/js/app.js)
- [app.py](file://carrot/app.py)
- [speech/whisper_stt.py](file://carrot/speech/whisper_stt.py)
- [conversation.py](file://carrot/conversation.py)
- [speech/kokoro_tts.py](file://carrot/speech/kokoro_tts.py)

### Text Query Flow
Straightforward path from typed input to AI-generated answer and optional persistence.

```mermaid
sequenceDiagram
participant U as "User"
participant WUI as "Web UI"
participant API as "App Layer"
participant CO as "Conversation Orchestrator"
participant LLM as "AI Client"
participant DB as "Database"
participant SRCH as "Search"
U->>WUI : "Type query"
WUI->>API : "POST /query"
API->>CO : "Process text"
CO->>LLM : "Prompt with context"
LLM-->>CO : "Answer or structured plan"
CO->>DB : "Optional read/write"
CO->>SRCH : "Index/update if needed"
CO-->>API : "Response payload"
API-->>WUI : "JSON/stream"
WUI-->>U : "Display result"
```

**Diagram sources**
- [web/js/app.js](file://carrot/web/js/app.js)
- [app.py](file://carrot/app.py)
- [conversation.py](file://carrot/conversation.py)
- [ollama_client.py](file://carrot/ollama_client.py)
- [database.py](file://carrot/database.py)
- [search.py](file://carrot/search.py)

**Section sources**
- [web/js/app.js](file://carrot/web/js/app.js)
- [app.py](file://carrot/app.py)
- [conversation.py](file://carrot/conversation.py)
- [ollama_client.py](file://carrot/ollama_client.py)
- [database.py](file://carrot/database.py)
- [search.py](file://carrot/search.py)

### File Operations Flow
Handling uploads, parsing, indexing, and searchability.

```mermaid
flowchart TD
A["Upload file via UI"] --> B["Receive multipart in API"]
B --> C["Validate and sanitize"]
C --> D["Extract text/metadata"]
D --> E["Persist to DB"]
E --> F["Index into Search"]
F --> G["Return success metadata"]
C --> |Invalid| H["Return validation error"]
E --> |Fail| I["Rollback and log error"]
F --> |Fail| J["Retry or mark partial"]
```

**Diagram sources**
- [web/js/app.js](file://carrot/web/js/app.js)
- [app.py](file://carrot/app.py)
- [database.py](file://carrot/database.py)
- [search.py](file://carrot/search.py)

**Section sources**
- [web/js/app.js](file://carrot/web/js/app.js)
- [app.py](file://carrot/app.py)
- [database.py](file://carrot/database.py)
- [search.py](file://carrot/search.py)

### Event-Driven Communication
Background tasks and real-time updates can be modeled as events:
- Input received -> STT queued -> LLM scheduled -> Tool execution -> TTS synthesis -> UI update.
- Use queues or async channels to decouple stages and enable retries and progress reporting.

```mermaid
stateDiagram-v2
[*] --> Idle
Idle --> Queued : "Input received"
Queued --> Processing : "STT/LLM/Tools started"
Processing --> Streaming : "Partial results available"
Streaming --> Completed : "Finalize"
Processing --> Error : "Exception"
Error --> Retry : "Retry policy"
Retry --> Processing
Completed --> Idle : "Reset"
```

[No sources needed since this diagram shows conceptual workflow, not actual code structure]

### Data Transformation Pipelines
- STT pipeline: raw audio -> normalized frames -> transcription -> cleaned text.
- LLM pipeline: prompt assembly -> model call -> structured extraction -> action planning.
- Tool pipeline: parsed intent -> parameter binding -> side effects -> result normalization.
- TTS pipeline: text normalization -> phoneme mapping -> audio synthesis.

```mermaid
flowchart LR
Raw["Raw Input"] --> Clean["Clean & Normalize"]
Clean --> Extract["Extract Entities/Intent"]
Extract --> Plan["Plan Actions"]
Plan --> Execute["Execute Tools"]
Execute --> Synthesize["Synthesize Output"]
Synthesize --> Deliver["Deliver to UI"]
```

[No sources needed since this diagram shows conceptual workflow, not actual code structure]

### Caching Strategies
- Prompt caching: cache frequent prompts and embeddings to reduce latency.
- Result caching: memoize expensive tool outputs keyed by inputs.
- Index caching: keep search index warm with incremental updates.
- Session state: maintain short-lived conversation context to avoid reprocessing.

[No sources needed since this section provides general guidance]

### State Management Across Layers
- UI state: input buffers, playback controls, message history.
- API state: request context, rate limits, timeouts.
- Orchestrator state: conversation turns, pending tasks, tool results.
- Service state: model clients, connection pools, caches.
- Persistence state: transactions, indexes, consistency markers.

[No sources needed since this section provides general guidance]

## Dependency Analysis
High-level dependencies between modules:

```mermaid
graph TB
App["app.py"] --> Conv["conversation.py"]
Conv --> STT["speech/whisper_stt.py"]
Conv --> TTS["speech/kokoro_tts.py"]
Conv --> Ollama["ollama_client.py"]
Conv --> DB["database.py"]
Conv --> Search["search.py"]
Conv --> Notes["notes.py"]
Conv --> Goals["goals.py"]
Conv --> Reminders["reminders.py"]
Conv --> Recap["recap.py"]
Conv --> Computer["computer_use.py"]
Conv --> Terminal["terminal.py"]
App --> Config["config.py"]
App --> Main["main.py"]
UI["web/js/app.js"] --> App
```

**Diagram sources**
- [app.py](file://carrot/app.py)
- [conversation.py](file://carrot/conversation.py)
- [speech/whisper_stt.py](file://carrot/speech/whisper_stt.py)
- [speech/kokoro_tts.py](file://carrot/speech/kokoro_tts.py)
- [ollama_client.py](file://carrot/ollama_client.py)
- [database.py](file://carrot/database.py)
- [search.py](file://carrot/search.py)
- [notes.py](file://carrot/notes.py)
- [goals.py](file://carrot/goals.py)
- [reminders.py](file://carrot/reminders.py)
- [recap.py](file://carrot/recap.py)
- [computer_use.py](file://carrot/computer_use.py)
- [terminal.py](file://carrot/terminal.py)
- [config.py](file://carrot/config.py)
- [main.py](file://carrot/main.py)
- [web/js/app.js](file://carrot/web/js/app.js)

**Section sources**
- [app.py](file://carrot/app.py)
- [conversation.py](file://carrot/conversation.py)
- [speech/whisper_stt.py](file://carrot/speech/whisper_stt.py)
- [speech/kokoro_tts.py](file://carrot/speech/kokoro_tts.py)
- [ollama_client.py](file://carrot/ollama_client.py)
- [database.py](file://carrot/database.py)
- [search.py](file://carrot/search.py)
- [notes.py](file://carrot/notes.py)
- [goals.py](file://carrot/goals.py)
- [reminders.py](file://carrot/reminders.py)
- [recap.py](file://carrot/recap.py)
- [computer_use.py](file://carrot/computer_use.py)
- [terminal.py](file://carrot/terminal.py)
- [config.py](file://carrot/config.py)
- [main.py](file://carrot/main.py)
- [web/js/app.js](file://carrot/web/js/app.js)

## Performance Considerations
- Asynchronous processing: run STT, LLM calls, and tool executions concurrently where safe.
- Streaming responses: send partial results to UI to improve perceived latency.
- Batch operations: group writes to DB and search index updates.
- Resource limits: enforce timeouts and memory caps for large audio/files.
- Caching: reuse embeddings, prompts, and tool outputs when inputs match.
- Backpressure: queue heavy workloads and signal progress to the UI.

[No sources needed since this section provides general guidance]

## Troubleshooting Guide
Common issues and diagnostics:
- STT failures: check audio quality, sample rates, and network connectivity; retry with fallback models.
- LLM errors: handle timeouts, rate limits, and malformed responses; implement retries and circuit breakers.
- Tool exceptions: validate parameters, isolate side effects, and rollback on failure.
- DB/Search inconsistencies: verify transactions and index rebuilds; use checksums for integrity.
- Logging: ensure structured logs at each stage with correlation IDs for tracing.

**Section sources**
- [speech/whisper_stt.py](file://carrot/speech/whisper_stt.py)
- [ollama_client.py](file://carrot/ollama_client.py)
- [database.py](file://carrot/database.py)
- [search.py](file://carrot/search.py)

## Conclusion
Carrot’s data flows integrate speech, AI, and domain tools through a clear orchestration layer. By applying asynchronous processing, streaming, caching, and robust error handling, the system delivers responsive and reliable experiences across voice, text, and file workflows.

[No sources needed since this section summarizes without analyzing specific files]

## Appendices
- Glossary: STT (speech-to-text), TTS (text-to-speech), LLM (large language model).
- References: See referenced files for implementation details.

[No sources needed since this section provides general guidance]