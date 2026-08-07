"""Tests for workspaces, folders, scoping, and the help/tutorial surface.

The load-bearing claim is that a workspace *scopes* rather than *contains*:
filing something changes what search returns and nothing else, and unfiling or
deleting a workspace never destroys content.
"""
import pytest

from carrot import (
    conversation as conv_mod,
    help as help_mod,
    memory as memory_mod,
    search as search_mod,
    workspaces as ws,
)


# ===== Folders =====

def test_folders_nest_and_list(isolated_db):
    school = ws.create_folder("School")
    term = ws.create_folder("Term 1", parent_id=school["id"])
    assert term["parent_id"] == school["id"]
    assert {f["name"] for f in ws.list_folders()} == {"School", "Term 1"}


def test_a_folder_needs_a_name(isolated_db):
    with pytest.raises(ValueError):
        ws.create_folder("   ")


def test_an_unknown_parent_is_refused(isolated_db):
    with pytest.raises(ValueError):
        ws.create_folder("Orphan", parent_id="nope")


def test_a_folder_cannot_contain_itself(isolated_db):
    folder = ws.create_folder("School")
    with pytest.raises(ValueError):
        ws.update_folder(folder["id"], parent_id=folder["id"])


def test_a_folder_cannot_be_moved_into_its_own_descendant(isolated_db):
    """A cycle here hangs every tree render, so it is refused at the write."""
    school = ws.create_folder("School")
    term = ws.create_folder("Term 1", parent_id=school["id"])
    week = ws.create_folder("Week 3", parent_id=term["id"])
    with pytest.raises(ValueError):
        ws.update_folder(school["id"], parent_id=week["id"])


def test_nesting_is_bounded(isolated_db):
    parent = None
    for index in range(ws.MAX_FOLDER_DEPTH):
        parent = ws.create_folder(f"level {index}", parent_id=parent["id"] if parent else None)["id"]
        parent = {"id": parent}
    with pytest.raises(ValueError):
        ws.create_folder("too deep", parent_id=parent["id"])


def test_deleting_a_folder_keeps_its_workspaces(isolated_db):
    """Tidying must never be a way to lose a project."""
    folder = ws.create_folder("School")
    workspace = ws.create_workspace("Thesis", folder_id=folder["id"])
    child = ws.create_folder("Term 1", parent_id=folder["id"])

    assert ws.delete_folder(folder["id"]) is True
    assert ws.get_workspace(workspace["id"])["folder_id"] is None
    assert ws.get_folder(child["id"])["parent_id"] is None


# ===== Workspaces =====

def test_workspaces_are_created_and_listed(isolated_db):
    workspace = ws.create_workspace("Thesis", description="the big one")
    assert workspace["name"] == "Thesis"
    assert workspace["counts"] == {kind: 0 for kind in ws.KINDS}
    assert [w["name"] for w in ws.list_workspaces()] == ["Thesis"]


def test_a_workspace_needs_a_name(isolated_db):
    with pytest.raises(ValueError):
        ws.create_workspace("  ")


def test_a_workspace_can_be_renamed_and_moved(isolated_db):
    folder = ws.create_folder("School")
    workspace = ws.create_workspace("Draft")
    updated = ws.update_workspace(workspace["id"], name="Thesis", folder_id=folder["id"])
    assert updated["name"] == "Thesis"
    assert updated["folder_id"] == folder["id"]


def test_archived_workspaces_are_hidden_by_default(isolated_db):
    workspace = ws.create_workspace("Old project")
    ws.update_workspace(workspace["id"], archived=True)
    assert ws.list_workspaces() == []
    assert len(ws.list_workspaces(include_archived=True)) == 1


# ===== Membership =====

def test_an_item_has_exactly_one_home(isolated_db):
    """Re-filing moves rather than duplicates — the UI needs one answer."""
    first = ws.create_workspace("A")
    second = ws.create_workspace("B")

    ws.file_item(ws.KIND_CONVERSATION, "c1", first["id"])
    ws.file_item(ws.KIND_CONVERSATION, "c1", second["id"])

    assert ws.workspace_of(ws.KIND_CONVERSATION, "c1") == second["id"]
    assert ws.item_ids(first["id"], ws.KIND_CONVERSATION) == []
    assert ws.item_ids(second["id"], ws.KIND_CONVERSATION) == ["c1"]


