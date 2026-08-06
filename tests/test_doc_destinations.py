"""Tests for sending a note somewhere other than chat, and for search modes.

Two features that share a theme: the note (or the turn) says what it wants, and
the backend honours it without the user having to remember which button does
what.
"""
import json
import os

import pytest

from carrot import agent_tools, app as carrot_app, doc_agent, research


# ===== Parsing @/to =====

@pytest.mark.parametrize("text,destination,option", [
    ("@/to/research/deep what changed?", "research", "deep"),
    ("@/to/research question", "research", "standard"),
    ("@/to/agent/browser do it", "agent", "browser"),
    ("@/to/agent", "agent", "browser"),
    ("@/to/chat hello", "chat", ""),
    ("no destination here", "chat", ""),
])
def test_destinations_parse(isolated_db, text, destination, option):
    resolved = doc_agent.resolve(text)
    assert resolved.destination == destination
    assert resolved.option == option


def test_an_unknown_destination_is_reported_not_guessed(isolated_db):
    resolved = doc_agent.resolve("@/to/telepathy do the thing")
    assert resolved.destination == "chat"
    assert any("unknown destination" in w for w in resolved.warnings)


def test_an_unknown_option_falls_back_and_says_so(isolated_db):
    """Someone who wrote 'exhaustive' had an expectation; running quietly is worse."""
    resolved = doc_agent.resolve("@/to/research/exhaustive q")
    assert (resolved.destination, resolved.option) == ("research", "standard")
    assert any("not a depth" in w for w in resolved.warnings)


def test_the_last_destination_wins_and_the_rest_are_marked(isolated_db):
    resolved = doc_agent.resolve("@/to/chat first @/to/agent second")
    assert resolved.destination == "agent"
    assert any("2 @/to references" in w for w in resolved.warnings)
    overridden = [r for r in resolved.references if r.kind == "to"][0]
    assert overridden.detail == "overridden by a later @/to"


def test_the_destination_picks_the_routing_task(isolated_db, monkeypatch):
    """A research note routes as research, so it lands on that task's model."""
    seen = {}

    def fake_route(task=None, model=None, provider=None, **kwargs):
        seen["task"] = task
        return doc_agent.router_mod.Route(
            task=task, provider=provider, model=model, local=False, reason="test",
        )

    monkeypatch.setattr(doc_agent.providers_mod, "get_provider", lambda p: {"id": p})
    monkeypatch.setattr(doc_agent.providers_mod, "usable", lambda p: True)
    monkeypatch.setattr(doc_agent.router_mod, "route", fake_route)

    doc_agent.resolve("@/to/research/deep @/model/openai/gpt-x question")
    assert seen["task"] == doc_agent.router_mod.TASK_RESEARCH


def test_destination_candidates_offer_both_forms(isolated_db):
    values = {c["value"] for c in doc_agent.destination_candidates()}
    assert "research" in values
    assert "research/deep" in values
    assert "agent/browser" in values


def test_destination_candidates_filter_by_prefix(isolated_db):
    values = {c["value"] for c in doc_agent.destination_candidates("age")}
    assert values and all(v.startswith("agent") for v in values)


# ===== Citations follow the note =====

