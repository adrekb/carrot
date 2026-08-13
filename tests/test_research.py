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


# ===== A plan that changes on what it finds =====
#
# The failure these exist for: asked about an aircraft programme, a run found
# that one had crashed and reported it in the single sentence its first search
# results happened to carry — wrong date, and "no reported injuries" when the
# pilot had been taken to hospital. Nothing in the pipeline was broken. The
# plan was written before anything had been read, so no sub-question asked
# what came of the crash, and no amount of reflection inside the sub-questions
# that did exist could have added one.

def _finding(claim):
    return {"claim": claim, "source_ids": ["S1"], "subquestion": "q"}


class TestThePlanGrowsOnWhatItFinds:
    def test_a_finding_that_raises_something_new_adds_a_subquestion(self, isolated_db, monkeypatch):
        monkeypatch.setattr(research, "_ask_json", lambda *a, **k: {"add": [{
            "question": "What were the injuries and cause of the 31 July crash?",
            "prompted_by": "An F-35B crashed on 31 July",
            "sources": ["web"],
        }]})
        added = research.revise_plan(
            "Where does the F-35 programme stand?",
            [{"question": "What is the current procurement rate?"}],
            [_finding("An F-35B crashed on 31 July")],
            "standard", 2, lambda e: None)
        assert len(added) == 1
        assert added[0]["added"] is True
        assert added[0]["prompted_by"] == "An F-35B crashed on 31 July"
        assert added[0]["sources"] == ["web"]

    def test_a_followup_that_points_at_no_finding_is_refused(self, isolated_db, monkeypatch):
        """Without the finding that forced it, this is the model changing the subject."""
        monkeypatch.setattr(research, "_ask_json", lambda *a, **k: {"add": [
            {"question": "What is the history of naval aviation?", "prompted_by": ""},
        ]})
        events = []
        added = research.revise_plan("q", [{"question": "planned"}],
                                     [_finding("something")], "standard", 2, events.append)
        assert added == []
        assert any("ungrounded" in (e.get("detail") or "") for e in events)

    def test_a_subquestion_already_on_the_plan_is_not_added_again(self, isolated_db, monkeypatch):
        """Restating the plan back at us would research the same thing twice."""
        monkeypatch.setattr(research, "_ask_json", lambda *a, **k: {"add": [
            {"question": "What caused the crash on 31 July?", "prompted_by": "a finding"},
        ]})
        added = research.revise_plan("q", [{"question": "What caused the crash on 31 July?"}],
                                     [_finding("a finding")], "standard", 2, lambda e: None)
        assert added == []

    def test_the_same_question_with_its_words_moved_is_still_a_duplicate(self, isolated_db, monkeypatch):
        monkeypatch.setattr(research, "_ask_json", lambda *a, **k: {"add": [
            {"question": "On 31 July, what caused the crash?", "prompted_by": "a finding"},
        ]})
        added = research.revise_plan("q", [{"question": "What caused the crash on 31 July?"}],
                                     [_finding("a finding")], "standard", 2, lambda e: None)
        assert added == []

    def test_no_more_followups_than_there_is_room_for(self, isolated_db, monkeypatch):
        monkeypatch.setattr(research, "_ask_json", lambda *a, **k: {"add": [
            {"question": f"A distinct follow-up number {n} about the crash", "prompted_by": "f"}
            for n in range(9)
        ]})
        added = research.revise_plan("q", [{"question": "planned"}],
                                     [_finding("f")], "standard", 2, lambda e: None)
        assert len(added) == 2

    def test_a_model_that_will_not_answer_leaves_the_plan_as_it_was(self, isolated_db, monkeypatch):
        """A refinement that could fail the run would be a bad trade at any hit rate."""
        monkeypatch.setattr(research, "_ask_json", lambda *a, **k: None)
        assert research.revise_plan("q", [{"question": "planned"}],
                                    [_finding("f")], "standard", 2, lambda e: None) == []

    def test_nothing_is_added_before_anything_has_been_found(self, isolated_db, monkeypatch):
        """No evidence means no grounds — and the run is about to fail honestly anyway."""
        called = []
        monkeypatch.setattr(research, "_ask_json", lambda *a, **k: called.append(1))
        assert research.revise_plan("q", [{"question": "planned"}], [],
                                    "standard", 2, lambda e: None) == []
        assert called == []

    def test_every_depth_can_follow_something_up(self):
        """A depth with no follow-up budget is the old fixed plan under a new name."""
        for name, profile in research.DEPTHS.items():
            assert profile["followups"] >= 1, name


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


