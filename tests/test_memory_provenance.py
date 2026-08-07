"""Where a memory was learned, and how to see only the ones that matter.

Memory already recorded *which message* a belief came from. It did not record
which part of Carrot was running when it was learned, which is the question a
list of two hundred rows actually raises: "Carrot thinks I prefer tabs" reads
very differently once you know it was picked up while the Code tab was open on
somebody else's repo.

Two separate axes, and they must not be confused:

* **origin** — what kind of work produced it (chat, code, document, you).
* **workspace** — which project the conversation it came from belonged to.
  Already recorded, already used for recall, but there was no way to ask for it.
"""
from pathlib import Path

import pytest

from carrot import app as A, memory, workspaces


def read_js(name):
    root = Path(__file__).resolve().parents[1]
    return (root / "carrot" / "web" / "js" / name).read_text(encoding="utf-8")


class TestOriginIsRecorded:
    def test_a_memory_defaults_to_chat(self, isolated_db):
        assert memory.create("fact", "gym", "The user swims.")["origin"] == memory.ORIGIN_CHAT

    def test_an_origin_is_kept(self, isolated_db):
        created = memory.create("fact", "tabs", "The user prefers tabs.",
                                origin=memory.ORIGIN_CODE)
        assert created["origin"] == memory.ORIGIN_CODE
        assert memory.get(created["id"])["origin"] == memory.ORIGIN_CODE

    def test_an_unknown_origin_falls_back_rather_than_storing_a_lie(self, isolated_db):
        created = memory.create("fact", "x", "Something.", origin="telepathy")
        assert created["origin"] == memory.ORIGIN_CHAT

    def test_every_offered_origin_has_a_label(self):
        # The filter dropdown is built from ORIGINS; an entry with no label
        # would show the user a column value instead of a word.
        assert set(memory.ORIGINS) == set(memory.ORIGIN_LABELS)
        assert set(memory.ORIGINS) == set(memory.ORIGIN_FILTER_LABELS)

    def test_the_dropdown_line_is_not_built_by_concatenation(self):
        # "Learned in " + label gives "Learned in you", which is why the
        # phrases are a table rather than a format string.
        assert memory.ORIGIN_FILTER_LABELS[memory.ORIGIN_MANUAL] == "Written by you"
        assert all(not phrase.endswith(" you") or phrase.startswith("Written")
                   for phrase in memory.ORIGIN_FILTER_LABELS.values())

    def test_manual_reads_as_you(self):
        # What the column stores and what it means are different words on
        # purpose: "manual" is a data value, "you" is the fact.
        assert memory.ORIGIN_LABELS[memory.ORIGIN_MANUAL] == "you"


class TestFilteringByOrigin:
    def seed(self):
        memory.create("fact", "one", "From a chat.", origin=memory.ORIGIN_CHAT)
        memory.create("fact", "two", "From the code tab.", origin=memory.ORIGIN_CODE)
        memory.create("fact", "three", "From a note.", origin=memory.ORIGIN_DOCUMENT)

    def test_one_origin_at_a_time(self, isolated_db):
        self.seed()
        listed = memory.list_memories(origin=memory.ORIGIN_CODE)
        assert [m["content"] for m in listed] == ["From the code tab."]

    def test_no_origin_means_all_of_them(self, isolated_db):
        self.seed()
        assert len(memory.list_memories()) == 3

    def test_stats_count_by_origin(self, isolated_db):
        self.seed()
        assert memory.stats()["by_origin"] == {
            memory.ORIGIN_CHAT: 1, memory.ORIGIN_CODE: 1, memory.ORIGIN_DOCUMENT: 1,
        }


class TestFilteringByWorkspace:
    """Scoping has to happen in SQL, or LIMIT stops meaning anything."""

    def test_only_memories_filed_there(self, isolated_db):
        thesis = workspaces.create_workspace("Thesis")["id"]
        side = workspaces.create_workspace("Side project")["id"]

        here = memory.create("fact", "here", "Belongs to the thesis.")
        there = memory.create("fact", "there", "Belongs to the side project.")
        workspaces.file_item(workspaces.KIND_MEMORY, here["id"], thesis)
        workspaces.file_item(workspaces.KIND_MEMORY, there["id"], side)

        listed = memory.list_memories(workspace_id=thesis)
        assert [m["id"] for m in listed] == [here["id"]]

    def test_an_empty_workspace_returns_nothing_rather_than_everything(self, isolated_db):
        # The failure that matters: an empty `IN ()` clause, or a skipped
        # filter, turns "this project" into "all projects" silently.
        memory.create("fact", "loose", "Filed nowhere.")
        empty = workspaces.create_workspace("Empty")["id"]
        assert memory.list_memories(workspace_id=empty) == []

    def test_no_workspace_means_no_scoping(self, isolated_db):
        memory.create("fact", "loose", "Filed nowhere.")
        assert len(memory.list_memories(workspace_id="")) == 1

    def test_the_limit_still_limits_within_the_scope(self, isolated_db):
        space = workspaces.create_workspace("Many")["id"]
        for n in range(5):
            created = memory.create("fact", f"subject{n}", f"Fact number {n}.")
            workspaces.file_item(workspaces.KIND_MEMORY, created["id"], space)
        assert len(memory.list_memories(workspace_id=space, limit=2)) == 2


