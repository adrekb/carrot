"""The coding agent's HTTP surface, its git tools, and its wiring into chat.

The unit tests in test_coder.py prove the pieces work. These prove they are
actually connected: that plan mode reaches the tool list the model is offered,
that project rules reach the system prompt, and that git refuses the things it
should refuse rather than shelling out to something clever.
"""
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

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

    def test_no_rules_means_no_wasted_rules_message(self, isolated_db, tmp_path, monkeypatch):
        # The mode preamble is always sent; an empty rules block never is.
        monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
        blocks = A._coder_context()
        assert not any("rule" in b["content"].lower() for b in blocks)

    def test_act_mode_states_itself_to_the_model(self, isolated_db, tmp_path, monkeypatch):
        """The bug behind "why can't the agent edit files".

        Act's preamble was written and then never sent: the branch only emitted
        the plan brief, and with no brief it emitted nothing at all. The one
        mode whose whole job is "use the tools" was the one that never said so,
        and a small local model did what models do without instruction — it
        printed the file into the chat.
        """
        from carrot import config

        monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
        config.set_config("coder_mode", "act")
        blocks = A._coder_context()
        assert any("ACT mode" in b["content"] for b in blocks)

    def test_act_mode_is_told_not_to_paste_files(self, isolated_db, tmp_path, monkeypatch):
        from carrot import config

        monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
        config.set_config("coder_mode", "act")
        joined = " ".join(b["content"] for b in A._coder_context())
        assert "write_file" in joined

    def test_plan_mode_states_itself_to_the_model(self, isolated_db, tmp_path, monkeypatch):
        from carrot import config

        monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
        config.set_config("coder_mode", "plan")
        assert any("PLAN mode" in b["content"] for b in A._coder_context())


class TestTheCodingModeStaysInTheCodeTab:
    """`coder_mode` is one global setting with no idea which panel is asking.

    So the plan/act preamble and the workspace's rules rode on every message
    in the app. Asked for "recent china political news", the model arrived
    holding an ACT-mode preamble telling it to use write_file and a set of
    rules for a Pong workspace, and went off to read pong.py — which is
    exactly what it had just been told it was for.

    The turns that want it say so; nothing else gets it.
    """

    def _history(self, coder, tmp_path, monkeypatch):
        from carrot import config, conversation as conv_mod

        monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
        (tmp_path / "AGENTS.md").write_text("never use global state")
        config.set_config("coder_mode", "act")
        conv = conv_mod.create_conversation("news")
        history, _ = A._prepare_history(conv, "recent china political news",
                                        None, coder=coder)
        return " ".join(m["content"] for m in history if m["role"] == "system")

    def test_an_ordinary_chat_is_not_told_it_is_a_coding_agent(
            self, isolated_db, tmp_path, monkeypatch):
        system = self._history(False, tmp_path, monkeypatch)
        assert "ACT mode" not in system
        assert "write_file" not in system
        assert "never use global state" not in system, "workspace rules leaked"

    def test_the_code_tab_still_gets_it(self, isolated_db, tmp_path, monkeypatch):
        system = self._history(True, tmp_path, monkeypatch)
        assert "ACT mode" in system
        assert "never use global state" in system

    def test_the_panel_asks_for_it_and_nothing_else_does(self):
        from pathlib import Path

        web = Path(__file__).resolve().parents[1] / "carrot" / "web" / "js"
        assert "coder: true" in (web / "features.js").read_text(encoding="utf-8"), (
            "the agent panel does not flag its turns, so the Code tab loses "
            "its mode preamble and rules entirely")
        assert "coder: true" not in (web / "app.js").read_text(encoding="utf-8"), (
            "ordinary chat is flagging itself as a coding turn again")


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
        return root.joinpath(*parts).read_text(encoding="utf-8")

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