def _stub_the_web(monkeypatch):
    """Two readable results for any query, with address resolution stubbed.

    The .test domains deliberately do not resolve and an unresolvable host is
    refused by design, so leaving the real check in would test DNS.
    """
    monkeypatch.setattr(policy, "_is_public_address", lambda host: (True, ""))
    monkeypatch.setattr(research.websearch, "search", lambda query, **kw: [
        {"title": "Result one", "url": "https://a.test/one", "snippet": "about the topic"},
        {"title": "Result two", "url": "https://b.test/two", "snippet": "more on the topic"},
    ])
    monkeypatch.setattr(research.websearch, "fetch", lambda url, **kw: {
        "url": url, "final_url": url, "title": "A page", "text": "The pilot was hospitalised.",
        "links": [], "error": "", "screening": {"tainted": False, "signals": [], "origin": url},
        "tainted": False, "truncated": False,
    })


FOLLOW_UP = "What were the injuries and cause of the 31 July crash?"


def test_a_followup_is_researched_with_the_whole_machinery(isolated_db, fake_ollama, monkeypatch):
    """The point of mutating the plan: the follow-up gets its own searches.

    Answering it from the pages already read would be the same run with a
    longer prompt — and those pages are the ones that only said a crash had
    happened, which is how "no reported injuries" got written down.
    """
    _stub_the_web(monkeypatch)
    prompts = []

    def fake_ask(task, prompt, **kwargs):
        prompts.append(prompt)
        if "WHAT THE RESEARCHERS ACTUALLY FOUND" in prompt:
            # Only ever offer the follow-up once; the second call sees it is
            # already on the plan and would be refused as a duplicate anyway.
            return {"add": [{"question": FOLLOW_UP,
                             "prompted_by": "An F-35B crashed on 31 July",
                             "sources": ["web"]}]}
        if "Break this research question" in prompt:
            return {"subquestions": [{"question": "What is the programme status?",
                                      "rationale": "r", "sources": ["web"]}]}
        if "search queries" in prompt:
            return {"queries": ["a query"]}
        if "pull out every statement" in prompt:
            claim = ("The pilot was hospitalised with non-life-threatening injuries"
                     if FOLLOW_UP in prompt else "An F-35B crashed on 31 July")
            return {"findings": [{"claim": claim, "sources": ["S1"], "confidence": 0.9}]}
        if "still unanswered" in prompt:
            return {"gaps": []}
        if "supports it" in prompt:
            return {"verdicts": [{"claim": n, "verdict": "supported"} for n in (1, 2)]}
        return None

    monkeypatch.setattr(research, "_ask_json", fake_ask)
    events = list(research.run_research_stream("Where does the F-35 stand?", depth="quick"))

    added = [event["plan_added"] for event in events if "plan_added" in event]
    assert added and added[0][0]["question"] == FOLLOW_UP

    # A researcher really ran it: the follow-up reached an extraction prompt,
    # which only happens after its own searches have been read.
    assert any("pull out every statement" in p and FOLLOW_UP in p for p in prompts)

    done = events[-1]
    assert done.get("done") is True
    assert "hospitalised" in done["report"] or done["findings"] == 2

    stored = research.get_run(done["run_id"])
    assert [step["question"] for step in stored["plan"]][-1] == FOLLOW_UP
    assert stored["plan"][-1]["added"] is True
    assert stored["plan"][0].get("added") is not True


def test_the_plan_cannot_keep_growing_forever(isolated_db, fake_ollama, monkeypatch):
    """A model that answers every follow-up with another one walks away from the question."""
    _stub_the_web(monkeypatch)
    counter = {"n": 0}

    def fake_ask(task, prompt, **kwargs):
        if "WHAT THE RESEARCHERS ACTUALLY FOUND" in prompt:
            counter["n"] += 1
            return {"add": [{"question": f"Follow-up number {counter['n']} about the crash",
                             "prompted_by": "a finding"}]}
        if "Break this research question" in prompt:
            return {"subquestions": [{"question": "What is the programme status?",
                                      "rationale": "r", "sources": ["web"]}]}
        if "search queries" in prompt:
            return {"queries": ["a query"]}
        if "pull out every statement" in prompt:
            return {"findings": [{"claim": "A crash happened", "sources": ["S1"], "confidence": 0.9}]}
        if "still unanswered" in prompt:
            return {"gaps": []}
        return None

    monkeypatch.setattr(research, "_ask_json", fake_ask)
    # deep has room for three follow-ups, so the wave cap is what has to stop it.
    events = list(research.run_research_stream("Where does the F-35 stand?", depth="deep"))
    added = [event["plan_added"] for event in events if "plan_added" in event]
    assert len(added) == research.MAX_PLAN_REVISIONS


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


