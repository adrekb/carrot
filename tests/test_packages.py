"""Turning a missing dependency into a button that fixes it.

The user this is for is not a programmer. They wrote `import pandas`, pressed
Run, and got fifty lines of traceback whose actual content is one line near the
bottom. Every test here is about that person: is the right thing detected, is
the right thing named, and is nothing dangerous done on their behalf.
"""
import subprocess
from unittest.mock import patch

import pytest

from carrot import packages, runner


class TestPythonDetection:
    def test_a_modulenotfound_traceback_is_read(self):
        output = (
            'Traceback (most recent call last):\n'
            '  File "/tmp/x.py", line 1, in <module>\n'
            '    import pandas\n'
            "ModuleNotFoundError: No module named 'pandas'\n"
        )
        offer = packages.detect(output, "Python")
        assert offer["package"] == "pandas"
        assert offer["manager"] == "pip"
        assert offer["installable"] is True

    def test_the_command_is_shown_so_nothing_is_a_surprise(self):
        offer = packages.detect("ModuleNotFoundError: No module named 'requests'", "Python")
        assert "pip install requests" in offer["command"]

    def test_a_submodule_resolves_to_its_top_level_package(self):
        offer = packages.detect(
            "ModuleNotFoundError: No module named 'numpy.linalg'", "Python")
        assert offer["package"] == "numpy"

    @pytest.mark.parametrize("imported,installed", [
        ("cv2", "opencv-python"),
        ("PIL", "Pillow"),
        ("sklearn", "scikit-learn"),
        ("yaml", "PyYAML"),
        ("bs4", "beautifulsoup4"),
        ("dotenv", "python-dotenv"),
        ("docx", "python-docx"),
        ("jwt", "PyJWT"),
    ])
    def test_the_import_name_is_translated_to_the_package_name(self, imported, installed):
        # Nothing in the error says that `import cv2` is fixed by installing
        # opencv-python. This mapping is the whole feature for a beginner.
        offer = packages.detect(f"ModuleNotFoundError: No module named '{imported}'", "Python")
        assert offer["package"] == installed

    def test_an_aliased_package_explains_the_rename(self):
        offer = packages.detect("ModuleNotFoundError: No module named 'cv2'", "Python")
        assert "comes from the package" in offer["note"]

    def test_a_standard_library_module_is_not_offered_for_install(self):
        # pip installing a stdlib name gets you nothing, or a typosquat.
        offer = packages.detect("ModuleNotFoundError: No module named 'json'", "Python")
        assert offer["installable"] is False
        assert "standard library" in offer["message"]

    def test_tkinter_gets_the_answer_that_actually_works(self):
        offer = packages.detect("ModuleNotFoundError: No module named 'tkinter'", "Python")
        assert offer["installable"] is False
        assert "python3-tk" in offer["message"]

    def test_pip_targets_the_interpreter_that_runs_the_file(self):
        # Installing with a different python is the classic "but I did install
        # it" failure.
        offer = packages.detect("ModuleNotFoundError: No module named 'rich'", "Python")
        assert offer["command"].startswith(packages.python_executable())


class TestOtherLanguages:
    def test_node_cannot_find_module(self):
        offer = packages.detect("Error: Cannot find module 'express'", "JavaScript")
        assert offer["package"] == "express" and offer["manager"] == "npm"

    def test_node_esm_phrasing(self):
        offer = packages.detect("Cannot find package 'chalk' imported from /x.mjs", "JavaScript")
        assert offer["package"] == "chalk"

    def test_typescript_fails_the_way_node_does(self):
        assert packages.detect("Cannot find module 'zod'", "TypeScript")["manager"] == "npm"

    def test_ruby(self):
        offer = packages.detect("cannot load such file -- nokogiri", "Ruby")
        assert offer["package"] == "nokogiri" and offer["manager"] == "gem"

    def test_rust(self):
        offer = packages.detect("error[E0463]: can't find crate for `rand`", "Rust")
        assert offer["package"] == "rand" and offer["manager"] == "cargo"

    def test_go(self):
        offer = packages.detect(
            "no required module provides package github.com/pkg/errors", "Go")
        assert offer["package"] == "github.com/pkg/errors"

    def test_perl(self):
        offer = packages.detect("Can't locate JSON/PP.pm in @INC (you may need", "Perl")
        assert offer["manager"] == "cpan"

    def test_a_c_header_is_named_but_not_offered(self):
        # There is no portable installer for a system library, and guessing
        # `apt install` on someone's machine is worse than saying so.
        offer = packages.detect(
            "main.cpp:1:10: fatal error: boost/asio.hpp: No such file or directory", "C++")
        assert offer["installable"] is False and "boost/asio.hpp" in offer["missing"]

    def test_a_missing_java_package_points_at_the_build_file(self):
        offer = packages.detect("error: package org.json does not exist", "Java")
        assert offer["installable"] is False and "Maven" in offer["message"]


