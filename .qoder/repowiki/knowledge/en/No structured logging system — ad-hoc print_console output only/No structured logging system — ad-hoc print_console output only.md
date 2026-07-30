---
kind: logging_system
name: No structured logging system — ad-hoc print/console output only
category: logging_system
scope:
    - '**'
source_files:
    - carrot/main.py
    - carrot/app.py
    - gui/main.js
    - pyproject.toml
---

This repository does not implement a structured logging system. Across the Python FastAPI backend and the Electron desktop shell, there is no use of any logging framework (no `logging`, `loguru`, `structlog`, or similar). All diagnostic output is produced through ad-hoc `print()` statements in the CLI (`carrot/main.py`) and `console.log`/`console.error` in the Electron main process (`gui/main.js`).

Key observations:
- The Python CLI (`carrot/main.py`) uses bare `print()` calls for usage hints, status messages, command output, and error messages. There is no log level management, no file sink, and no structured fields.
- The FastAPI application (`carrot/app.py`) has no logger configured; it relies on Uvicorn's default stdout logging (configured via `uvicorn.run(app, host="127.0.0.1", port=8181)`) and raises `HTTPException` for error responses rather than emitting logs.
- The Electron main process (`gui/main.js`) uses `console.error` for startup failures and `console.log` elsewhere, with no centralized logging configuration.
- No logging configuration files, environment variables for log levels, or log rotation exist anywhere in the repo.
- Dependencies in `pyproject.toml` do not include any logging library beyond what FastAPI/Uvicorn provide by default.

As a result, log output is unstructured, goes only to stdout/stderr, and cannot be filtered by severity or routed to different sinks.