"""Doc to agent: @file / @model references, resolution, and sending."""
import json
import os

import pytest

from carrot import config, doc_agent, providers, router


@pytest.fixture
def workspace(isolated_db, tmp_path, monkeypatch):
    """An agent workspace with a couple of citable files."""
    root = tmp_path / "workspace"
    (root / "src").mkdir(parents=True)
    (root / "PLAN.md").write_text("the plan is to ship it", encoding="utf-8")
    (root / "src" / "router.py").write_text("def route():\n    return 1\n", encoding="utf-8")
    (root / "my notes.md").write_text("spaced filename body", encoding="utf-8")
    config.set_config("code_workspace_dir", str(root))
    doc_agent.clear_candidate_cache()
    return root


# ===== Parsing =====

def test_file_and_model_references_are_found():
    refs = doc_agent.parse_references("Refactor @file:src/router.py using @model:groq/fast")
    assert [(r.kind, r.value) for r in refs] == [
        ("file", "src/router.py"), ("model", "groq/fast"),
    ]


def test_quoted_paths_keep_their_spaces():
    refs = doc_agent.parse_references('Look at @file:"my notes.md" please')
    assert refs[0].value == "my notes.md"


def test_trailing_sentence_punctuation_is_not_part_of_the_path():
    refs = doc_agent.parse_references("Read @file:PLAN.md, then @file:src/router.py.")
    assert [r.value for r in refs] == ["PLAN.md", "src/router.py"]


def test_ollama_style_model_tags_keep_their_colon():
    refs = doc_agent.parse_references("Use @model:gemma4:e4b for this")
    assert refs[0].value == "gemma4:e4b"


def test_text_without_references_parses_to_nothing():
    assert doc_agent.parse_references("just an email a@b.com and a plan") == []


def test_bare_at_sign_is_not_a_reference():
    assert doc_agent.parse_references("costs @ 5 dollars") == []


# ===== Model references =====

def test_known_provider_prefix_is_split_off(isolated_db):
    providers.upsert_provider("groq", kind="openai", base_url="https://api.groq.com/openai/v1")
    assert doc_agent.resolve_model_reference("groq/some-model") == ("groq", "some-model")


def test_slashes_inside_a_model_id_survive(isolated_db):
    """OpenRouter-style ids contain slashes; only a real provider prefix splits."""
    providers.upsert_provider("openrouter", kind="openai",
                              base_url="https://openrouter.ai/api/v1")
    assert doc_agent.resolve_model_reference("openrouter/meta-llama/llama-3-70b") == (
        "openrouter", "meta-llama/llama-3-70b",
    )


def test_unknown_prefix_is_treated_as_part_of_the_model_name(isolated_db):
    assert doc_agent.resolve_model_reference("meta-llama/llama-3-70b") == (
        "", "meta-llama/llama-3-70b",
    )


def test_bare_model_name_has_no_provider(isolated_db):
    assert doc_agent.resolve_model_reference("gemma4:e4b") == ("", "gemma4:e4b")


# ===== Resolution =====

def test_cited_file_contents_are_attached(workspace):
    resolved = doc_agent.resolve("Refactor @file:src/router.py to be cleaner")
    assert "def route():" in resolved.context
    assert resolved.references[0].ok is True
    assert not resolved.warnings


def test_the_prompt_keeps_the_reference_text(workspace):
    """The message stored in the conversation should read like what was written."""
    resolved = doc_agent.resolve("Refactor @file:src/router.py to be cleaner")
    assert resolved.prompt == "Refactor @file:src/router.py to be cleaner"


def test_quoted_path_with_spaces_resolves(workspace):
    resolved = doc_agent.resolve('Check @file:"my notes.md" over')
    assert resolved.references[0].ok is True
    assert "spaced filename body" in resolved.context


def test_missing_file_warns_rather_than_failing_silently(workspace):
    resolved = doc_agent.resolve("See @file:nope.md")
    assert resolved.references[0].ok is False
    assert resolved.warnings
    assert "no such file" in resolved.warnings[0]


