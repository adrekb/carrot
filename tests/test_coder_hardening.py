"""The six hardening passes on the coding agent.

Each one exists because the naive version fails a specific way with a small
open-weights model: it hallucinates a tool it was never given, it panics and
rewrites a whole file when an edit is rejected, it drags a planning transcript
into the implementation, it attends to none of eight hundred lines of merged
rules, and it invokes a recipe with a parameter missing.
"""
import json
import os
import subprocess

import pytest

from carrot import agent_tools, app as A, coder, config, gitops


def make_repo(path):
    if not gitops.git_available():
        pytest.skip("git is not installed")
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "first"], cwd=path, check=True)


# ===== 1. Plan/Act, enforced client-side as well as by omission =====

class TestHallucinatedToolCalls:
    def test_a_write_tool_called_in_plan_mode_is_rejected(self):
        # Removing the declaration is the first defence. A small model will
        # still emit a name it saw in training and was never offered.
        refusal = coder.reject_tool("carrot__write_file", coder.MODE_PLAN)
        assert refusal["status"] == "REJECTED"
        assert "PLAN mode" in refusal["reason"]

    def test_the_rejection_says_what_to_do_instead(self):
        refusal = coder.reject_tool("carrot__run_command", coder.MODE_PLAN)
        assert "ACT mode" in refusal["fix"]

    def test_a_read_tool_is_not_rejected(self):
        assert coder.reject_tool("carrot__read_file", coder.MODE_PLAN) is None

    def test_nothing_is_rejected_in_act_mode(self):
        assert coder.reject_tool("carrot__write_file", coder.MODE_ACT) is None

    def test_the_loop_rejects_before_running_anything(self, isolated_db):
        config.set_config("coder_mode", "plan")
        events = list(A._run_tool("carrot__write_file", {"path": "x", "content": "y"}, None))
        payload = json.loads(events[-1]["_tool_result"])
        assert payload["status"] == "REJECTED"

    def test_the_loop_still_runs_read_tools_in_plan_mode(self, isolated_db, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
        (tmp_path / "a.py").write_text("hello")
        config.set_config("coder_mode", "plan")
        events = list(A._run_tool("carrot__read_file", {"path": "a.py"}, None))
        assert "hello" in events[-1]["_tool_result"]


# ===== 1b. Plan -> Act context compaction =====

class TestCompaction:
    def history(self):
        return [
            {"role": "user", "content": "add retries to the client"},
            {"role": "assistant", "content": "I read client.py; it has no retry loop."},
            {"role": "user", "content": "use exponential backoff"},
        ]

    def test_a_brief_is_produced_from_the_plan(self, isolated_db):
        def fake(resolved, messages, tools=None):
            yield {"type": "text", "text": "GOAL — add retries.\nFILES — client.py"}

        brief = coder.compact_plan(self.history(), None, fake)
        assert "GOAL" in brief

    def test_the_transcript_is_what_gets_compacted(self, isolated_db):
        captured = {}

        def fake(resolved, messages, tools=None):
            captured["prompt"] = messages[0]["content"]
            yield {"type": "text", "text": "brief"}

        coder.compact_plan(self.history(), None, fake)
        assert "exponential backoff" in captured["prompt"]
        assert "GOAL" in captured["prompt"]  # the requested sections

    def test_system_messages_are_not_part_of_the_plan(self):
        history = [{"role": "system", "content": "you are a bot"}] + self.history()
        assert all(m["role"] != "system" for m in coder.plan_messages(history))

    def test_a_failed_compaction_never_blocks_the_switch(self, isolated_db):
        def broken(resolved, messages, tools=None):
            raise RuntimeError("model went away")
            yield  # pragma: no cover

        # Being stuck in Plan because a summariser hiccuped is far worse than
        # falling back to carrying the history.
        assert coder.compact_plan(self.history(), None, broken) == ""

    def test_an_empty_plan_compacts_to_nothing(self, isolated_db):
        assert coder.compact_plan([], None, lambda *a, **k: iter([])) == ""

    def test_a_brief_is_stored_and_read_back(self, isolated_db):
        coder.store_snapshot("conv-1", "GOAL — do the thing")
        assert "do the thing" in coder.snapshot_for("conv-1")

    def test_an_empty_brief_is_not_stored(self, isolated_db):
        assert coder.store_snapshot("conv-1", "   ") is False

    def test_the_brief_reaches_the_act_prompt(self, isolated_db, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
        config.set_config("coder_mode", "act")
        coder.store_snapshot("conv-1", "GOAL — add retries")
        blocks = A._coder_context("conv-1")
        assert any("add retries" in b["content"] for b in blocks)

    def test_the_brief_does_not_leak_into_plan_mode(self, isolated_db, tmp_path, monkeypatch):
        # It describes a plan that is now being reconsidered; carrying it into
        # the new plan would anchor it.
        monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
        config.set_config("coder_mode", "plan")
        coder.store_snapshot("conv-1", "GOAL — add retries")
        blocks = A._coder_context("conv-1")
        assert not any("add retries" in b["content"] for b in blocks)

    def test_switching_back_to_plan_discards_the_brief(self, client, isolated_db):
        coder.store_snapshot("conv-1", "GOAL — old plan")
        client.put("/api/coder/mode", json={"mode": "plan", "conversation_id": "conv-1"})
        assert coder.snapshot_for("conv-1") == ""

    def test_the_brief_is_exposed_so_it_can_be_reviewed(self, client, isolated_db):
        coder.store_snapshot("conv-9", "GOAL — visible")
        assert "visible" in client.get("/api/coder/brief/conv-9").json()["brief"]


# ===== 2. Structural feedback on a rejected edit =====

class TestEditRejectionFeedback:
    def test_a_mismatch_names_the_line_and_both_sides(self):
        content = "def f():\n    return 1\n    print('done')\n"
        with pytest.raises(coder.EditError) as caught:
            coder.apply_edits(content, [("def f():\n    return 2", "x")])
        payload = caught.value.payload
        assert payload["status"] == "REJECTED"
        assert payload["line"] == 2
        assert "return 2" in payload["expected"]
        assert "return 1" in payload["found"]

    def test_the_message_reads_like_a_compiler_not_an_apology(self):
        with pytest.raises(coder.EditError) as caught:
            coder.apply_edits("alpha\nbeta\n", [("alpha\ngamma", "x")])
        assert "expected 'gamma', found 'beta'" in str(caught.value)

    def test_a_block_whose_first_line_is_absent_says_so(self):
        with pytest.raises(coder.EditError) as caught:
            coder.apply_edits("alpha\n", [("nowhere\nnear", "x")])
        payload = caught.value.payload
        assert payload["fix"] == "reread_file" and payload["line"] is None

    def test_a_block_running_past_the_end_of_the_file(self):
        # No trailing newline, so there is genuinely no line 2 to compare.
        with pytest.raises(coder.EditError) as caught:
            coder.apply_edits("alpha", [("alpha\nbeta\ngamma", "x")])
        payload = caught.value.payload
        assert payload["fix"] == "reread_file" and payload["found"] is None

    def test_a_block_overrunning_a_trailing_newline_still_names_the_line(self):
        # "alpha\n" has an empty final line, so the divergence is locatable —
        # and a located divergence is worth more than "read the file again".
        with pytest.raises(coder.EditError) as caught:
            coder.apply_edits("alpha\n", [("alpha\nbeta\ngamma", "x")])
        payload = caught.value.payload
        assert payload["line"] == 2 and payload["expected"] == "beta"

    def test_an_ambiguous_block_asks_for_context_rather_than_coordinates(self):
        with pytest.raises(coder.EditError) as caught:
            coder.apply_edits("x\nx\n", [("x", "y")])
        assert caught.value.payload["fix"] == "add_context"
        assert caught.value.payload["matches"] == 2

    def test_the_tool_returns_the_payload_as_json(self, isolated_db, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
        (tmp_path / "a.py").write_text("alpha\nbeta\n")
        out = agent_tools._tool_edit_file(
            "a.py", "------- SEARCH\nalpha\ngamma\n=======\nx\n+++++++ REPLACE")
        payload = json.loads(out)
        assert payload["status"] == "REJECTED" and payload["line"] == 2

    def test_a_rejected_edit_still_changes_nothing(self, isolated_db, tmp_path, monkeypatch):
        monkeypatch.setattr(agent_tools, "workspace_root", lambda: str(tmp_path))
        (tmp_path / "a.py").write_text("alpha\nbeta\n")
        agent_tools._tool_edit_file(
            "a.py", "------- SEARCH\nalpha\ngamma\n=======\nx\n+++++++ REPLACE")
        assert (tmp_path / "a.py").read_text() == "alpha\nbeta\n"


# ===== 3. Git-backed isolation checkpoints =====

class TestGitCheckpoints:
    def test_a_checkpoint_in_a_repo_is_a_git_tree(self, tmp_path, isolated_db):
        make_repo(tmp_path)
        made = coder.create_checkpoint(str(tmp_path), "before")
        assert made["backend"] == "git" and made["tree"]

    def test_the_users_index_is_not_disturbed(self, tmp_path, isolated_db):
        # `git add -A` against the real index would quietly stage their work.
        make_repo(tmp_path)
        (tmp_path / "b.py").write_text("y = 2\n")
        coder.create_checkpoint(str(tmp_path), "before")
        staged = subprocess.run(["git", "diff", "--cached", "--name-only"],
                                cwd=tmp_path, capture_output=True, text=True)
        assert staged.stdout.strip() == ""

    def test_a_nine_step_rabbit_hole_is_purged(self, tmp_path, isolated_db):
        make_repo(tmp_path)
        made = coder.create_checkpoint(str(tmp_path), "before the mess")
        (tmp_path / "a.py").write_text("x = WRECKED\n")
        for n in range(9):
            (tmp_path / f"ghost{n}.py").write_text("# invented\n")

        coder.restore_checkpoint(made["id"])
        assert (tmp_path / "a.py").read_text() == "x = 1\n"
        assert not any((tmp_path / f"ghost{n}.py").exists() for n in range(9))

    def test_a_deleted_file_comes_back(self, tmp_path, isolated_db):
        make_repo(tmp_path)
        made = coder.create_checkpoint(str(tmp_path), "before")
        (tmp_path / "a.py").unlink()
        coder.restore_checkpoint(made["id"])
        assert (tmp_path / "a.py").exists()

    def test_the_restore_reports_that_it_purged(self, tmp_path, isolated_db):
        make_repo(tmp_path)
        made = coder.create_checkpoint(str(tmp_path), "before")
        (tmp_path / "a.py").write_text("changed\n")
        assert coder.restore_checkpoint(made["id"])["purged"] is True

    def test_a_non_repo_falls_back_to_copying(self, tmp_path, isolated_db):
        (tmp_path / "a.py").write_text("x = 1\n")
        made = coder.create_checkpoint(str(tmp_path), "before")
        assert made["backend"] == "snapshot"
        (tmp_path / "a.py").write_text("wrecked\n")
        coder.restore_checkpoint(made["id"])
        assert (tmp_path / "a.py").read_text() == "x = 1\n"

    def test_a_bogus_tree_is_refused(self, tmp_path):
        make_repo(tmp_path)
        with pytest.raises(gitops.GitError):
            gitops.restore_tree(str(tmp_path), "HEAD; rm -rf ~")

    def test_the_checkpoint_state_lives_under_dot_carrot(self, tmp_path):
        make_repo(tmp_path)
        # Built with os.path.join: the separator is `\` on Windows, and
        # hard-coding `/` failed this everywhere the app mostly runs — on a
        # path the code was constructing perfectly correctly.
        assert gitops.checkpoint_index_path(str(tmp_path)).endswith(
            os.path.join(gitops.CHECKPOINT_DIR, gitops.CHECKPOINT_INDEX))

    def test_the_panel_is_told_which_backend_each_checkpoint_uses(self, tmp_path, isolated_db):
        make_repo(tmp_path)
        coder.create_checkpoint(str(tmp_path), "before")
        assert coder.list_checkpoints()[0]["tree"]


# ===== 4. The rule compiler =====

class TestRuleCompiler:
    def test_identical_rules_from_two_vendors_appear_once(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("- Always write tests")
        (tmp_path / ".cursorrules").write_text("Always write tests")
        text = coder.load_rules(str(tmp_path))
        assert text.count("Always write tests") == 1

    def test_punctuation_and_casing_do_not_defeat_deduplication(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("Always write tests.")
        (tmp_path / ".goosehints").write_text("- always write TESTS")
        assert coder.load_rules(str(tmp_path)).lower().count("always write tests") == 1

    def test_the_carrot_file_wins_a_conflict(self, tmp_path):
        (tmp_path / "CARROT.md").write_text("Indent with 4 spaces")
        (tmp_path / ".cursorrules").write_text("Indent with tabs")
        text = coder.load_rules(str(tmp_path))
        assert "4 spaces" in text and "Indent with tabs" not in text

    def test_carrotrules_also_outranks_external_files(self, tmp_path):
        (tmp_path / ".carrotrules").write_text("Use pytest for tests")
        (tmp_path / ".clinerules").write_text("Use unittest for tests")
        text = coder.load_rules(str(tmp_path))
        assert "pytest" in text and "unittest" not in text

    def test_a_dropped_conflict_is_disclosed_not_hidden(self, tmp_path):
        (tmp_path / "CARROT.md").write_text("Indent with tabs")
        (tmp_path / ".cursorrules").write_text("Indent with 2 spaces")
        assert "dropped as conflicting" in coder.load_rules(str(tmp_path))

    def test_non_conflicting_rules_all_survive(self, tmp_path):
        (tmp_path / "CARROT.md").write_text("Prefer small functions")
        (tmp_path / ".goosehints").write_text("Never log secrets")
        text = coder.load_rules(str(tmp_path))
        assert "small functions" in text and "Never log secrets" in text

    def test_headings_and_rules_are_stripped(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("# Project rules\n\n---\n\n- Be terse")
        text = coder.load_rules(str(tmp_path))
        assert "# Project rules" not in text and "Be terse" in text

    def test_bullets_are_normalized_to_one_list(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("* Be terse\n1. Be correct")
        text = coder.load_rules(str(tmp_path))
        assert "- Be terse" in text and "- Be correct" in text

    def test_no_per_file_headers_remain(self, tmp_path):
        # A model given "--- .cursorrules ---" spends attention deciding how
        # much a vendor's file counts for. It should not have to.
        (tmp_path / "AGENTS.md").write_text("Be terse")
        assert "--- AGENTS.md ---" not in coder.load_rules(str(tmp_path))

    def test_the_sources_are_still_named_once(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("Be terse")
        assert "AGENTS.md" in coder.load_rules(str(tmp_path))

    def test_no_rules_files_still_means_no_block(self, tmp_path):
        assert coder.load_rules(str(tmp_path)) == ""


# ===== 5. Recipe schema validation, client-side =====

class TestRecipeValidation:
    def test_a_missing_parameter_is_caught_locally(self, isolated_db):
        coder.save_recipe("tidy", "Tidy", "Clean up {{path}}")
        out = agent_tools._tool_run_recipe("tidy", {})
        payload = json.loads(out)
        assert "Missing" in payload["error"] or "missing" in payload["error"]
        assert "path" in payload["required"]

    def test_a_complete_call_returns_the_prompt_not_json(self, isolated_db):
        coder.save_recipe("tidy", "Tidy", "Clean up {{path}}")
        assert agent_tools._tool_run_recipe("tidy", {"path": "src/"}) == "Clean up src/"

    def test_the_literal_placeholder_never_reaches_the_model(self, isolated_db):
        # Sending "{{path}}" through looks like it worked, which is worse than
        # failing.
        coder.save_recipe("tidy", "Tidy", "Clean up {{path}}")
        assert "{{path}}" not in agent_tools._tool_run_recipe("tidy", {})

    def test_an_unknown_recipe_lists_the_real_ones(self, isolated_db):
        coder.save_recipe("tidy", "Tidy", "x")
        payload = json.loads(agent_tools._tool_run_recipe("ghost", {}))
        assert payload["available"] == ["tidy"]

    def test_a_parameter_with_a_default_is_not_required(self, isolated_db):
        coder.save_recipe("tidy", "Tidy", "Clean {{path}}",
                          parameters=[{"name": "path", "default": "."}])
        assert coder.required_parameters("tidy") == []

    def test_required_parameters_are_reported_for_the_model_to_fix(self, isolated_db):
        coder.save_recipe("both", "Both", "Move {{src}} to {{dst}}")
        assert coder.required_parameters("both") == ["dst", "src"]

    def test_the_tool_is_registered_read_only(self):
        assert agent_tools.TOOLS["run_recipe"]["mutating"] is False


# ===== 6. Git capability tiers =====

class TestGitTiers:
    @pytest.mark.parametrize("tool", ["git_status", "git_diff", "git_log"])
    def test_read_only_git_runs_unattended(self, tool):
        assert agent_tools.TOOLS[tool]["mutating"] is False

    def test_mutating_git_is_gated(self):
        assert agent_tools.TOOLS["git_commit"]["mutating"] is True

    def test_the_gated_ones_are_the_ones_plan_mode_removes(self):
        assert coder.tools_for_mode(["carrot__git_commit", "carrot__git_status"],
                                    coder.MODE_PLAN) == ["carrot__git_status"]

    def test_the_approval_prompt_shows_the_commit_message(self):
        summary = agent_tools._summarize_call("git_commit", {"message": "fix the parser"})
        assert "fix the parser" in summary

    def test_an_injected_branch_name_creates_no_file(self, tmp_path):
        make_repo(tmp_path)
        marker = tmp_path / "pwned"
        with pytest.raises(gitops.GitError):
            gitops.create_branch(str(tmp_path), f"x; touch {marker}")
        assert not marker.exists()

    def test_an_injected_checkout_name_creates_no_file(self, tmp_path):
        make_repo(tmp_path)
        marker = tmp_path / "pwned2"
        with pytest.raises(gitops.GitError):
            gitops.checkout(str(tmp_path), f"main; touch {marker}")
        assert not marker.exists()

    def test_no_git_call_ever_uses_a_shell(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "carrot" / "gitops.py").read_text(encoding="utf-8")
        assert "shell=True" not in source


class TestCarrotLeavesNoTrace:
    """A tool that leaves its scratch files in someone's history is unforgivable."""

    def test_the_checkpoint_state_is_gitignored(self, tmp_path):
        make_repo(tmp_path)
        gitops.checkpoint_index_path(str(tmp_path))
        ignore = tmp_path / gitops.CHECKPOINT_DIR / ".gitignore"
        assert ignore.exists() and "*" in ignore.read_text()

    def test_a_checkpoint_does_not_enter_the_snapshot(self, tmp_path, isolated_db):
        make_repo(tmp_path)
        made = coder.create_checkpoint(str(tmp_path), "before")
        listed = gitops.tree_files(str(tmp_path), made["tree"])
        assert not any(name.startswith(gitops.CHECKPOINT_DIR) for name in listed)

    def test_a_commit_does_not_pick_up_carrot_state(self, tmp_path, isolated_db):
        make_repo(tmp_path)
        coder.create_checkpoint(str(tmp_path), "before")
        (tmp_path / "a.py").write_text("x = 2\n")
        gitops.commit(str(tmp_path), "a real change")
        listed = subprocess.run(["git", "show", "--name-only", "--pretty=format:", "HEAD"],
                                cwd=tmp_path, capture_output=True, text=True).stdout
        assert gitops.CHECKPOINT_DIR not in listed

    def test_restoring_does_not_delete_the_checkpoint_state(self, tmp_path, isolated_db):
        # `clean -fd` would otherwise wipe the very directory tracking the run.
        make_repo(tmp_path)
        made = coder.create_checkpoint(str(tmp_path), "before")
        (tmp_path / "ghost.py").write_text("# invented\n")
        coder.restore_checkpoint(made["id"])
        assert (tmp_path / gitops.CHECKPOINT_DIR).is_dir()
        assert not (tmp_path / "ghost.py").exists()


class TestActModeHasToActuallyAct:
    """A code block in the chat is the failure ACT mode exists to prevent.

    The user asked for a snake game, in Act, and got a fenced block to copy —
    which is the same thing a chat window does, from the tab that has write
    tools. An instruction is a request; this is the check.
    """

    def test_a_pasted_file_is_recognised(self):
        text = "Here you go:\n\n```python\n" + ("x = 1\n" * 120) + "```\n"
        assert coder.looks_like_a_pasted_file(text)

    def test_a_short_illustration_is_not(self):
        assert not coder.looks_like_a_pasted_file(
            "Use `range`:\n\n```python\nfor i in range(3):\n    print(i)\n```")

    def test_prose_with_no_code_is_not(self):
        assert not coder.looks_like_a_pasted_file("I would change three files.")

    def test_the_push_back_names_the_tool_to_call(self):
        assert "write_file" in coder.ACT_NOT_ACTING

    def test_act_mode_is_pushed_back_once_then_left_alone(self, isolated_db):
        from unittest.mock import patch

        from carrot import app as A, config

        config.set_config("coder_mode", "act")
        pasted = "```python\n" + ("x = 1\n" * 120) + "```"
        rounds = {"n": 0}

        def stream(resolved, messages, tools=None):
            rounds["n"] += 1
            yield {"type": "text", "text": pasted}

        class Route:
            def as_dict(self):
                return {}

        with patch.object(A.router_mod, "stream_events", stream), \
             patch.object(A, "_available_tools", lambda m: []):
            events = list(A._agentic_chat_events(
                [{"role": "user", "content": "make a snake game"}],
                Route(), None, None, A.SEARCH_OFF))

        gates = [e for e in events if "gate" in e]
        assert gates, "the model pasted a file in Act mode and was not pushed back"
        assert len(gates) <= A.MAX_GATE_NUDGES, "it must give up, not loop forever"
        assert next(e["_final_text"] for e in events if "_final_text" in e).strip()

    def test_plan_mode_is_left_alone(self, isolated_db):
        from unittest.mock import patch

        from carrot import app as A, config

        config.set_config("coder_mode", "plan")
        pasted = "```python\n" + ("x = 1\n" * 120) + "```"

        def stream(resolved, messages, tools=None):
            yield {"type": "text", "text": pasted}

        class Route:
            def as_dict(self):
                return {}

        with patch.object(A.router_mod, "stream_events", stream), \
             patch.object(A, "_available_tools", lambda m: []):
            events = list(A._agentic_chat_events(
                [{"role": "user", "content": "how would you do it?"}],
                Route(), None, None, A.SEARCH_OFF))
        # Plan mode's entire job is proposing without writing. Showing the code
        # it would write is the correct behaviour there.
        assert not [e for e in events if "gate" in e]


class TestARestoreThatCannotFinishSaysSo:
    """A file the OS will not release stopped `checkout-index` dead, and the
    error escaped raw — after some files had already been rewritten. So
    "restore" could leave the tree half-way and report only
    `unable to unlink old 'x'`, which is the opposite of what it promises.

    This is not hypothetical: a SQLite database open in another program cannot
    be replaced on Windows, and that is exactly how it was found — the test
    harness's own database was sitting inside the repo under test.
    """

    def test_a_locked_file_does_not_abort_the_rest(self, tmp_path, isolated_db):
        import sqlite3

        make_repo(tmp_path)
        # A database committed into the project, then held open — the shape
        # that cannot be unlinked on Windows.
        db = tmp_path / "app.db"
        sqlite3.connect(str(db)).close()
        subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-qm", "with db"], cwd=tmp_path, check=True)

        made = coder.create_checkpoint(str(tmp_path), "before")
        (tmp_path / "a.py").unlink()

        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE IF NOT EXISTS t (x)")
        conn.execute("INSERT INTO t VALUES (1)")
        try:
            result = coder.restore_checkpoint(made["id"])
        finally:
            conn.close()

        # The point: the file that *could* be restored was.
        assert (tmp_path / "a.py").exists()
        assert "blocked" in result

    def test_a_failure_git_did_not_name_still_propagates(self):
        """Only "could not write this path" is downgraded to a report. Any
        other git failure is a different problem and must not be quietly
        turned into "some files were skipped"."""
        assert gitops._unwritable_paths("fatal: not a git repository") == []
        assert gitops._unwritable_paths(
            "error: unable to unlink old 'carrot.db': Invalid argument") == ["carrot.db"]