# ===== Source authority =====
#
# The complaint these cover: a figure published by the company it is about and
# a content farm's rewording of that figure used to render identically —
# same `[S3]`, same grey row, same weight in the writer's prompt. Being able to
# trace a claim to a page is worth much less if every page looks alike.

class TestAuthority:
    """Who is speaking, decided from the domain and the question."""

    def test_the_subject_of_the_question_is_first_party(self):
        from carrot import websearch
        verdict = websearch.authority("https://www.gm.com/newsroom/q1",
                                      "What were GM production figures in Q1?")
        assert verdict["tier"] == websearch.TIER_FIRST_PARTY

    def test_a_subdomain_of_the_subject_is_still_first_party(self):
        from carrot import websearch
        verdict = websearch.authority("https://media.gm.com/q1", "GM Q1 figures")
        assert verdict["tier"] == websearch.TIER_FIRST_PARTY

    def test_an_unrelated_company_is_not_first_party(self):
        """gm.com is primary for GM and an interested party for Ford."""
        from carrot import websearch
        verdict = websearch.authority("https://www.gm.com/x", "Ford Q1 figures")
        assert verdict["tier"] != websearch.TIER_FIRST_PARTY

    def test_a_squatter_matching_the_name_is_not_promoted(self):
        """Matching the subject name is exactly what a squatter does.

        If a name match alone were enough, the rule would hand the strongest
        tier to the weakest source — the check easiest to game deciding the
        outcome.
        """
        from carrot import websearch
        verdict = websearch.authority("https://gm.blogspot.com/figures",
                                      "GM Q1 figures")
        assert verdict["tier"] == websearch.TIER_LOW

    def test_government_domains_are_official(self):
        from carrot import websearch
        assert websearch.authority("https://www.bls.gov/x", "unemployment")["tier"] \
            == websearch.TIER_OFFICIAL

    def test_a_known_outlet_is_reputable_not_primary(self):
        from carrot import websearch
        assert websearch.authority("https://www.reuters.com/x", "GM Q1 figures")["tier"] \
            == websearch.TIER_REPUTABLE

    def test_the_long_tail_is_unknown_rather_than_rejected(self):
        """An unrecognised site is usable and cited — it is just not a confirmation."""
        from carrot import websearch
        assert websearch.authority("https://someones-blog.net/x", "GM Q1")["tier"] \
            == websearch.TIER_UNKNOWN

    def test_best_tier_of_nothing_is_the_weakest(self):
        from carrot import websearch
        assert websearch.best_tier([]) == websearch.TIER_LOW
        assert websearch.best_tier(["low", "reputable", "unknown"]) == "reputable"