class TestNoFalseOffers:
    def test_a_syntax_error_produces_nothing(self):
        # Offering to install something on top of a real error is noise.
        assert packages.detect("SyntaxError: invalid syntax", "Python") is None

    def test_a_successful_run_produces_nothing(self):
        assert packages.detect("hello world\n", "Python") is None

    def test_empty_output_produces_nothing(self):
        assert packages.detect("", "Python") is None

    def test_an_unknown_language_produces_nothing(self):
        assert packages.detect("Cannot find module 'x'", "Brainfuck") is None

    def test_a_relative_import_is_not_an_installable_package(self):
        # `require('./helpers')` failing means their file is missing, not that
        # npm has a package called "./helpers".
        assert packages.detect("Cannot find module './helpers'", "JavaScript") is None


class TestNameSafety:
    @pytest.mark.parametrize("name", [
        "pandas; rm -rf ~", "--upgrade", "-e .", "pkg && curl evil.sh",
        "$(whoami)", "a`b`", "pkg|tee", "", "has space",
    ])
    def test_a_dangerous_name_produces_no_offer(self, name):
        assert packages._offer(name, "pip", "Python") is None

    @pytest.mark.parametrize("name", [
        "pandas; rm -rf ~", "--upgrade", "$(whoami)", "",
    ])
    def test_a_dangerous_name_is_refused_at_install_time_too(self, name):
        # The endpoint has no way to know which button called it, so the name
        # is validated again rather than trusted from detection.
        result = packages.install(name, "pip")
        assert result["ok"] is False and "not a valid package name" in result["output"]

    def test_an_unknown_manager_is_refused(self):
        assert packages.install("pandas", "brew")["ok"] is False

    def test_the_installer_never_uses_a_shell(self):
        with patch.object(packages.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "done", "")
            packages.install("pandas", "pip")
        # A list argv with no shell=True is what makes a package name a package
        # name even when it is full of punctuation.
        assert isinstance(run.call_args[0][0], list)
        assert run.call_args.kwargs.get("shell") in (None, False)

    def test_the_package_is_one_argv_element(self):
        with patch.object(packages.subprocess, "run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "done", "")
            packages.install("scikit-learn", "pip")
        assert "scikit-learn" in run.call_args[0][0]


class TestInstalling:
    def test_a_successful_install_reports_ok(self):
        with patch.object(packages.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0, "Successfully installed", "")):
            result = packages.install("pandas", "pip")
        assert result["ok"] is True and "Successfully installed" in result["output"]

    def test_a_failed_install_carries_the_reason(self):
        with patch.object(packages.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 1, "", "No matching distribution")):
            result = packages.install("pandaz", "pip")
        assert result["ok"] is False and "No matching distribution" in result["output"]

    def test_a_hung_install_is_stopped(self):
        with patch.object(packages.subprocess, "run",
                          side_effect=subprocess.TimeoutExpired("pip", 300)):
            result = packages.install("pandas", "pip")
        assert result["ok"] is False and "longer than" in result["output"]

    def test_a_missing_manager_says_so_rather_than_crashing(self):
        with patch.object(packages.shutil, "which", return_value=None):
            result = packages.install("express", "npm")
        assert result["ok"] is False and "not installed on this computer" in result["output"]


