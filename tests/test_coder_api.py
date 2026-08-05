"""The coding agent's HTTP surface, its git tools, and its wiring into chat.

The unit tests in test_coder.py prove the pieces work. These prove they are
actually connected: that plan mode reaches the tool list the model is offered,
that project rules reach the system prompt, and that git refuses the things it
should refuse rather than shelling out to something clever.
"""
import json
import os
import subprocess

import pytest

from carrot import agent_tools, app as A, coder, gitops


def make_repo(path):
    """A real git repo with one commit, or skip — these tests test git."""
    if not gitops.git_available():
        pytest.skip("git is not installed")
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=path, check=True)


class TestCoderEndpoints:
    def test_state_reports_the_mode_and_its_guidance(self, client):
        body = client.get("/api/coder/state").json()
        assert body["mode"] in coder.MODES
        assert body["guidance"]
        assert body["modes"] == list(coder.MODES)

    def test_the_mode_can_be_switched(self, client):
        assert client.put("/api/coder/mode", json={"mode": "plan"}).json()["mode"] == "plan"
        assert client.get("/api/coder/state").json()["mode"] == "plan"

    def test_an_unknown_mode_is_rejected_rather_than_silently_coerced(self, client):
        # Coercing "acr" to plan would leave the user staring at a mode they
        # did not ask for and no explanation.
        assert client.put("/api/coder/mode", json={"mode": "acr"}).status_code == 400

    def test_a_recipe_round_trips_through_the_api(self, client):
        saved = client.put("/api/coder/recipes", json={
            "id": "tidy", "title": "Tidy", "prompt": "Clean {{path}}",
        })
        assert saved.status_code == 200
        rendered = client.post("/api/coder/recipes/tidy/render", json={"values": {"path": "src"}})
        assert rendered.json()["prompt"] == "Clean src"

    def test_a_recipe_missing_a_parameter_is_a_400(self, client):
        client.put("/api/coder/recipes", json={"id": "tidy", "prompt": "Clean {{path}}"})
        assert client.post("/api/coder/recipes/tidy/render", json={"values": {}}).status_code == 400

    def test_an_invalid_recipe_id_is_a_400(self, client):
        assert client.put("/api/coder/recipes",
                          json={"id": "NOPE!", "prompt": "x"}).status_code == 400

    def test_deleting_an_unknown_recipe_is_a_404(self, client):
        assert client.delete("/api/coder/recipes/ghost").status_code == 404

    def test_restoring_an_unknown_checkpoint_is_a_404(self, client):
        assert client.post("/api/coder/checkpoints/ghost/restore").status_code == 404

    def test_a_checkpoint_can_be_taken_and_listed(self, client, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
        (tmp_path / "a.py").write_text("x")
        made = client.post("/api/coder/checkpoints", json={"label": "before"}).json()
        listed = client.get("/api/coder/checkpoints").json()["checkpoints"]
        assert made["id"] in [c["id"] for c in listed]


class TestPlanModeIsEnforced:
    """The whole value of plan/act is that plan *cannot* write."""

    def test_plan_mode_strips_write_tools_from_what_the_model_sees(self, isolated_db):
        from carrot import config

        config.set_config("coder_mode", "plan")
        names = {t["function"]["name"] for t in A._available_tools()}
        assert "carrot__read_file" in names
        assert "carrot__write_file" not in names
        assert "carrot__run_command" not in names
        assert "carrot__edit_file" not in names

    def test_act_mode_offers_them(self, isolated_db):
        from carrot import config

        config.set_config("coder_mode", "act")
        names = {t["function"]["name"] for t in A._available_tools()}
        assert "carrot__write_file" in names

    def test_the_default_is_act_so_existing_behaviour_is_unchanged(self, isolated_db):
        names = {t["function"]["name"] for t in A._available_tools()}
        assert "carrot__write_file" in names


class TestRulesReachThePrompt:
    def test_a_repos_rules_are_injected(self, isolated_db, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
        (tmp_path / "AGENTS.md").write_text("never use global state")
        blocks = A._coder_context()
        assert any("never use global state" in b["content"] for b in blocks)

    def test_no_rules_means_no_wasted_system_message(self, isolated_db, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
        assert A._coder_context() == []

    def test_plan_mode_states_itself_to_the_model(self, isolated_db, tmp_path, monkeypatch):
        from carrot import config

        monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
        config.set_config("coder_mode", "plan")
        assert any("PLAN mode" in b["content"] for b in A._coder_context())


class TestEditFileTool:
    def test_a_matching_edit_is_applied_and_journaled(self, isolated_db, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
        (tmp_path / "a.py").write_text("def f():\n    return 1\n")
        out = agent_tools._tool_edit_file(
            "a.py", "------- SEARCH\n    return 1\n=======\n    return 2\n+++++++ REPLACE"
        )
        assert (tmp_path / "a.py").read_text() == "def f():\n    return 2\n"
        assert "Revert with journal entry" in out

    def test_a_non_matching_edit_leaves_the_file_alone(self, isolated_db, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
        (tmp_path / "a.py").write_text("original\n")
        out = agent_tools._tool_edit_file(
            "a.py", "------- SEARCH\nabsent\n=======\nnew\n+++++++ REPLACE"
        )
        # Structured, not prose: a generic "edit failed" makes a small model
        # panic and rewrite the whole file.
        payload = json.loads(out)
        assert payload["status"] == "REJECTED" and payload["path"] == "a.py"
        assert (tmp_path / "a.py").read_text() == "original\n"

    def test_editing_a_missing_file_points_at_write_file(self, isolated_db, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
        out = agent_tools._tool_edit_file("ghost.py", "------- SEARCH\na\n=======\nb\n+++++++ REPLACE")
        assert "write_file" in out

    def test_malformed_blocks_explain_the_format(self, isolated_db, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
        (tmp_path / "a.py").write_text("x\n")
        assert "SEARCH" in agent_tools._tool_edit_file("a.py", "just change it please")

    def test_the_tool_is_registered_and_mutating(self):
        assert agent_tools.TOOLS["edit_file"]["mutating"] is True

    def test_the_approval_summary_says_what_changes(self):
        summary = agent_tools._summarize_call(
            "edit_file", {"path": "a.py", "edits": "x\n=======\ny"}
        )
        assert "a.py" in summary and "characters" not in summary


class TestGitOps:
    def test_status_parses_the_branch_and_changes(self, tmp_path):
        make_repo(tmp_path)
        (tmp_path / "a.py").write_text("x = 2\n")
        state = gitops.status(str(tmp_path))
        assert state["clean"] is False
        assert any(c["path"] == "a.py" for c in state["changes"])

    def test_a_clean_tree_says_so(self, tmp_path):
        make_repo(tmp_path)
        assert gitops.status(str(tmp_path))["clean"] is True

    def test_diff_shows_the_change(self, tmp_path):
        make_repo(tmp_path)
        (tmp_path / "a.py").write_text("x = 2\n")
        assert "x = 2" in gitops.diff(str(tmp_path))

    def test_log_is_parsed_into_fields(self, tmp_path):
        make_repo(tmp_path)
        entries = gitops.log(str(tmp_path))
        assert entries[0]["subject"] == "first" and entries[0]["sha"]

    def test_commit_records_the_change(self, tmp_path):
        make_repo(tmp_path)
        (tmp_path / "a.py").write_text("x = 2\n")
        gitops.commit(str(tmp_path), "second change")
        assert gitops.log(str(tmp_path))[0]["subject"] == "second change"

    def test_an_empty_commit_message_is_refused(self, tmp_path):
        make_repo(tmp_path)
        (tmp_path / "a.py").write_text("x = 2\n")
        with pytest.raises(gitops.GitError):
            gitops.commit(str(tmp_path), "  ")

    def test_committing_a_clean_tree_is_refused(self, tmp_path):
        make_repo(tmp_path)
        with pytest.raises(gitops.GitError):
            gitops.commit(str(tmp_path), "nothing to say")

    def test_a_non_repo_says_so_plainly(self, tmp_path):
        if not gitops.git_available():
            pytest.skip("git is not installed")
        with pytest.raises(gitops.GitError) as caught:
            gitops.status(str(tmp_path))
        assert "git init" in str(caught.value)

    def test_a_branch_name_is_an_argument_not_shell_text(self, tmp_path):
        # If this were shelled out, the `;` would run a second command. It is
        # passed as one argv entry, so git simply rejects the name.
        make_repo(tmp_path)
        marker = tmp_path / "pwned.txt"
        with pytest.raises(gitops.GitError):
            gitops.create_branch(str(tmp_path), f"x; touch {marker}")
        assert not marker.exists()

    def test_a_branch_can_be_created_and_switched_to(self, tmp_path):
        make_repo(tmp_path)
        gitops.create_branch(str(tmp_path), "feature")
        assert gitops.branches(str(tmp_path))["current"] == "feature"

    def test_a_flag_shaped_branch_name_is_refused(self, tmp_path):
        make_repo(tmp_path)
        with pytest.raises(gitops.GitError):
            gitops.create_branch(str(tmp_path), "--force")

    def test_git_tools_are_registered_read_only_where_they_should_be(self):
        assert agent_tools.TOOLS["git_status"]["mutating"] is False
        assert agent_tools.TOOLS["git_diff"]["mutating"] is False
        assert agent_tools.TOOLS["git_log"]["mutating"] is False
        assert agent_tools.TOOLS["git_commit"]["mutating"] is True


class TestCodeTabWiring:
    """The switch is worthless if the tab does not draw it or call it."""

    def read(self, *parts):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1] / "carrot" / "web"
        return root.joinpath(*parts).read_text()

    def test_the_mode_switch_is_in_the_markup(self):
        html = self.read("index.html")
        assert 'onclick="setCoderMode(\'plan\')"' in html
        assert 'onclick="setCoderMode(\'act\')"' in html

    def test_the_source_control_and_checkpoint_panels_exist(self):
        html = self.read("index.html")
        assert 'id="panel-git"' in html
        assert 'id="panel-checkpoints"' in html

    def test_the_panel_switcher_knows_the_new_panels(self):
        js = self.read("js", "features.js")
        assert "'output', 'terminal', 'git', 'checkpoints'" in js

    def test_the_state_is_loaded_when_the_tab_opens(self):
        assert "loadCoderState();" in self.read("js", "features.js")

    def test_restoring_a_checkpoint_asks_first(self):
        # It throws away work; doing it on a single click would be hostile.
        js = self.read("js", "features.js")
        assert "async function restoreCheckpoint" in js
        assert "confirm(" in js.split("async function restoreCheckpoint")[1][:400]

    def test_every_css_token_the_agent_bar_uses_is_defined(self):
        import re

        css = self.read("css", "style.css")
        block = css.split("/* ===== The coding agent =====")[1]
        used = set(re.findall(r"var\((--[a-z0-9-]+)", block))
        defined = set(re.findall(r"^\s*(--[a-z0-9-]+):", css, re.M))
        assert used <= defined, f"undefined CSS tokens: {sorted(used - defined)}"
