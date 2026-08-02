"""Tests for Carrot Research.

The pipeline's promise is that a claim in the report can be traced to text that
was actually read. Most of these tests are about the places that promise could
quietly break: uncited claims slipping through, a fabricated source id being
treated as real, or a claim that failed verification reaching the writer.
"""
import json

import pytest

from carrot import policy, research


# ===== Tolerant JSON parsing =====

@pytest.mark.parametrize("raw,expected", [
    ('{"a": 1}', {"a": 1}),
    ('```json\n{"a": 1}\n```', {"a": 1}),
    ('Sure! Here you go:\n{"a": 1}\nHope that helps.', {"a": 1}),
    ('[1, 2, 3]', [1, 2, 3]),
    ('{"text": "a } brace in a string"}', {"text": "a } brace in a string"}),
    ('{"nested": {"deep": true}}', {"nested": {"deep": True}}),
    ("no json here at all", None),
    ("", None),
])
def test_extract_json_survives_what_small_models_produce(raw, expected):
    assert research.extract_json(raw) == expected


# ===== Findings validation =====

def test_uncited_claims_are_dropped():
    """A claim with no source is the model talking from memory."""
    parsed = {"findings": [
        {"claim": "Supported thing", "sources": ["S1"], "confidence": 0.9},
        {"claim": "Remembered thing", "sources": [], "confidence": 0.9},
        {"claim": "Also remembered"},
    ]}
    findings = research._finding_rows(parsed, ["S1"], "q")
    assert [f["claim"] for f in findings] == ["Supported thing"]


def test_citations_to_sources_that_do_not_exist_are_dropped():
    parsed = {"findings": [{"claim": "Invented citation", "sources": ["S99"]}]}
    assert research._finding_rows(parsed, ["S1", "S2"], "q") == []


def test_partially_invented_citations_keep_only_the_real_ones():
    parsed = {"findings": [{"claim": "Half right", "sources": ["S1", "S42"]}]}
    findings = research._finding_rows(parsed, ["S1"], "q")
    assert findings[0]["source_ids"] == ["S1"]


def test_confidence_is_clamped_and_defaulted():
    parsed = {"findings": [
        {"claim": "a", "sources": ["S1"], "confidence": 5},
        {"claim": "b", "sources": ["S1"], "confidence": "nonsense"},
    ]}
    findings = research._finding_rows(parsed, ["S1"], "q")
    assert findings[0]["confidence"] == 1.0
    assert findings[1]["confidence"] == 0.6


def test_malformed_responses_yield_no_findings():
    assert research._finding_rows(None, ["S1"], "q") == []
    assert research._finding_rows({"findings": "not a list"}, ["S1"], "q") == []


# ===== Evidence store =====

def test_sources_get_stable_ids_and_are_deduplicated(isolated_db):
    store = research.SourceStore(research.create_run("q", "quick"))
    first = store.add("web", "https://a.test/1", "A", "snip", "short body")
    second = store.add("web", "https://b.test/1", "B", "snip", "body")
    again = store.add("web", "https://a.test/1", "A", "snip", "a much longer body this time")

    assert first["id"] == "S1"
    assert second["id"] == "S2"
    assert again["id"] == "S1"
    assert again["content"] == "a much longer body this time"
    assert len(store.all()) == 2


def test_sources_are_persisted_with_their_text(isolated_db):
    from carrot.database import get_db

    conn = get_db()
    conn.execute(
        "INSERT INTO research_runs (id, question, created_at) VALUES ('run-y', 'q', '2026-01-01')"
    )
    conn.commit()
    conn.close()

    store = research.SourceStore("run-y")
    store.add("web", "https://a.test/1", "A", "snip", "the evidence text")

    conn = get_db()
    row = conn.execute("SELECT * FROM research_sources WHERE run_id = 'run-y'").fetchone()
    conn.close()
    assert row["content"] == "the evidence text"
    assert row["ordinal"] == 1


# ===== Planning =====

def test_planning_falls_back_to_the_question_itself(isolated_db, monkeypatch):
    monkeypatch.setattr(research, "_ask_json", lambda *a, **k: None)
    events = []
    plan = research.plan_subquestions("What changed in Python 3.14?", "quick", events.append)
    assert len(plan) == 1
    assert plan[0]["question"] == "What changed in Python 3.14?"


def test_planning_uses_the_model_when_it_answers(isolated_db, monkeypatch):
    monkeypatch.setattr(research, "_ask_json", lambda *a, **k: {"subquestions": [
        {"question": "What is new in the language?", "rationale": "r", "sources": ["web"]},
        {"question": "What did I write about it?", "rationale": "r", "sources": ["local"]},
    ]})
    plan = research.plan_subquestions("What changed in Python 3.14?", "quick", lambda e: None)
    assert [p["sources"] for p in plan] == [["web"], ["local"]]