class TestAgentPanel:
    """A Plan/Act switch with nowhere to talk to the agent is a steering wheel
    with no car attached. This is the car."""

    def read(self, *parts):
        from pathlib import Path
        return Path(__file__).resolve().parents[1].joinpath("carrot", "web", *parts).read_text(encoding="utf-8")

    def test_the_panel_exists_in_the_code_tab(self):
        html = self.read("index.html")
        assert 'id="agent-side"' in html and 'id="agent-log"' in html

    def test_there_is_a_way_to_reopen_it(self):
        # Closing a panel with no way back is how a feature disappears.
        assert 'onclick="toggleAgentSide()"' in self.read("index.html")

    def test_it_streams_the_same_chat_endpoint(self):
        assert "'/api/chat/stream'" in self.read("js", "features.js")

    def test_the_open_file_is_sent_as_context(self):
        # So "fix this" works without pasting a path.
        js = self.read("js", "features.js")
        assert "function agentContext" in js and "activeFilePath" in js

    def test_tool_calls_are_shown_as_they_happen(self):
        js = self.read("js", "features.js")
        assert "payload.tool" in js and "agentTrace" in js

    def test_approval_prompts_are_answerable_from_the_panel(self):
        # Otherwise a mutating tool blocks silently until it times out.
        js = self.read("js", "features.js")
        assert "/api/agent/approvals/" in js

    def test_the_panel_listens_for_the_event_the_server_actually_sends(self):
        """It listened for `payload.approval`, which nothing has ever sent.

        The event is `approval_request`. So the card was never built: the panel
        sat on a turn waiting for a click that could not happen, and ended at
        the timeout ten minutes later. The old assertion here was
        `"payload.approval" in js`, which is a *substring* of the correct
        `payload.approval_request` — it passed before the fix and after it, and
        could never have failed. So this asks the server what it emits instead
        of asking the file what it says.
        """
        from carrot import agent_tools

        emitted = []

        def emit(event):
            emitted.append(event)
            request = event.get("approval_request")
            if request:
                agent_tools.resolve_approval(request["id"], "allow")

        agent_tools.request_approval(
            "write_file", {"path": "a.txt", "content": "x"}, "Write a.txt",
            "low", emit)

        keys = {k for event in emitted for k in event}
        assert "approval_request" in keys, "the flow changed; this test is stale"

        js = self.read("js", "features.js")
        listened = {k for k in keys if f"payload.{k}" in js}
        assert "approval_request" in listened, (
            f"the panel does not handle approval_request; server sends {keys}"
        )

    def test_the_panel_answers_in_the_shape_the_endpoint_accepts(self, client):
        """It POSTed {approved: true}. The endpoint requires {decision: ...}.

        So even once the card rendered, the only button that could unblock the
        turn would have been rejected before reaching the gate. Sending the
        real body at the real endpoint is the only way to catch that.
        """
        from carrot import agent_tools

        holder = {}

        def emit(event):
            request = event.get("approval_request")
            if request:
                holder["id"] = request["id"]

        # daemon, and released in `finally`: a blocked request_approval waits
        # APPROVAL_TIMEOUT_SECONDS, so an assertion that fires before the POST
        # would otherwise hang the whole suite for ten minutes.
        thread = threading.Thread(
            target=agent_tools.request_approval,
            args=("write_file", {"path": "a.txt", "content": "x"},
                  "Write a.txt", "low", emit),
            daemon=True)
        thread.start()
        try:
            for _ in range(50):
                if holder.get("id"):
                    break
                time.sleep(0.05)
            assert holder.get("id"), "no approval was raised"

            js = self.read("js", "features.js")
            body = re.search(r"JSON\.stringify\(\{\s*decision:[^}]*\}\)", js)
            assert body, "the panel no longer sends a `decision`; it will 422"

            resp = client.post(f"/api/agent/approvals/{holder['id']}",
                               json={"decision": "allow"})
            assert resp.status_code == 200, resp.text
        finally:
            if holder.get("id"):
                agent_tools.resolve_approval(holder["id"], "deny")
            thread.join(timeout=5)

    def test_the_task_can_be_stopped(self):
        js = self.read("js", "features.js")
        assert "AbortController" in js and "function stopAgentTask" in js

    def test_the_tree_refreshes_after_the_agent_touches_files(self):
        # Bounded by the end of the function rather than the first 5000
        # characters of it: that window was an accident of how long the body
        # happened to be, and adding comments to it broke the test without
        # changing any behaviour. A top-level `}` on its own line ends it.
        js = self.read("js", "features.js")
        body = js.split("async function sendAgentTask")[1].split("\n}\n")[0]
        assert "loadCodeTree();" in body and "loadCoderState();" in body

    def test_attachments_are_supported(self):
        html, js = self.read("index.html"), self.read("js", "features.js")
        assert 'id="agent-attach-tray"' in html
        assert "function addAgentAttachments" in js
        assert "attachments: attachments.map" in js

    def test_a_screenshot_can_be_pasted(self):
        assert "clipboardData?.files" in self.read("js", "features.js")

    def test_files_can_be_dropped_on_the_panel(self):
        js = self.read("js", "features.js")
        assert "dataTransfer?.files" in js

    def test_a_non_vision_model_is_flagged_before_sending(self):
        # Turning "400: gemma cannot read images" into a sentence that appears
        # while there is still time to switch models.
        assert "function warnIfNoVision" in self.read("js", "features.js")

    def test_the_mode_switch_lives_with_the_agent_it_governs(self):
        """One control, not two at opposite ends of the screen.

        Plan/Act sat at the far left of the editor bar while the agent sat at
        the far right behind a chip that also displayed the mode — two
        representations of one piece of state, and the read-only one looked
        like a button.
        """
        html = self.read("index.html")
        head = html.split('class="agent-side-head"')[1].split("</div>")[0]
        assert 'id="mode-plan"' in head and 'id="mode-act"' in head

    def test_there_is_only_one_mode_switch(self):
        """One Plan/Act control, in the agent's own header.

        Counted inside the Code view rather than across the page: the same
        two-button component is used elsewhere now — the LaTeX tab switches
        between split and reading with it — and a page-wide count would make
        this fail for a control that has nothing to do with what the agent is
        allowed to do to your files.
        """
        html = self.read("index.html")
        assert html.count('id="mode-plan"') == 1
        # Bounded by the next view rather than by a named neighbour. It used to
        # end at `id="view-latex"`, which stopped existing the day LaTeX became
        # a pane inside Write — and a slice anchored on a section that has
        # moved fails for a reason that has nothing to do with what it tests.
        start = html.index('id="view-code"')
        end = html.index('<section id="view-', start + 1)
        code_view = html[start:end]
        assert code_view.count('class="mode-switch"') == 1

    def test_the_status_line_says_what_the_agent_may_do(self):
        # "act" told the user nothing about whether their files were at risk.
        js = self.read("js", "features.js")
        assert "mode-status" in js
        assert "delete files" in js

    def test_every_css_token_the_panel_uses_is_defined(self):
        import re

        css = self.read("css", "style.css")
        block = css.split("/* ===== The agent panel =====")[1]
        used = set(re.findall(r"var\((--[a-z0-9-]+)", block))
        defined = set(re.findall(r"^\s*(--[a-z0-9-]+):", css, re.M))
        assert used <= defined, f"undefined CSS tokens: {sorted(used - defined)}"


