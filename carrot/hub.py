"""Carrot Hub — hardware-aware model recommendations.

A new user cannot be expected to know which model, at which quantization,
runs well on their machine. This module closes that gap in three steps:

1. **Detect specs.** RAM and CPU come from the leaderboard's hardware
   profile; on top of that we detect usable VRAM (nvidia-smi on
   NVIDIA, unified memory on Apple Silicon) and pick an inference
   backend (cuda / metal / cpu).
2. **Fit the catalog.** Every catalog entry declares how much memory it
   needs to run (`min_mem_gb`, weights + context overhead). Against the
   machine's *model budget* each entry gets a fit level: ``great``,
   ``good``, ``tight`` (runs, but partially offloaded and slow) or
   ``too_big``.
3. **Recommend.** From the entries that fit, pick a best all-rounder, a
   light/fast option, and per-use-case picks (coding, reasoning) so the
   setup splash can offer sensible one-click choices — with a Skip
   button for people who already know what they want.

The bundled catalog below is a snapshot. The living version is served
by the Carrot Hub website (updated daily as models are released); we
re-fetch it at most once a day and fall back to the cache, then to the
bundle, so the app never depends on the network being up.
"""
import os
import json
import math
import platform
import re
import subprocess
from datetime import datetime, timezone
from typing import Optional

import requests

from carrot.config import CARROT_DIR, get_config
from carrot.leaderboard import get_hardware_profile

# The Carrot Hub website. The catalog it serves has the same shape as
# BUNDLED_CATALOG; both are overridable in config for self-hosters.
# There is no Carrot Hub website. The live catalog comes straight from the
# public Hugging Face API, and the curated list ships in this file. Point
# ``hub_url``/``hub_catalog_url`` at your own host to serve a custom catalog;
# left empty, Carrot uses HF plus the bundle and never calls a dead domain.
HUB_URL = ""
# Where "browse the source" sends the user: the index the live results
# actually come from.
HUB_BROWSE_URL = "https://huggingface.co/models?library=gguf&sort=trending"
HUB_CATALOG_PATH = "/catalog.json"
CATALOG_CACHE_PATH = os.path.join(CARROT_DIR, "config", "hub_catalog.json")
CATALOG_MAX_AGE_HOURS = 24

# After a failed fetch, don't retry for a while — otherwise every Hub load
# on an offline machine stalls for the full request timeout.
FAIL_RETRY_MINUTES = 15
_fail_memo: dict = {}


def _recently_failed(key: str) -> bool:
    last = _fail_memo.get(key)
    return last is not None and (datetime.now(timezone.utc) - last).total_seconds() < FAIL_RETRY_MINUTES * 60


def _note_failure(key: str):
    _fail_memo[key] = datetime.now(timezone.utc)