@pytest.fixture
def cited_note(isolated_db):
    """A note citing a real file in the agent workspace."""
    workspace = agent_tools.workspace_root()
    path = os.path.join(workspace, "seed_paper.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("The measured throughput was 41 tokens per second.")
    yield "@/to/research/quick How fast is it? See @/file/seed_paper.md"
    os.remove(path)


def test_cited_files_become_research_evidence(cited_note):
    resolved = doc_agent.resolve(cited_note)
    seeds = resolved.seed_sources()

    assert len(seeds) == 1
    assert seeds[0]["kind"] == "document"
    assert "41 tokens per second" in seeds[0]["content"]


def test_a_note_with_no_citations_seeds_nothing(isolated_db):
    assert doc_agent.resolve("@/to/research just a question").seed_sources() == []


def test_seeded_sources_take_the_first_citation_numbers(isolated_db, fake_ollama, monkeypatch):
    """The documents the user brought are S1, S2 — the order a person expects."""
    monkeypatch.setattr(research.websearch, "search", lambda *a, **k: [])
    monkeypatch.setattr(research, "search_local", lambda *a, **k: [])
    monkeypatch.setattr(research, "_ask_json", lambda *a, **k: None)

    events = list(research.run_research_stream(
        "A question", depth="quick",
        seed_sources=[{"kind": "document", "locator": "/papers/one.pdf",
                       "title": "one.pdf", "snippet": "", "content": "body one"}],
    ))
    sources = [e["source"] for e in events if "source" in e]
    assert sources[0]["id"] == "S1"
    assert sources[0]["title"] == "one.pdf"


def test_seeds_are_read_by_every_researcher(isolated_db, fake_ollama, monkeypatch):
    """A supplied paper is evidence for each sub-question, not just the first."""
    monkeypatch.setattr(research.websearch, "search", lambda *a, **k: [])
    monkeypatch.setattr(research, "search_local", lambda *a, **k: [])

    seen = []

    def fake_ask(task, prompt, **kwargs):
        if "sub-questions" in prompt:
            return {"subquestions": [
                {"question": "Sub one", "rationale": "", "sources": ["web"]},
                {"question": "Sub two", "rationale": "", "sources": ["web"]},
            ]}
        if "pull out every statement" in prompt:
            seen.append(prompt)
            return {"findings": [{"claim": "c", "sources": ["S1"], "confidence": 0.8}]}
        if "supports it" in prompt:
            return {"verdicts": [{"claim": 1, "verdict": "supported"}]}
        return None

    monkeypatch.setattr(research, "_ask_json", fake_ask)

    list(research.run_research_stream(
        "A question", depth="quick",
        seed_sources=[{"kind": "document", "locator": "/p.pdf", "title": "p.pdf",
                       "snippet": "", "content": "the seeded evidence"}],
    ))
    assert len(seen) == 2
    assert all("the seeded evidence" in prompt for prompt in seen)


# ===== The send endpoint =====

def test_doc_send_rejects_an_unknown_destination(client):
    response = client.post("/api/doc/send", json={"text": "hi", "destination": "telepathy"})
    assert response.status_code == 400


def test_doc_send_rejects_an_empty_note(client):
    assert client.post("/api/doc/send", json={"text": "   "}).status_code == 400


def test_the_picker_overrides_the_note(client, isolated_db, monkeypatch):
    """An explicit choice beats the note's @/to — the UI is an override."""
    captured = {}

    def fake_stream(question, depth="standard", conversation_id=None, seed_sources=None, **kwargs):
        captured.update({"question": question, "depth": depth})
        yield {"done": True, "run_id": "r", "report": "", "sources": 0,
               "findings": 0, "rejected": 0, "tainted": False}

    monkeypatch.setattr(carrot_app.research_mod, "run_research_stream", fake_stream)

    response = client.post("/api/doc/send", json={
        "text": "@/to/chat this note says chat",
        "destination": "research", "option": "deep",
    })
    assert response.status_code == 200
    assert captured["depth"] == "deep"


def test_a_note_reaches_the_agent_with_its_citations_as_background(client, isolated_db, monkeypatch):
    captured = {}

    def fake_stream(task, surface="browser", **kwargs):
        captured.update({"task": task, "surface": surface})
        yield {"done": True, "run_id": "r", "status": "complete", "result": "",
               "error": "", "steps": 0, "tainted": False}

    monkeypatch.setattr(carrot_app.carrot_agent, "run_agent_stream", fake_stream)

    workspace = agent_tools.workspace_root()
    path = os.path.join(workspace, "brief.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("submit before Friday")
    try:
        response = client.post("/api/doc/send", json={
            "text": "@/to/agent/both do the thing. context: @/file/brief.md",
        })
        assert response.status_code == 200
        assert captured["surface"] == "both"
        assert "submit before Friday" in captured["task"]
    finally:
        os.remove(path)


def test_destinations_endpoint_describes_each_option(client):
    body = client.get("/api/doc/destinations").json()
    by_id = {d["id"]: d for d in body["destinations"]}
    assert by_id["research"]["options"] == ["quick", "standard", "deep"]
    assert by_id["agent"]["option_label"] == "surface"


def test_to_candidates_are_served_to_the_mention_menu(client):
    body = client.get("/api/doc/candidates", params={"kind": "to", "q": "res"}).json()
    assert body["kind"] == "to"
    assert all(c["value"].startswith("research") for c in body["candidates"])


# ===== Chat search modes =====

def test_search_off_removes_the_web_tools_entirely(isolated_db):
    """An instruction not to search is a request; an absent tool is a fact."""
    names = {t["function"]["name"].split("__")[-1]
             for t in carrot_app._available_tools(carrot_app.SEARCH_OFF)}
    assert not (names & carrot_app.ALL_SEARCH_TOOLS)


def test_single_search_offers_search_and_read(isolated_db):
    names = {t["function"]["name"].split("__")[-1]
             for t in carrot_app._available_tools(carrot_app.SEARCH_SINGLE)}
    assert {"web_search", "read_url"} <= names
    assert "start_research" not in names


def test_multi_search_adds_the_research_handoff(isolated_db):
    names = {t["function"]["name"].split("__")[-1]
             for t in carrot_app._available_tools(carrot_app.SEARCH_MULTI)}
    assert {"web_search", "read_url", "start_research"} <= names


def test_non_web_tools_survive_every_mode(isolated_db):
    """Turning search off must not cost the user their file and memory tools."""
    for mode in carrot_app.SEARCH_MODES:
        names = {t["function"]["name"].split("__")[-1]
                 for t in carrot_app._available_tools(mode)}
        assert {"read_file", "write_file", "search_memory"} <= names, mode


def test_the_mode_falls_back_to_the_saved_default(isolated_db):
    from carrot import config

    assert carrot_app.search_mode() == carrot_app.SEARCH_SINGLE
    config.set_config("chat_search_mode", "off")
    assert carrot_app.search_mode() == carrot_app.SEARCH_OFF
    # A per-turn choice still wins over the default.
    assert carrot_app.search_mode("multi") == carrot_app.SEARCH_MULTI
    # And nonsense falls back rather than raising.
    assert carrot_app.search_mode("telepathy") == carrot_app.SEARCH_SINGLE


def test_each_mode_carries_its_own_directive(isolated_db):
    assert "no web access" in carrot_app.search_directive(carrot_app.SEARCH_OFF)
    assert "single-pass" in carrot_app.search_directive(carrot_app.SEARCH_SINGLE)
    assert "search again" in carrot_app.search_directive(carrot_app.SEARCH_MULTI)


def test_multi_turn_gets_a_larger_round_budget(isolated_db):
    assert carrot_app.MAX_TOOL_ROUNDS_MULTI > carrot_app.MAX_TOOL_ROUNDS


def test_the_directive_reaches_the_model(isolated_db, fake_ollama):
    from carrot import conversation as conv_mod

    conv = conv_mod.create_conversation(title="t")
    history, _ = carrot_app._prepare_history(
        conv_mod.get_conversation(conv["id"]), "hello", None, mode=carrot_app.SEARCH_OFF
    )
    assert history[0]["role"] == "system"
    assert "no web access" in history[0]["content"]


def test_search_modes_endpoint_lists_all_three(client):
    body = client.get("/api/chat/search-modes").json()
    assert [m["id"] for m in body["modes"]] == ["off", "single", "multi"]
    assert body["current"] == "single"


def test_a_chat_turn_reports_the_mode_it_ran_in(client, isolated_db, fake_ollama):
    """The UI should never have to guess whether that answer used the web."""
    response = client.post("/api/chat/stream", json={"message": "hi", "search_mode": "off"})
    assert response.status_code == 200
    events = [json.loads(line[5:]) for line in response.text.splitlines()
              if line.startswith("data:")]
    assert any(event.get("search_mode") == "off" for event in events)


# ===== Extension packs =====
#
# The pack had tools, skills, capability probes and settings but nothing that
# could turn it on. These pin the contract the UI now depends on.

def test_packs_are_listed_with_their_counts(client):
    packs = client.get("/api/extensions").json()["extensions"]
    academia = next(p for p in packs if p["id"] == "academia")
    assert academia["enabled"] is False
    assert academia["tool_count"] > 0 and academia["skill_count"] > 0


def test_pack_detail_reports_capabilities_settings_and_tools(client):
    pack = client.get("/api/extensions/academia").json()
    assert {c["id"] for c in pack["capabilities"]} == {"latex", "matlab", "cad"}
    assert {s["key"] for s in pack["settings"]} == {"venue", "citation_style", "strict_citations"}
    assert all("available" in c and "install_hint" in c for c in pack["capabilities"])
    assert any(t["requires"] == "latex" for t in pack["tools"])


def test_enabling_a_pack_installs_its_skills(client):
    """Enabling is the whole point: the skills have to actually show up."""
    assert client.put("/api/extensions/academia/enabled", json={"enabled": True}).status_code == 200

    slugs = {s["slug"] for s in client.get("/api/skills").json()}
    assert "academic-writing" in slugs

    client.put("/api/extensions/academia/enabled", json={"enabled": False})
    assert "academic-writing" not in {s["slug"] for s in client.get("/api/skills").json()}


def test_a_pack_setting_rewrites_the_skill_that_uses_it(client):
    """Change the venue and the instructions must name the new one."""
    client.put("/api/extensions/academia/enabled", json={"enabled": True})
    client.put("/api/extensions/academia/settings/venue", json="ICML 2026")

    instructions = client.get("/api/skills/academic-writing").json()["instructions"]
    assert "ICML 2026" in instructions

    client.put("/api/extensions/academia/enabled", json={"enabled": False})


def test_an_unknown_pack_setting_is_refused(client):
    assert client.put(
        "/api/extensions/academia/settings/not_a_setting", json="x"
    ).status_code == 400


def test_a_pack_tool_refuses_when_its_program_is_missing(client, isolated_db):
    """A missing engine is reported up front, not discovered halfway through."""
    from carrot import extensions

    client.put("/api/extensions/academia/enabled", json={"enabled": True})
    result = extensions.call("ext__academia__latex_compile", {"path": "paper.tex"})
    assert result.startswith("error:")
    assert "not installed" in result
    client.put("/api/extensions/academia/enabled", json={"enabled": False})


def test_a_disabled_packs_tools_are_out_of_reach(client, isolated_db):
    from carrot import extensions

    client.put("/api/extensions/academia/enabled", json={"enabled": False})
    assert "not enabled" in extensions.call("ext__academia__latex_validate", {"path": "x.tex"})
    assert not extensions.ollama_tools()
