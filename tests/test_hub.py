"""Tests for Carrot Hub — spec detection, fit levels, and recommendations."""
import json

import pytest

from carrot import hub


# ===== Fit levels =====

def test_fit_levels_against_budget():
    assert hub.fit_level(6.0, 12.0) == "great"      # well under 80%
    assert hub.fit_level(11.0, 12.0) == "good"      # under budget
    assert hub.fit_level(14.0, 12.0) == "tight"     # within 1.3x, CPU offload
    assert hub.fit_level(20.0, 12.0) == "too_big"
    assert hub.fit_level(2.0, 0) == "too_big"       # no budget at all


# ===== Recommendations =====

BIG_RIG = {"model_budget_gb": 24.0}
LAPTOP = {"model_budget_gb": 6.0}
POTATO = {"model_budget_gb": 1.0}


def test_big_machine_gets_a_power_model():
    recs = hub.recommend(hub.BUNDLED_CATALOG, BIG_RIG)
    assert recs["fits_anything"] is True
    assert recs["best"]["tier"] == "power"
    assert recs["best"]["min_mem_gb"] <= 24.0
    # Light pick is genuinely lighter than the best pick.
    assert recs["light"]["min_mem_gb"] < recs["best"]["min_mem_gb"]


def test_laptop_never_recommended_beyond_budget():
    recs = hub.recommend(hub.BUNDLED_CATALOG, LAPTOP)
    assert recs["fits_anything"] is True
    for role in ("best", "light"):
        assert recs[role]["min_mem_gb"] <= 6.0
    for pick in recs["by_use_case"].values():
        assert pick["min_mem_gb"] <= 6.0


def test_use_case_picks_match_their_use_case():
    recs = hub.recommend(hub.BUNDLED_CATALOG, BIG_RIG)
    for uc, pick in recs["by_use_case"].items():
        assert uc in pick["use_cases"]


def test_potato_falls_back_to_smallest_model():
    recs = hub.recommend(hub.BUNDLED_CATALOG, POTATO)
    assert recs["fits_anything"] is False
    smallest = min(hub.BUNDLED_CATALOG, key=lambda m: m["min_mem_gb"])
    assert recs["best"]["id"] == smallest["id"]


def test_annotate_fit_marks_every_model():
    annotated = hub.annotate_fit(hub.BUNDLED_CATALOG, LAPTOP)
    assert len(annotated) == len(hub.BUNDLED_CATALOG)
    assert all(m["fit"] in ("great", "good", "tight", "too_big") for m in annotated)


# ===== Spec detection =====

def test_detect_specs_cpu_only(monkeypatch):
    monkeypatch.setattr(hub, "get_hardware_profile", lambda: {
        "os": "Windows", "cpu": "i5", "cpu_cores": 8, "ram_gb": 16.0, "gpu": "Intel UHD",
    })
    monkeypatch.setattr(hub, "_detect_nvidia_vram_gb", lambda: 0.0)
    monkeypatch.setattr(hub, "_is_apple_silicon", lambda: False)
    specs = hub.detect_specs()
    assert specs["backend"] == "cpu"
    assert specs["model_budget_gb"] == 8.0  # half of RAM


def test_detect_specs_cuda(monkeypatch):
    monkeypatch.setattr(hub, "get_hardware_profile", lambda: {
        "os": "Windows", "cpu": "i9", "cpu_cores": 16, "ram_gb": 32.0, "gpu": "RTX 3060",
    })
    monkeypatch.setattr(hub, "_detect_nvidia_vram_gb", lambda: 12.0)
    specs = hub.detect_specs()
    assert specs["backend"] == "cuda"
    assert specs["model_budget_gb"] == 12.0  # VRAM is the budget


def test_detect_specs_apple_silicon(monkeypatch):
    monkeypatch.setattr(hub, "get_hardware_profile", lambda: {
        "os": "Darwin", "cpu": "Apple M2", "cpu_cores": 8, "ram_gb": 16.0, "gpu": "Apple M2",
    })
    monkeypatch.setattr(hub, "_detect_nvidia_vram_gb", lambda: 0.0)
    monkeypatch.setattr(hub, "_is_apple_silicon", lambda: True)
    specs = hub.detect_specs()
    assert specs["backend"] == "metal"
    assert specs["vram_gb"] == 16.0  # unified memory
    assert specs["model_budget_gb"] == pytest.approx(16.0 * 0.65, abs=0.1)