# Every entry is one pullable Ollama tag. `min_mem_gb` is what the model
# actually needs while running (quantized weights + KV cache at a modest
# context), not just the download size. `tier` orders quality coarsely so
# the recommender can prefer the strongest model that still fits.
BUNDLED_CATALOG = [
    {"id": "llama3.2:1b", "label": "Llama 3.2 1B", "family": "llama", "params_b": 1.2,
     "quant": "Q4_K_M", "download_gb": 1.3, "min_mem_gb": 2.5, "tier": "light",
     "use_cases": ["chat", "fast"], "blurb": "Tiny and instant — fine for quick questions on low-RAM machines."},
    {"id": "llama3.2:3b", "label": "Llama 3.2 3B", "family": "llama", "params_b": 3.2,
     "quant": "Q4_K_M", "download_gb": 2.0, "min_mem_gb": 4.0, "tier": "light",
     "use_cases": ["chat", "fast"], "blurb": "Fast general chat with a small footprint."},
    {"id": "gemma4:e4b", "label": "Gemma 4 E4B", "family": "gemma", "params_b": 4.0,
     "quant": "Q4_K_M", "download_gb": 4.2, "min_mem_gb": 6.0, "tier": "balanced",
     "use_cases": ["chat", "fast", "coding"], "blurb": "Carrot's default all-rounder — good answers at laptop-friendly size."},
    {"id": "mistral:7b", "label": "Mistral 7B", "family": "mistral", "params_b": 7.2,
     "quant": "Q4_K_M", "download_gb": 4.1, "min_mem_gb": 6.5, "tier": "balanced",
     "use_cases": ["chat"], "blurb": "Solid general-purpose model, efficient for its size."},
    {"id": "qwen2.5:7b", "label": "Qwen 2.5 7B", "family": "qwen", "params_b": 7.6,
     "quant": "Q4_K_M", "download_gb": 4.7, "min_mem_gb": 7.0, "tier": "balanced",
     "use_cases": ["chat", "coding"], "blurb": "Strong multilingual chat with decent coding ability."},
    {"id": "qwen2.5-coder:7b", "label": "Qwen 2.5 Coder 7B", "family": "qwen", "params_b": 7.6,
     "quant": "Q4_K_M", "download_gb": 4.7, "min_mem_gb": 7.0, "tier": "balanced",
     "use_cases": ["coding"], "blurb": "The go-to local coding model at this size."},
    {"id": "llama3.1:8b", "label": "Llama 3.1 8B", "family": "llama", "params_b": 8.0,
     "quant": "Q4_K_M", "download_gb": 4.9, "min_mem_gb": 7.5, "tier": "balanced",
     "use_cases": ["chat"], "blurb": "Well-rounded chat with a long context window."},
    {"id": "deepseek-r1:8b", "label": "DeepSeek R1 8B", "family": "deepseek", "params_b": 8.0,
     "quant": "Q4_K_M", "download_gb": 4.9, "min_mem_gb": 7.5, "tier": "balanced",
     "use_cases": ["reasoning"], "blurb": "Thinks step-by-step before answering — good for hard questions."},
    {"id": "llava:7b", "label": "LLaVA 7B", "family": "llava", "params_b": 7.0,
     "quant": "Q4_K_M", "download_gb": 4.7, "min_mem_gb": 7.0, "tier": "balanced",
     "use_cases": ["vision"], "modalities": ["image"],
     "blurb": "Understands images — screenshots, photos, diagrams."},
    {"id": "llama3.2-vision:11b", "label": "Llama 3.2 Vision 11B", "family": "llama", "params_b": 10.6,
     "quant": "Q4_K_M", "download_gb": 7.8, "min_mem_gb": 10.0, "tier": "power",
     "use_cases": ["vision", "chat"], "modalities": ["image"],
     "blurb": "Strong image understanding plus solid chat."},
    {"id": "qwen2.5vl:7b", "label": "Qwen 2.5 VL 7B", "family": "qwen", "params_b": 8.3,
     "quant": "Q4_K_M", "download_gb": 6.0, "min_mem_gb": 8.5, "tier": "balanced",
     "use_cases": ["vision"], "modalities": ["image", "video"],
     "blurb": "Reads images and understands video clips."},
    {"id": "qwen2-audio:7b", "label": "Qwen 2 Audio 7B", "family": "qwen", "params_b": 7.6,
     "quant": "Q4_K_M", "download_gb": 4.9, "min_mem_gb": 7.5, "tier": "balanced",
     "use_cases": ["chat"], "modalities": ["audio"],
     "blurb": "Understands speech and sounds, answers in text."},
    {"id": "qwen2.5-coder:14b", "label": "Qwen 2.5 Coder 14B", "family": "qwen", "params_b": 14.8,
     "quant": "Q4_K_M", "download_gb": 9.0, "min_mem_gb": 12.0, "tier": "power",
     "use_cases": ["coding"], "blurb": "Noticeably better code than the 7B if you have the memory."},
    {"id": "phi4:14b", "label": "Phi 4 14B", "family": "phi", "params_b": 14.7,
     "quant": "Q4_K_M", "download_gb": 9.1, "min_mem_gb": 12.0, "tier": "power",
     "use_cases": ["reasoning", "chat"], "blurb": "Strong reasoning for its size."},
    {"id": "deepseek-r1:14b", "label": "DeepSeek R1 14B", "family": "deepseek", "params_b": 14.8,
     "quant": "Q4_K_M", "download_gb": 9.0, "min_mem_gb": 12.0, "tier": "power",
     "use_cases": ["reasoning"], "blurb": "Deliberate step-by-step reasoning, mid-size."},
    {"id": "qwen2.5:32b", "label": "Qwen 2.5 32B", "family": "qwen", "params_b": 32.8,
     "quant": "Q4_K_M", "download_gb": 20.0, "min_mem_gb": 24.0, "tier": "power",
     "use_cases": ["chat", "coding", "reasoning"], "blurb": "Near-frontier local quality — needs a big GPU or lots of unified memory."},
    {"id": "llama3.3:70b", "label": "Llama 3.3 70B", "family": "llama", "params_b": 70.6,
     "quant": "Q4_K_M", "download_gb": 43.0, "min_mem_gb": 48.0, "tier": "power",
     "use_cases": ["chat", "reasoning"], "blurb": "The strongest bundled option — workstation-class hardware only."},
]

