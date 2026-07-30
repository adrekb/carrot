import subprocess
import asyncio
import os
import json
from datetime import datetime, timezone


MAX_OUTPUT_CHARS = 50000


def execute_command(cmd: str, cwd: str = None, timeout: int = 30, env: dict = None):
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or os.getcwd(),
            env={**os.environ, **(env or {})},
        )
        output = result.stdout
        if result.stderr:
            output += "\n[STDERR]\n" + result.stderr
        return {
            "success": result.returncode == 0,
            "returncode": result.returncode,
            "output": output[:MAX_OUTPUT_CHARS],
            "command": cmd,
            "cwd": cwd or os.getcwd(),
            "timeout": False,
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "returncode": -1,
            "output": f"Command timed out after {timeout} seconds",
            "command": cmd,
            "cwd": cwd or os.getcwd(),
            "timeout": True,
        }
    except Exception as e:
        return {
            "success": False,
            "returncode": -1,
            "output": f"Error: {str(e)}",
            "command": cmd,
            "cwd": cwd or os.getcwd(),
            "timeout": False,
        }


async def execute_command_async(cmd: str, cwd: str = None, timeout: int = 30):
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or os.getcwd(),
        )
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
            output = stdout_b.decode("utf-8", errors="replace")
            stderr = stderr_b.decode("utf-8", errors="replace")
            if stderr:
                output += "\n[STDERR]\n" + stderr
            return {
                "success": proc.returncode == 0,
                "returncode": proc.returncode,
                "output": output[:MAX_OUTPUT_CHARS],
                "command": cmd,
                "cwd": cwd or os.getcwd(),
                "timeout": False,
            }
        except asyncio.TimeoutExpired:
            proc.kill()
            await proc.wait()
            return {
                "success": False,
                "returncode": -1,
                "output": f"Command timed out after {timeout} seconds",
                "command": cmd,
                "cwd": cwd or os.getcwd(),
                "timeout": True,
            }
    except Exception as e:
        return {
            "success": False,
            "returncode": -1,
            "output": f"Error: {str(e)}",
            "command": cmd,
            "cwd": cwd or os.getcwd(),
            "timeout": False,
        }


def detect_language(filepath: str) -> str:
    ext = os.path.splitext(filepath)[1].lower()
    mapping = {
        ".py": "python",
        ".js": "node",
        ".ts": "npx ts-node",
        ".rs": "cargo run",
        ".go": "go run",
        ".rb": "ruby",
        ".c": "gcc",
        ".java": "java",
        ".sh": "bash",
    }
    return mapping.get(ext, "bash")