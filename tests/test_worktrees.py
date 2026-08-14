"""A second checkout, so "try this" and "keep working" are different folders.

Without one they are the same folder: the agent's edits land on top of
whatever you had open, and undoing them means undoing yours too. A worktree
gives the agent a whole checkout on its own branch, sharing the object
database, for the price of a directory.
"""
import os
import subprocess

import pytest

from carrot import gitops


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, capture_output=True, check=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _git("init", "-b", "main", cwd=root)
    _git("config", "user.email", "t@example.test", cwd=root)
    _git("config", "user.name", "Test", cwd=root)
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    _git("add", "-A", cwd=root)
    _git("commit", "-m", "first", cwd=root)
    return str(root)


pytestmark = pytest.mark.skipif(not gitops.git_available(), reason="git is not installed")


class TestMakingOne:
    def test_a_worktree_is_a_real_checkout_on_its_own_branch(self, repo):
        made = gitops.add_worktree(repo, "try/refactor")
        assert os.path.isfile(os.path.join(made["path"], "a.txt"))
        assert made["branch"] == "try/refactor"

    def test_it_lands_beside_the_repository_not_inside_it(self, repo):
        """A worktree in a subdirectory of its own repository is a directory
        git ignores and every other tool walks — the indexer, the file tree
        and the test runner would each see two copies of the project."""
        made = gitops.add_worktree(repo, "try/refactor")
        assert not made["path"].startswith(os.path.join(repo, ""))
        assert os.path.dirname(made["path"]) == os.path.dirname(repo)

    def test_work_in_one_does_not_touch_the_other(self, repo):
        """The entire point."""
        made = gitops.add_worktree(repo, "try/refactor")
        with open(os.path.join(made["path"], "a.txt"), "w", encoding="utf-8") as handle:
            handle.write("changed by the agent\n")
        with open(os.path.join(repo, "a.txt"), encoding="utf-8") as handle:
            assert handle.read() == "one\n"

    def test_the_main_checkout_is_listed_first(self, repo):
        """It is the thing the others are branches of, not one experiment
        among several."""
        gitops.add_worktree(repo, "try/one")
        listed = gitops.worktrees(repo)
        assert os.path.abspath(listed[0]["path"]) == os.path.abspath(repo)
        assert len(listed) == 2

    def test_a_path_with_a_space_in_it_still_parses(self, tmp_path):
        """The human-readable listing puts path, commit and branch on one line
        with no delimiter, which is unparseable on Windows."""
        root = tmp_path / "my project"
        root.mkdir()
        _git("init", "-b", "main", cwd=root)
        _git("config", "user.email", "t@example.test", cwd=root)
        _git("config", "user.name", "Test", cwd=root)
        (root / "a.txt").write_text("one\n", encoding="utf-8")
        _git("add", "-A", cwd=root)
        _git("commit", "-m", "first", cwd=root)
        listed = gitops.worktrees(str(root))
        assert os.path.abspath(listed[0]["path"]) == os.path.abspath(str(root))
        assert listed[0]["branch"] == "main"


class TestWhatItRefuses:
    def test_a_branch_name_git_would_reject_is_named_plainly(self, repo):
        """Git's own message is about ref formats. This one is about what the
        user typed."""
        with pytest.raises(gitops.GitError) as caught:
            gitops.add_worktree(repo, "two words")
        assert "spaces" in str(caught.value)

    def test_a_nameless_worktree(self, repo):
        with pytest.raises(gitops.GitError):
            gitops.add_worktree(repo, "  ")

    def test_it_will_not_overwrite_a_directory_that_is_there(self, repo):
        gitops.add_worktree(repo, "try/one")
        with pytest.raises(gitops.GitError) as caught:
            gitops.add_worktree(repo, "try/one")
        assert "exists" in str(caught.value) or "already" in str(caught.value)

    def test_a_worktree_with_uncommitted_work_is_not_dropped(self, repo):
        """The whole point of working in one is that the work in it is real,
        and a one-click button that discards it is a button somebody presses
        on the wrong row."""
        made = gitops.add_worktree(repo, "try/refactor")
        with open(os.path.join(made["path"], "a.txt"), "w", encoding="utf-8") as handle:
            handle.write("hours of work\n")
        with pytest.raises(gitops.GitError) as caught:
            gitops.remove_worktree(repo, made["path"])
        assert "uncommitted" in str(caught.value)
        assert os.path.isfile(os.path.join(made["path"], "a.txt"))

    def test_a_clean_one_is_dropped(self, repo):
        made = gitops.add_worktree(repo, "try/refactor")
        gitops.remove_worktree(repo, made["path"])
        assert not os.path.exists(made["path"])

    def test_you_cannot_remove_the_one_you_are_standing_in(self, repo):
        with pytest.raises(gitops.GitError) as caught:
            gitops.remove_worktree(repo, repo)
        assert "working in" in str(caught.value)


class TestTheApi:
    def test_a_folder_that_is_not_a_repository_says_so_quietly(self, client, isolated_db, tmp_path):
        """Not an error: most workspaces are not repositories, and the picker
        simply does not apply there."""
        from carrot import files_api

        client.post("/api/files/root", json={"root": str(tmp_path / "plain")})
        body = client.get("/api/coder/worktrees").json()
        assert body["repo"] is False
        assert body["worktrees"] == []

    def test_making_one_switches_the_workspace_to_it(self, client, isolated_db, repo):
        """A worktree you then have to go and open by hand is a directory,
        not a feature."""
        client.post("/api/files/root", json={"root": repo})
        made = client.post("/api/coder/worktrees",
                           json={"branch": "try/api", "switch": True}).json()
        assert made["switched"] is True
        assert client.get("/api/files/root").json()["root"].lower() == made["path"].lower()

    def test_a_bad_branch_name_is_a_400_not_a_500(self, client, isolated_db, repo):
        client.post("/api/files/root", json={"root": repo})
        assert client.post("/api/coder/worktrees",
                           json={"branch": "two words"}).status_code == 400