class TestTierReachesTheReport:
    """A tier nobody downstream can see is the same as no tier at all."""

    def test_sources_carry_their_tier_into_the_store(self, isolated_db):
        store = research.SourceStore(research.create_run("q", "quick"),
                                     "GM Q1 production figures")
        farm = store.add("web", "https://cars.blogspot.com/x", "Farm", "", "body")
        gm = store.add("web", "https://www.gm.com/x", "GM", "", "body")
        from carrot import websearch
        assert farm["tier"] == websearch.TIER_LOW
        assert gm["tier"] == websearch.TIER_FIRST_PARTY

    def test_the_users_own_files_are_first_party_to_them(self, isolated_db):
        """A paper you indexed outranks a wire service for a question about it."""
        from carrot import websearch
        store = research.SourceStore(research.create_run("q", "quick"), "my thesis")
        doc = store.add("document", "/home/me/thesis.pdf#chunk1", "thesis", "", "body")
        assert doc["tier"] == websearch.TIER_FIRST_PARTY

    def test_a_finding_records_the_best_tier_it_rests_on(self, isolated_db):
        from carrot import websearch
        store = research.SourceStore(research.create_run("q", "quick"), "GM Q1")
        store.add("web", "https://cars.blogspot.com/x", "Farm", "", "body")   # S1 low
        store.add("web", "https://www.reuters.com/x", "Reuters", "", "body")  # S2
        parsed = {"findings": [
            {"claim": "farm only", "sources": ["S1"]},
            {"claim": "both", "sources": ["S1", "S2"]},
        ]}
        rows = research._finding_rows(parsed, store.known_ids(), "q", store)
        assert rows[0]["tier"] == websearch.TIER_LOW
        assert rows[1]["tier"] == websearch.TIER_REPUTABLE

    def test_weakly_sourced_claims_are_marked_for_the_writer(self, isolated_db):
        """Verified but weak is a different problem from unverified.

        The claim is not dropped — the page really does say it. It is flagged,
        so the writer attributes it instead of asserting it.
        """
        store = research.SourceStore(research.create_run("q", "quick"), "GM Q1")
        store.add("web", "https://cars.blogspot.com/x", "Farm", "", "body")
        findings = [{"subquestion": "q", "claim": "Weak claim",
                     "source_ids": ["S1"], "verdict": "supported", "tier": "low"}]
        prompt = research.build_report_prompt("Q?", findings, store, [])
        assert "Weak claim" in prompt
        assert "WEAK SOURCING" in prompt
        assert "rest only on unrecognised or content-farm sources" in prompt

    def test_a_strong_claim_is_not_marked(self, isolated_db):
        store = research.SourceStore(research.create_run("q", "quick"), "GM Q1")
        store.add("web", "https://www.gm.com/x", "GM", "", "body")
        findings = [{"subquestion": "q", "claim": "Strong claim",
                     "source_ids": ["S1"], "verdict": "supported",
                     "tier": "first-party"}]
        prompt = research.build_report_prompt("Q?", findings, store, [])
        assert "WEAK SOURCING" not in prompt

    def test_the_appendix_says_what_kind_of_source_each_one_is(self, isolated_db):
        store = research.SourceStore(research.create_run("q", "quick"), "GM Q1")
        store.add("web", "https://cars.blogspot.com/x", "Farm", "", "body")
        store.add("web", "https://www.gm.com/x", "GM", "", "body")
        appendix = research.source_appendix(store)
        assert "first-party" in appendix and "low" in appendix
        # Strongest first, so the reading order is an argument about weight
        # rather than an accident of which page the backend returned first.
        assert appendix.index("[S2]") < appendix.index("[S1]")

    def test_the_extractor_is_told_who_is_speaking(self, isolated_db):
        store = research.SourceStore(research.create_run("q", "quick"), "GM Q1")
        source = store.add("web", "https://cars.blogspot.com/x", "Farm", "", "body")
        block = research._evidence_block([source], 100)
        assert "Source type: low" in block


# ===== Strong sources that disagree with each other =====
#
# The case that exposed this: GM's June 2025 launch release calls the car a
# 2026 Corvette ZR1X; GM's own current product pages have partly moved to 2027.
# Both are first-party. Verification passes both, because each really does say
# what it says. Tier cannot separate them. So the writer received two
# established facts, picked one, and produced an answer that was accurate about
# its citation and wrong about the world — the most expensive kind of wrong,
# because it checks out.

class TestCurrency:
    """Authority does not expire. Currency does. They are different fields."""

    def test_a_page_states_its_own_date(self):
        from bs4 import BeautifulSoup
        from carrot import websearch
        html = ('<html><head><meta property="article:published_time" '
                'content="2025-06-17T09:00:00Z"></head><body>x</body></html>')
        assert websearch.page_date(BeautifulSoup(html, "html.parser")) == "2025-06-17"

    def test_the_url_is_the_fallback_not_the_first_choice(self):
        from bs4 import BeautifulSoup
        from carrot import websearch
        soup = BeautifulSoup("<html></html>", "html.parser")
        assert websearch.page_date(soup, "https://news.gm.com/2025/jun/0617-zr1x") \
            == "2025-06-17"

    def test_an_undated_page_says_so_rather_than_guessing(self, isolated_db):
        store = research.SourceStore(research.create_run("q", "quick"), "GM")
        source = store.add("web", "https://example.test/x", "T", "", "body")
        assert "undated" in research._tier_label(source)

    def test_the_date_reaches_the_writer(self, isolated_db):
        store = research.SourceStore(research.create_run("q", "quick"), "GM ZR1X")
        store.add("web", "https://www.gm.com/a", "Launch release", "", "body",
                  published="2025-06-17")
        findings = [{"subquestion": "q", "claim": "It is a 2026 model",
                     "source_ids": ["S1"], "verdict": "supported",
                     "tier": "first-party"}]
        prompt = research.build_report_prompt("Q?", findings, store, [])
        assert "2025-06-17" in prompt
        assert "Authority does not expire but currency does" in prompt