class TestOriginComesFromTheTurn:
    """`_memory_origin` is the one place that decides, so it is the one to test."""

    class Req:
        coder = False

    def test_an_ordinary_turn_is_chat(self):
        assert A._memory_origin(self.Req()) == memory.ORIGIN_CHAT

    def test_a_code_tab_turn_is_code(self):
        req = self.Req()
        req.coder = True
        assert A._memory_origin(req) == memory.ORIGIN_CODE

    def test_a_caller_that_knows_better_wins(self):
        # The doc-send path builds its own ChatRequest, so `coder` says nothing
        # useful about it — the endpoint names the origin instead.
        req = self.Req()
        req.coder = True
        assert A._memory_origin(req, memory.ORIGIN_DOCUMENT) == memory.ORIGIN_DOCUMENT


class TestExtractionCarriesTheOrigin:
    def test_a_stored_memory_keeps_the_turn_s_origin(self, isolated_db, monkeypatch):
        import json as _json

        class Client:
            def is_available(self):
                return True

            def structured_chat(self, messages, model=None, response_format=None):
                return _json.dumps({"memories": [{
                    "kind": "preference", "subject": "tabs",
                    "content": "The user prefers tabs.", "confidence": 0.9,
                }]})

        from carrot import ollama_client
        monkeypatch.setattr(ollama_client, "OllamaClient", lambda *a, **k: Client())

        stored = memory.extract_from_turn(
            "I always use tabs", "Noted.", origin=memory.ORIGIN_CODE)
        assert [m["origin"] for m in stored] == [memory.ORIGIN_CODE]


class TestTheApi:
    def test_a_hand_written_memory_is_marked_yours(self, client):
        created = client.post("/api/memory", json={
            "kind": "fact", "subject": "coffee", "content": "The user drinks coffee.",
        }).json()
        assert created["origin"] == memory.ORIGIN_MANUAL

    def test_the_list_can_be_narrowed_by_origin(self, client):
        client.post("/api/memory", json={
            "kind": "fact", "subject": "coffee", "content": "The user drinks coffee."})
        body = client.get("/api/memory", params={"origin": memory.ORIGIN_MANUAL}).json()
        assert len(body["memories"]) == 1
        assert client.get("/api/memory", params={"origin": "code"}).json()["memories"] == []

    def test_the_filter_options_come_from_the_server(self, client):
        body = client.get("/api/memory").json()
        assert [o["id"] for o in body["origins"]] == list(memory.ORIGINS)
        assert all(o["label"] and o["filter"] for o in body["origins"])

    def test_the_audit_shows_everything_unless_asked_otherwise(self, client):
        # Opening the panel inside a project must not quietly hide the rest of
        # what Carrot believes about you.
        space = client.post("/api/workspaces", json={"name": "Thesis"}).json()
        client.put("/api/workspaces/active", json={"workspace_id": space["id"]})
        client.post("/api/memory", json={
            "kind": "fact", "subject": "coffee", "content": "The user drinks coffee."})
        assert len(client.get("/api/memory").json()["memories"]) == 1

    def test_a_workspace_with_nothing_in_it_shows_nothing(self, client):
        client.post("/api/memory", json={
            "kind": "fact", "subject": "coffee", "content": "The user drinks coffee."})
        space = client.post("/api/workspaces", json={"name": "Empty"}).json()
        body = client.get("/api/memory", params={"workspace": space["id"]}).json()
        assert body["memories"] == []

    def test_search_honours_the_scope_it_was_given(self, client):
        space = client.post("/api/workspaces", json={"name": "Empty"}).json()
        client.post("/api/memory", json={
            "kind": "fact", "subject": "coffee", "content": "The user drinks coffee."})
        assert client.get("/api/memory/search",
                          params={"q": "coffee", "workspace": space["id"]}).json()["results"] == []


