import os
import json
import sqlite3


CARROT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
CONFIG_DB_KEY = "config"
DEFAULTS = {
    "ollama_host": "http://localhost:11434",
    "ollama_model": "gemma4:e4b",
    "ollama_model_recap": "gemma4:e4b",
    "ollama_model_search": "gemma4:e4b",
    "data_dir": CARROT_DIR,
    "conversations_dir": os.path.join(CARROT_DIR, "conversations"),
    "notes_dir": os.path.join(CARROT_DIR, "notes"),
    "goals_dir": os.path.join(CARROT_DIR, "goals"),
    "db_path": os.path.join(CARROT_DIR, "carrot.db"),
    "server_host": "127.0.0.1",
    "server_port": 8181,
    "web_ui": True,
    "recap_enabled": False,
    "recap_hours": [7, 8],
    "recap_rss_feeds": [
        "https://hnrss.org/newest",
        "https://www.reddit.com/r/programming/.rss",
    ],
    "recap_max_items": 10,
    # Memory
    "memory_enabled": True,
    "memory_min_confidence": 0.6,
    "summarize_enabled": True,
    # Local document index
    "index_dirs": [],
    "index_on_startup": False,
    # Agent tools
    "agent_tools_enabled": True,
    "agent_require_approval": True,
    "code_workspace_dir": "",
    # Model routing
    "model_routes": {},
    "cloud_enabled": False,
    "cloud_api_key": "",
    "cloud_model": "claude-opus-5",
    "cloud_effort": "high",
    "cloud_tasks": ["reasoning", "code"],
    # Security
    "auth_enabled": True,
    "terminal_confirm_destructive": True,
    "terminal_restrict_cwd": False,
    "terminal_extra_roots": [],
    # Proactive notifications
    "proactive_enabled": True,
    "proactive_interval_seconds": 300,
    "proactive_disabled_checks": [],
}

# Keys that must never be returned by the read-only config endpoint.
SECRET_KEYS = {"cloud_api_key"}


def get_config():
    conn = sqlite3.connect(os.path.join(CARROT_DIR, "carrot.db"))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT key, value FROM config").fetchall()
    conn.close()
    config = dict(DEFAULTS)
    for row in rows:
        try:
            config[row["key"]] = json.loads(row["value"])
        except (json.JSONDecodeError, TypeError):
            config[row["key"]] = row["value"]
    return config


def set_config(key, value):
    conn = sqlite3.connect(os.path.join(CARROT_DIR, "carrot.db"))
    conn.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
        (key, json.dumps(value)),
    )
    conn.commit()
    conn.close()