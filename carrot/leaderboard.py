import json
import os
import platform
import uuid
from datetime import datetime, timezone

import psutil

from carrot.database import get_db


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def get_hardware_profile():
    profile = {
        "os": platform.system(),
        "os_version": platform.version(),
        "cpu": platform.processor() or "Unknown CPU",
        "cpu_cores": psutil.cpu_count(logical=True),
        "cpu_physical_cores": psutil.cpu_count(logical=False),
        "ram_gb": round(psutil.virtual_memory().total / (1024 ** 3), 1),
        "ram_available_gb": round(psutil.virtual_memory().available / (1024 ** 3), 1),
        "gpu": _detect_gpu(),
        "hostname": platform.node(),
        "python_version": platform.python_version(),
    }
    return profile


def _detect_gpu():
    gpus = []
    system = platform.system()

    if system == "Windows":
        try:
            import subprocess
            output = subprocess.check_output(
                ["wmic", "path", "win32_videocontroller", "get", "name"],
                shell=True,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).decode("utf-8", errors="replace")
            for line in output.strip().split("\n"):
                line = line.strip()
                if line and "Name" not in line:
                    gpus.append(line)
        except Exception:
            pass

    elif system == "Linux":
        try:
            import subprocess
            output = subprocess.check_output(
                ["lspci"],
                shell=True,
                stderr=subprocess.DEVNULL,
                timeout=5,
            ).decode("utf-8", errors="replace")
            for line in output.split("\n"):
                lower = line.lower()
                if "vga" in lower or "3d" in lower or "display" in lower:
                    parts = line.split(":")
                    if len(parts) >= 3:
                        gpus.append(parts[-1].strip())
        except Exception:
            pass

    elif system == "Darwin":
        try:
            import subprocess
            output = subprocess.check_output(
                ["system_profiler", "SPDisplaysDataType"],
                shell=True,
                stderr=subprocess.DEVNULL,
                timeout=10,
            ).decode("utf-8", errors="replace")
            for line in output.split("\n"):
                if "Chip" in line or "Model" in line or "VRAM" in line:
                    gpus.append(line.strip())
        except Exception:
            pass

    if not gpus:
        return "Integrated/Unknown"
    return "; ".join(gpus)


def submit_leaderboard_entry(anon_id: str = None):
    profile = get_hardware_profile()
    if anon_id is None:
        anon_id = str(uuid.uuid4())[:12]

    conn = get_db()
    conn.execute(
        """INSERT INTO leaderboard 
           (id, os, cpu, ram_gb, gpu, active_model, task_type, tokens_per_second, submitted_at, anonymous_id, metadata) 
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(uuid.uuid4())[:12],
            profile["os"],
            profile["cpu"],
            profile["ram_gb"],
            profile["gpu"],
            "",
            "general",
            0.0,
            now_iso(),
            anon_id,
            json.dumps(profile),
        ),
    )
    conn.commit()
    conn.close()
    return {"anonymous_id": anon_id, "profile": profile}


def update_leaderboard_model(anon_id: str, model_name: str, task_type: str = "general"):
    conn = get_db()
    conn.execute(
        "UPDATE leaderboard SET active_model = ?, task_type = ? WHERE anonymous_id = ?",
        (model_name, task_type, anon_id),
    )
    conn.commit()
    conn.close()


def get_leaderboard(filters: dict = None, limit: int = 50):
    conn = get_db()
    query = "SELECT * FROM leaderboard WHERE 1=1"
    params = []

    if filters:
        if filters.get("os"):
            query += " AND os = ?"
            params.append(filters["os"])
        if filters.get("ram_gb_min"):
            query += " AND ram_gb >= ?"
            params.append(filters["ram_gb_min"])
        if filters.get("gpu"):
            query += " AND gpu LIKE ?"
            params.append(f"%{filters['gpu']}%")
        if filters.get("model"):
            query += " AND active_model LIKE ?"
            params.append(f"%{filters['model']}%")

    query += " ORDER BY submitted_at DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()

    return [
        {
            "id": r["id"],
            "os": r["os"],
            "cpu": r["cpu"],
            "ram_gb": r["ram_gb"],
            "gpu": r["gpu"],
            "active_model": r["active_model"],
            "task_type": r["task_type"],
            "tokens_per_second": r["tokens_per_second"],
            "submitted_at": r["submitted_at"],
            "anonymous_id": r["anonymous_id"],
            "metadata": json.loads(r["metadata"] or "{}"),
        }
        for r in rows
    ]


def get_leaderboard_stats():
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) as c FROM leaderboard").fetchone()["c"]

    os_counts = conn.execute(
        "SELECT os, COUNT(*) as count FROM leaderboard GROUP BY os ORDER BY count DESC"
    ).fetchall()

    model_counts = conn.execute(
        "SELECT active_model, COUNT(*) as count FROM leaderboard WHERE active_model != '' GROUP BY active_model ORDER BY count DESC"
    ).fetchall()

    ram_distribution = conn.execute(
        "SELECT CASE WHEN ram_gb < 8 THEN '0-8GB' WHEN ram_gb < 16 THEN '8-16GB' WHEN ram_gb < 32 THEN '16-32GB' ELSE '32GB+' END as range, COUNT(*) as count FROM leaderboard GROUP BY range ORDER BY range"
    ).fetchall()

    gpu_counts = conn.execute(
        "SELECT gpu, COUNT(*) as count FROM leaderboard GROUP BY gpu ORDER BY count DESC LIMIT 10"
    ).fetchall()

    conn.close()

    return {
        "total_submissions": total,
        "by_os": [{"os": r["os"], "count": r["count"]} for r in os_counts],
        "by_model": [{"model": r["active_model"], "count": r["count"]} for r in model_counts],
        "ram_distribution": [{"range": r["range"], "count": r["count"]} for r in ram_distribution],
        "top_gpus": [{"gpu": r["gpu"], "count": r["count"]} for r in gpu_counts],
    }


def get_model_recommendations(user_ram_gb: float = None, user_gpu: str = None):
    conn = get_db()
    query = "SELECT active_model, COUNT(*) as count, AVG(tokens_per_second) as avg_tps FROM leaderboard WHERE active_model != ''"
    params = []

    if user_ram_gb:
        query += " AND ram_gb >= ?"
        params.append(user_ram_gb)

    if user_gpu and user_gpu != "Integrated/Unknown":
        query += " AND gpu LIKE ?"
        params.append(f"%{user_gpu}%")

    query += " GROUP BY active_model ORDER BY count DESC LIMIT 5"

    rows = conn.execute(query, params).fetchall()
    conn.close()

    return [
        {"model": r["active_model"], "count": r["count"], "avg_tps": round(r["avg_tps"] or 0, 1)}
        for r in rows
    ]