class TestRunIntegration:
    def test_a_failed_run_carries_the_offer(self, isolated_db, tmp_path, monkeypatch):
        from carrot import files_api

        monkeypatch.setattr(files_api, "get_root", lambda: str(tmp_path))
        # A name nothing could plausibly have installed, so this exercises the
        # real interpreter, the real traceback and the real detector.
        (tmp_path / "x.py").write_text("import carrot_no_such_package_xyz\n")
        result = runner.run_file("x.py")
        assert result["ok"] is False
        assert result["missing_package"]["package"] == "carrot_no_such_package_xyz"
        assert result["missing_package"]["manager"] == "pip"

    def test_a_successful_run_carries_no_offer(self, isolated_db, tmp_path, monkeypatch):
        from carrot import files_api

        monkeypatch.setattr(files_api, "get_root", lambda: str(tmp_path))
        (tmp_path / "x.py").write_text("print('hi')\n")
        result = runner.run_file("x.py")
        assert result["ok"] is True and result["missing_package"] is None

    def test_a_missing_toolchain_carries_its_download_page(self, isolated_db, tmp_path, monkeypatch):
        from carrot import files_api

        monkeypatch.setattr(files_api, "get_root", lambda: str(tmp_path))
        (tmp_path / "x.rs").write_text("fn main() {}\n")
        monkeypatch.setattr(runner, "_resolve_tool", lambda recipe: None)
        result = runner.run_file("x.rs")
        # Pressing Run is when someone finds out they have no Rust; the answer
        # has to include where to get it.
        assert result["missing_tool"] and result["help_url"] == "https://rustup.rs/"

    def test_detection_failing_never_breaks_a_run(self, isolated_db, tmp_path, monkeypatch):
        from carrot import files_api

        monkeypatch.setattr(files_api, "get_root", lambda: str(tmp_path))
        (tmp_path / "x.py").write_text("raise SystemExit(1)\n")
        with patch.object(packages, "detect", side_effect=RuntimeError("boom")):
            result = runner.run_file("x.py")
        assert result["missing_package"] is None


class TestInstallEndpoint:
    def test_a_package_can_be_installed(self, client):
        with patch.object(packages.subprocess, "run",
                          return_value=subprocess.CompletedProcess([], 0, "ok", "")):
            body = client.post("/api/files/install",
                               json={"package": "pandas", "manager": "pip"})
        assert body.status_code == 200 and body.json()["ok"] is True

    def test_a_dangerous_name_is_a_400(self, client):
        body = client.post("/api/files/install",
                           json={"package": "x; rm -rf ~", "manager": "pip"})
        assert body.status_code == 400

    def test_an_unknown_manager_is_reported_not_run(self, client):
        body = client.post("/api/files/install",
                           json={"package": "pandas", "manager": "brew"})
        assert body.json()["ok"] is False

    def test_the_endpoint_needs_a_session_token(self, unauthenticated_client):
        assert unauthenticated_client.post(
            "/api/files/install", json={"package": "pandas", "manager": "pip"}).status_code == 401


class TestCodeTabWiring:
    def read(self, *parts):
        from pathlib import Path
        return Path(__file__).resolve().parents[1].joinpath("carrot", "web", *parts).read_text(encoding="utf-8")

    def test_the_offer_area_exists(self):
        assert 'id="run-offer"' in self.read("index.html")

    def test_a_failed_run_shows_the_right_offer(self):
        js = self.read("js", "features.js")
        assert "if (r.missing_tool) showToolchainOffer(r);" in js
        assert "else if (r.missing_package) showPackageOffer(r.missing_package);" in js

    def test_installing_re_runs_the_file(self):
        # The reason anyone pressed the button was to get their program to run.
        js = self.read("js", "features.js")
        after = js.split("async function installMissingPackage")[1][:1600]
        assert "runCurrentFile();" in after

    def test_the_command_is_shown_next_to_the_button(self):
        assert "offer-cmd" in self.read("js", "features.js")

    def test_every_css_token_the_offer_uses_is_defined(self):
        import re

        css = self.read("css", "style.css")
        block = css.split('/* ===== "You\'re missing something" offers =====')[1]
        used = set(re.findall(r"var\((--[a-z0-9-]+)", block))
        defined = set(re.findall(r"^\s*(--[a-z0-9-]+):", css, re.M))
        assert used <= defined, f"undefined CSS tokens: {sorted(used - defined)}"