def test_a_citation_cannot_escape_the_workspace(workspace, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("private", encoding="utf-8")
    resolved = doc_agent.resolve(f"Read @file:{secret}")
    assert resolved.references[0].ok is False
    assert "private" not in resolved.context
    assert resolved.warnings


def test_traversal_out_of_the_workspace_is_refused(workspace):
    resolved = doc_agent.resolve("Read @file:../../etc/passwd")
    assert resolved.references[0].ok is False
    assert not resolved.context


def test_indexed_folders_are_citable(workspace, tmp_path):
    """Folders the user added to the index are readable by citation too."""
    papers = tmp_path / "papers"
    papers.mkdir()
    paper = papers / "attention.md"
    paper.write_text("scaled dot-product attention", encoding="utf-8")
    config.set_config("index_dirs", [str(papers)])

    resolved = doc_agent.resolve(f"Summarize @file:{paper}")
    assert resolved.references[0].ok is True
    assert "scaled dot-product attention" in resolved.context


def test_duplicate_citations_are_attached_once(workspace):
    resolved = doc_agent.resolve("@file:PLAN.md and again @file:PLAN.md")
    assert resolved.context.count("the plan is to ship it") == 1
    assert resolved.references[1].detail == "already cited"


def test_oversized_files_are_truncated(workspace):
    (workspace / "big.txt").write_text("x" * (doc_agent.MAX_FILE_CHARS + 5000), encoding="utf-8")
    resolved = doc_agent.resolve("@file:big.txt")
    assert resolved.references[0].ok is True
    assert "truncated" in resolved.context
    assert len(resolved.context) < doc_agent.MAX_FILE_CHARS + 2000


def test_total_citation_budget_is_enforced(workspace):
    for i in range(6):
        (workspace / f"f{i}.txt").write_text("y" * doc_agent.MAX_FILE_CHARS, encoding="utf-8")
    resolved = doc_agent.resolve(" ".join(f"@file:f{i}.txt" for i in range(6)))
    assert len(resolved.context) <= doc_agent.MAX_TOTAL_CHARS + 5000
    assert any("budget" in w for w in resolved.warnings)


def test_model_reference_selects_the_route(workspace):
    providers.upsert_provider("groq", kind="openai", base_url="https://api.groq.com/openai/v1")
    providers.set_api_key("groq", "gsk-test")
    resolved = doc_agent.resolve("Think hard about this @model:groq/big-model")
    assert resolved.route.provider == "groq"
    assert resolved.route.model == "big-model"
    assert resolved.route.local is False


def test_model_reference_to_an_unconfigured_provider_warns(workspace, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    resolved = doc_agent.resolve("@model:openai/some-model")
    assert resolved.route is None
    assert any("not configured" in w for w in resolved.warnings)


def test_the_last_model_reference_wins(workspace):
    providers.upsert_provider("groq", kind="openai", base_url="https://api.groq.com/openai/v1")
    providers.set_api_key("groq", "gsk-test")
    resolved = doc_agent.resolve("@model:gemma4:e4b then later @model:groq/final")
    assert resolved.route.model == "final"
    assert any("using the last one" in w for w in resolved.warnings)
    assert resolved.references[0].detail == "overridden by a later @model"


def test_a_document_with_no_references_still_resolves(workspace):
    resolved = doc_agent.resolve("  just a plan, no references  ")
    assert resolved.prompt == "just a plan, no references"
    assert resolved.route is None
    assert resolved.context == ""


# ===== Candidates =====

def test_workspace_files_are_offered_as_relative_paths(workspace):
    values = {c["value"] for c in doc_agent.file_candidates()}
    assert "PLAN.md" in values
    assert os.path.join("src", "router.py") in values


def test_file_candidates_filter_on_the_query(workspace):
    values = {c["value"] for c in doc_agent.file_candidates("router")}
    assert values == {os.path.join("src", "router.py")}


def test_hidden_files_are_not_offered(workspace):
    (workspace / ".secret").write_text("x", encoding="utf-8")
    assert ".secret" not in {c["value"] for c in doc_agent.file_candidates()}


# ===== API =====

def test_parse_endpoint_reports_resolution(client, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "PLAN.md").write_text("ship it", encoding="utf-8")
    config.set_config("code_workspace_dir", str(root))

    body = client.post("/api/doc/parse", json={"text": "Do @file:PLAN.md now"}).json()
    assert body["references"][0]["ok"] is True
    assert "ship it" in body["context"]


def test_parse_endpoint_reports_a_bad_citation(client):
    body = client.post("/api/doc/parse", json={"text": "Do @file:missing.md"}).json()
    assert body["references"][0]["ok"] is False
    assert body["warnings"]


def test_candidates_endpoint_lists_files(client, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "PLAN.md").write_text("ship it", encoding="utf-8")
    config.set_config("code_workspace_dir", str(root))
    doc_agent.clear_candidate_cache()

    body = client.get("/api/doc/candidates?kind=file").json()
    assert "PLAN.md" in {c["value"] for c in body["candidates"]}


def test_send_streams_a_turn_and_reports_its_citations(client, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "PLAN.md").write_text("ship it on friday", encoding="utf-8")
    config.set_config("code_workspace_dir", str(root))

    response = client.post("/api/doc/send", json={
        "text": "Execute @file:PLAN.md", "title": "My plan",
    })
    assert response.status_code == 200
    events = [json.loads(line[5:]) for line in response.text.splitlines()
              if line.startswith("data:")]

    # The citation report is the first event, before any tokens.
    assert events[0]["document"]["references"][0]["ok"] is True
    assert "ship it on friday" in events[0]["document"]["context"]

    assert "".join(e["chunk"] for e in events if "chunk" in e) == "Hello from Carrot"
    assert events[-1]["done"] is True


def test_send_stores_the_note_text_as_the_user_message(client, tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "PLAN.md").write_text("ship it", encoding="utf-8")
    config.set_config("code_workspace_dir", str(root))

    client.post("/api/doc/send", json={"text": "Execute @file:PLAN.md", "title": "My plan"})
    conversations = client.get("/api/conversations").json()
    assert conversations[0]["title"] == "My plan"

    messages = client.get(f"/api/conversations/{conversations[0]['id']}").json()["messages"]
    # The stored message reads like the note — the file body rides along as
    # context, not pasted into what the user appears to have written.
    assert messages[0]["content"] == "Execute @file:PLAN.md"
    assert "ship it" not in messages[0]["content"]


def test_send_refuses_an_empty_document(client):
    assert client.post("/api/doc/send", json={"text": "   "}).status_code == 400


def test_send_continues_an_existing_conversation(client, tmp_path):
    config.set_config("code_workspace_dir", str(tmp_path))
    created = client.post("/api/conversations", json={"title": "Ongoing"}).json()
    client.post("/api/doc/send", json={"text": "a follow-up", "conversation_id": created["id"]})

    messages = client.get(f"/api/conversations/{created['id']}").json()["messages"]
    assert messages[0]["content"] == "a follow-up"
    assert len(client.get("/api/conversations").json()) == 1
