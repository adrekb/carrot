"""Minimal MCP (Model Context Protocol) client over stdio.

Speaks newline-delimited JSON-RPC 2.0 to locally configured MCP servers so
Carrot's chat agent can discover and call external tools. Servers are declared
in carrot/data/config/mcp.json:

    {"servers": {"weather": {"command": "python", "args": ["weather_mcp.py"], "enabled": true}}}
"""
import json
import os
import subprocess
import threading

MCP_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data", "config", "mcp.json"
)

PROTOCOL_VERSION = "2024-11-05"
CALL_TIMEOUT = 30


# ===== config =====

def load_mcp_config() -> dict:
    if not os.path.exists(MCP_CONFIG_PATH):
        return {"servers": {}}
    try:
        with open(MCP_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg.setdefault("servers", {})
        return cfg
    except (json.JSONDecodeError, OSError):
        return {"servers": {}}


def save_mcp_config(cfg: dict):
    os.makedirs(os.path.dirname(MCP_CONFIG_PATH), exist_ok=True)
    with open(MCP_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


def add_server(name: str, command: str, args: list = None, enabled: bool = True):
    cfg = load_mcp_config()
    cfg["servers"][name] = {"command": command, "args": args or [], "enabled": enabled}
    save_mcp_config(cfg)
    return cfg["servers"][name]


def remove_server(name: str) -> bool:
    cfg = load_mcp_config()
    if name in cfg["servers"]:
        del cfg["servers"][name]
        save_mcp_config(cfg)
        return True
    return False


def set_server_enabled(name: str, enabled: bool) -> bool:
    cfg = load_mcp_config()
    if name in cfg["servers"]:
        cfg["servers"][name]["enabled"] = enabled
        save_mcp_config(cfg)
        return True
    return False


# ===== stdio JSON-RPC client =====

class McpError(Exception):
    pass


class McpSession:
    """One short-lived stdio session with an MCP server."""

    def __init__(self, name: str, command: str, args: list = None):
        self.name = name
        self.command = command
        self.args = args or []
        self.proc = None
        self._id = 0
        self._lock = threading.Lock()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    def start(self):
        try:
            self.proc = subprocess.Popen(
                [self.command] + self.args,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, FileNotFoundError) as e:
            raise McpError(f"could not start server '{self.name}': {e}")
        self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "carrot", "version": "0.2.0"},
        })
        self._notify("notifications/initialized", {})

    def close(self):
        if self.proc:
            try:
                self.proc.terminate()
            except OSError:
                pass
            self.proc = None

    def _send(self, payload: dict):
        line = json.dumps(payload) + "\n"
        try:
            self.proc.stdin.write(line)
            self.proc.stdin.flush()
        except (OSError, BrokenPipeError, AttributeError) as e:
            raise McpError(f"server '{self.name}' pipe closed: {e}")

    def _notify(self, method: str, params: dict):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: dict):
        with self._lock:
            self._id += 1
            req_id = self._id
            self._send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            result = {}

            def read():
                while True:
                    line = self.proc.stdout.readline()
                    if not line:
                        result["error"] = f"server '{self.name}' closed the connection"
                        return
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if msg.get("id") == req_id:
                        if "error" in msg:
                            result["error"] = msg["error"].get("message", str(msg["error"]))
                        else:
                            result["result"] = msg.get("result", {})
                        return
                    # ignore notifications / unrelated messages

            reader = threading.Thread(target=read, daemon=True)
            reader.start()
            reader.join(timeout=CALL_TIMEOUT)
            if reader.is_alive():
                raise McpError(f"server '{self.name}' timed out on {method}")
            if "error" in result:
                raise McpError(result["error"])
            return result.get("result", {})

    def list_tools(self) -> list:
        result = self._request("tools/list", {})
        return result.get("tools", [])

    def call_tool(self, tool: str, arguments: dict) -> str:
        result = self._request("tools/call", {"name": tool, "arguments": arguments or {}})
        parts = []
        for item in result.get("content", []):
            if item.get("type") == "text":
                parts.append(item.get("text", ""))
        return "\n".join(parts) or json.dumps(result)


# ===== high-level helpers used by the chat agent =====

def enabled_servers() -> dict:
    return {
        name: spec
        for name, spec in load_mcp_config()["servers"].items()
        if spec.get("enabled", True)
    }


def discover_all_tools() -> dict:
    """Return {server_name: {"tools": [...], "error": str|None}} for every server."""
    out = {}
    for name, spec in load_mcp_config()["servers"].items():
        entry = {"enabled": spec.get("enabled", True), "tools": [], "error": None}
        if entry["enabled"]:
            try:
                with McpSession(name, spec["command"], spec.get("args", [])) as session:
                    entry["tools"] = session.list_tools()
            except McpError as e:
                entry["error"] = str(e)
        out[name] = entry
    return out


def ollama_tools() -> list:
    """Flatten enabled MCP tools into Ollama tool specs.

    Tool names are namespaced as '<server>__<tool>' so calls can be routed back.
    """
    specs = []
    for server, spec in enabled_servers().items():
        try:
            with McpSession(server, spec["command"], spec.get("args", [])) as session:
                for tool in session.list_tools():
                    specs.append({
                        "type": "function",
                        "function": {
                            "name": f"{server}__{tool['name']}",
                            "description": tool.get("description", ""),
                            "parameters": tool.get("inputSchema", {"type": "object", "properties": {}}),
                        },
                    })
        except McpError:
            continue
    return specs


def call_namespaced_tool(namespaced: str, arguments: dict) -> str:
    """Execute '<server>__<tool>' against its MCP server."""
    if "__" not in namespaced:
        raise McpError(f"unknown tool '{namespaced}'")
    server, tool = namespaced.split("__", 1)
    spec = load_mcp_config()["servers"].get(server)
    if not spec:
        raise McpError(f"unknown MCP server '{server}'")
    with McpSession(server, spec["command"], spec.get("args", [])) as session:
        return session.call_tool(tool, arguments)
