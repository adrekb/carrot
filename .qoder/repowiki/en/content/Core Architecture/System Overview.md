# System Overview

<cite>
**Referenced Files in This Document**
- [README.md](file://README.md)
- [PLAN.md](file://PLAN.md)
- [pyproject.toml](file://pyproject.toml)
- [build.bat](file://build.bat)
- [carrot/main.py](file://carrot/main.py)
- [carrot/app.py](file://carrot/app.py)
- [carrot/config.py](file://carrot/config.py)
- [carrot/database.py](file://carrot/database.py)
- [carrot/conversation.py](file://carrot/conversation.py)
- [carrot/ollama_client.py](file://carrot/ollama_client.py)
- [carrot/speech/kokoro_tts.py](file://carrot/speech/kokoro_tts.py)
- [carrot/speech/whisper_stt.py](file://carrot/speech/whisper_stt.py)
- [carrot/computer_use.py](file://carrot/computer_use.py)
- [carrot/goals.py](file://carrot/goals.py)
- [carrot/leaderboard.py](file://carrot/leaderboard.py)
- [carrot/notes.py](file://carrot/notes.py)
- [carrot/recap.py](file://carrot/recap.py)
- [carrot/reminders.py](file://carrot/reminders.py)
- [carrot/search.py](file://carrot/search.py)
- [carrot/terminal.py](file://carrot/terminal.py)
- [gui/main.js](file://gui/main.js)
- [gui/preload.js](file://gui/preload.js)
- [gui/package.json](file://gui/package.json)
- [gui/vite.config.js](file://gui/vite.config.js)
- [gui/public/overlay.html](file://gui/public/overlay.html)
- [carrot/web/index.html](file://carrot/web/index.html)
- [carrot/web/js/app.js](file://carrot/web/js/app.js)
- [carrot/web/js/search.js](file://carrot/web/js/search.js)
- [carrot/web/css/style.css](file://carrot/web/css/style.css)
</cite>

## Table of Contents
1. Introduction
2. Project Structure
3. Core Components
4. Architecture Overview
5. Detailed Component Analysis
6. Dependency Analysis
7. Performance Considerations
8. Troubleshooting Guide
9. Conclusion

## Introduction
This document provides a comprehensive system overview of the Carrot application, explaining its high-level architecture and how it integrates speech processing, AI capabilities, computer automation, and productivity features into a cohesive desktop experience. The system is composed of:
- A modular Python backend that implements core services (conversation, memory, search, reminders, goals, notes, terminal, and more).
- A web interface layer that renders the UI and communicates with the backend via HTTP or IPC.
- An Electron desktop wrapper that packages the app for distribution and bridges native OS capabilities to the web UI.

The design emphasizes modularity, clear separation of concerns, and extensibility. It balances local-first data storage with optional cloud-based AI services, enabling both offline usability and advanced AI-powered features.

## Project Structure
At a high level, the repository organizes functionality by feature modules under carrot/, a web UI under carrot/web/, and an Electron wrapper under gui/. Configuration and build scripts are at the root.

Key areas:
- Python backend:
  - Entry points and runtime orchestration
  - Feature modules for conversation, AI client, speech, computer use, productivity tools
  - Data persistence and configuration
- Web interface:
  - HTML/CSS/JS for the user-facing UI
  - Client-side logic for search and interactions
- Electron wrapper:
  - Desktop shell, window management, and IPC bridge
  - Packaging and build configuration

```mermaid
graph TB
subgraph "Desktop Shell"
GUI_MAIN["gui/main.js"]
GUI_PRELOAD["gui/preload.js"]
GUI_OVERLAY["gui/public/overlay.html"]
GUI_PKG["gui/package.json"]
GUI_VITE["gui/vite.config.js"]
end
subgraph "Web Interface"
WEB_INDEX["carrot/web/index.html"]
WEB_APP_JS["carrot/web/js/app.js"]
WEB_SEARCH_JS["carrot/web/js/search.js"]
WEB_CSS["carrot/web/css/style.css"]
end
subgraph "Python Backend"
MAIN_PY["carrot/main.py"]
APP_PY["carrot/app.py"]
CONFIG_PY["carrot/config.py"]
DB_PY["carrot/database.py"]
CONVERSATION_PY["carrot/conversation.py"]
OLLAMA_PY["carrot/ollama_client.py"]
TTS_PY["carrot/speech/kokoro_tts.py"]
STT_PY["carrot/speech/whisper_stt.py"]
COMPUTER_USE_PY["carrot/computer_use.py"]
NOTES_PY["carrot/notes.py"]
GOALS_PY["carrot/goals.py"]
REMINDERS_PY["carrot/reminders.py"]
SEARCH_PY["carrot/search.py"]
TERMINAL_PY["carrot/terminal.py"]
RECAP_PY["carrot/recap.py"]
LEADERBOARD_PY["carrot/leaderboard.py"]
end
GUI_MAIN --> WEB_INDEX
GUI_PRELOAD --> MAIN_PY
WEB_APP_JS --> APP_PY
WEB_SEARCH_JS --> SEARCH_PY
CONVERSATION_PY --> OLLAMA_PY
CONVERSATION_PY --> DB_PY
TTS_PY --> DB_PY
STT_PY --> DB_PY
COMPUTER_USE_PY --> DB_PY
NOTES_PY --> DB_PY
GOALS_PY --> DB_PY
REMINDERS_PY --> DB_PY
SEARCH_PY --> DB_PY
TERMINAL_PY --> DB_PY
RECAP_PY --> DB_PY
LEADERBOARD_PY --> DB_PY
```

**Diagram sources**
- [gui/main.js](file://gui/main.js)
- [gui/preload.js](file://gui/preload.js)
- [gui/public/overlay.html](file://gui/public/overlay.html)
- [gui/package.json](file://gui/package.json)
- [gui/vite.config.js](file://gui/vite.config.js)
- [carrot/web/index.html](file://carrot/web/index.html)
- [carrot/web/js/app.js](file://carrot/web/js/app.js)
- [carrot/web/js/search.js](file://carrot/web/js/search.js)
- [carrot/web/css/style.css](file://carrot/web/css/style.css)
- [carrot/main.py](file://carrot/main.py)
- [carrot/app.py](file://carrot/app.py)
- [carrot/config.py](file://carrot/config.py)
- [carrot/database.py](file://carrot/database.py)
- [carrot/conversation.py](feature modules)
- [carrot/ollama_client.py](file://carrot/ollama_client.py)
- [carrot/speech/kokoro_tts.py](file://carrot/speech/kokoro_tts.py)
- [carrot/speech/whisper_stt.py](file://carrot/speech/whisper_stt.py)
- [carrot/computer_use.py](file://carrot/computer_use.py)
- [carrot/notes.py](file://carrot/notes.py)
- [carrot/goals.py](file://carrot/goals.py)
- [carrot/reminders.py](file://carrot/reminders.py)
- [carrot/search.py](file://carrot/search.py)
- [carrot/terminal.py](file://carrot/terminal.py)
- [carrot/recap.py](file://carrot/recap.py)
- [carrot/leaderboard.py](file://carrot/leaderboard.py)

**Section sources**
- [README.md](file://README.md)
- [PLAN.md](file://PLAN.md)
- [pyproject.toml](file://pyproject.toml)
- [build.bat](file://build.bat)

## Core Components
- Python backend entrypoints:
  - Application bootstrap and server initialization
  - Configuration management and environment setup
  - Database access abstraction and persistence layer
- Conversation and AI:
  - Conversation state management and history
  - AI client integration for model inference
- Speech pipeline:
  - Text-to-speech synthesis
  - Speech-to-text transcription
- Productivity and automation:
  - Notes, goals, reminders, recap generation
  - Search across content and metadata
  - Terminal-like operations and computer automation utilities
- Leaderboard and analytics:
  - Tracking and reporting of productivity metrics

These components follow a modular design where each feature module encapsulates its own responsibilities and interacts through well-defined interfaces, primarily via the database and shared configuration.

**Section sources**
- [carrot/main.py](file://carrot/main.py)
- [carrot/app.py](file://carrot/app.py)
- [carrot/config.py](file://carrot/config.py)
- [carrot/database.py](file://carrot/database.py)
- [carrot/conversation.py](file://carrot/conversation.py)
- [carrot/ollama_client.py](file://carrot/ollama_client.py)
- [carrot/speech/kokoro_tts.py](file://carrot/speech/kokoro_tts.py)
- [carrot/speech/whisper_stt.py](file://carrot/speech/whisper_stt.py)
- [carrot/computer_use.py](file://carrot/computer_use.py)
- [carrot/notes.py](file://carrot/notes.py)
- [carrot/goals.py](file://carrot/goals.py)
- [carrot/reminders.py](file://carrot/reminders.py)
- [carrot/search.py](file://carrot/search.py)
- [carrot/terminal.py](file://carrot/terminal.py)
- [carrot/recap.py](file://carrot/recap.py)
- [carrot/leaderboard.py](file://carrot/leaderboard.py)

## Architecture Overview
Carrot follows a layered architecture:
- Presentation layer: Electron shell and web UI render the interface and handle user interactions.
- Application layer: Python backend exposes services and endpoints consumed by the web UI and Electron preload script.
- Data layer: Local-first persistence ensures reliability and privacy; optional external AI services provide advanced capabilities.

```mermaid
sequenceDiagram
participant User as "User"
participant Electron as "Electron Shell<br/>gui/main.js"
participant Preload as "Preload Bridge<br/>gui/preload.js"
participant WebUI as "Web UI<br/>carrot/web/index.html"
participant Backend as "Python Backend<br/>carrot/app.py"
participant Services as "Feature Modules<br/>conversation, search, notes, etc."
participant DB as "Persistence<br/>carrot/database.py"
participant AI as "AI Client<br/>carrot/ollama_client.py"
User->>Electron : Launch App
Electron->>WebUI : Load index.html
WebUI->>Backend : HTTP/IPC Request
Backend->>Services : Route to appropriate service
Services->>DB : Read/Write data
Services->>AI : Optional AI calls
AI-->>Services : Model response
Services-->>Backend : Result
Backend-->>WebUI : Response
WebUI-->>User : Updated UI
```

**Diagram sources**
- [gui/main.js](file://gui/main.js)
- [gui/preload.js](file://gui/preload.js)
- [carrot/web/index.html](file://carrot/web/index.html)
- [carrot/app.py](file://carrot/app.py)
- [carrot/database.py](file://carrot/database.py)
- [carrot/ollama_client.py](file://carrot/ollama_client.py)

## Detailed Component Analysis

### Python Backend Orchestration
Responsibilities:
- Initialize the application lifecycle
- Configure settings and environment variables
- Start the web server and expose endpoints
- Coordinate between feature modules and the database

Design patterns:
- Modular service composition
- Centralized configuration
- Clear separation between I/O and business logic

**Section sources**
- [carrot/main.py](file://carrot/main.py)
- [carrot/app.py](file://carrot/app.py)
- [carrot/config.py](file://carrot/config.py)

### Conversation and AI Integration
Responsibilities:
- Manage conversation state and history
- Interact with AI models via the client
- Persist conversations and context

Data flow:
- Incoming prompts are processed by the conversation module
- Context is retrieved from the database
- AI client generates responses
- Results are stored and returned to the UI

```mermaid
classDiagram
class Conversation {
+startSession()
+addMessage(role, content)
+getHistory()
+generateResponse(prompt)
}
class OllamaClient {
+sendRequest(messages)
+parseResponse(response)
}
class Database {
+saveConversation(id, messages)
+loadConversation(id)
+query(criteria)
}
Conversation --> OllamaClient : "uses"
Conversation --> Database : "persists"
```

**Diagram sources**
- [carrot/conversation.py](file://carrot/conversation.py)
- [carrot/ollama_client.py](file://carrot/ollama_client.py)
- [carrot/database.py](file://carrot/database.py)

**Section sources**
- [carrot/conversation.py](file://carrot/conversation.py)
- [carrot/ollama_client.py](file://carrot/ollama_client.py)
- [carrot/database.py](file://carrot/database.py)

### Speech Processing Pipeline
Responsibilities:
- Convert text to speech using a TTS engine
- Transcribe audio input to text using STT
- Integrate with conversation and productivity modules

Processing logic:
- Audio capture triggers STT
- Transcribed text flows into conversation or commands
- Responses can be synthesized back to speech

```mermaid
flowchart TD
Start(["Audio Input"]) --> STT["Speech-to-Text<br/>whisper_stt.py"]
STT --> Text{"Transcription Valid?"}
Text --> |No| Error["Handle Error"]
Text --> |Yes| Process["Route to Service<br/>conversation/commands"]
Process --> TTS["Text-to-Speech<br/>kokoro_tts.py"]
TTS --> Output(["Audio Output"])
Error --> End(["Exit"])
Output --> End
```

**Diagram sources**
- [carrot/speech/whisper_stt.py](file://carrot/speech/whisper_stt.py)
- [carrot/speech/kokoro_tts.py](file://carrot/speech/kokoro_tts.py)
- [carrot/conversation.py](file://carrot/conversation.py)

**Section sources**
- [carrot/speech/whisper_stt.py](file://carrot/speech/whisper_stt.py)
- [carrot/speech/kokoro_tts.py](file://carrot/speech/kokoro_tts.py)

### Computer Automation and Terminal Utilities
Responsibilities:
- Execute system commands safely
- Automate repetitive tasks
- Provide terminal-like interaction within the app

Integration:
- Commands are validated and executed in a sandboxed manner
- Results are logged and can trigger further actions

**Section sources**
- [carrot/computer_use.py](file://carrot/computer_use.py)
- [carrot/terminal.py](file://carrot/terminal.py)

### Productivity Features
Notes:
- Create, update, delete, and search notes
- Link notes to conversations and goals

Goals and Reminders:
- Define goals with deadlines
- Set reminders and notifications
- Track progress and completion

Recap and Leaderboard:
- Generate daily/weekly recaps
- Display productivity metrics and achievements

Search:
- Full-text search across notes, conversations, and metadata
- Indexing and query optimization

**Section sources**
- [carrot/notes.py](file://carrot/notes.py)
- [carrot/goals.py](file://carrot/goals.py)
- [carrot/reminders.py](file://carrot/reminders.py)
- [carrot/recap.py](file://carrot/recap.py)
- [carrot/leaderboard.py](file://carrot/leaderboard.py)
- [carrot/search.py](file://carrot/search.py)

### Web Interface Layer
Responsibilities:
- Render the user interface
- Handle user interactions and display results
- Communicate with the backend via HTTP or IPC

Components:
- HTML structure and styling
- JavaScript modules for app logic and search
- Responsive design for desktop usage

**Section sources**
- [carrot/web/index.html](file://carrot/web/index.html)
- [carrot/web/js/app.js](file://carrot/web/js/app.js)
- [carrot/web/js/search.js](file://carrot/web/js/search.js)
- [carrot/web/css/style.css](file://carrot/web/css/style.css)

### Electron Desktop Wrapper
Responsibilities:
- Package the web UI and backend into a desktop application
- Provide native OS integrations and window management
- Bridge IPC between the web UI and Python backend

Configuration:
- Build scripts and packaging options
- Vite configuration for development and production

**Section sources**
- [gui/main.js](file://gui/main.js)
- [gui/preload.js](file://gui/preload.js)
- [gui/package.json](file://gui/package.json)
- [gui/vite.config.js](file://gui/vite.config.js)
- [gui/public/overlay.html](file://gui/public/overlay.html)

## Dependency Analysis
The system exhibits low coupling between feature modules, with the database acting as the central coordination point. External dependencies include AI model clients and optional speech engines.

```mermaid
graph LR
APP["app.py"] --> CONVERSATION["conversation.py"]
APP --> NOTES["notes.py"]
APP --> GOALS["goals.py"]
APP --> REMINDERS["reminders.py"]
APP --> SEARCH["search.py"]
APP --> TERMINAL["terminal.py"]
APP --> COMPUTER_USE["computer_use.py"]
APP --> RECAP["recap.py"]
APP --> LEADERBOARD["leaderboard.py"]
CONVERSATION --> OLLAMA["ollama_client.py"]
CONVERSATION --> DB["database.py"]
NOTES --> DB
GOALS --> DB
REMINDERS --> DB
SEARCH --> DB
TERMINAL --> DB
COMPUTER_USE --> DB
RECAP --> DB
LEADERBOARD --> DB
```

**Diagram sources**
- [carrot/app.py](file://carrot/app.py)
- [carrot/conversation.py](file://carrot/conversation.py)
- [carrot/notes.py](file://carrot/notes.py)
- [carrot/goals.py](file://carrot/goals.py)
- [carrot/reminders.py](file://carrot/reminders.py)
- [carrot/search.py](file://carrot/search.py)
- [carrot/terminal.py](file://carrot/terminal.py)
- [carrot/computer_use.py](file://carrot/computer_use.py)
- [carrot/recap.py](file://carrot/recap.py)
- [carrot/leaderboard.py](file://carrot/leaderboard.py)
- [carrot/ollama_client.py](file://carrot/ollama_client.py)
- [carrot/database.py](file://carrot/database.py)

**Section sources**
- [carrot/app.py](file://carrot/app.py)
- [carrot/database.py](file://carrot/database.py)

## Performance Considerations
- Local-first data storage reduces latency and improves reliability
- Asynchronous operations for AI and speech processing prevent UI blocking
- Efficient indexing for search queries enhances responsiveness
- Caching strategies for frequently accessed data reduce redundant computations
- Resource management for long-running processes like TTS and STT

[No sources needed since this section provides general guidance]

## Troubleshooting Guide
Common issues and resolutions:
- AI client connection failures: Verify model availability and network connectivity
- Speech processing errors: Check audio input permissions and engine configurations
- Database corruption: Use backup restoration and integrity checks
- UI rendering problems: Validate HTML/CSS/JS assets and browser compatibility
- Electron packaging issues: Review build scripts and dependency versions

**Section sources**
- [carrot/ollama_client.py](file://carrot/ollama_client.py)
- [carrot/speech/whisper_stt.py](file://carrot/speech/whisper_stt.py)
- [carrot/speech/kokoro_tts.py](file://carrot/speech/kokoro_tts.py)
- [carrot/database.py](file://carrot/database.py)
- [gui/main.js](file://gui/main.js)
- [build.bat](file://build.bat)

## Conclusion
Carrot’s architecture combines a modular Python backend, a responsive web interface, and an Electron desktop wrapper to deliver a powerful productivity suite. The design emphasizes modularity, clear boundaries, and extensibility while integrating speech processing, AI capabilities, and computer automation. This approach enables both offline functionality and advanced AI-powered features, providing users with a cohesive and efficient desktop experience.

[No sources needed since this section summarizes without analyzing specific files]