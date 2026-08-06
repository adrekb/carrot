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
    monkeypatch.setattr(hub, "_catalog_urls",
                        lambda: ("https://hub.example", "https://hub.example/catalog.json"))

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
    # No fake Carrot Hub domain is shipped; browsing goes to the real source.
    assert data["hub_url"] == ""
    assert "huggingface.co" in data["browse_url"]
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

    monkeypatch.setattr(hub, "_catalog_urls",
                        lambda: ("https://hub.example", "https://hub.example/catalog.json"))
    assert hub.refresh_catalog() is None
    assert hub.refresh_catalog() is None  # memoized failure, no second request
    assert hub.fetch_hf_trending() == []
    assert hub.fetch_hf_trending() == []
    assert calls["n"] == 2  # one real attempt each
    # A manual refresh bypasses the memo.
    hub.refresh_catalog(force=True)
    assert calls["n"] == 3
    hub._fail_memo.clear()


def test_no_catalog_url_means_no_network_call(tmp_path, monkeypatch):
    """Shipping a default domain that does not exist made every install
    fire a doomed DNS lookup. With nothing configured, do not call out."""
    monkeypatch.setattr(hub, "CATALOG_CACHE_PATH", str(tmp_path / "c.json"))
    hub._fail_memo.clear()
    called = {"n": 0}

    def boom(*a, **k):
        called["n"] += 1
        raise AssertionError("should not reach the network")
    monkeypatch.setattr(hub.requests, "get", boom)
    monkeypatch.setattr(hub, "_catalog_urls", lambda: ("", ""))

    assert hub.refresh_catalog(force=True) is None
    assert called["n"] == 0
    assert hub.get_catalog()["source"] == "bundled"


# ===== Quantization descent =====

def test_quant_descent_uses_best_quality_that_fits():
    # 8B params: Q8_0 weights ~8.6 GB -> ~11 GB running. Plenty of room on 24 GB.
    plan = hub.quant_plan(8.0, 24.0)
    assert plan["quant"] == "Q8_0"
    assert plan["fit"] in ("great", "good")
    # Same model on 8 GB budget must step down just enough, not give up.
    plan_small = hub.quant_plan(8.0, 8.0)
    assert plan_small["quant"] != "Q8_0"
    assert plan_small["min_mem_gb"] <= 8.0
    # Truly hopeless: smallest quant, honestly marked.
    plan_none = hub.quant_plan(70.0, 4.0)
    assert plan_none["quant"] == "Q2_K"
    assert plan_none["fit"] == "too_big"


def test_apply_quant_plan_retags_pull_id():
    entry = {"id": "hf.co/org/Model-8B-GGUF:Q4_K_M", "params_b": 8.0}
    out = hub.apply_quant_plan(entry, 24.0)
    assert out["id"] == "hf.co/org/Model-8B-GGUF:Q8_0"
    assert out["quant_reason"].endswith("Q8_0")


# ===== Workload understanding =====

def test_workload_text_maps_to_use_cases_and_modalities():
    p = hub.workload_to_profile("long conversations and goal tracking, daily updates")
    assert "chat" in p["use_cases"]
    p2 = hub.workload_to_profile("help me debug my python scripts")
    assert "coding" in p2["use_cases"]
    p3 = hub.workload_to_profile("describe screenshots and transcribe voice memos")
    assert set(p3["modalities"]) >= {"image", "audio"}
    assert hub.workload_to_profile("")["use_cases"] == []


# ===== Live thin-client search =====

def _fake_hf_rows():
    return [
        {"id": "org/Chat-Instruct-8B-GGUF", "downloads": 500000},
        {"id": "org/BigChat-70B-GGUF", "downloads": 900000},
        {"id": "org/TinyCoder-3B-GGUF", "downloads": 40000},
        {"id": "org/Fresh-Junk-7B-GGUF", "downloads": 3},
    ]


def test_live_search_plans_quants_filters_and_ranks(monkeypatch):
    monkeypatch.setattr(hub, "detect_specs", lambda: {
        "ram_gb": 16.0, "vram_gb": 0.0, "backend": "cpu", "model_budget_gb": 8.0,
        "os": "Linux", "cpu": "x", "cpu_cores": 8, "gpu": "none",
    })
    monkeypatch.setattr(hub, "_hf_api_get", lambda params, key: _fake_hf_rows())
    out = hub.live_search(workload="long conversations", sort="trending")
    ids = [m["id"] for m in out["results"]]
    # The 70B cannot run on an 8 GB budget even at Q2_K -> dropped entirely.
    assert not any("70B" in i for i in ids)
    # The chat model matches the workload and outranks the coder.
    assert ids[0].startswith("hf.co/org/Chat-Instruct-8B-GGUF")
    # Every survivor got a machine-specific quant plan and speed estimate.
    for m in out["results"]:
        assert m["quant"] in dict((q, g) for q, g in hub.QUANT_LADDER)
        assert m["id"].endswith(":" + m["quant"])
        assert m["fit"] != "too_big" and m["est_tps"]


def test_live_search_recent_mode_has_popularity_floor(monkeypatch):
    monkeypatch.setattr(hub, "detect_specs", lambda: {
        "ram_gb": 16.0, "vram_gb": 0.0, "backend": "cpu", "model_budget_gb": 8.0,
        "os": "Linux", "cpu": "x", "cpu_cores": 8, "gpu": "none",
    })
    monkeypatch.setattr(hub, "_hf_api_get", lambda params, key: _fake_hf_rows())
    out = hub.live_search(sort="recent")
    assert not any("Fresh-Junk" in m["id"] for m in out["results"])


def test_live_search_offline_degrades_gracefully(monkeypatch):
    monkeypatch.setattr(hub, "detect_specs", lambda: {
        "ram_gb": 16.0, "vram_gb": 0.0, "backend": "cpu", "model_budget_gb": 8.0,
        "os": "Linux", "cpu": "x", "cpu_cores": 8, "gpu": "none",
    })
    monkeypatch.setattr(hub, "_hf_api_get", lambda params, key: None)
    out = hub.live_search(workload="coding")
    assert out["source"] == "offline" and out["results"] == []


def test_hub_search_endpoint(client, monkeypatch):
    from carrot import hub as hub_mod
    monkeypatch.setattr(hub_mod, "detect_specs", lambda: {
        "ram_gb": 32.0, "vram_gb": 24.0, "backend": "cuda", "model_budget_gb": 24.0,
        "os": "Linux", "cpu": "x", "cpu_cores": 16, "gpu": "RTX 4090",
    })
    monkeypatch.setattr(hub_mod, "_hf_api_get", lambda params, key: _fake_hf_rows())
    resp = client.get("/api/hub/search?workload=coding&sort=popular")
    assert resp.status_code == 200
    data = resp.json()
    assert data["source"] == "huggingface"
    assert "coding" in data["profile"]["use_cases"]
    # 24 GB of VRAM: the 8B should be planned at Q8_0, not stuck at Q4.
    eight_b = next(m for m in data["results"] if "8B" in m["id"])
    assert eight_b["quant"] == "Q8_0"