def test_filing_into_nothing_unfiles(isolated_db):
    workspace = ws.create_workspace("A")
    ws.file_item(ws.KIND_NOTE, "n1", workspace["id"])
    ws.file_item(ws.KIND_NOTE, "n1", "")
    assert ws.workspace_of(ws.KIND_NOTE, "n1") is None


def test_filing_into_an_unknown_workspace_does_nothing(isolated_db):
    assert ws.file_item(ws.KIND_NOTE, "n1", "nope") is None
    assert ws.workspace_of(ws.KIND_NOTE, "n1") is None


def test_counts_are_per_kind(isolated_db):
    workspace = ws.create_workspace("A")
    ws.file_item(ws.KIND_CONVERSATION, "c1", workspace["id"])
    ws.file_item(ws.KIND_CONVERSATION, "c2", workspace["id"])
    ws.file_item(ws.KIND_MEMORY, "m1", workspace["id"])
    counts = ws.item_counts(workspace["id"])
    assert counts[ws.KIND_CONVERSATION] == 2
    assert counts[ws.KIND_MEMORY] == 1
    assert counts[ws.KIND_DOCUMENT] == 0


# ===== The active workspace =====

def test_no_workspace_is_active_on_a_fresh_install(isolated_db):
    assert ws.active_workspace_id() == ""
    assert ws.active_workspace() is None


def test_new_work_is_filed_into_the_active_workspace(isolated_db):
    workspace = ws.create_workspace("Thesis")
    ws.set_active_workspace(workspace["id"])

    conversation = conv_mod.create_conversation(title="a chat")
    assert ws.workspace_of(ws.KIND_CONVERSATION, conversation["id"]) == workspace["id"]


def test_nothing_is_filed_when_no_workspace_is_active(isolated_db):
    conversation = conv_mod.create_conversation(title="a chat")
    assert ws.workspace_of(ws.KIND_CONVERSATION, conversation["id"]) is None


def test_a_memory_inherits_its_conversations_workspace(isolated_db):
    """Extraction runs in the background; it must not land wherever you drifted to."""
    thesis = ws.create_workspace("Thesis")
    other = ws.create_workspace("Other")
    ws.set_active_workspace(thesis["id"])
    conversation = conv_mod.create_conversation(title="thesis chat")

    ws.set_active_workspace(other["id"])   # the user moved on before extraction finished
    memory = memory_mod.create(
        "decision", "topic", "The user is writing about attention",
        source_conversation_id=conversation["id"],
    )
    assert ws.workspace_of(ws.KIND_MEMORY, memory["id"]) == thesis["id"]


def test_switching_to_an_unknown_workspace_is_refused(isolated_db):
    with pytest.raises(ValueError):
        ws.set_active_workspace("nope")


def test_a_deleted_active_workspace_falls_back_to_everything(isolated_db):
    workspace = ws.create_workspace("Thesis")
    ws.set_active_workspace(workspace["id"])
    ws.delete_workspace(workspace["id"])
    assert ws.active_workspace_id() == ""


# ===== Scoping =====

@pytest.fixture
def two_projects(isolated_db):
    """Two workspaces, each with a conversation mentioning the same word."""
    thesis = ws.create_workspace("Thesis")
    course = ws.create_workspace("CS 3110")

    ws.set_active_workspace(thesis["id"])
    first = conv_mod.create_conversation(title="thesis chat")
    conv_mod.add_message(first["id"], "user", "the attention deadline is Friday")

    ws.set_active_workspace(course["id"])
    second = conv_mod.create_conversation(title="course chat")
    conv_mod.add_message(second["id"], "user", "the attention lecture is Friday")

    ws.set_active_workspace("")
    return {"thesis": thesis["id"], "course": course["id"],
            "first": first["id"], "second": second["id"]}


