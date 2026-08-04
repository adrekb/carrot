"""File operations behind the Code tab's mini-IDE behaviour.

The tab could previously only read and write files that already existed. These
cover the operations that make it an editor — create, rename, move, delete,
find — and the sandbox they all have to stay inside.
"""
import os

import pytest

from carrot import config, files_api


@pytest.fixture
def workspace(tmp_path, isolated_db):
    root = tmp_path / "ws"
    root.mkdir()
    config.set_config("code_workspace_dir", str(root))
    return root


def post(client, endpoint, **body):
    # Not `path`: every one of these endpoints takes a `path` field in the
    # body, which would collide with the parameter name.
    return client.post(f"/api/files/{endpoint}", json=body)


class TestCreate:
    def test_create_a_file(self, client, workspace):
        r = post(client, "create", path="", name="hello.py")
        assert r.status_code == 200
        assert (workspace / "hello.py").is_file()
        assert r.json()["path"] == "hello.py"

    def test_create_a_folder(self, client, workspace):
        r = post(client, "create", path="", name="src", is_dir=True)
        assert r.status_code == 200
        assert (workspace / "src").is_dir()

    def test_create_inside_a_folder(self, client, workspace):
        post(client, "create", path="", name="src", is_dir=True)
        r = post(client, "create", path="src", name="main.py")
        assert r.status_code == 200
        assert (workspace / "src" / "main.py").is_file()
        assert r.json()["path"] == "src/main.py"

    def test_creating_over_something_is_refused(self, client, workspace):
        (workspace / "taken.py").write_text("x")
        r = post(client, "create", path="", name="taken.py")
        assert r.status_code == 409

    def test_an_empty_file_is_created_empty_not_missing(self, client, workspace):
        post(client, "create", path="", name="new.txt")
        assert client.get("/api/files/read?path=new.txt").json()["content"] == ""

    @pytest.mark.parametrize("name", ["..", ".", "", "a/b", "a\\b"])
    def test_names_that_would_escape_are_refused(self, client, workspace, name):
        assert post(client, "create", path="", name=name).status_code == 400


class TestRename:
    def test_rename_a_file(self, client, workspace):
        (workspace / "old.py").write_text("code")
        r = post(client, "rename", path="old.py", new_name="new.py")
        assert r.status_code == 200
        assert (workspace / "new.py").read_text() == "code"
        assert not (workspace / "old.py").exists()

    def test_rename_a_folder_keeps_its_contents(self, client, workspace):
        (workspace / "a").mkdir()
        (workspace / "a" / "f.txt").write_text("inside")
        post(client, "rename", path="a", new_name="b")
        assert (workspace / "b" / "f.txt").read_text() == "inside"

    def test_renaming_onto_an_existing_name_is_refused(self, client, workspace):
        (workspace / "a.py").write_text("1")
        (workspace / "b.py").write_text("2")
        assert post(client, "rename", path="a.py", new_name="b.py").status_code == 409
        assert (workspace / "b.py").read_text() == "2", "the target was overwritten"

    def test_the_root_cannot_be_renamed(self, client, workspace):
        assert post(client, "rename", path="", new_name="elsewhere").status_code == 400


class TestMove:
    def test_move_into_a_folder(self, client, workspace):
        (workspace / "dst").mkdir()
        (workspace / "f.txt").write_text("hi")
        r = post(client, "move", path="f.txt", dest_dir="dst")
        assert r.status_code == 200
        assert (workspace / "dst" / "f.txt").read_text() == "hi"

    def test_move_back_to_the_root(self, client, workspace):
        (workspace / "d").mkdir()
        (workspace / "d" / "f.txt").write_text("hi")
        post(client, "move", path="d/f.txt", dest_dir="")
        assert (workspace / "f.txt").exists()

    def test_a_folder_cannot_be_moved_into_itself(self, client, workspace):
        """This detaches the whole subtree — shutil would happily do it."""
        (workspace / "a" / "b").mkdir(parents=True)
        assert post(client, "move", path="a", dest_dir="a/b").status_code == 400
        assert (workspace / "a" / "b").is_dir()

    def test_move_onto_an_existing_name_is_refused(self, client, workspace):
        (workspace / "dst").mkdir()
        (workspace / "dst" / "f.txt").write_text("original")
        (workspace / "f.txt").write_text("other")
        assert post(client, "move", path="f.txt", dest_dir="dst").status_code == 409
        assert (workspace / "dst" / "f.txt").read_text() == "original"