def test_query_derivation_falls_back_to_the_subquestion(isolated_db, monkeypatch):
    monkeypatch.setattr(research, "_ask_json", lambda *a, **k: None)
    assert research.derive_queries("Is X true?", "quick") == ["Is X true?"]


def test_query_derivation_uses_gaps_on_later_rounds(isolated_db, monkeypatch):
    monkeypatch.setattr(research, "_ask_json", lambda *a, **k: None)
    assert research.derive_queries("Is X true?", "quick", gaps=["what about Y"]) == ["what about Y"]


# ===== Verification =====

def test_verification_records_a_verdict_per_claim(isolated_db, monkeypatch):
    store = research.SourceStore(research.create_run("q", "quick"))
    store.add("web", "https://a.test", "A", "", "The sky is blue on clear days.")

    findings = [
        {"id": "1", "subquestion": "q", "claim": "The sky is blue", "source_ids": ["S1"],
         "confidence": 0.8, "verdict": "unchecked"},
        {"id": "2", "subquestion": "q", "claim": "The sky is green", "source_ids": ["S1"],
         "confidence": 0.8, "verdict": "unchecked"},
    ]
    monkeypatch.setattr(research, "_ask_json", lambda *a, **k: {"verdicts": [
        {"claim": 1, "verdict": "supported"},
        {"claim": 2, "verdict": "contradicted"},
    ]})

    verified = research.verify_findings(findings, store, lambda e: None)
    assert [f["verdict"] for f in verified] == ["supported", "contradicted"]


def test_verification_leaves_claims_unchecked_when_the_model_fails(isolated_db, monkeypatch):
    """A failed verification must not silently promote a claim to supported."""
    store = research.SourceStore(research.create_run("q", "quick"))
    store.add("web", "https://a.test", "A", "", "text")
    findings = [{"id": "1", "subquestion": "q", "claim": "c", "source_ids": ["S1"],
                 "confidence": 0.8, "verdict": "unchecked"}]
    monkeypatch.setattr(research, "_ask_json", lambda *a, **k: None)
    assert research.verify_findings(findings, store, lambda e: None)[0]["verdict"] == "unchecked"


def test_out_of_range_verdicts_are_ignored(isolated_db, monkeypatch):
    store = research.SourceStore(research.create_run("q", "quick"))
    store.add("web", "https://a.test", "A", "", "text")
    findings = [{"id": "1", "subquestion": "q", "claim": "c", "source_ids": ["S1"],
                 "confidence": 0.8, "verdict": "unchecked"}]
    monkeypatch.setattr(research, "_ask_json", lambda *a, **k: {"verdicts": [
        {"claim": 99, "verdict": "supported"},
        {"claim": "nonsense", "verdict": "supported"},
    ]})
    assert research.verify_findings(findings, store, lambda e: None)[0]["verdict"] == "unchecked"


# ===== Synthesis input =====

def test_rejected_claims_are_kept_out_of_the_report_prompt(isolated_db):
    store = research.SourceStore(research.create_run("q", "quick"))
    store.add("web", "https://a.test", "Title A", "", "body")

    findings = [
        {"subquestion": "q", "claim": "Verified claim", "source_ids": ["S1"], "verdict": "supported"},
        {"subquestion": "q", "claim": "Rejected claim", "source_ids": ["S1"], "verdict": "unsupported"},
    ]
    prompt = research.build_report_prompt("Q?", findings, store, [])

    assert "Verified claim" in prompt
    assert "must NOT appear in the report" in prompt
    # The rejected claim appears only inside the exclusion list, never in the
    # findings the writer is told to use.
    findings_section, exclusion_section = prompt.split("must NOT appear in the report")
    assert "Rejected claim" not in findings_section
    assert "Rejected claim" in exclusion_section


def test_hostile_sources_are_flagged_to_the_writer(isolated_db):
    store = research.SourceStore(research.create_run("q", "quick"))
    store.add("web", "https://evil.test", "Evil", "", "body", tainted=True)
    findings = [{"subquestion": "q", "claim": "c", "source_ids": ["S1"], "verdict": "supported"}]
    prompt = research.build_report_prompt(
        "Q?", findings, store, [{"signal": "tried to reassign the role"}]
    )
    assert "prompt injection" in prompt
    assert "Caveats" in prompt


def test_source_appendix_lists_every_source(isolated_db):
    store = research.SourceStore(research.create_run("q", "quick"))
    store.add("web", "https://a.test", "Title A", "", "body")
    store.add("document", "/home/me/paper.pdf#chunk2", "paper.pdf", "", "body", tainted=True)
    appendix = store and research.source_appendix(store)
    assert "[S1]" in appendix and "[S2]" in appendix
    assert "prompt injection" in appendix