class TestConflicts:
    """A third pass, with its own narrow job: do the survivors agree?"""

    LAUNCH = {"subquestion": "model year", "claim": "The ZR1X is a 2026 model year car",
              "source_ids": ["S1"], "verdict": "supported", "tier": "first-party"}
    CURRENT = {"subquestion": "model year", "claim": "The ZR1X is a 2027 model year car",
               "source_ids": ["S2"], "verdict": "supported", "tier": "first-party"}

    def test_a_conflict_between_supported_claims_is_found(self, monkeypatch):
        monkeypatch.setattr(research, "_ask_json", lambda *a, **k: {"conflicts": [
            {"claims": [1, 2], "subject": "model year",
             "note": "GM's own pages give different years"},
        ]})
        found = research.find_conflicts([self.LAUNCH, self.CURRENT], lambda e: None)
        assert len(found) == 1
        assert found[0]["subject"] == "model year"
        assert found[0]["tiers"] == ["first-party", "first-party"]

    def test_a_conflict_naming_a_claim_that_does_not_exist_is_dropped(self, monkeypatch):
        """Same treatment as a citation to a source that does not exist."""
        monkeypatch.setattr(research, "_ask_json", lambda *a, **k: {"conflicts": [
            {"claims": [1, 99], "subject": "nonsense"},
            {"claims": [1, 1], "subject": "itself"},
            {"claims": ["x", "y"], "subject": "unparseable"},
        ]})
        assert research.find_conflicts([self.LAUNCH, self.CURRENT], lambda e: None) == []

    def test_one_claim_cannot_conflict(self):
        """No model call is worth making to compare a list of one."""
        assert research.find_conflicts([self.LAUNCH], lambda e: None) == []

    def test_rejected_claims_are_not_compared(self, monkeypatch):
        """A claim the sources did not support is not evidence of a dispute."""
        called = []
        monkeypatch.setattr(research, "_ask_json",
                            lambda *a, **k: called.append(1) or {"conflicts": []})
        dropped = {**self.CURRENT, "verdict": "unsupported"}
        assert research.find_conflicts([self.LAUNCH, dropped], lambda e: None) == []
        assert not called, "the model was asked to compare one usable claim"

    def test_a_broken_conflict_pass_costs_the_caveats_not_the_report(self, monkeypatch):
        monkeypatch.setattr(research, "_ask_json", lambda *a, **k: "not json at all")
        assert research.find_conflicts([self.LAUNCH, self.CURRENT], lambda e: None) == []

    def test_same_tier_conflicts_forbid_picking_a_winner(self, isolated_db):
        """The rule the ZR1X answer needed and did not have.

        Told only to prefer the stronger source, a writer facing two
        first-party pages picks whichever it read first and writes it as
        settled — accurate about its citation, wrong about the world.
        """
        store = research.SourceStore(research.create_run("q", "quick"), "GM ZR1X")
        store.add("web", "https://www.gm.com/a", "Launch", "", "b", published="2025-06-17")
        store.add("web", "https://www.gm.com/b", "Current", "", "b", published="2026-08-01")
        conflicts = [{
            "claims": ["It is a 2026 model", "It is a 2027 model"],
            "tiers": ["first-party", "first-party"],
            "sources": [["S1"], ["S2"]],
            "subject": "model year", "note": "GM's own pages differ",
        }]
        prompt = research.build_report_prompt(
            "What model year?", [self.LAUNCH, self.CURRENT], store, [], conflicts)
        assert "CONFLICT" in prompt
        assert "there is no stronger one" in prompt
        assert "Do NOT pick one" in prompt

    def test_a_cross_tier_conflict_still_prefers_the_stronger(self, isolated_db):
        store = research.SourceStore(research.create_run("q", "quick"), "GM ZR1X")
        store.add("web", "https://www.gm.com/a", "GM", "", "b")
        conflicts = [{
            "claims": ["1250 hp", "1100 hp"],
            "tiers": ["first-party", "low"],
            "sources": [["S1"], ["S2"]],
            "subject": "power output", "note": "a farm has a different figure",
        }]
        prompt = research.build_report_prompt("Q?", [self.LAUNCH], store, [], conflicts)
        assert "Lead with the stronger source" in prompt
        assert "Do NOT pick one" not in prompt