class TestPythonIsFound:
    """A perfectly good Windows install reported 'Python is not set up'."""

    def test_the_windows_name_is_tried_too(self):
        from unittest.mock import patch

        from carrot import runner

        # `python3` is the Linux and macOS name; Windows ships `python.exe`.
        with patch.object(runner.shutil, "which",
                          side_effect=lambda n: r"C:\Python\python.exe" if n == "python" else None):
            assert runner.find_python() == r"C:\Python\python.exe"

    def test_the_py_launcher_is_the_last_resort(self):
        from unittest.mock import patch

        from carrot import runner

        with patch.object(runner.shutil, "which",
                          side_effect=lambda n: r"C:\Windows\py.exe" if n == "py" else None):
            assert runner.find_python() == r"C:\Windows\py.exe"

    def test_the_microsoft_store_stub_is_skipped(self, tmp_path):
        from unittest.mock import patch

        from carrot import runner

        # Windows ships a zero-byte alias at this path that opens the Store
        # instead of running anything — using it looks like a silent crash.
        stub = tmp_path / "WindowsApps" / "python3.exe"
        stub.parent.mkdir()
        stub.write_bytes(b"")
        with patch.object(runner.shutil, "which",
                          side_effect=lambda n: str(stub) if n == "python3" else None):
            assert runner.find_python() is None

    def test_nothing_installed_is_none_not_a_crash(self):
        from unittest.mock import patch

        from carrot import runner

        with patch.object(runner.shutil, "which", return_value=None):
            assert runner.find_python() is None