# ===== End to end =====

def test_full_pipeline_produces_a_cited_report(isolated_db, fake_ollama, monkeypatch):
    """Plan, research two sub-questions, verify, and write — with the web stubbed.

    Address resolution is stubbed too: the .test domains below deliberately do
    not resolve, and an unresolvable host is refused by design, so leaving the
    real check in place would test DNS rather than the pipeline.
    """
    monkeypatch.setattr(policy, "_is_public_address", lambda host: (True, ""))
    monkeypatch.setattr(research.websearch, "search", lambda query, max_results=6: [
        {"title": "Result one", "url": "https://a.test/one", "snippet": "about the topic"},
        {"title": "Result two", "url": "https://b.test/two", "snippet": "more on the topic"},
    ])
    monkeypatch.setattr(research.websearch, "fetch", lambda url, max_chars=6000: {
        "url": url, "final_url": url, "title": "A page", "text": "The answer is 42.",
        "links": [], "error": "", "screening": {"tainted": False, "signals": [], "origin": url},
        "tainted": False, "truncated": False,
    })

    def fake_ask(task, prompt, **kwargs):
        if "sub-questions" in prompt:
            return {"subquestions": [{"question": "Sub one", "rationale": "r", "sources": ["web"]}]}
        if "search queries" in prompt:
            return {"queries": ["a query"]}
        if "pull out every statement" in prompt:
            return {"findings": [{"claim": "The answer is 42", "sources": ["S1"], "confidence": 0.9}]}
        if "still unanswered" in prompt:
            return {"gaps": []}
        if "supports it" in prompt:
            return {"verdicts": [{"claim": 1, "verdict": "supported"}]}
        return None

    monkeypatch.setattr(research, "_ask_json", fake_ask)

    events = list(research.run_research_stream("What is the answer?", depth="quick"))
    kinds = {key for event in events for key in event}

    assert "run_id" in kinds
    assert "plan" in kinds
    assert "source" in kinds
    assert "finding" in kinds
    assert "verdict" in kinds

    done = events[-1]
    assert done.get("done") is True
    assert done["sources"] >= 1
    assert done["findings"] == 1
    assert "## Sources" in done["report"]

    stored = research.get_run(done["run_id"])
    assert stored["status"] == "complete"
    assert stored["findings"][0]["verdict"] == "supported"
    assert stored["sources"][0]["id"] == "S1"


def test_a_run_with_no_readable_sources_fails_honestly(isolated_db, fake_ollama, monkeypatch):
    monkeypatch.setattr(research.websearch, "search", lambda *a, **k: [])
    monkeypatch.setattr(research, "search_local", lambda *a, **k: [])
    monkeypatch.setattr(research, "_ask_json", lambda *a, **k: None)

    events = list(research.run_research_stream("Unanswerable?", depth="quick"))
    assert "error" in events[-1]
    assert research.list_runs()[0]["status"] == "failed"


def test_private_urls_are_never_fetched_by_a_researcher(isolated_db, fake_ollama, monkeypatch):
    """A search result pointing at the LAN is skipped, not read."""
    monkeypatch.setattr(research.websearch, "search", lambda *a, **k: [
        {"title": "Router", "url": "http://192.168.1.1/admin", "snippet": ""},
    ])
    fetched = []
    monkeypatch.setattr(research.websearch, "fetch",
                        lambda url, **k: fetched.append(url) or {"error": "should not happen"})
    monkeypatch.setattr(research, "_ask_json", lambda *a, **k: None)
    monkeypatch.setattr(research, "search_local", lambda *a, **k: [])

    list(research.run_research_stream("anything", depth="quick"))
    assert fetched == []


def test_an_empty_question_is_rejected(isolated_db):
    assert list(research.run_research_stream("   "))[0]["error"]


def test_runs_can_be_listed_and_deleted(isolated_db):
    run_id = research.create_run("A question", "quick")
    research.finish_run(run_id, "complete", report="# Report")

    assert research.list_runs()[0]["id"] == run_id
    assert research.get_run(run_id)["report"] == "# Report"
    assert research.delete_run(run_id) is True
    assert research.get_run(run_id) is None


# ===== Search relevance guard =====
#
# The abandoned duckduckgo-search package now proxies to Bing and returns
# unrelated pages: a real "RTX 4090" research run came back with
# centimetre-to-feet converters, which then got read and cited.

from carrot import websearch as websearch_mod

RTX_QUERY = "RTX 4090 open source LLM inference speed benchmark comparison"