# ===== Catalog: bundled -> cached -> remote =====

def test_catalog_falls_back_to_bundle_when_hub_unreachable(tmp_path, monkeypatch):
    monkeypatch.setattr(hub, "CATALOG_CACHE_PATH", str(tmp_path / "hub_catalog.json"))
    monkeypatch.setattr(hub, "refresh_catalog", lambda: None)
    catalog = hub.get_catalog()
    assert catalog["source"] == "bundled"
    assert catalog["models"] == hub.BUNDLED_CATALOG


def test_refresh_catalog_validates_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(hub, "CATALOG_CACHE_PATH", str(tmp_path / "hub_catalog.json"))

    class FakeResp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self):
            return {"models": [
                {"id": "newmodel:7b", "min_mem_gb": 7.0, "label": "New Model"},
                {"id": "https://evil.example/x", "min_mem_gb": 1.0},  # rejected: not a tag
                {"id": "no-mem-model"},                                # rejected: no min_mem_gb
            ]}

    monkeypatch.setattr(hub.requests, "get", lambda *a, **k: FakeResp())
    models = hub.refresh_catalog()
    assert models == [{"id": "newmodel:7b", "min_mem_gb": 7.0, "label": "New Model"}]

    # And the cache is now the preferred source.
    catalog = hub.get_catalog()
    assert catalog["source"] in ("cache", "hub")
    assert catalog["models"][0]["id"] == "newmodel:7b"


def test_stale_cache_survives_failed_refresh(tmp_path, monkeypatch):
    cache_path = tmp_path / "hub_catalog.json"
    monkeypatch.setattr(hub, "CATALOG_CACHE_PATH", str(cache_path))
    cache_path.write_text(json.dumps({
        "fetched_at": "2000-01-01T00:00:00+00:00",  # ancient
        "models": [{"id": "cached:1b", "min_mem_gb": 2.0}],
    }))
    monkeypatch.setattr(hub, "refresh_catalog", lambda: None)
    catalog = hub.get_catalog()
    assert catalog["source"] == "cache"
    assert catalog["models"][0]["id"] == "cached:1b"


# ===== API =====

def test_hub_endpoint_returns_specs_and_picks(client, monkeypatch):
    from carrot import hub as hub_mod
    monkeypatch.setattr(hub_mod, "detect_specs", lambda: {
        "os": "Linux", "cpu": "x", "cpu_cores": 8, "ram_gb": 16.0, "gpu": "none",
        "vram_gb": 0.0, "backend": "cpu", "model_budget_gb": 8.0,
    })
    monkeypatch.setattr(hub_mod, "refresh_catalog", lambda: None)
    monkeypatch.setattr(hub_mod, "fetch_hf_trending", lambda limit=12: [
        {"id": "hf.co/org/Trend-7B-GGUF:Q4_K_M", "download_gb": 4.2, "min_mem_gb": 6.8},
    ])
    resp = client.get("/api/hub")
    assert resp.status_code == 200
    data = resp.json()
    assert data["specs"]["model_budget_gb"] == 8.0
    assert data["recommendations"]["best"]["min_mem_gb"] <= 8.0
    assert all("fit" in m for m in data["models"])
    assert data["hub_url"].startswith("http")
    assert data["modalities"] == ["image", "audio", "video"]
    assert data["trending"][0]["fit"] in ("great", "good", "tight", "too_big")


def test_hub_choose_sets_active_model(client):
    resp = client.post("/api/hub/choose", json={"model": "llama3.2:3b"})
    assert resp.status_code == 200
    assert resp.json()["active_model"] == "llama3.2:3b"
    from carrot import config
    assert config.get_config()["ollama_model"] == "llama3.2:3b"