class TestUpgradingAnExistingDatabase:
    """The migration path, which is where this nearly went badly wrong.

    `get_db()` runs SCHEMA on every connection, and `CREATE TABLE IF NOT
    EXISTS` does nothing to a table that already exists. An index naming the
    new column, written into SCHEMA, therefore failed on *every* connection to
    a pre-existing database — including the one `init_db` had to open in order
    to run the migration at all. Nobody's Carrot would have started.
    """

    def old_database(self, tmp_path, monkeypatch):
        """A memories table exactly as it was before `origin` existed."""
        import sqlite3

        from carrot import database

        path = str(tmp_path / "old.db")
        conn = sqlite3.connect(path)
        conn.execute("""CREATE TABLE memories (
            id TEXT PRIMARY KEY, kind TEXT, subject TEXT, content TEXT,
            confidence REAL, status TEXT, pinned INTEGER,
            source_message_id INTEGER, source_conversation_id TEXT,
            superseded_by TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
        conn.execute(
            """INSERT INTO memories (id, kind, subject, content, confidence, status,
                                     pinned, created_at, updated_at)
               VALUES ('old1', 'fact', 'tea', 'The user drinks tea.', 1.0, 'active',
                       0, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')""")
        conn.commit()
        conn.close()

        monkeypatch.setattr(database, "DB_PATH", path)
        monkeypatch.setattr(database, "DBCORE_DIR", str(tmp_path))
        return path

    def test_opening_it_does_not_explode(self, tmp_path, monkeypatch):
        from carrot import database

        self.old_database(tmp_path, monkeypatch)
        database.get_db().close()   # this is what used to raise

    def test_the_column_is_added_and_old_rows_read_as_chat(self, tmp_path, monkeypatch):
        from carrot import database

        self.old_database(tmp_path, monkeypatch)
        database.init_db()
        conn = database.get_db()
        columns = {row[1] for row in conn.execute("PRAGMA table_info(memories)")}
        row = conn.execute("SELECT origin FROM memories WHERE id = 'old1'").fetchone()
        conn.close()
        assert "origin" in columns
        # Chat was the only thing that wrote memories, so this is the truth
        # about the old rows rather than a placeholder.
        assert row["origin"] == memory.ORIGIN_CHAT

    def test_the_index_arrives_with_the_column(self, tmp_path, monkeypatch):
        from carrot import database

        self.old_database(tmp_path, monkeypatch)
        database.init_db()
        conn = database.get_db()
        names = {r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        conn.close()
        assert "idx_memories_origin" in names


class TestOldRowsStillRead:
    def test_a_database_written_before_the_column_existed(self, isolated_db):
        from carrot.database import get_db

        conn = get_db()
        conn.execute("ALTER TABLE memories RENAME TO memories_old")
        conn.execute("""CREATE TABLE memories (
            id TEXT PRIMARY KEY, kind TEXT, subject TEXT, content TEXT,
            confidence REAL, status TEXT, pinned INTEGER,
            source_message_id INTEGER, source_conversation_id TEXT,
            superseded_by TEXT, created_at TEXT, updated_at TEXT)""")
        conn.execute(
            """INSERT INTO memories (id, kind, subject, content, confidence, status,
                                     pinned, created_at, updated_at)
               VALUES ('old1', 'fact', 'tea', 'The user drinks tea.', 1.0, 'active',
                       0, '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')""")
        conn.commit()
        conn.close()

        # Provenance must never be the reason a memory cannot be shown at all.
        assert memory.get("old1")["origin"] == memory.ORIGIN_CHAT


class TestThePanel:
    def test_the_row_says_where_it_was_learned(self):
        assert "memoryOriginLabel(m.origin)" in read_js("agentops.js")

    def test_both_filters_reach_the_request(self):
        source = read_js("agentops.js")
        assert "params.set('origin'" in source
        assert "params.set('workspace'" in source

    def test_the_panel_starts_unscoped(self):
        assert "workspace: 'all'" in read_js("agentops.js")

    def test_search_carries_the_scope_too(self):
        assert "&workspace=" in read_js("agentops.js")

    def test_an_empty_filtered_list_does_not_claim_nothing_is_remembered(self):
        assert "Nothing matches these filters." in read_js("agentops.js")

    def test_the_workspace_filter_is_rebuilt_not_filled_in_once(self):
        # A workspace created after this panel first opened has to appear in
        # its own filter.
        source = read_js("agentops.js")
        assert "wsEl.innerHTML = '<option value=\"all\">All workspaces</option>'" in source

    def test_the_markup_has_both_selects(self):
        root = Path(__file__).resolve().parents[1]
        html = (root / "carrot" / "web" / "index.html").read_text(encoding="utf-8")
        assert 'id="memory-origin"' in html
        assert 'id="memory-workspace"' in html