def test_search_is_restricted_to_one_workspace(two_projects):
    everything = search_mod.search_conversations("attention")["results"]
    thesis_only = search_mod.search_conversations(
        "attention", workspace_id=two_projects["thesis"]
    )["results"]

    assert len(everything) == 2
    assert len(thesis_only) == 1
    assert thesis_only[0]["conversation_id"] == two_projects["first"]


def test_an_empty_scope_searches_everything(two_projects):
    assert len(search_mod.search_conversations("attention", workspace_id="")["results"]) == 2


def test_memory_recall_is_scoped(isolated_db):
    thesis = ws.create_workspace("Thesis")
    other = ws.create_workspace("Other")

    ws.set_active_workspace(thesis["id"])
    conversation = conv_mod.create_conversation(title="c")
    memory_mod.create("decision", "topic", "The user is writing about attention",
                      source_conversation_id=conversation["id"])
    ws.set_active_workspace("")

    assert len(memory_mod.recall("attention", workspace_id=thesis["id"])) == 1
    assert len(memory_mod.recall("attention", workspace_id=other["id"])) == 0
    assert len(memory_mod.recall("attention")) == 1


def test_a_pin_does_not_follow_you_into_another_project(isolated_db):
    """Pinned means always relevant *here*, not everywhere."""
    thesis = ws.create_workspace("Thesis")
    other = ws.create_workspace("Other")

    ws.set_active_workspace(thesis["id"])
    conversation = conv_mod.create_conversation(title="c")
    memory = memory_mod.create("decision", "topic", "Something pinned",
                               source_conversation_id=conversation["id"])
    memory_mod.update(memory["id"], pinned=True)
    ws.set_active_workspace("")

    assert any(m["id"] == memory["id"] for m in memory_mod.recall("unrelated", workspace_id=thesis["id"]))
    assert not any(m["id"] == memory["id"] for m in memory_mod.recall("unrelated", workspace_id=other["id"]))


def test_an_unfiled_pin_is_visible_everywhere(isolated_db):
    """Something never filed belongs to no project, so it belongs to all of them."""
    workspace = ws.create_workspace("Thesis")
    memory = memory_mod.create("preference", "editor", "The user prefers vim")
    memory_mod.update(memory["id"], pinned=True)
    assert any(m["id"] == memory["id"]
               for m in memory_mod.recall("anything", workspace_id=workspace["id"]))


def test_resolve_scope_defaults_to_the_active_workspace(isolated_db):
    workspace = ws.create_workspace("Thesis")
    ws.set_active_workspace(workspace["id"])

    assert ws.resolve_scope(None) == workspace["id"]
    assert ws.resolve_scope("all") == ""
    assert ws.resolve_scope("") == ""
    assert ws.resolve_scope("nonexistent") == ""


def test_scope_clause_is_empty_without_a_workspace(isolated_db):
    assert ws.scope_clause(ws.KIND_MEMORY, "", "m.id") == ("", [])
    sql, params = ws.scope_clause(ws.KIND_MEMORY, "w1", "m.id")
    assert sql.startswith(" AND m.id IN")
    assert params == ["w1", ws.KIND_MEMORY]


# ===== Deletion is not destruction =====

def test_deleting_a_workspace_keeps_its_contents(two_projects):
    assert ws.delete_workspace(two_projects["thesis"]) is True

    assert conv_mod.get_conversation(two_projects["first"]) is not None
    assert ws.workspace_of(ws.KIND_CONVERSATION, two_projects["first"]) is None
    assert len(search_mod.search_conversations("attention")["results"]) == 2


# ===== Views =====

def test_the_tree_nests_folders_and_lists_loose_workspaces(isolated_db):
    school = ws.create_folder("School")
    ws.create_folder("Term 1", parent_id=school["id"])
    ws.create_workspace("Thesis", folder_id=school["id"])
    ws.create_workspace("Loose")

    tree = ws.tree()
    assert [f["name"] for f in tree["folders"]] == ["School"]
    assert [w["name"] for w in tree["folders"][0]["workspaces"]] == ["Thesis"]
    assert [f["name"] for f in tree["folders"][0]["children"]] == ["Term 1"]
    assert [w["name"] for w in tree["workspaces"]] == ["Loose"]


