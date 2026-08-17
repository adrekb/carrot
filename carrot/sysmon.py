"""Live machine load, and how fast the model is actually going.

`leaderboard.get_hardware_profile()` answers "what is this machine" — a static
description, probed once. This answers "what is it doing right now", which is a
different question and the one that matters while a local model is running:
the reason a turn is slow is almost always visible here, and the app could not
show it.

Two things live here because they answer the same question from opposite ends.
The meters say what the machine is spending; the throughput says what you are
getting for it. A 90%-busy GPU at 40 tokens/second and a 90%-busy GPU at 4 are
the same picture from psutil and completely different situations, and you can
only tell them apart with both numbers side by side.

Everything degrades rather than fails. A machine with no NVIDIA card has no
GPU section, not an error; a build with no `psutil` reports what it can and
says the rest is unavailable. A dashboard widget must never be the thing that
breaks the dashboard.
"""

from __future__ import annotations

import platform
import re
import subprocess
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional

# How many samples of throughput to keep. Enough for a sparkline and a rolling
# average over a working session; small enough to be free.
HISTORY = 60

# Nothing slower than this is a real measurement — it is a one-token reply
# where the time is all startup.
MIN_SAMPLE_TOKENS = 5


def _run(command: List[str], timeout: float = 2.0) -> str:
    """Run a probe, or return "" if it is not installed.

    Short timeout on purpose. This is called from a dashboard poll, and a
    hung `nvidia-smi` must cost the widget its refresh rather than the
    request its thread. Never `shell=True`.
    """
    try:
        return subprocess.check_output(
            command, stderr=subprocess.DEVNULL, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        ).decode("utf-8", errors="replace")
    except Exception:
        return ""


# ===== CPU and memory =====

def _cpu_and_memory() -> Dict[str, Any]:
    try:
        import psutil
    except ImportError:
        return {"available": False, "why": "psutil is not installed"}

    memory = psutil.virtual_memory()
    out: Dict[str, Any] = {
        "available": True,
        # interval=None is the non-blocking form: it reports load since the
        # last call rather than sleeping to measure. A dashboard that polls
        # every couple of seconds gets a real figure, and the first call after
        # start returns 0.0, which is honest about having nothing to compare
        # against yet.
        "cpu_percent": round(psutil.cpu_percent(interval=None), 1),
        "cpu_cores": psutil.cpu_count(logical=True),
        "ram_percent": round(memory.percent, 1),
        "ram_used_gb": round(memory.used / (1024 ** 3), 2),
        "ram_total_gb": round(memory.total / (1024 ** 3), 2),
    }
    try:
        # Per-core, so a single pegged core is distinguishable from a machine
        # that is genuinely saturated. Those look identical in the average and
        # mean opposite things about whether more parallelism would help.
        out["cpu_per_core"] = [round(v, 1) for v in
                               psutil.cpu_percent(interval=None, percpu=True)]
    except Exception:
        pass
    try:
        temps = psutil.sensors_temperatures()
        for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz"):
            if temps.get(key):
                out["cpu_temp_c"] = round(temps[key][0].current, 1)
                break
    except Exception:
        pass       # Windows exposes no temperatures through psutil at all.
    return out


# ===== GPU =====

_NVIDIA_QUERY = (
    "utilization.gpu,memory.used,memory.total,temperature.gpu,name"
)


def _nvidia() -> List[Dict[str, Any]]:
    out = _run(["nvidia-smi", f"--query-gpu={_NVIDIA_QUERY}",
                "--format=csv,noheader,nounits"])
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            used, total = float(parts[1]), float(parts[2])
            gpus.append({
                "name": parts[4],
                "vendor": "nvidia",
                "gpu_percent": float(parts[0]),
                "vram_used_gb": round(used / 1024, 2),
                "vram_total_gb": round(total / 1024, 2),
                "vram_percent": round(used / total * 100, 1) if total else 0.0,
                "temp_c": float(parts[3]) if parts[3].replace(".", "").isdigit() else None,
            })
        except (ValueError, ZeroDivisionError):
            continue
    return gpus


def _apple() -> List[Dict[str, Any]]:
    """Apple Silicon reports no utilisation without elevated privileges.

    `powermetrics` has the number and needs sudo, which a dashboard widget is
    never going to have and should never ask for. So the GPU is named and its
    utilisation is reported as unknown — which is the truth, and better than
    a zero that reads as "idle".
    """
    if platform.system() != "Darwin":
        return []
    out = _run(["system_profiler", "SPDisplaysDataType"])
    names = re.findall(r"Chipset Model:\s*(.+)", out)
    return [{
        "name": name.strip(), "vendor": "apple",
        "gpu_percent": None, "vram_used_gb": None,
        "vram_total_gb": None, "vram_percent": None, "temp_c": None,
        "why": "macOS does not expose GPU load without elevated privileges",
    } for name in names[:2]]