class TestDelete:
    def test_delete_a_file(self, client, workspace):
        (workspace / "gone.py").write_text("x")
        assert post(client, "delete", path="gone.py").status_code == 200
        assert not (workspace / "gone.py").exists()

    def test_delete_a_folder_and_its_contents(self, client, workspace):
        (workspace / "d" / "sub").mkdir(parents=True)
        (workspace / "d" / "sub" / "f.txt").write_text("x")
        assert post(client, "delete", path="d").status_code == 200
        assert not (workspace / "d").exists()

    def test_the_root_cannot_be_deleted(self, client, workspace):
        assert post(client, "delete", path="").status_code == 400
        assert workspace.is_dir()

    def test_deleting_something_absent_is_404_not_500(self, client, workspace):
        assert post(client, "delete", path="never-existed").status_code == 404


class TestSandbox:
    """resolve() promises nothing escapes the workspace root."""

    @pytest.mark.parametrize("path", ["../outside.txt", "a/../../outside.txt",
                                      "../../etc/passwd"])
    def test_dot_dot_cannot_escape(self, client, workspace, path):
        assert client.get(f"/api/files/read?path={path}").status_code in (403, 404)

    def test_create_cannot_escape_via_the_parent(self, client, workspace):
        assert post(client, "create", path="..", name="escaped.txt").status_code in (403, 404)
        assert not (workspace.parent / "escaped.txt").exists()

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlink support")
    def test_a_symlink_out_of_the_workspace_is_refused(self, client, workspace, tmp_path):
        """abspath resolves '..' but does not follow links, so a prefix check
        on an unresolved path lets a link inside the workspace read anything
        the user can read."""
        secret = tmp_path / "secret.txt"
        secret.write_text("private")
        try:
            os.symlink(str(secret), str(workspace / "link.txt"))
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not permitted here")
        assert client.get("/api/files/read?path=link.txt").status_code == 403

    @pytest.mark.skipif(not hasattr(os, "symlink"), reason="no symlink support")
    def test_writing_through_a_symlink_is_refused(self, client, workspace, tmp_path):
        target = tmp_path / "target.txt"
        target.write_text("original")
        try:
            os.symlink(str(target), str(workspace / "link.txt"))
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not permitted here")
        post(client, "write", path="link.txt", content="overwritten")
        assert target.read_text() == "original", "wrote through a symlink out of the sandbox"


class TestWriteLimits:
    def test_a_file_too_big_to_reopen_is_refused(self, client, workspace):
        """The read side caps at MAX_FILE_BYTES, so accepting a larger write
        creates a file the editor can never open again."""
        oversized = "x" * (files_api.MAX_FILE_BYTES + 10)
        assert post(client, "write", path="big.txt", content=oversized).status_code == 413

    def test_writing_over_a_directory_is_refused(self, client, workspace):
        (workspace / "d").mkdir()
        assert post(client, "write", path="d", content="x").status_code == 409


class TestSearch:
    def test_finds_a_match_with_its_line_number(self, client, workspace):
        (workspace / "a.py").write_text("import os\nDEBUG = True\n")
        hits = client.get("/api/files/search?q=DEBUG").json()["hits"]
        assert len(hits) == 1
        assert hits[0]["path"] == "a.py"
        assert hits[0]["line"] == 2
        assert "DEBUG" in hits[0]["text"]

    def test_searches_nested_folders(self, client, workspace):
        (workspace / "src").mkdir()
        (workspace / "src" / "b.py").write_text("needle here")
        hits = client.get("/api/files/search?q=needle").json()["hits"]
        assert [h["path"] for h in hits] == ["src/b.py"]

    def test_case_insensitive_by_default(self, client, workspace):
        (workspace / "a.py").write_text("Needle")
        assert client.get("/api/files/search?q=needle").json()["hits"]

    def test_case_sensitive_when_asked(self, client, workspace):
        (workspace / "a.py").write_text("Needle")
        body = client.get("/api/files/search?q=needle&case_sensitive=true").json()
        assert body["hits"] == []

    def test_skips_the_directories_the_tree_skips(self, client, workspace):
        (workspace / "node_modules").mkdir()
        (workspace / "node_modules" / "x.js").write_text("needle")
        assert client.get("/api/files/search?q=needle").json()["hits"] == []

    def test_binary_files_do_not_raise(self, client, workspace):
        (workspace / "blob.bin").write_bytes(b"\x00\x01\x02needle")
        assert client.get("/api/files/search?q=needle").status_code == 200

    def test_an_empty_query_returns_nothing(self, client, workspace):
        (workspace / "a.py").write_text("content")
        assert client.get("/api/files/search?q=").json()["hits"] == []

    def test_results_are_capped_and_say_so(self, client, workspace):
        (workspace / "many.txt").write_text("needle\n" * 50)
        body = client.get("/api/files/search?q=needle&max_hits=10").json()
        assert len(body["hits"]) == 10
        assert body["truncated"] is True
