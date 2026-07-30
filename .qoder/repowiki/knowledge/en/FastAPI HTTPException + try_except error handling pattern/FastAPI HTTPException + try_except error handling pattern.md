---
kind: error_handling
name: FastAPI HTTPException + try/except error handling pattern
category: error_handling
scope:
    - '**'
source_files:
    - carrot/app.py
    - carrot/ollama_client.py
    - carrot/computer_use.py
    - carrot/database.py
    - carrot/main.py
---

The Carrot codebase uses a straightforward, ad-hoc error handling approach built on FastAPI's `HTTPException` and Python's native `try/except` blocks. There is no centralized error module, custom exception hierarchy, or middleware-based error transformation.

**System/approach used:**
- **FastAPI HTTPException**: All API endpoints that encounter client or server errors raise `fastapi.HTTPException` with an explicit `status_code` and `detail` message (e.g., 404 for missing resources, 503 for unavailable Ollama service).
- **try/except blocks**: Individual functions wrap risky operations (file I/O, network calls, JSON parsing) in local `try/except` blocks, catching specific exceptions like `OSError`, `PermissionError`, `ImportError`, `json.JSONDecodeError`, and broad `Exception` as a fallback.
- **No global exception handler**: There is no `@app.exception_handler` decorator or custom middleware to centralize error formatting or logging.
- **No custom error types**: The codebase does not define any domain-specific exception classes; it relies entirely on built-in Python exceptions and FastAPI's `HTTPException`.

**Key files and patterns:**
- `carrot/app.py`: Central route definitions where nearly every endpoint converts internal failures into `HTTPException(status_code=..., detail=...)`. Examples include 404 responses for missing conversations, notes, files, images, and profiles, and a 503 when the Ollama backend is unreachable.
- `carrot/ollama_client.py`: Network requests use `requests.raise_for_status()` to surface HTTP errors, while connection failures are caught explicitly (`requests.ConnectionError`) and converted to boolean availability checks. JSON parsing errors during streaming responses are silently skipped via `json.JSONDecodeError` catches.
- `carrot/computer_use.py`: File system and optional dependency operations use defensive try/except blocks. Missing optional dependencies (e.g., `pyautogui`) return structured result dicts with an `error` field rather than raising. IO errors (`OSError`, `PermissionError`) are caught per-file and skipped gracefully.
- `carrot/database.py`: Database access uses raw `sqlite3` without transaction wrappers or custom error propagation; database errors bubble up unhandled.
- `carrot/main.py`: CLI entry point prints user-friendly error messages to stdout/stderr but does not raise exceptions to callers.

**Architecture and conventions:**
- Error propagation is shallow: layer boundaries are thin, so most errors are handled at the call site rather than propagated upward through a layered exception chain.
- External service failures are treated as operational conditions checked before use (e.g., `OllamaClient.is_available()`) rather than raised exceptions.
- Optional features degrade gracefully by returning error payloads (e.g., `{"success": False, "error": "..."}`) instead of throwing.
- No structured logging of errors is present; failures are either returned as JSON responses or printed to console.

**Conventions and constraints observed:**
- API endpoints consistently use `raise HTTPException(status_code=..., detail="...")` for all error cases — this is the de facto standard across all routes.
- File and OS operations catch `(OSError, PermissionError)` specifically and skip the problematic file, never re-raising.
- Optional imports are wrapped in `except ImportError` and return a dict with `"error": "...", "success": False`.
- JSON parsing from external LLM responses catches `json.JSONDecodeError` and falls back to returning the raw response text.
- No `panic`/`recover` equivalent exists (Python convention); there are no `sys.exit()` calls for error paths either.