def _gpus() -> List[Dict[str, Any]]:
    gpus = _nvidia()
    if gpus:
        return gpus
    return _apple()


# ===== Throughput =====
#
# Ollama returns `eval_count` and `eval_duration` on the final frame of every
# generation and nothing was reading them. That is the only trustworthy source
# for this number: timing it from outside includes the queue, the prompt
# evaluation and the network, and reports a figure two or three times lower
# than the model is actually producing — which is precisely the sort of
# plausible-but-wrong number that makes someone go and buy a graphics card.

class _Throughput:
    """A rolling record of generation speed, per model."""

    def __init__(self):
        self._lock = threading.Lock()
        self._samples: Deque[Dict[str, Any]] = deque(maxlen=HISTORY)

    def record(self, model: str, eval_count: int, eval_duration_ns: int,
               prompt_eval_count: int = 0, prompt_eval_duration_ns: int = 0):
        """Record one generation from Ollama's own counters."""
        try:
            tokens = int(eval_count or 0)
            nanos = int(eval_duration_ns or 0)
        except (TypeError, ValueError):
            return
        if tokens < MIN_SAMPLE_TOKENS or nanos <= 0:
            return
        sample = {
            "model": model or "",
            "tokens": tokens,
            "seconds": round(nanos / 1e9, 3),
            "tps": round(tokens / (nanos / 1e9), 1),
            "at": time.time(),
            # A high-resolution stamp beside the wall-clock one, for "did this
            # happen after that".
            #
            # `time.time()` steps in ~15ms on Windows, and so does
            # `time.monotonic()` — both are coarse enough that a turn which
            # started and finished inside one tick compares equal to its own
            # start, so its sample either looks like it predates the turn or
            # gets handed to the next one. `perf_counter()` is sub-microsecond
            # and monotonic, which is what this comparison actually needs.
            "at_mono": time.perf_counter(),
        }
        # Prompt processing is a separate rate and a separate bottleneck: a
        # long context is slow to *ingest* even when generation is fast, and
        # reporting one number hides which of the two the user is waiting on.
        try:
            ptokens, pnanos = int(prompt_eval_count or 0), int(prompt_eval_duration_ns or 0)
            if ptokens > 0 and pnanos > 0:
                sample["prompt_tokens"] = ptokens
                sample["prompt_tps"] = round(ptokens / (pnanos / 1e9), 1)
        except (TypeError, ValueError):
            pass
        with self._lock:
            self._samples.append(sample)

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            samples = list(self._samples)
        if not samples:
            return {"samples": [], "latest": None, "average": None, "by_model": {}}

        by_model: Dict[str, List[float]] = {}
        for sample in samples:
            by_model.setdefault(sample["model"], []).append(sample["tps"])
        return {
            "samples": samples[-HISTORY:],
            "latest": samples[-1],
            # The mean over the window, not over all time: a machine that was
            # thermally throttled an hour ago is not the machine you have now.
            "average": round(sum(s["tps"] for s in samples) / len(samples), 1),
            "by_model": {
                model: {"average": round(sum(v) / len(v), 1), "runs": len(v)}
                for model, v in by_model.items()
            },
        }

    def since(self, after: float) -> Optional[Dict[str, Any]]:
        """The newest sample recorded after ``after`` (a `time.perf_counter()`), or None.

        For attaching a rate to the message it belongs to. The plain latest
        sample is the wrong thing: on a turn the model never ran — a cached
        answer, a tool-only reply, a hosted model, a failure — it is whatever
        the *previous* turn produced, and labelling this message with the last
        one's speed is worse than saying nothing, because it looks measured.
        """
        with self._lock:
            for sample in reversed(self._samples):
                # Strictly after, which only works because the stamp is a
                # perf_counter: with a 15ms clock the previous turn's sample
                # compares equal to this turn's start and gets handed to it.
                if sample.get("at_mono", 0) > after:
                    return dict(sample)
        return None

    def clear(self):
        with self._lock:
            self._samples.clear()


throughput = _Throughput()


def record_ollama_metrics(model: str, data: Dict[str, Any]):
    """Record throughput from an Ollama final frame, if it carries one.

    Called from the streaming loops, where a malformed or metric-free frame is
    entirely normal — every non-final frame is one. Contained here so no
    caller has to guard it.
    """
    if not isinstance(data, dict):
        return
    try:
        throughput.record(
            model,
            data.get("eval_count", 0), data.get("eval_duration", 0),
            data.get("prompt_eval_count", 0), data.get("prompt_eval_duration", 0),
        )
    except Exception:
        pass


# ===== The reading a widget asks for =====

def meters() -> Dict[str, Any]:
    """One sample of everything, for the dashboard."""
    reading: Dict[str, Any] = {"at": time.time()}
    reading.update(_cpu_and_memory())
    try:
        reading["gpus"] = _gpus()
    except Exception:
        reading["gpus"] = []
    return reading