USE_CASES = ["chat", "coding", "reasoning", "fast", "vision"]
# Extra input modalities beyond text, for the Hub's y/n filters.
MODALITIES = ["image", "audio", "video"]


# ===== Spec detection =====

def _detect_nvidia_vram_gb() -> float:
    """Total VRAM across NVIDIA GPUs via nvidia-smi, or 0 if unavailable."""
    try:
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode("utf-8", errors="replace")
        mib = sum(float(line) for line in out.split() if line.strip())
        return round(mib / 1024, 1)
    except Exception:
        return 0.0


def _detect_amd_vram_gb() -> float:
    """Total VRAM across AMD GPUs via rocm-smi, or 0 if unavailable."""
    try:
        out = subprocess.check_output(
            ["rocm-smi", "--showmeminfo", "vram", "--csv"],
            stderr=subprocess.DEVNULL, timeout=5,
        ).decode("utf-8", errors="replace")
        total_bytes = 0
        for line in out.splitlines():
            parts = line.split(",")
            # card rows look like: card0,<total bytes>,<used bytes>
            if len(parts) >= 2 and parts[0].strip().startswith("card"):
                total_bytes += float(parts[1])
        return round(total_bytes / (1024 ** 3), 1)
    except Exception:
        return 0.0


def _is_apple_silicon() -> bool:
    return platform.system() == "Darwin" and platform.machine() in ("arm64", "aarch64")


# Effective memory bandwidth (GB/s) assumed per backend when we don't know
# the exact card — the dominant factor in local token speed. The 0.55
# efficiency factor (kernel overhead, KV-cache reads) follows llmfit's
# published methodology (github.com/AlexsJones/llmfit).
_BANDWIDTH_GB_S = {"cuda": 220, "rocm": 180, "metal": 160, "cpu": 50}
_SPEED_EFFICIENCY = 0.55


def estimate_tokens_per_sec(download_gb: float, backend: str, fit: str) -> Optional[int]:
    """Rough generation speed: bandwidth / weights-read-per-token, derated.

    A tight fit spills layers to system RAM, which dominates the run —
    derate hard rather than pretend. Never a promise, just enough signal
    to tell 'instant' from 'coffee break' before a 40 GB download.
    """
    if fit == "too_big" or not download_gb:
        return None
    tps = _BANDWIDTH_GB_S.get(backend, 50) / download_gb * _SPEED_EFFICIENCY
    if fit == "tight":
        tps *= 0.35
    return max(1, round(tps))


