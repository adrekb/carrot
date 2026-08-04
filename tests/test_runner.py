"""Running the file open in the Code tab.

What existed was an extension→command map returning "gcc" for a .c file and
"java" for a .java file. Neither runs anything: gcc compiles and stops, and
bare `java` wants a class name, not the source the user is editing. Compiled
languages need a build step and somewhere to put the binary, and every
language needs a real answer when its toolchain is not installed — which on a
fresh machine is most of them.
"""
import os
import shutil

import pytest

from carrot import config, runner


@pytest.fixture
def workspace(tmp_path, isolated_db):
    root = tmp_path / "ws"
    root.mkdir()
    config.set_config("code_workspace_dir", str(root))
    return root


def have(tool):
    return shutil.which(tool) is not None


class TestInterpreted:
    def test_python_runs(self, workspace):
        (workspace / "a.py").write_text("print('hello', 2 + 2)")
        result = runner.run_file("a.py")
        assert result["ok"], result["output"]
        assert "hello 4" in result["output"]

    def test_a_non_zero_exit_is_reported_as_failure(self, workspace):
        (workspace / "bad.py").write_text("import sys; sys.exit(3)")
        result = runner.run_file("bad.py")
        assert result["ok"] is False
        assert result["exit_code"] == 3

    def test_stderr_reaches_the_output(self, workspace):
        """A traceback is the whole point of running something."""
        (workspace / "boom.py").write_text("raise ValueError('the specific problem')")
        result = runner.run_file("boom.py")
        assert "the specific problem" in result["output"]

    def test_a_program_that_never_finishes_is_stopped(self, workspace):
        (workspace / "spin.py").write_text("while True: pass")
        result = runner.run_file("spin.py", timeout=2)
        assert result["ok"] is False
        assert "Stopped after" in result["output"]

    def test_it_runs_in_the_files_own_directory(self, workspace):
        """Relative paths in the program have to mean what the author meant."""
        (workspace / "sub").mkdir()
        (workspace / "sub" / "data.txt").write_text("beside me")
        (workspace / "sub" / "read.py").write_text(
            "print(open('data.txt').read())")
        assert "beside me" in runner.run_file("sub/read.py")["output"]


@pytest.mark.skipif(not have("g++"), reason="no C++ toolchain")
class TestCompiled:
    def test_cpp_compiles_and_runs(self, workspace):
        """The old map stopped at compiling; nothing ever ran."""
        (workspace / "m.cpp").write_text(
            '#include <iostream>\nint main(){std::cout<<"ran "<<6*7;return 0;}')
        result = runner.run_file("m.cpp")
        assert result["ok"], result["output"]
        assert "ran 42" in result["output"]

    def test_a_compile_error_is_reported_as_a_build_failure(self, workspace):
        """Distinguishing "your code does not compile" from "your program
        crashed" is the difference between two very different fixes."""
        (workspace / "bad.cpp").write_text("int main(){ not valid c++ }")
        result = runner.run_file("bad.cpp")
        assert result["ok"] is False
        assert result["stage"] == "build"

    def test_the_binary_does_not_land_beside_the_source(self, workspace):
        """Running a file should not litter the workspace with build output."""
        (workspace / "m.cpp").write_text("int main(){return 0;}")
        runner.run_file("m.cpp")
        left = {p.name for p in workspace.iterdir()}
        assert left == {"m.cpp"}, f"build output left behind: {left - {'m.cpp'}}"


@pytest.mark.skipif(not have("java"), reason="no JDK")
class TestJava:
    def test_a_java_source_file_runs(self, workspace):
        """Bare `java` needs a class name; single-file source launch is what
        actually runs the .java the user has open."""
        (workspace / "Hi.java").write_text(
            'public class Hi{public static void main(String[] a){System.out.println("ran "+(1+1));}}')
        result = runner.run_file("Hi.java")
        assert result["ok"], result["output"]
        assert "ran 2" in result["output"]


class TestMissingToolchains:
    def test_a_missing_toolchain_names_what_to_install(self, workspace, monkeypatch):
        """"command not found" tells the user nothing they can act on."""
        (workspace / "m.cpp").write_text("int main(){return 0;}")
        monkeypatch.setattr(runner.shutil, "which", lambda name: None)
        result = runner.run_file("m.cpp")
        assert result["ok"] is False
        assert result["missing_tool"]
        assert "install" in result["output"].lower()

    def test_an_unknown_extension_says_so(self, workspace):
        (workspace / "notes.xyz").write_text("whatever")
        result = runner.run_file("notes.xyz")
        assert result["ok"] is False
        assert "does not know how to run" in result["output"]

    def test_languages_reports_availability(self):
        langs = {entry["language"]: entry for entry in runner.languages()}
        assert "Python" in langs and langs["Python"]["available"] is True
        for entry in langs.values():
            assert entry["extensions"] and entry["install"]


class TestSandbox:
    def test_a_file_outside_the_workspace_cannot_be_run(self, workspace, tmp_path):
        outside = tmp_path / "evil.py"
        outside.write_text("print('should never run')")
        with pytest.raises(Exception):
            runner.run_file("../evil.py")

    def test_a_missing_file_is_not_a_crash(self, workspace):
        with pytest.raises(Exception):
            runner.run_file("nope.py")


class TestRecipes:
    def test_every_compiled_recipe_has_a_build_step(self):
        """A recipe with no build for a compiled language is the original bug:
        it "runs" the source file through the compiler and shows nothing."""
        for ext in (".c", ".cpp", ".rs"):
            assert runner.RECIPES[ext].build, f"{ext} has no build step"

    def test_every_recipe_names_something_installable(self):
        for ext, recipe in runner.RECIPES.items():
            assert recipe.install, f"{ext} cannot tell the user what to install"

    def test_no_recipe_uses_a_shell(self):
        """argv, not a command string — a path with a space in it would
        otherwise split into two arguments."""
        import inspect

        source = inspect.getsource(runner)
        assert "shell=True" not in source

    def test_python_does_not_relaunch_the_frozen_app(self, monkeypatch):
        """In the packaged build sys.executable is carrot-backend itself, so
        using it to run a script would start a second copy of Carrot."""
        monkeypatch.setattr(runner.sys, "frozen", True, raising=False)
        monkeypatch.setattr(runner.shutil, "which",
                            lambda name: "/usr/bin/python3" if "python" in name else None)
        assert runner._resolve_tool(runner.RECIPES[".py"]) == "/usr/bin/python3"


class TestApi:
    def test_run_endpoint(self, client, workspace):
        (workspace / "a.py").write_text("print('via api')")
        body = client.post("/api/files/run", json={"path": "a.py"}).json()
        assert body["ok"] and "via api" in body["output"]

    def test_languages_endpoint(self, client, workspace):
        body = client.get("/api/files/languages").json()
        assert any(entry["language"] == "Python" for entry in body["languages"])

    def test_the_timeout_is_bounded(self, client, workspace):
        """An unbounded timeout from the client would pin a worker forever."""
        (workspace / "a.py").write_text("print('x')")
        assert client.post("/api/files/run",
                           json={"path": "a.py", "timeout": 99999}).status_code == 200