class TestNothingHereIsAboutTime:
    """The last gap in the pipeline, from a real run.

    A report said the F-35's PTMU contract award was "imminent — in the next
    few months (mid-2026)" and cited Defense Daily. The article was real, it
    genuinely discussed PTMU, and the claim summarised it faithfully. It was
    published in July 2024 and described a contract expected that autumn.

    The source was reputable. The claim was supported by its text. Nothing
    contradicted it, because nothing else in the evidence was about PTMU at
    all. Every check passed — because authority, support and consistency are
    none of them about time.
    """

    def _store_and_findings(self, isolated_db):
        store = research.SourceStore(research.create_run("q", "quick"), "F-35")
        store.add("web", "https://defensedaily.test/ptmu", "PTMU", "", "b",
                  published="2024-07-22")
        store.add("web", "https://gao.gov/x", "GAO", "", "b", published="2026-06-01")
        store.add("web", "https://airforcetimes.test/y", "AFT", "", "b",
                  published="2026-06-24")
        findings = [
            {"id": "a", "subquestion": "q", "verdict": "supported", "tier": "reputable",
             "claim": "The PTMU contract award is imminent", "source_ids": ["S1"]},
            {"id": "b", "subquestion": "q", "verdict": "supported", "tier": "official",
             "claim": "Full mission capable rate fell to 25%", "source_ids": ["S2"]},
        ]
        return store, findings

    def test_a_claim_from_a_much_older_page_is_flagged(self, isolated_db):
        store, findings = self._store_and_findings(isolated_db)
        stale = research.stale_findings(findings, store)
        assert [s["claim"] for s in stale] == ["The PTMU contract award is imminent"]
        assert stale[0]["days_behind"] > 600

    def test_a_current_claim_is_left_alone(self, isolated_db):
        store, findings = self._store_and_findings(isolated_db)
        claims = {s["claim"] for s in research.stale_findings(findings, store)}
        assert "Full mission capable rate fell to 25%" not in claims

    def test_it_compares_against_the_evidence_not_against_today(self, isolated_db):
        """If every page found is from 2019 the subject is simply old, and
        flagging all of them says nothing."""
        store = research.SourceStore(research.create_run("q", "quick"), "history")
        store.add("web", "https://a.test/1", "A", "", "b", published="2019-01-01")
        store.add("web", "https://a.test/2", "B", "", "b", published="2019-03-01")
        findings = [
            {"id": "a", "subquestion": "q", "claim": "old thing",
             "source_ids": ["S1"], "verdict": "supported"},
            {"id": "b", "subquestion": "q", "claim": "other old thing",
             "source_ids": ["S2"], "verdict": "supported"},
        ]
        assert research.stale_findings(findings, store) == []

    def test_undated_sources_do_not_produce_a_verdict(self, isolated_db):
        """Reporting everything as stale because no date could be read would
        be worse than silence."""
        store = research.SourceStore(research.create_run("q", "quick"), "x")
        store.add("web", "https://a.test/1", "A", "", "b")
        findings = [{"id": "a", "subquestion": "q", "claim": "c",
                     "source_ids": ["S1"], "verdict": "supported"}]
        assert research.stale_findings(findings, store) == []

    def test_a_claims_newest_source_is_what_counts(self, isolated_db):
        """A claim corroborated by something current is not stale just
        because it also cites something old."""
        store = research.SourceStore(research.create_run("q", "quick"), "x")
        store.add("web", "https://a.test/old", "Old", "", "b", published="2024-01-01")
        store.add("web", "https://a.test/new", "New", "", "b", published="2026-06-01")
        findings = [{"id": "a", "subquestion": "q", "claim": "c",
                     "source_ids": ["S1", "S2"], "verdict": "supported"}]
        assert research.stale_findings(findings, store) == []

    def test_the_writer_is_told_not_to_state_it_as_current(self, isolated_db):
        store, findings = self._store_and_findings(isolated_db)
        stale = research.stale_findings(findings, store)
        prompt = research.build_report_prompt("Q?", findings, store, [], None, stale)
        assert "OUT OF DATE" in prompt
        assert "may be written as the current position" in prompt
        # The specific wording that goes wrong worst on an old page.
        assert "imminent" in prompt