def detect_specs() -> dict:
    """Hardware profile plus the fields the recommender needs.

    ``model_budget_gb`` is the memory we're willing to plan models into:
      - CUDA: the card's VRAM (fits there = runs at full speed).
      - Apple Silicon: ~65% of unified memory (macOS's own GPU ceiling).
      - CPU-only: half of system RAM, leaving room for the OS and Carrot.
    ``tight`` fits deliberately exceed this — Ollama will offload layers
    to CPU — which is why they're labeled slow rather than impossible.
    """
    profile = get_hardware_profile()
    ram = profile.get("ram_gb") or 0
    vram = _detect_nvidia_vram_gb()
    if vram >= 2:
        backend = "cuda"
        budget = vram
    elif (vram := _detect_amd_vram_gb()) >= 2:
        backend = "rocm"
        budget = vram
    elif _is_apple_silicon():
        backend = "metal"
        vram = ram  # unified memory
        budget = round(ram * 0.65, 1)
    else:
        backend = "cpu"
        budget = round(ram * 0.5, 1)
    return {
        "os": profile.get("os"),
        "cpu": profile.get("cpu"),
        "cpu_cores": profile.get("cpu_cores"),
        "ram_gb": ram,
        "gpu": profile.get("gpu"),
        "vram_gb": vram,
        "backend": backend,
        "model_budget_gb": budget,
    }


# ===== Catalog (bundled -> cached -> remote) =====

def _catalog_urls() -> tuple:
    # The splash queries the hub before bootstrap, possibly before the
    # config database exists — fall back to the stock URLs.
    try:
        cfg = get_config()
    except Exception:
        cfg = {}
    hub_url = (cfg.get("hub_url") or HUB_URL).rstrip("/")
    # No catalog URL unless someone actually configured a host.
    catalog_url = cfg.get("hub_catalog_url") or (hub_url + HUB_CATALOG_PATH if hub_url else "")
    return hub_url, catalog_url


