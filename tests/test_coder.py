"""The coding agent: plan/act, search/replace edits, checkpoints, rules, recipes.

Each behaviour here is one of the ideas taken from Cline, Continue or Goose,
and each is tested for the failure that makes the idea worthless if it slips —
a plan mode that can still write, an edit that applies half of itself, a
checkpoint that restores "mostly", rules that are read but not enforced.
"""
import os

import pytest

from carrot import coder


# ===== Plan / Act =====

class TestModes:
    def test_an_unknown_mode_is_plan(self):
        # The cautious reading of a typo is the read-only one.
        assert coder.normalize_mode("acting") == coder.MODE_PLAN
        assert coder.normalize_mode(None) == coder.MODE_PLAN
        assert coder.normalize_mode("  ACT ") == coder.MODE_ACT

    def test_plan_mode_removes_the_write_tools(self):
        offered = ["carrot__read_file", "carrot__write_file", "carrot__edit_file",
                   "carrot__run_command", "carrot__list_dir"]
        kept = coder.tools_for_mode(offered, coder.MODE_PLAN)
        assert kept == ["carrot__read_file", "carrot__list_dir"]

    def test_act_mode_keeps_everything(self):
        offered = ["carrot__read_file", "carrot__write_file"]
        assert coder.tools_for_mode(offered, coder.MODE_ACT) == offered

    def test_the_namespace_prefix_does_not_hide_a_write_tool(self):
        # Tools reach the model as `carrot__write_file`; matching the bare name
        # is what makes the filter actually bite.
        assert coder.tools_for_mode(["carrot__git_commit"], coder.MODE_PLAN) == []


# ===== Search/replace edits =====

class TestParseEditBlocks:
    def test_the_angle_bracket_spelling(self):
        blocks = coder.parse_edit_blocks(
            "<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE"
        )
        assert blocks == [("old", "new")]

    def test_the_dash_spelling(self):
        # Different agents emit different delimiters; rejecting one spelling is
        # a failure with no upside.
        blocks = coder.parse_edit_blocks(
            "------- SEARCH\nold\n=======\nnew\n+++++++ REPLACE"
        )
        assert blocks == [("old", "new")]

    def test_several_blocks_in_order(self):
        blocks = coder.parse_edit_blocks(
            "------- SEARCH\na\n=======\nA\n+++++++ REPLACE\n"
            "------- SEARCH\nb\n=======\nB\n+++++++ REPLACE"
        )
        assert blocks == [("a", "A"), ("b", "B")]

    def test_indentation_inside_a_block_survives(self):
        blocks = coder.parse_edit_blocks(
            "------- SEARCH\n    return 1\n=======\n    return 2\n+++++++ REPLACE"
        )
        assert blocks == [("    return 1", "    return 2")]

    def test_a_missing_divider_is_an_error(self):
        with pytest.raises(coder.EditError):
            coder.parse_edit_blocks("------- SEARCH\nold\n+++++++ REPLACE")

    def test_a_missing_terminator_is_an_error(self):
        with pytest.raises(coder.EditError):
            coder.parse_edit_blocks("------- SEARCH\nold\n=======\nnew")

    def test_no_blocks_at_all_explains_the_format(self):
        with pytest.raises(coder.EditError) as caught:
            coder.parse_edit_blocks("please change line 4")
        assert "SEARCH" in str(caught.value)


class TestApplyEdits:
    def test_a_single_exact_match(self):
        assert coder.apply_edits("a\nb\nc\n", [("b", "B")]) == "a\nB\nc\n"

    def test_edits_apply_in_order(self):
        out = coder.apply_edits("one two", [("one", "1"), ("two", "2")])
        assert out == "1 2"

    def test_an_ambiguous_block_is_refused(self):
        # Replacing the first of three identical lines is a coin flip; a coding
        # agent that flips coins on edits is worse than one that refuses.
        with pytest.raises(coder.EditError) as caught:
            coder.apply_edits("x\nx\nx\n", [("x", "y")])
        assert "matches 3" in str(caught.value)

    def test_a_block_that_does_not_match_is_refused(self):
        with pytest.raises(coder.EditError):
            coder.apply_edits("hello\n", [("goodbye", "hi")])

    def test_nothing_is_applied_when_a_later_block_fails(self):
        # Half an edit is the worst outcome: the file is broken and the model
        # believes it succeeded.
        original = "keep\nchange\n"
        with pytest.raises(coder.EditError):
            coder.apply_edits(original, [("change", "changed"), ("absent", "x")])
        # apply_edits is pure; the caller still holds the untouched original.
        assert original == "keep\nchange\n"

    def test_trailing_whitespace_drift_still_matches(self):
        content = "def f():\n    return 1   \n"
        out = coder.apply_edits(content, [("def f():\n    return 1", "def f():\n    return 2")])
        assert "return 2" in out

    def test_windows_line_endings_still_match(self):
        content = "alpha\r\nbeta\r\n"
        out = coder.apply_edits(content, [("alpha\nbeta", "gamma")])
        assert "gamma" in out

    def test_an_empty_search_appends(self):
        assert coder.apply_edits("a\n", [("", "b")]) == "a\nb"