def test_contents_resolve_to_readable_labels(isolated_db):
    workspace = ws.create_workspace("Thesis")
    ws.set_active_workspace(workspace["id"])
    conv_mod.create_conversation(title="A memorable title")

    contents = ws.contents(workspace["id"])
    labels = [item["label"] for item in contents["items"][ws.KIND_CONVERSATION]]
    assert labels == ["A memorable title"]


def test_contents_of_an_unknown_workspace_is_empty(isolated_db):
    assert ws.contents("nope") == {}


# ===== API =====

def test_the_tree_endpoint_reports_the_active_workspace(client, isolated_db):
    created = client.post("/api/workspaces", json={"name": "Thesis"}).json()
    client.post("/api/workspaces/active", json={"workspace_id": created["id"]})

    tree = client.get("/api/workspaces").json()
    assert tree["active"] == created["id"]
    assert [w["name"] for w in tree["workspaces"]] == ["Thesis"]


def test_switching_to_an_unknown_workspace_is_a_404(client):
    assert client.post("/api/workspaces/active", json={"workspace_id": "nope"}).status_code == 404


def test_items_can_be_filed_and_unfiled_over_the_api(client, isolated_db):
    workspace = client.post("/api/workspaces", json={"name": "Thesis"}).json()
    response = client.post(f"/api/workspaces/{workspace['id']}/items", json={
        "items": [{"kind": "conversation", "item_id": "c1"}],
    })
    assert response.json()["filed"] == 1
    assert response.json()["counts"]["conversation"] == 1

    assert client.delete("/api/workspaces/items/conversation/c1").json()["unfiled"] is True


def test_search_honours_the_active_workspace_by_default(client, two_projects):
    client.post("/api/workspaces/active", json={"workspace_id": two_projects["thesis"]})

    scoped = client.get("/api/search", params={"q": "attention"}).json()
    assert scoped["workspace_id"] == two_projects["thesis"]
    assert len(scoped["results"]) == 1

    everything = client.get("/api/search", params={"q": "attention", "workspace": "all"}).json()
    assert everything["workspace_id"] == ""
    assert len(everything["results"]) == 2


def test_unified_search_is_scoped_too(client, two_projects):
    client.post("/api/workspaces/active", json={"workspace_id": two_projects["course"]})
    body = client.get("/api/search/all", params={"q": "attention"}).json()
    assert body["workspace_id"] == two_projects["course"]
    assert len(body["conversations"]) == 1


def test_a_bad_workspace_body_is_rejected(client):
    assert client.post("/api/workspaces", json={"name": "  "}).status_code == 400
    assert client.post("/api/folders", json={"name": ""}).status_code == 400


def test_deleting_a_missing_workspace_is_a_404(client):
    assert client.delete("/api/workspaces/nope").status_code == 404
    assert client.delete("/api/folders/nope").status_code == 404


# ===== Help =====

def test_every_topic_is_well_formed():
    for topic in help_mod.topics():
        assert topic["id"] and topic["title"] and topic["summary"] and topic["body"]
        assert topic["section"] in help_mod.SECTIONS, topic["id"]


def test_topic_ids_are_unique():
    ids = [topic["id"] for topic in help_mod.topics()]
    assert len(ids) == len(set(ids))


def test_help_search_ranks_title_matches_first():
    results = help_mod.search_topics("workspaces")
    assert results[0]["id"] == "workspaces"


def test_help_search_finds_body_text():
    """Someone searching a phrase they half-remember should still land somewhere."""
    assert any(t["id"] == "privacy" for t in help_mod.search_topics("telemetry"))
    assert any(t["id"] == "agent-safety" for t in help_mod.search_topics("CAPTCHA"))


