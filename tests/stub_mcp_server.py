"""A tiny stdio MCP server used by the test suite.

Speaks newline-delimited JSON-RPC 2.0 and exposes a single ``echo`` tool that
returns its ``text`` argument. Run as: ``python tests/stub_mcp_server.py``.
"""
import json
import sys


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method")
        req_id = msg.get("id")
        if req_id is None:
            # notification (e.g. notifications/initialized) — no response
            continue
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "stub", "version": "0.0.1"},
            }
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "echo",
                        "description": "Echo back the provided text.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                        },
                    }
                ]
            }
        elif method == "tools/call":
            params = msg.get("params", {})
            text = (params.get("arguments") or {}).get("text", "")
            result = {"content": [{"type": "text", "text": f"echo: {text}"}]}
        else:
            result = {}
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