# ===== Project rules =====

class TestRules:
    def test_no_rules_files_is_empty(self, tmp_path):
        assert coder.load_rules(str(tmp_path)) == ""

    def test_a_missing_root_is_empty(self):
        assert coder.load_rules("/nonexistent/path/anywhere") == ""

    @pytest.mark.parametrize("name", [
        "AGENTS.md", ".clinerules", ".continuerules", ".goosehints", ".cursorrules",
    ])
    def test_every_supported_rules_file_is_read(self, tmp_path, name):
        (tmp_path / name).write_text("always use tabs")
        text = coder.load_rules(str(tmp_path))
        assert "always use tabs" in text
        assert name in text

    def test_several_files_are_all_included_and_labelled(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("rule one")
        (tmp_path / ".goosehints").write_text("rule two")
        text = coder.load_rules(str(tmp_path))
        assert "rule one" in text and "rule two" in text

    def test_a_rules_directory_is_read(self, tmp_path):
        folder = tmp_path / ".clinerules"
        folder.mkdir()
        (folder / "style.md").write_text("no trailing commas")
        assert "no trailing commas" in coder.load_rules(str(tmp_path))

    def test_an_empty_file_contributes_nothing(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("   \n")
        assert coder.load_rules(str(tmp_path)) == ""

    def test_enormous_rules_are_truncated(self, tmp_path):
        (tmp_path / "AGENTS.md").write_text("x" * (coder.MAX_RULES_CHARS + 5000))
        text = coder.load_rules(str(tmp_path))
        assert "[rules truncated]" in text
        assert len(text) < coder.MAX_RULES_CHARS + 500


# ===== Checkpoints =====

class TestSnapshot:
    def test_text_files_are_captured(self, tmp_path):
        (tmp_path / "a.py").write_text("print(1)")
        (tmp_path / "b.md").write_text("# hi")
        files = coder.snapshot(str(tmp_path))
        assert files == {"a.py": "print(1)", "b.md": "# hi"}

    def test_build_output_is_skipped(self, tmp_path):
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "x.js").write_text("junk")
        (tmp_path / "keep.js").write_text("mine")
        assert set(coder.snapshot(str(tmp_path))) == {"keep.js"}

    def test_binaries_are_skipped(self, tmp_path):
        (tmp_path / "logo.png").write_bytes(b"\x89PNG\x00\x01")
        (tmp_path / "code.py").write_text("x = 1")
        assert set(coder.snapshot(str(tmp_path))) == {"code.py"}

    def test_nested_paths_use_forward_slashes(self, tmp_path):
        sub = tmp_path / "src"
        sub.mkdir()
        (sub / "main.py").write_text("x")
        assert "src/main.py" in coder.snapshot(str(tmp_path))

    def test_a_huge_file_is_skipped_rather_than_blowing_the_snapshot(self, tmp_path):
        (tmp_path / "big.txt").write_text("y" * (coder.MAX_FILE_BYTES + 10))
        (tmp_path / "small.txt").write_text("ok")
        assert set(coder.snapshot(str(tmp_path))) == {"small.txt"}


class TestCheckpointRoundTrip:
    def test_an_edited_file_comes_back(self, tmp_path, isolated_db):
        (tmp_path / "a.py").write_text("original")
        made = coder.create_checkpoint(str(tmp_path), "before")
        (tmp_path / "a.py").write_text("wrecked")

        result = coder.restore_checkpoint(made["id"])
        assert (tmp_path / "a.py").read_text() == "original"
        assert "a.py" in result["restored"]

    def test_a_file_created_afterwards_is_removed(self, tmp_path, isolated_db):
        # "Restore" that leaves the agent's new files behind means "mostly
        # restore", which is the thing that makes people stop trusting undo.
        (tmp_path / "a.py").write_text("original")
        made = coder.create_checkpoint(str(tmp_path), "before")
        (tmp_path / "invented.py").write_text("surprise")

        result = coder.restore_checkpoint(made["id"])
        assert not (tmp_path / "invented.py").exists()
        assert "invented.py" in result["removed"]

    def test_a_deleted_file_comes_back(self, tmp_path, isolated_db):
        (tmp_path / "a.py").write_text("original")
        made = coder.create_checkpoint(str(tmp_path), "before")
        os.remove(tmp_path / "a.py")

        coder.restore_checkpoint(made["id"])
        assert (tmp_path / "a.py").read_text() == "original"

    def test_an_untouched_file_is_not_reported_as_restored(self, tmp_path, isolated_db):
        (tmp_path / "a.py").write_text("same")
        made = coder.create_checkpoint(str(tmp_path), "before")
        assert coder.restore_checkpoint(made["id"])["restored"] == []

    def test_an_unknown_checkpoint_raises(self, isolated_db):
        with pytest.raises(KeyError):
            coder.restore_checkpoint("nope")

    def test_checkpoints_are_listed_newest_first(self, tmp_path, isolated_db):
        (tmp_path / "a.py").write_text("x")
        first = coder.create_checkpoint(str(tmp_path), "first")
        second = coder.create_checkpoint(str(tmp_path), "second")
        listed = [c["id"] for c in coder.list_checkpoints()]
        assert listed.index(second["id"]) < listed.index(first["id"])

    def test_a_checkpoint_can_be_deleted(self, tmp_path, isolated_db):
        (tmp_path / "a.py").write_text("x")
        made = coder.create_checkpoint(str(tmp_path), "x")
        assert coder.delete_checkpoint(made["id"]) is True
        assert coder.delete_checkpoint(made["id"]) is False


# ===== Recipes =====

class TestRecipes:
    def test_save_and_read_back(self, isolated_db):
        coder.save_recipe("tidy", "Tidy up", "Clean {{path}}")
        assert coder.get_recipe("tidy")["title"] == "Tidy up"

    def test_an_invalid_id_is_refused(self, isolated_db):
        with pytest.raises(ValueError):
            coder.save_recipe("Not An Id!", "x", "do something")

    def test_an_empty_prompt_is_refused(self, isolated_db):
        with pytest.raises(ValueError):
            coder.save_recipe("empty", "x", "   ")

    def test_saving_twice_replaces_rather_than_duplicates(self, isolated_db):
        coder.save_recipe("tidy", "One", "a")
        coder.save_recipe("tidy", "Two", "b")
        matching = [r for r in coder.recipes() if r["id"] == "tidy"]
        assert len(matching) == 1 and matching[0]["title"] == "Two"

    def test_render_substitutes_parameters(self, isolated_db):
        coder.save_recipe("tidy", "Tidy", "Clean up {{path}} carefully")
        assert coder.render_recipe("tidy", {"path": "src/"}) == "Clean up src/ carefully"

    def test_a_missing_parameter_is_an_error_not_a_literal(self, isolated_db):
        # Sending the model the literal text "{{path}}" looks like it worked,
        # which is worse than failing.
        coder.save_recipe("tidy", "Tidy", "Clean up {{path}}")
        with pytest.raises(ValueError) as caught:
            coder.render_recipe("tidy", {})
        assert "path" in str(caught.value)

    def test_a_declared_default_fills_in(self, isolated_db):
        coder.save_recipe("tidy", "Tidy", "Clean {{path}}",
                          parameters=[{"name": "path", "default": "."}])
        assert coder.render_recipe("tidy", {}) == "Clean ."

    def test_deleting_a_recipe(self, isolated_db):
        coder.save_recipe("tidy", "Tidy", "x")
        assert coder.delete_recipe("tidy") is True
        assert coder.delete_recipe("tidy") is False

    def test_an_unknown_recipe_raises(self, isolated_db):
        with pytest.raises(KeyError):
            coder.render_recipe("ghost", {})