def test_off_topic_results_are_dropped():
    junk = [
        ("CM to Feet Converter", "Convert centimeters to feet easily"),
        ("Centimeters to feet (cm to ft) converter", "cm to ft conversion table"),
        ("TVS Apache RTX: Price, Mileage", "TVS Apache RTX motorcycle specs"),
    ]
    for title, snippet in junk:
        assert not websearch_mod._is_relevant(RTX_QUERY, title, snippet), title


def test_on_topic_results_are_kept():
    good = [
        ("RTX 4090 LLM inference benchmark", "tokens per second for Llama on a 4090"),
        ("Open-source models for 24GB VRAM", "RTX 4090 quantization and inference guide"),
    ]
    for title, snippet in good:
        assert websearch_mod._is_relevant(RTX_QUERY, title, snippet), title


def test_short_queries_stay_permissive():
    """A two-word query has little to match on; don't over-filter it."""
    assert websearch_mod._is_relevant("carrot recipes", "Carrot cake", "the best carrot cake")


def test_search_filters_junk_from_the_backend(monkeypatch):
    monkeypatch.setattr(websearch_mod, "_raw_search", lambda q, n, r: [
        {"title": "CM to Feet Converter", "href": "https://rapidtables.com/cm",
         "body": "convert centimeters to feet"},
        {"title": "RTX 4090 inference benchmark", "href": "https://example.com/bench",
         "body": "LLM tokens per second on the 4090"},
    ])
    results = websearch_mod.search(RTX_QUERY)
    assert [r["url"] for r in results] == ["https://example.com/bench"]


def test_search_returns_empty_when_every_backend_fails(monkeypatch):
    """Better to report nothing than to hand the model unrelated pages."""
    def boom(q, n, r):
        raise RuntimeError("backend down")
    monkeypatch.setattr(websearch_mod, "_raw_search", boom)
    assert websearch_mod.search(RTX_QUERY) == []


def test_raw_search_prefers_the_maintained_client(monkeypatch):
    """ddgs replaced duckduckgo_search; the old one returns junk now."""
    import sys, types

    used = []

    class FakeDDGS:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def text(self, query, **kwargs):
            used.append("ddgs")
            return [{"title": "ok", "href": "https://example.com", "body": "body"}]

    monkeypatch.setitem(sys.modules, "ddgs", types.SimpleNamespace(DDGS=FakeDDGS))
    websearch_mod._raw_search("q", 3, "wt-wt")
    assert used == ["ddgs"]


# ===== Junk URLs =====

def test_homepages_and_signin_pages_are_not_content():
    """A run that 'reads github.com' has read a marketing page."""
    for url in ["https://github.com", "https://github.com/", "https://nvidia.com/",
                "https://github.com/login", "https://example.com/signup",
                "https://site.com/pricing", "https://x.com/about"]:
        assert not websearch_mod.is_content_url(url), url


def test_real_pages_are_content():
    for url in ["https://github.com/ollama/ollama",
                "https://example.com/blog/rtx-4090-benchmarks",
                "https://site.org/?article=42"]:
        assert websearch_mod.is_content_url(url), url


# ===== Search-health reporting =====

def test_run_context_flags_a_dead_search_backend():
    from carrot import policy
    ctx = policy.RunContext("run1", budget=policy.Budget())
    for _ in range(5):
        ctx.note_search(False)
    assert ctx.search_is_broken is False      # not enough evidence yet
    ctx.note_search(False)
    assert ctx.search_is_broken is True


def test_one_good_search_means_the_backend_works():
    from carrot import policy
    ctx = policy.RunContext("run2", budget=policy.Budget())
    for _ in range(9):
        ctx.note_search(False)
    ctx.note_search(True)
    assert ctx.search_is_broken is False


# ===== Depth gating =====

def test_exhaustive_depth_is_cloud_only(monkeypatch):
    from carrot import research

    class LocalRoute:
        local, model = True, "llama3.2:1b"
    monkeypatch.setattr(research.router_mod, "route", lambda *a, **k: LocalRoute())
    info = research.available_depths()
    assert "exhaustive" not in info["depths"]
    assert info["local"] is True

    class CloudRoute:
        local, model = False, "claude-opus-5"
    monkeypatch.setattr(research.router_mod, "route", lambda *a, **k: CloudRoute())
    info = research.available_depths()
    assert "exhaustive" in info["depths"]
    assert info["local"] is False


def test_exhaustive_profile_is_actually_bigger():
    from carrot import research
    deep, exhaustive = research.DEPTHS["deep"], research.DEPTHS["exhaustive"]
    for key in ("subquestions", "rounds", "workers", "reads_per_round", "chars_per_page"):
        assert exhaustive[key] > deep[key], key