class TestTheQuestionFormIsWiredUp:
    """Parsed in Python, rendered in the panel, and it has to cross that gap.

    The approval card is the cautionary tale: it listened for an event nobody
    sent, and the test asserted a substring that could not fail. So these check
    the event name the server actually emits against the one the panel reads.
    """

    def read(self, *parts):
        from pathlib import Path
        return Path(__file__).resolve().parents[1].joinpath(
            "carrot", "web", *parts).read_text(encoding="utf-8")

    def test_the_server_emits_questions_under_the_name_the_panel_reads(self):
        server = Path(A.__file__).read_text(encoding="utf-8")
        assert "'questions': questions" in server or '"questions"' in server
        js = self.read("js", "features.js")
        assert "payload.questions" in js, "the panel never sees the event"

    def test_a_broken_block_cannot_cost_the_answer(self):
        # The parse sits in the SSE body, after the 200 and the headers have
        # gone out: an exception there is a closed socket, not an error
        # response, and the user gets a turn that ends with no text at all.
        #
        # The parse moved when questions became turn-ending — it reads the
        # gate's accumulated text rather than the finished reply, because by
        # then the block has already been cut out of it. The guarantee did not
        # move, so this follows it to the new call site rather than retiring.
        lines = Path(A.__file__).read_text(encoding="utf-8").splitlines()
        at = next(i for i, line in enumerate(lines)
                  if "asked.questions()" in line)
        before = "\n".join(lines[max(0, at - 6):at])
        after = "\n".join(lines[at:at + 6])
        assert "try:" in before, "the questions parse is not inside a try"
        assert "except" in after, "the try around the questions parse has no except"

    def test_answering_switches_to_act(self):
        # The whole point: the form is the moment Act was waiting for, so the
        # user should not have to find the mode button afterwards.
        js = self.read("js", "features.js")
        submit = js.split("async function submitAgentQuestions")[1].split("\n}\n")[0]
        assert "setCoderMode('act')" in submit
        assert "sendAgentTask()" in submit

    def test_skipping_answers_rather_than_saying_nothing(self):
        # Skip means "pick something sensible", so it submits the model's own
        # first option for each rather than sending an empty turn.
        js = self.read("js", "features.js")
        assert "q.options[0]" in js

    def test_the_raw_block_is_stripped_from_what_is_displayed(self):
        js = self.read("js", "features.js")
        assert "function stripQuestions" in js
        assert "stripQuestions(answer)" in js, (
            "the answer is rendered without stripping, so the user sees the raw "
            "JSON as well as the form built from it")

    def test_chat_listens_for_questions_too(self):
        """The Code panel had a form; chat had the event and no listener.

        So in ordinary chat the questions were invisible *and* self-answered —
        the server parsed them, emitted them, and nothing on the page read the
        event. A form that only exists in one of the two places the model can
        ask from is not a form, it is a coincidence.
        """
        js = self.read("js", "app.js")
        assert "payload.questions" in js, "chat never sees the questions event"
        assert "function chatQuestions" in js

    def test_the_blocking_flag_reaches_both_panels(self):
        """Whether a turn is waiting is decided by the server.

        It is the only side that saw where the question fell in the reply.
        Re-deriving it in the browser would be a second opinion on a settled
        question, and the two would disagree the first time either changed.
        """
        server = Path(A.__file__).read_text(encoding="utf-8")
        assert '"blocking": asked.blocking()' in server
        for name in ("app.js", "features.js"):
            assert "blocking" in self.read("js", name), f"{name} ignores it"

    def test_a_waiting_turn_does_not_get_a_manufactured_answer(self):
        """The empty-answer recovery must not fire on a turn that asked.

        That path is built never to come back empty — it falls through to an
        answer written from the evidence. Running it over a question would
        rebuild exactly what the cut just removed, with more conviction.
        """
        server = Path(A.__file__).read_text(encoding="utf-8")
        assert "not (asked and asked.blocking())" in server

    def test_a_waiting_turn_does_not_write_memories(self):
        """Memory extraction reads conclusions out of a turn.

        A turn that ended on a question has not concluded anything, so letting
        it run files the guesses the model was asking about as things now
        known about the user — the failure the gate exists to prevent, made
        durable.
        """
        server = Path(A.__file__).read_text(encoding="utf-8")
        # The call site, not the `def` — searching for the bare name finds the
        # definition first and would pass against anything. Found by "indented
        # and not a def" rather than by an exact indent: the call moved out of
        # the SSE body into `_persist_turn` when a cancelled turn stopped being
        # thrown away, which changed its indentation and nothing else.
        sites = [m.start() for m in re.finditer(r"\n\s+_post_turn\(", server)
                 if "def _post_turn" not in server[m.start():m.start() + 40]]
        assert sites, "the memory extractor is never called"
        for at in sites:
            window = server[max(0, at - 400):at]
            assert "pending_questions" in window and "blocking" in window


