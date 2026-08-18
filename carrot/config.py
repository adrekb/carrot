import os
import sys
import json
import sqlite3


def _default_data_dir() -> str:
    """Where Carrot keeps its database, config and caches.

    Running from a checkout: ``carrot/data`` next to the code, as always.
    Running as an installed app (frozen backend), the install dir is
    read-only, so use the platform's per-user data directory instead.
    ``CARROT_DATA_DIR`` overrides everything (tests, portable installs).
    """
    env = os.environ.get("CARROT_DATA_DIR")
    if env:
        return os.path.abspath(env)
    if getattr(sys, "frozen", False):
        home = os.path.expanduser("~")
        if sys.platform == "win32":
            base = os.environ.get("APPDATA") or os.path.join(home, "AppData", "Roaming")
            return os.path.join(base, "Carrot")
        if sys.platform == "darwin":
            return os.path.join(home, "Library", "Application Support", "Carrot")
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
        return os.path.join(base, "carrot")
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


CARROT_DIR = _default_data_dir()
CONFIG_DB_KEY = "config"
DEFAULTS = {
    "ollama_host": "http://localhost:11434",
    # Empty means "nobody has chosen yet", and that is a different thing from
    # any particular model. It used to be `gemma4:e4b` for every machine ever
    # to run Carrot — 6 GB, which thrashes on an 8 GB laptop where a 3B would
    # have been quick, and is a toy on a workstation with a 24 GB card. The
    # Hub has always known how to size a model to a machine; the default never
    # asked it. `hub.configured_or_default_model()` is where the question gets
    # asked now, and it is asked lazily, because a default that ran nvidia-smi
    # from an import would run it in every process that touches config.
    "ollama_model": "",
    "ollama_model_recap": "",
    "ollama_model_search": "",
    "data_dir": CARROT_DIR,
    "conversations_dir": os.path.join(CARROT_DIR, "conversations"),
    "notes_dir": os.path.join(CARROT_DIR, "notes"),
    "goals_dir": os.path.join(CARROT_DIR, "goals"),
    "db_path": os.path.join(CARROT_DIR, "carrot.db"),
    "server_host": "127.0.0.1",
    "server_port": 8181,
    "web_ui": True,
    # First run shows the onboarding flow once; skipping still sets this.
    "onboarding_done": False,
    # Appearance. The browser is the source of truth (it has to decide the
    # theme before it can call this API); these are the fallback for a
    # machine whose local storage was cleared. "auto" follows the OS.
    "ui_theme": "auto",
    "ui_accent": "carrot",
    "recap_enabled": False,
    "recap_hours": [7, 8],
    "recap_rss_feeds": [
        "https://hnrss.org/newest",
        "https://www.reddit.com/r/programming/.rss",
    ],
    "recap_max_items": 10,
    # Memory
    "memory_enabled": True,
    # Chat noticing a commitment and offering to track it. On by default and
    # switchable, because it is the one feature here that speaks without being
    # spoken to — and a thing that appears uninvited has to be a thing you can
    # stop.
    "goal_chips_enabled": True,
    "memory_min_confidence": 0.6,
    "summarize_enabled": True,
    # Local document index
    "index_dirs": [],
    "index_on_startup": False,
    # Agent tools
    "agent_tools_enabled": True,
    "agent_require_approval": True,
    "code_workspace_dir": "",
    # Coding agent. "act" keeps the tools it has always had; "plan" takes the
    # write tools away so a change can be agreed before anything moves.
    "coder_mode": "act",
    "coder_recipes": [],
    # Implementation briefs, one per conversation, written when Plan hands off
    # to Act so the transcript can be dropped.
    "coder_snapshots": {},
    # Image and video generation. Empty backend means "pick the best set-up
    # one, preferring on-device" rather than a hardcoded cloud default.
    "media_backend_image": "",
    "media_backend_video": "",
    "media_keys": {},
    "media_endpoints": {},
    "media_comfy_workflow": {},
    # Semester planning: intake answers, courses, cached campus geometry and
    # the last plan produced.
    "planner_profile": {},
    "planner_campus": {},
    "planner_last_plan": {},
    # Local webhooks. Off until asked for: this is the one door into Carrot
    # that does not require the app's own session.
    "webhooks_enabled": False,
    "webhooks": [],
    "webhook_targets": [],
    # Multi-model debate. Empty until the user picks a panel: two models that
    # fail differently is the point, and Carrot cannot guess which they have.
    "consensus_panel": [],
    "consensus_synthesiser": {},
    "consensus_runs": [],
    # Ambient capture policy. Off, and default-deny on everything that
    # matters, until the user says otherwise.
    "ambient_policy": {},
    # Dual authentication: per-provider "api_key" or "subscription", the OAuth
    # client each installation registers, and the tokens a sign-in produced.
    "auth_modes": {},
    "oauth_clients": {},
    "oauth_tokens": {},
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
    # Interop with apps the user already lives in.
    "obsidian_vault_path": "",
    # Calendar via secret iCal URL (Google Calendar etc.) — no OAuth, no keys.
    "calendar_ics_url": "",
    "calendar_enabled": False,
    # Whether the chat assistant may see upcoming events as context.
    "calendar_agent_aware": False,
    # Carrot Hub — hardware-aware model catalog. The catalog URL is derived
    # from hub_url unless overridden (self-hosted hub, corporate mirror).
    # Empty = use the bundled catalog plus live Hugging Face results.
    # Set these only to serve your own catalog.json.
    "hub_url": "",
    "hub_catalog_url": "",
    # Security
    "auth_enabled": True,
    "terminal_confirm_destructive": True,
    "terminal_restrict_cwd": False,
    "terminal_extra_roots": [],
    # Chat. Whether a chat turn may reach the web: off | single | multi.
    "chat_search_mode": "single",
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
# calendar_ics_url is a capability URL: anyone holding it can read the
# whole calendar, so it is redacted like an API key.
SECRET_KEYS = {
    "cloud_api_key", "provider_keys", "agent_secrets", "calendar_ics_url",
    # Generation backends hold keys of their own, and an OAuth token is a
    # bearer credential — leaking one is leaking the account.
    "media_keys", "oauth_tokens",
    # A hook's token is a credential that works without a session, so it must
    # never come back out through the config endpoint.
    "webhooks",
    # A search key is billed to somebody. Optional — the free endpoint needs
    # none — which is exactly why it would be easy to forget it is a
    # credential when someone does paste one in.
    "exa_api_key",
}


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


# These two go through `database.get_db()` rather than opening the file
# themselves. Imported inside the functions because database imports config for
# the data directory, and at module scope that is a cycle.
#
# What the raw connection was missing: the schema. `get_db()` creates it on
# first connect, and these were the two places that read and wrote a table
# without ever guaranteeing it existed — so config on a database no one had
# called `init_db()` against raised `no such table: config` rather than
# returning the defaults. They also inherit WAL and the 30-second busy
# timeout, which matters for a write that can land while the indexer holds
# the database.
def get_config():
    from carrot import database

    conn = database.get_db()
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
    from carrot import database

    conn = database.get_db()
    conn.execute(
        "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)",
        (key, json.dumps(value)),
    )
    conn.commit()
    conn.close()