def _load_cache() -> Optional[dict]:
    try:
        with open(CATALOG_CACHE_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
        if isinstance(cache.get("models"), list) and cache["models"]:
            return cache
    except (OSError, json.JSONDecodeError):
        pass
    return None


def _save_cache(models: list):
    os.makedirs(os.path.dirname(CATALOG_CACHE_PATH), exist_ok=True)
    with open(CATALOG_CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump({"fetched_at": datetime.now(timezone.utc).isoformat(), "models": models}, f, indent=2)


def _valid_entry(m) -> bool:
    return (
        isinstance(m, dict)
        and isinstance(m.get("id"), str)
        # An Ollama tag, not a URL or shell metacharacters from a bad feed.
        and re.fullmatch(r"[\w.\-]+(:[\w.\-]+)?", m["id"]) is not None
        and isinstance(m.get("min_mem_gb"), (int, float))
    )


def _cache_age_hours(cache: dict) -> float:
    try:
        fetched = datetime.fromisoformat(cache["fetched_at"])
        return (datetime.now(timezone.utc) - fetched).total_seconds() / 3600
    except (KeyError, ValueError):
        return float("inf")


def refresh_catalog(force: bool = False) -> Optional[list]:
    """Fetch a custom catalog if one is configured; None otherwise."""
    if _recently_failed("catalog") and not force:
        return None
    _, catalog_url = _catalog_urls()
    if not catalog_url:
        return None      # nothing configured — bundled catalog + live HF
    try:
        resp = requests.get(catalog_url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        models = data.get("models") if isinstance(data, dict) else data
        if isinstance(models, list):
            models = [m for m in models if _valid_entry(m)]
            if models:
                _fail_memo.pop("catalog", None)
                _save_cache(models)
                return models
    except Exception:
        pass
    _note_failure("catalog")
    return None


def get_catalog(refresh: bool = False) -> dict:
    """Return {models, source, fetched_at}, refreshing from the Hub when stale."""
    cache = _load_cache()
    stale = cache is None or _cache_age_hours(cache) > CATALOG_MAX_AGE_HOURS
    if refresh or stale:
        fetched = refresh_catalog()
        if fetched is not None:
            return {"models": fetched, "source": "hub", "fetched_at": _load_cache()["fetched_at"]}
    if cache is not None:
        return {"models": cache["models"], "source": "cache", "fetched_at": cache.get("fetched_at")}
    return {"models": BUNDLED_CATALOG, "source": "bundled", "fetched_at": None}


# ===== Quantization descent =====
#
# Locking every model to one quant leaves performance on the table: a
# 24 GB card should run an 8B model at Q8_0, not Q4_K_M. Like llmfit,
# we walk the ladder from highest quality down and take the best quant
# whose running footprint fits the machine's budget.

QUANT_LADDER = [
    # (name, GB of weights per B params, quality note)
    ("Q8_0", 1.07),
    ("Q6_K", 0.82),
    ("Q5_K_M", 0.71),
    ("Q4_K_M", 0.60),
    ("Q3_K_M", 0.49),
    ("Q2_K", 0.35),
]
# Running footprint = weights + KV cache + runtime, roughly:
_MEM_OVERHEAD_FACTOR = 1.15
_MEM_OVERHEAD_FLAT_GB = 1.2


def quant_plan(params_b: float, budget_gb: float) -> dict:
    """Best-quality quant that fits the budget, or the smallest one marked
    tight/too_big when nothing does."""
    chosen = None
    for name, gb_per_b in QUANT_LADDER:
        download = params_b * gb_per_b
        min_mem = download * _MEM_OVERHEAD_FACTOR + _MEM_OVERHEAD_FLAT_GB
        if min_mem <= budget_gb:
            chosen = (name, download, min_mem)
            break
    if chosen is None:
        name, gb_per_b = QUANT_LADDER[-1]
        download = params_b * gb_per_b
        min_mem = download * _MEM_OVERHEAD_FACTOR + _MEM_OVERHEAD_FLAT_GB
        chosen = (name, download, min_mem)
    name, download, min_mem = chosen
    return {
        "quant": name,
        "download_gb": round(download, 1),
        "min_mem_gb": round(min_mem, 1),
        "fit": fit_level(min_mem, budget_gb),
        "quant_reason": f"best quality that fits this machine: {name}",
    }


def apply_quant_plan(entry: dict, budget_gb: float) -> dict:
    """Re-plan an HF entry's quant for this machine and retag its pull id."""
    params_b = float(entry.get("params_b") or 0)
    if params_b <= 0:
        return entry
    plan = quant_plan(params_b, budget_gb)
    out = {**entry, **plan}
    base_id = entry["id"].rsplit(":", 1)[0]
    out["id"] = f"{base_id}:{plan['quant']}"
    return out


# ===== Workload understanding =====
#
# The user types what they want to do ("long conversations", "daily
# updates", "goal tracking") — plain words, not ML vocabulary. This maps
# that text to the use cases and modalities the ranking engine speaks.

WORKLOAD_KEYWORDS = {
    "coding": ["code", "coding", "program", "debug", "script", "develop", "refactor"],
    "reasoning": ["reason", "math", "research", "analy", "plan", "study", "homework", "think"],
    "chat": ["chat", "conversation", "talk", "journal", "assistant", "goal", "reminder",
             "daily", "note", "track", "diary", "update", "recap"],
    "fast": ["fast", "quick", "instant", "light", "snappy"],
    "vision": ["image", "photo", "screenshot", "diagram", "vision", "picture", "chart"],
}
MODALITY_KEYWORDS = {
    "image": ["image", "photo", "screenshot", "picture", "diagram", "vision", "chart"],
    "audio": ["audio", "voice", "speech", "sound", "transcri", "listen"],
    "video": ["video", "clip", "footage", "recording"],
}


def workload_to_profile(text: str) -> dict:
    """Free-text workload -> {use_cases, modalities}. Empty text means no
    preference, which ranks purely on fit and popularity."""
    lowered = (text or "").lower()
    use_cases = [uc for uc, kws in WORKLOAD_KEYWORDS.items() if any(k in lowered for k in kws)]
    modalities = [m for m, kws in MODALITY_KEYWORDS.items() if any(k in lowered for k in kws)]
    return {"use_cases": use_cases, "modalities": modalities, "text": text or ""}


# ===== Live Hugging Face search (the thin-client path) =====
#
# The GUI is a thin client: hardware read locally, models fetched live
# from the public HF API (so the list is as fresh as HF itself), then a
# local logic engine plans quants, drops what can't run here, and ranks
# by the user's workload. Responses are cached briefly for snappiness
# and served stale when offline.

HF_SORT_MODES = {
    # UI label -> HF API sort field. "recent" gets a popularity floor so
    # brand-new-but-bad uploads don't reach the user.
    "trending": "trendingScore",
    "popular": "downloads",
    "recent": "createdAt",
}
# Modality -> HF pipeline tag for multimodal GGUF models.
HF_PIPELINE_FOR_MODALITY = {
    "image": "image-text-to-text",
    "video": "video-text-to-text",
    "audio": "audio-text-to-text",
}
HF_SEARCH_CACHE_PATH = os.path.join(CARROT_DIR, "config", "hub_hf_search.json")
HF_SEARCH_CACHE_HOURS = 1
_RECENT_MIN_DOWNLOADS = 200


def _hf_api_get(params: dict, cache_key: str) -> Optional[list]:
    """GET the HF models API with a short cache and the shared fail memo."""
    cache = {}
    try:
        with open(HF_SEARCH_CACHE_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
        hit = cache.get(cache_key)
        if hit and _cache_age_hours(hit) <= HF_SEARCH_CACHE_HOURS:
            return hit["rows"]
    except (OSError, json.JSONDecodeError):
        cache = {}
    if _recently_failed("hf"):
        hit = cache.get(cache_key)
        return hit["rows"] if hit else None
    try:
        resp = requests.get(HF_TRENDING_URL, params=params, timeout=10)
        resp.raise_for_status()
        rows = resp.json()
        if not isinstance(rows, list):
            raise ValueError("unexpected HF response")
        _fail_memo.pop("hf", None)
        cache[cache_key] = {"fetched_at": datetime.now(timezone.utc).isoformat(), "rows": rows}
        os.makedirs(os.path.dirname(HF_SEARCH_CACHE_PATH), exist_ok=True)
        with open(HF_SEARCH_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        return rows
    except Exception:
        _note_failure("hf")
        hit = cache.get(cache_key)
        return hit["rows"] if hit else None


def _rank_key(m: dict, profile: dict):
    # The user's stated workload dominates: a model that matches what they
    # want to do outranks a better-fitting but unrelated one. Fit still
    # separates comfortable from tight, and popularity breaks ties.
    fit_score = {"great": 3, "good": 2.5, "tight": 0.5}.get(m.get("fit"), 0)
    match = sum(1 for uc in profile.get("use_cases", []) if uc in (m.get("use_cases") or []))
    popularity = math.log10(max(m.get("downloads", 0), 1))
    return (fit_score * 10 + match * 8 + popularity, float(m.get("params_b") or 0))


def live_search(workload: str = "", sort: str = "trending",
                modalities: Optional[list] = None, limit: int = 20) -> dict:
    """The full thin-client flow: specs + workload -> live HF fetch ->
    local quant planning and fit filtering -> ranked results."""
    specs = detect_specs()
    budget = specs.get("model_budget_gb") or 0
    profile = workload_to_profile(workload)
    wanted_modalities = sorted(set(profile["modalities"]) | set(modalities or []))
    profile["modalities"] = wanted_modalities

    params = {"filter": "gguf", "limit": 50,
              "sort": HF_SORT_MODES.get(sort, "trendingScore"), "direction": "-1"}
    # One pipeline tag per query; extra required modalities filter locally.
    if wanted_modalities:
        params["pipeline_tag"] = HF_PIPELINE_FOR_MODALITY[wanted_modalities[0]]
    if "coding" in profile["use_cases"]:
        params["search"] = "coder"
    cache_key = json.dumps(params, sort_keys=True)
    rows = _hf_api_get(params, cache_key)
    if rows is None:
        return {"specs": specs, "profile": profile, "sort": sort, "results": [],
                "source": "offline", "detail": "Hugging Face unreachable — showing the offline catalog below."}

    results = []
    for repo in rows:
        if sort == "recent" and repo.get("downloads", 0) < _RECENT_MIN_DOWNLOADS:
            continue
        entry = _hf_repo_to_entry(repo)
        if not entry or not _valid_hf_id(entry["id"]):
            continue
        entry = apply_quant_plan(entry, budget)
        if entry["fit"] == "too_big":
            continue  # step 3: drop what can't run here, even at Q2_K
        entry["est_tps"] = estimate_tokens_per_sec(
            entry["download_gb"], specs.get("backend", "cpu"), entry["fit"])
        entry["modalities"] = wanted_modalities if params.get("pipeline_tag") else []
        results.append(entry)

    results.sort(key=lambda m: _rank_key(m, profile), reverse=True)
    return {"specs": specs, "profile": profile, "sort": sort,
            "results": results[:limit], "source": "huggingface"}


# ===== Trending on Hugging Face =====
#
# Besides the curated daily catalog, the Hub shows what the community is
# actually downloading right now, straight from Hugging Face's public API —
# no compile-time snapshot, it updates itself. Ollama can pull GGUF repos
# directly (``ollama pull hf.co/<repo>:Q4_K_M``), so these are one-click
# installable through the same pull endpoint as catalog models.

HF_TRENDING_URL = "https://huggingface.co/api/models"
HF_CACHE_PATH = os.path.join(CARROT_DIR, "config", "hub_hf_trending.json")
_PARAMS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*[bB]\b")


def _hf_repo_to_entry(repo: dict) -> Optional[dict]:
    """Turn an HF API row into a catalog-shaped entry, or None if we can't
    size it (no parameter count in the name means no honest fit estimate)."""
    repo_id = repo.get("id") or ""
    if "/" not in repo_id:
        return None
    match = _PARAMS_RE.search(repo_id.split("/")[-1].replace("-", " ").replace("_", " "))
    if not match:
        return None
    params_b = float(match.group(1))
    if params_b <= 0 or params_b > 500:
        return None
    # Q4_K_M weights are ~0.6 GB per B params; running needs headroom for
    # the KV cache and runtime on top. (Callers on the live path re-plan
    # the quant per machine with apply_quant_plan.)
    download_gb = round(params_b * 0.6, 1)
    name_lower = repo_id.lower()
    use_cases = []
    if any(k in name_lower for k in ("coder", "code", "codellama")):
        use_cases.append("coding")
    if any(k in name_lower for k in ("-r1", "reason", "think", "qwq", "math")):
        use_cases.append("reasoning")
    if any(k in name_lower for k in ("instruct", "chat", "assistant")):
        use_cases.append("chat")
    return {
        "id": f"hf.co/{repo_id}:Q4_K_M",
        "label": repo_id.split("/")[-1],
        "family": "huggingface",
        "params_b": params_b,
        "quant": "Q4_K_M",
        "download_gb": download_gb,
        "min_mem_gb": round(params_b * 0.75 + 1.5, 1),
        "tier": "power" if params_b >= 12 else ("balanced" if params_b >= 4 else "light"),
        "use_cases": use_cases,
        "modalities": [],
        "blurb": f"{repo.get('downloads', 0):,} downloads on Hugging Face.",
        "hf_url": f"https://huggingface.co/{repo_id}",
        "downloads": repo.get("downloads", 0),
    }


def fetch_hf_trending(limit: int = 12, force: bool = False) -> list:
    """Most-downloaded GGUF repos from the public HF API, cached for a day."""
    try:
        with open(HF_CACHE_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
        if _cache_age_hours(cache) <= CATALOG_MAX_AGE_HOURS and isinstance(cache.get("models"), list):
            return cache["models"][:limit]
    except (OSError, json.JSONDecodeError):
        pass
    if _recently_failed("hf") and not force:
        return []
    try:
        resp = requests.get(
            HF_TRENDING_URL,
            params={"filter": "gguf", "sort": "downloads", "direction": "-1", "limit": 40},
            timeout=10,
        )
        resp.raise_for_status()
        entries = []
        for repo in resp.json():
            entry = _hf_repo_to_entry(repo)
            if entry and _valid_hf_id(entry["id"]):
                entries.append(entry)
            if len(entries) >= limit:
                break
        os.makedirs(os.path.dirname(HF_CACHE_PATH), exist_ok=True)
        with open(HF_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": datetime.now(timezone.utc).isoformat(), "models": entries}, f, indent=2)
        _fail_memo.pop("hf", None)
        return entries
    except Exception:
        _note_failure("hf")
        return []


def _valid_hf_id(model_id: str) -> bool:
    return re.fullmatch(r"hf\.co/[\w.\-]+/[\w.\-]+(:[\w.\-]+)?", model_id) is not None


# ===== Fit + recommendations =====

_TIER_RANK = {"light": 0, "balanced": 1, "power": 2}


def fit_level(min_mem_gb: float, budget_gb: float) -> str:
    if budget_gb <= 0:
        return "too_big"
    if min_mem_gb <= budget_gb * 0.8:
        return "great"
    if min_mem_gb <= budget_gb:
        return "good"
    if min_mem_gb <= budget_gb * 1.3:
        return "tight"
    return "too_big"


def annotate_fit(models: list, specs: dict) -> list:
    budget = specs.get("model_budget_gb") or 0
    backend = specs.get("backend") or "cpu"
    out = []
    for m in models:
        entry = dict(m)
        entry["fit"] = fit_level(float(m.get("min_mem_gb", 0)), budget)
        entry["est_tps"] = estimate_tokens_per_sec(
            float(m.get("download_gb") or 0), backend, entry["fit"])
        out.append(entry)
    return out


def _quality_key(m: dict):
    return (_TIER_RANK.get(m.get("tier"), 1), float(m.get("params_b", 0)))


def recommend(models: list, specs: dict) -> dict:
    """Pick one model per role from the entries that fit this machine.

    Roles: ``best`` (strongest comfortable all-rounder — this is what the
    splash preselects), ``light`` (fastest that still fits), and a best
    pick per use case. Falls back to the smallest model when nothing fits
    so a 4 GB machine still gets an answer instead of an empty screen.
    """
    annotated = annotate_fit(models, specs)
    comfortable = [m for m in annotated if m["fit"] in ("great", "good")]
    if not comfortable:
        smallest = min(annotated, key=lambda m: float(m.get("min_mem_gb", 0))) if annotated else None
        return {"best": smallest, "light": smallest, "by_use_case": {}, "fits_anything": False}

    best = max(comfortable, key=_quality_key)
    light = min(comfortable, key=lambda m: float(m.get("min_mem_gb", 0)))
    by_use_case = {}
    for uc in USE_CASES:
        candidates = [m for m in comfortable if uc in (m.get("use_cases") or [])]
        if candidates:
            # On equal quality, a specialist (fewer declared use cases)
            # beats a generalist — coder models win the coding pick.
            by_use_case[uc] = max(
                candidates,
                key=lambda m: (*_quality_key(m), -len(m.get("use_cases") or [])),
            )
    return {"best": best, "light": light, "by_use_case": by_use_case, "fits_anything": True}


def hub_overview(refresh: bool = False) -> dict:
    """Everything the setup splash and the Hub tab need, in one payload."""
    specs = detect_specs()
    catalog = get_catalog(refresh=refresh)
    models = annotate_fit(catalog["models"], specs)
    recs = recommend(catalog["models"], specs)
    hub_url, _ = _catalog_urls()
    return {
        "specs": specs,
        "models": models,
        "recommendations": recs,
        "trending": annotate_fit(fetch_hf_trending(), specs),
        "hub_url": hub_url,
        "browse_url": HUB_BROWSE_URL,
        "catalog_source": catalog["source"],
        "catalog_fetched_at": catalog["fetched_at"],
        "use_cases": USE_CASES,
        "modalities": MODALITIES,
    }