class TestTheCodingAgentCanLookThingsUp:
    """It could not. The panel sent search_mode "off", which does not merely
    discourage searching — SEARCH_OFF contributes an empty tool set, so
    web_search and read_url were removed from what the model was offered.

    An agent that cannot check a library signature or paste an unfamiliar
    error into a search guesses instead, and a small model guessing at an API
    is where the afternoon goes.
    """

    def test_the_panel_does_not_disable_search(self):
        from pathlib import Path

        js = (Path(__file__).resolve().parents[1] / "carrot" / "web" / "js"
              / "features.js").read_text(encoding="utf-8")
        send = js.split("async function sendAgentTask")[1].split("\n}\n")[0]
        assert "search_mode: 'off'" not in send, (
            "the agent cannot look up an API or an error message")
        assert "search_mode: 'single'" in send, (
            "multi-turn search would have it researching instead of working")

    def test_off_really_does_remove_the_tools(self):
        # The reason "off" was the wrong setting rather than a mild preference.
        assert A.SEARCH_MODES[A.SEARCH_OFF]["tools"] == set()
        assert "web_search" in A.SEARCH_MODES[A.SEARCH_SINGLE]["tools"]
        assert "read_url" in A.SEARCH_MODES[A.SEARCH_SINGLE]["tools"]

    def test_act_mode_says_what_the_web_is_for(self, isolated_db, tmp_path, monkeypatch):
        # Otherwise it either ignores the tools or disappears into them.
        guidance = coder.MODE_PREAMBLE[coder.MODE_ACT]
        assert "search the web" in guidance
        assert "error message" in guidance


class TestYouCanSeeWhatYouAreApproving:
    """"Write 4145 characters to magnetic_field_simulator.py" is not something
    anyone can agree to. Seven of those in a row get seven reflex clicks, and
    a gate that is always waved through has stopped being a gate.

    The content was in the request the whole time and thrown away at the point
    of asking, which is the one moment it was needed.
    """

    def read(self, *parts):
        return Path(__file__).resolve().parents[1].joinpath(
            "carrot", "web", *parts).read_text(encoding="utf-8")

    def test_the_card_can_show_what_would_be_written(self):
        js = self.read("js", "features.js")
        assert "function approvalPreview" in js
        body = js.split("function approvalPreview")[1].split("\n}\n")[0]
        for tool in ("write_file", "edit_file", "run_command",
                     "delete_file", "move_file"):
            assert tool in body, f"{tool} approvals show no preview"

    def test_an_unknown_tool_still_shows_its_arguments(self):
        # Pack and MCP tools are not enumerable here, and a bare summary for
        # them is the same problem again.
        js = self.read("js", "features.js")
        body = js.split("function approvalPreview")[1].split("\n}\n")[0]
        assert "default" in body and "JSON.stringify(args" in body

    def test_the_preview_is_folded_away(self):
        # Seven expanded cards would be worse than seven collapsed ones.
        js = self.read("js", "features.js")
        assert "createElement('details')" in js

    def test_an_answered_prompt_does_not_look_answerable(self):
        # The buttons were disabled in script and styled as though live, so a
        # column of resolved cards read as decisions still waiting.
        css = self.read("css", "style.css")
        assert ".agent-approval button:disabled" in css

    def test_the_model_is_told_it_can_make_folders(self):
        # It always could — write_file creates missing parents — but nothing
        # said so, so it put everything at the top level instead.
        spec = agent_tools.TOOLS["write_file"]
        assert "folders" in spec["description"].lower()
