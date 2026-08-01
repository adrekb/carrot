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
    # Model routing. `model_routes` maps a task to {provider, model, effort};
    # a bare model string from an older build is still read.
    "model_routes": {},
    "custom_tasks": [],
    # Providers. Built-ins (ollama, anthropic, openai) live in providers.py;
    # these hold user-added endpoints, per-provider tweaks, and BYOK keys.
    "custom_providers": [],
    "provider_settings": {},
    "provider_keys": {},
    # Automatic escalation — which provider unassigned opted-in tasks go to.
    "escalation_provider": "anthropic",
    "escalation_model": "",
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
    # Carrot Research
    "research_default_depth": "standard",
    "research_max_seconds": 1800,
    # Carrot Agent. The defaults are the cautious ones on purpose: nothing is
    # allowed to touch the machine or a website until the user says which.
    "agent_allowed_domains": [],
    "agent_blocked_domains": [],
    "agent_secrets": {},
    "agent_app_allowlist": [],
    "agent_open_roots": [],
    "agent_desktop_control_enabled": False,
    "agent_allow_critical_actions": False,
    "agent_require_plan_approval": True,
    "agent_browser_headless": False,
    "agent_max_steps": 40,
    "agent_max_seconds": 900,
    "agent_max_navigations": 30,
    "agent_max_domains": 10,
    # Proactive notifications
    "proactive_enabled": True,
    "proactive_interval_seconds": 300,
    "proactive_disabled_checks": [],
}

# Keys that must never be returned by the read-only config endpoint. A dict
# value is reduced to a map of booleans, so the UI can still tell which
# providers have a key without any key leaving the process.
SECRET_KEYS = {"cloud_api_key", "provider_keys", "agent_secrets"}


def redact(settings):
    """Replace every secret with a boolean (or a boolean per entry)."""
    redacted = dict(settings)
    for key in SECRET_KEYS:
        value = redacted.get(key)
        if isinstance(value, dict):
            redacted[key] = {name: bool(secret) for name, secret in value.items()}
        else:
            redacted[key] = bool(value)
    return redacted


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