def test_an_empty_query_returns_everything():
    assert len(help_mod.search_topics("")) == len(help_mod.topics())


def test_a_query_matching_nothing_returns_nothing():
    assert help_mod.search_topics("zzzzqqqq") == []


def test_help_endpoints(client):
    body = client.get("/api/help").json()
    assert body["topics"] and body["sections"]

    assert client.get("/api/help/workspaces").json()["title"] == "Workspaces and folders"
    assert client.get("/api/help/not-a-topic").status_code == 404

    filtered = client.get("/api/help", params={"q": "credentials"}).json()
    assert filtered["topics"]


# ===== Tutorial =====

def test_the_tutorial_reports_live_state(isolated_db):
    result = help_mod.tutorial()
    assert result["total"] == len(help_mod.STEPS)
    assert all(step["done"] in (True, False, None) for step in result["steps"])

    # Nothing has been done on a fresh database — but "engine" asks whether
    # Ollama is running on this *machine*, which an isolated database says
    # nothing about. Asserting zero completed steps meant this test passed only
    # on a machine with Ollama switched off, and failed for everyone actually
    # set up to use Carrot.
    from_db = [s for s in result["steps"] if s["id"] != "engine"]
    assert all(step["done"] is False for step in from_db)

    # `next` is the first unfinished step, whichever that turns out to be.
    assert result["next"] == next(s["id"] for s in result["steps"] if s["done"] is not True)


def test_a_tutorial_step_ticks_when_the_thing_is_actually_done(isolated_db):
    """The point of the checks: doing the thing is what completes the step."""
    before = {s["id"]: s["done"] for s in help_mod.tutorial()["steps"]}
    assert before["workspace"] is False

    ws.create_workspace("Thesis")

    after = {s["id"]: s["done"] for s in help_mod.tutorial()["steps"]}
    assert after["workspace"] is True


def test_a_step_unticks_when_the_thing_is_undone(isolated_db):
    workspace = ws.create_workspace("Thesis")
    assert {s["id"]: s["done"] for s in help_mod.tutorial()["steps"]}["workspace"] is True

    ws.delete_workspace(workspace["id"])
    assert {s["id"]: s["done"] for s in help_mod.tutorial()["steps"]}["workspace"] is False


def test_a_failing_check_reports_unknown_rather_than_failing(isolated_db, monkeypatch):
    """A red cross for something unmeasurable is worse than admitting it."""
    def boom():
        raise RuntimeError("database is on fire")

    monkeypatch.setitem(help_mod.STEPS[2], "check", boom)
    step = next(s for s in help_mod.tutorial()["steps"] if s["id"] == "workspace")
    assert step["done"] is None
    assert "could not check" in step["detail"]


def test_every_step_points_somewhere_real(isolated_db):
    topic_ids = {topic["id"] for topic in help_mod.topics()}
    for step in help_mod.STEPS:
        if step.get("topic"):
            assert step["topic"] in topic_ids, step["id"]
        assert step.get("action", {}).get("tab")


def test_tutorial_endpoint(client, isolated_db):
    body = client.get("/api/help/tutorial").json()
    assert body["total"] > 0
    assert "steps" in body and body["steps"][0]["title"]


def test_the_computer_use_scan_endpoint_works(client, isolated_db, monkeypatch):
    """Regression: it called a function that never existed and always 500'd."""
    from carrot import computer_use

    monkeypatch.setattr(computer_use, "computer_use_scan", lambda *a, **k: 3)
    response = client.get("/api/computer_use/scan")
    assert response.status_code == 200
    assert response.json() == {"indexed": 3}


def test_the_chat_list_honours_the_active_workspace(client, two_projects):
    """The sidebar must not contradict the search box on the same screen."""
    client.post("/api/workspaces/active", json={"workspace_id": two_projects["thesis"]})
    scoped = client.get("/api/conversations").json()
    assert [c["id"] for c in scoped] == [two_projects["first"]]

    everything = client.get("/api/conversations", params={"workspace": "all"}).json()
    assert len(everything) == 2
