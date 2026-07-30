---
kind: configuration_system
name: SQLite-backed Runtime Configuration with Default Overrides
category: configuration_system
scope:
    - '**'
source_files:
    - carrot/config.py
    - carrot/app.py
    - carrot/main.py
    - carrot/database.py
---

The Carrot application uses a simple, file-based configuration system centered around a single Python module (`carrot/config.py`) that stores all runtime settings in the embedded SQLite database (`carrot/data/carrot.db`). There is no external config file format (YAML, JSON, .env) — configuration is loaded entirely from the database with an in-memory defaults dictionary providing fallback values.

**How it works:**
- `DEFAULTS` defines all supported keys and their default values: Ollama connection settings (`ollama_host`, `ollama_model`, `ollama_model_recap`, `ollama_model_search`), directory paths (`data_dir`, `conversations_dir`, `notes_dir`, `goals_dir`, `db_path`), server settings (`server_host`, `server_port`), UI toggles (`web_ui`), and recap/recording options (`recap_enabled`, `recap_hours`, `recap_rss_feeds`, `recap_max_items`).
- `get_config()` opens the SQLite DB, reads all rows from the `config` table, merges them over the defaults dict, and returns a flat key-value map. Values are JSON-parsed when stored, falling back to raw strings on decode errors.
- `set_config(key, value)` persists a single key-value pair via `INSERT OR REPLACE`, serializing values with `json.dumps`.
- The base `CARROT_DIR` resolves to `carrot/data/` relative to the module location.

**Access patterns:**
- Modules import `get_config` directly from `carrot.config` (e.g., `main.py`, `ollama_client.py`, `recap.py`) and call it at runtime each time they need settings — there is no global singleton or caching layer, so every read hits the database.
- The FastAPI web server exposes two REST endpoints for remote configuration access: `GET /api/config` returns the full merged config, and `PUT /api/config/{key}` updates a single key through `set_config`.
- The Electron GUI (`gui/main.js`) does not read config files directly; it communicates with the Python backend over HTTP on `127.0.0.1:8181`, so all configuration flows through the same SQLite-backed API.

**Environment variables:**
- No `.env` loading or `os.environ` usage is present for configuration purposes. The only environment variable reference found is in `terminal.py`, which passes `os.environ` into a subprocess call — this is not used for application configuration.

**Persistence and schema:**
- Configuration is stored in a `config` table within `carrot.db` with columns `key` (text) and `value` (text, JSON-encoded). There is no explicit migration or schema versioning for this table beyond the initial creation logic in `database.py`.

**Conventions observed:**
- All configuration keys are flat strings; nested structures are represented as JSON-encoded values.
- Defaults are defined centrally in one place and always applied first, then overridden by persisted values — this ensures the app runs out-of-the-box without any prior setup.
- There is no validation or type coercion of configuration values at load time; consumers must handle unexpected types.