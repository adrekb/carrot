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
    monkeypatch.setattr(research.websearch, "search", lambda query, **kw: [
        {"title": "Result one", "url": "https://a.test/one", "snippet": "about the topic"},
        {"title": "Result two", "url": "https://b.test/two", "snippet": "more on the topic"},
    ])
    monkeypatch.setattr(research.websearch, "fetch", lambda url, **kw: {
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


@pytest.fixture(autouse=True)
def _assume_search_works(monkeypatch):
    """The sandbox has no network. Seed the probe's cache rather than
    replacing the function, so the probe's own tests still exercise it."""
    import time
    monkeypatch.setattr(websearch_mod, "_health_cache",
                        {"value": True, "at": time.monotonic()})

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


# ===== Backend health probe =====

def test_probe_detects_a_backend_returning_unrelated_results(monkeypatch):
    """The exact failure seen in the wild: soup recipes for a CS question."""
    websearch_mod._health_cache.clear()
    monkeypatch.setattr(websearch_mod, "_raw_search", lambda q, n, r: [
        {"title": "Provencal Vegetable Soup Recipe", "body": "Ina Garten, Food Network"},
        {"title": "Target : Expect More. Pay Less.", "body": "shop all categories"},
    ])
    assert websearch_mod.search_backend_healthy(force=True) is False


def test_probe_accepts_a_working_backend(monkeypatch):
    websearch_mod._health_cache.clear()
    monkeypatch.setattr(websearch_mod, "_raw_search", lambda q, n, r: [
        {"title": "Dynamic programming - Wikipedia",
         "body": "a method for solving a complex problem by breaking it into subproblems"},
    ])
    assert websearch_mod.search_backend_healthy(force=True) is True


def test_probe_result_is_cached(monkeypatch):
    websearch_mod._health_cache.clear()
    calls = {"n": 0}

    def counted(q, n, r):
        calls["n"] += 1
        return [{"title": "Dynamic programming algorithm", "body": "wikipedia"}]
    monkeypatch.setattr(websearch_mod, "_raw_search", counted)
    websearch_mod.search_backend_healthy(force=True)
    websearch_mod.search_backend_healthy()
    websearch_mod.search_backend_healthy()
    assert calls["n"] == 1, "one probe should serve a whole run"


# ===== Wikipedia fallback =====

def test_search_all_tops_up_from_wikipedia_when_thin(monkeypatch):
    monkeypatch.setattr(websearch_mod, "search", lambda q, **kw: [
        {"title": "only one", "url": "https://a.test/1", "snippet": "s"}])
    monkeypatch.setattr(websearch_mod, "search_wikipedia", lambda q, max_results=5: [
        {"title": "Dynamic programming", "url": "https://en.wikipedia.org/wiki/Dynamic_programming",
         "snippet": "method"}])
    results = websearch_mod.search_all("dynamic programming")
    assert len(results) == 2
    assert any("wikipedia.org" in r["url"] for r in results)


def test_search_all_leaves_healthy_results_alone(monkeypatch):
    monkeypatch.setattr(websearch_mod, "search", lambda q, **kw: [
        {"title": "a", "url": "https://a.test/1", "snippet": "s"},
        {"title": "b", "url": "https://b.test/2", "snippet": "s"}])
    called = {"wiki": False}

    def wiki(q, max_results=5):
        called["wiki"] = True
        return []
    monkeypatch.setattr(websearch_mod, "search_wikipedia", wiki)
    assert len(websearch_mod.search_all("q")) == 2
    assert called["wiki"] is False


# ===== Domain diversity =====

def test_one_domain_cannot_dominate_a_round(isolated_db, monkeypatch):
    """Four Food Network recipes is one source wearing four hats."""
    from carrot import policy
    monkeypatch.setattr(websearch_mod, "search_all", lambda q, **kw: [
        {"title": f"Recipe {i}", "url": f"https://foodnetwork.com/r{i}", "snippet": "soup"}
        for i in range(5)
    ] + [{"title": "Real", "url": "https://other.test/page", "snippet": "topic"}])
    monkeypatch.setattr(websearch_mod, "fetch", lambda url, **kw: {
        "url": url, "final_url": url, "title": "t", "text": "body", "links": [],
        "error": "", "screening": {"tainted": False, "signals": [], "origin": url},
        "tainted": False, "truncated": False})
    monkeypatch.setattr(policy, "_is_public_address", lambda host: (True, ""))

    run_id = research.create_run("q", "deep")
    ctx = policy.register_run(policy.RunContext(run_id, budget=policy.Budget()))
    store = research.SourceStore(run_id)
    agent = research.Researcher(
        run_id, {"question": "q", "sources": ["web"]}, "deep", store, ctx, lambda e: None)
    gathered = agent._gather_web(["query"])
    hosts = [websearch_mod.host_label(s["locator"]) for s in gathered]
    assert hosts.count("foodnetwork.com") <= 2, hosts


# ===== No module bypasses the ddgs/duckduckgo_search fallback =====
#
# recap.py and deep_research.py used to `from duckduckgo_search import DDGS`
# at the top of the file. duckduckgo_search was dropped from dependencies in
# favor of ddgs, so on a machine that only has ddgs installed, importing
# either module (and therefore importing `carrot` itself, since recap is
# imported in carrot/__init__.py) raised ModuleNotFoundError before the app
# could even start.

def test_no_module_imports_duckduckgo_search_at_top_level():
    import ast
    import pathlib
    carrot_dir = pathlib.Path(__file__).resolve().parent.parent / "carrot"
    offenders = []
    for path in carrot_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:  # top-level only; lazy imports inside
                                 # functions are fine (see websearch._raw_search)
            if isinstance(node, ast.ImportFrom) and node.module == "duckduckgo_search":
                offenders.append(path.name)
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in ("duckduckgo_search", "ddgs"):
                        offenders.append(path.name)
    assert not offenders, (
        f"{offenders} import a DDG client at module load time — this crashes "
        "carrot's whole import chain if that package isn't installed. Route "
        "through carrot.websearch instead."
    )


def test_carrot_package_imports_without_any_ddg_client_installed(monkeypatch):
    """Reproduces the frozen-build crash directly, without needing to
    actually uninstall packages: block both DDG modules from being found."""
    import sys
    import builtins
    real_import = builtins.__import__

    def blocking_import(name, *args, **kwargs):
        if name in ("duckduckgo_search", "ddgs"):
            raise ModuleNotFoundError(f"No module named '{name}'")
        return real_import(name, *args, **kwargs)

    for mod in ("carrot", "carrot.recap", "carrot.deep_research", "carrot.websearch"):
        monkeypatch.delitem(sys.modules, mod, raising=False)
    monkeypatch.setattr(builtins, "__import__", blocking_import)
    import carrot  # noqa: F401 — must not raise


class TestAResearchRunCannotEndInSilence:
    """Same class of defect as the chat one, in the stream deep research uses.

    ``_sse`` wraps every research and agent trace. It had no exception handler,
    and by the time its body runs the 200 and the headers are already sent — so
    a run that died on its ninth page fetch ended as a closed socket: a spinner
    that stops spinning, with nothing anywhere saying what happened.
    """

    def drive(self, generator):
        """Collect everything the SSE wrapper puts on the wire."""
        import asyncio

        from carrot import app as A

        async def collect():
            response = A._sse(generator)
            return "".join([part async for part in response.body_iterator])

        return asyncio.new_event_loop().run_until_complete(collect())

    def exploding(self):
        yield {"type": "step", "text": "reading page 9"}
        raise RuntimeError("the fetch pool died")

    def test_the_work_done_before_the_crash_still_reaches_the_user(self):
        assert "reading page 9" in self.drive(self.exploding())

    def test_the_reason_is_named(self):
        assert "the fetch pool died" in self.drive(self.exploding())

    def test_the_run_is_closed_off_rather_than_left_spinning(self):
        assert '"type": "done"' in self.drive(self.exploding())