def test_bootstrap_run_accepts_chosen_model(client, monkeypatch):
    from carrot import bootstrap
    seen = {}
    def fake_run(progress_cb=None, model=None):
        seen["model"] = model
        return {"ollama_installed": True, "model_pulled": True, "model": model, "error": None}
    monkeypatch.setattr(bootstrap, "run_bootstrap", fake_run)
    resp = client.post("/api/bootstrap/run", json={"model": "phi4:14b"})
    assert resp.status_code == 200
    assert seen["model"] == "phi4:14b"
    # No body still works (Skip path).
    resp = client.post("/api/bootstrap/run")
    assert resp.status_code == 200
    assert seen["model"] is None


# ===== llmfit-inspired extras: speed estimate, HF trending, modalities =====

def test_speed_estimate_scales_with_backend_and_fit():
    fast = hub.estimate_tokens_per_sec(5.0, "cuda", "great")
    slow = hub.estimate_tokens_per_sec(5.0, "cpu", "great")
    assert fast > slow > 0
    # A tight fit is derated hard, and too_big has no estimate.
    assert hub.estimate_tokens_per_sec(5.0, "cpu", "tight") < slow
    assert hub.estimate_tokens_per_sec(5.0, "cpu", "too_big") is None


def test_annotate_fit_includes_speed_estimate():
    annotated = hub.annotate_fit(hub.BUNDLED_CATALOG, {"model_budget_gb": 8.0, "backend": "cpu"})
    runnable = [m for m in annotated if m["fit"] != "too_big"]
    assert runnable and all(isinstance(m["est_tps"], int) for m in runnable)


def test_hf_repo_becomes_installable_ollama_ref():
    entry = hub._hf_repo_to_entry({"id": "TheBloke/Mistral-7B-Instruct-GGUF", "downloads": 12345})
    assert entry["id"] == "hf.co/TheBloke/Mistral-7B-Instruct-GGUF:Q4_K_M"
    assert entry["params_b"] == 7.0
    assert entry["min_mem_gb"] > entry["download_gb"] > 0
    # No parameter count in the name -> no honest fit estimate -> skipped.
    assert hub._hf_repo_to_entry({"id": "someone/mystery-model-GGUF"}) is None


def test_fetch_hf_trending_parses_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(hub, "HF_CACHE_PATH", str(tmp_path / "hf.json"))

    class FakeResp:
        def raise_for_status(self): pass
        def json(self):
            return [
                {"id": "org/CoolModel-8B-GGUF", "downloads": 999},
                {"id": "org/NoParams-GGUF", "downloads": 5},
            ]

    calls = {"n": 0}
    def fake_get(*a, **k):
        calls["n"] += 1
        return FakeResp()
    monkeypatch.setattr(hub.requests, "get", fake_get)

    models = hub.fetch_hf_trending()
    assert [m["label"] for m in models] == ["CoolModel-8B-GGUF"]
    # Second call inside the cache window doesn't hit the network.
    hub.fetch_hf_trending()
    assert calls["n"] == 1


def test_multimodal_entries_declare_modalities():
    by_id = {m["id"]: m for m in hub.BUNDLED_CATALOG}
    assert "image" in by_id["llava:7b"]["modalities"]
    assert "video" in by_id["qwen2.5vl:7b"]["modalities"]
    assert "audio" in by_id["qwen2-audio:7b"]["modalities"]
    assert set(hub.MODALITIES) == {"image", "audio", "video"}


def test_failed_fetch_is_not_retried_immediately(tmp_path, monkeypatch):
    monkeypatch.setattr(hub, "CATALOG_CACHE_PATH", str(tmp_path / "hub_catalog.json"))
    monkeypatch.setattr(hub, "HF_CACHE_PATH", str(tmp_path / "hf.json"))
    hub._fail_memo.clear()
    calls = {"n": 0}
    def fake_get(*a, **k):
        calls["n"] += 1
        raise OSError("offline")
    monkeypatch.setattr(hub.requests, "get", fake_get)

    assert hub.refresh_catalog() is None
    assert hub.refresh_catalog() is None  # memoized failure, no second request
    assert hub.fetch_hf_trending() == []
    assert hub.fetch_hf_trending() == []
    assert calls["n"] == 2  # one real attempt each
    # A manual refresh bypasses the memo.
    hub.refresh_catalog(force=True)
    assert calls["n"] == 3
    hub._fail_memo.clear()
