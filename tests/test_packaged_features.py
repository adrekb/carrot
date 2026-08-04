"""Optional features must survive the trip into the installer.

Every optional dependency in Carrot is imported lazily — inside a function,
behind a try/except — so a source checkout without it still runs. That is the
right runtime behaviour, and it interacts badly with freezing: PyInstaller
decides what to bundle by walking imports statically, sees no reference to a
module that is only named inside a function body, and ships a build without
it. The feature then reports "not installed" on a machine where it *was*
installed at build time, and the only symptom is a missing tab.

That is exactly how browser control, Claude routing and vector search came to
be dead in shipped builds. These tests pin the two halves of the fix: the
build names each optional dependency explicitly, and the CI job installs the
extras that provide them.
"""
import ast
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = ROOT / "scripts" / "build_installer.py"
WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
PYPROJECT = ROOT / "pyproject.toml"

BUILD_SRC = BUILD_SCRIPT.read_text(encoding="utf-8")
WORKFLOW_SRC = WORKFLOW.read_text(encoding="utf-8")
PYPROJECT_SRC = PYPROJECT.read_text(encoding="utf-8")

sys.path.insert(0, str(ROOT / "scripts"))
import build_installer  # noqa: E402


# The extras CI is expected to install. pyautogui (desktop control) is
# deliberately excluded: it is off by default and gated by the policy kernel,
# so shipping it would only remove an install step nobody takes.
SHIPPED_EXTRAS = ["browser", "cloud", "vectors", "speech"]


def _ci_install_line():
    match = re.search(r"pip install -e \"?\.\[([^\]]+)\]\"?", WORKFLOW_SRC)
    return match.group(1) if match else ""


@pytest.mark.parametrize("extra", SHIPPED_EXTRAS)
def test_ci_installs_every_shipped_extra(extra):
    """`pip install -e .` alone means the feature is absent from the build,
    and the user has no Python to install it into afterwards."""
    assert extra in _ci_install_line(), (
        f"release.yml does not install the '{extra}' extra, so that feature "
        f"is dead in every shipped installer"
    )


def test_ci_does_not_ship_desktop_control():
    assert "desktop" not in _ci_install_line()


def test_every_shipped_extra_exists_in_pyproject():
    for extra in SHIPPED_EXTRAS:
        assert re.search(rf"^{extra} = \[", PYPROJECT_SRC, re.M), \
            f"release.yml installs '{extra}' but pyproject defines no such extra"


def test_optional_modules_are_collected_by_the_freeze():
    """The list the build walks must cover the modules the extras provide.

    Installing an extra is necessary but not sufficient — PyInstaller still
    has to be told to collect it, or it is installed at build time and absent
    at runtime.
    """
    collected = {module for module, _ in build_installer.OPTIONAL_COLLECTS}
    for module in ("playwright", "anthropic", "sqlite_vec",
                   "kokoro_onnx", "onnxruntime", "sounddevice"):
        assert module in collected, \
            f"{module} is installed by CI but never collected into the freeze"


def test_optional_flags_only_name_installed_modules():
    """--collect-all on a module that is not installed fails the build."""
    import importlib.util
    flags = build_installer.optional_dependency_flags()
    named = [flags[i + 1] for i, flag in enumerate(flags) if flag == "--collect-all"]
    for module in named:
        assert importlib.util.find_spec(module) is not None, \
            f"build would pass --collect-all {module}, which is not installed"


def test_collect_flags_are_well_formed():
    flags = build_installer.optional_dependency_flags()
    assert len(flags) % 2 == 0
    assert all(flag == "--collect-all" for flag in flags[::2])


def test_browser_resource_is_packaged():
    """The Chromium tree has to be copied into the app, and it has to land at
    the path carrot/browser.py looks for."""
    import json
    cfg = json.loads((ROOT / "gui" / "package.json").read_text(encoding="utf-8"))
    resources = cfg["build"]["extraResources"]
    targets = {entry["to"] for entry in resources}
    assert "pw-browsers" in targets, \
        "the bundled Chromium is not copied into the app's resources"
    sources = {entry["from"] for entry in resources if entry["to"] == "pw-browsers"}
    assert sources == {"../dist/pw-browsers"}


def test_runtime_looks_where_the_build_puts_the_browser():
    """The build writes to dist/pw-browsers -> resources/pw-browsers, and the
    runtime reads CARROT_RESOURCES/pw-browsers. If those two names drift the
    Agent tab goes quietly dead again."""
    from carrot import browser
    build_target = Path(build_installer.PW_BROWSERS_DIST).name
    src = Path(browser.__file__).read_text(encoding="utf-8")
    assert f'"{build_target}"' in src, \
        f"browser.py does not look for the '{build_target}' directory the build produces"


def test_build_script_still_parses():
    ast.parse(BUILD_SRC)


class TestBundledBrowserDiscovery:
    """carrot.browser's resolution of the shipped Chromium."""

    def test_no_resources_means_no_bundle(self, monkeypatch):
        from carrot import browser
        monkeypatch.delenv("CARROT_RESOURCES", raising=False)
        assert browser.bundled_browsers_dir() is None

    def test_missing_directory_means_no_bundle(self, monkeypatch, tmp_path):
        from carrot import browser
        monkeypatch.setenv("CARROT_RESOURCES", str(tmp_path))
        assert browser.bundled_browsers_dir() is None

    def test_found_when_present(self, monkeypatch, tmp_path):
        from carrot import browser
        (tmp_path / "pw-browsers").mkdir()
        monkeypatch.setenv("CARROT_RESOURCES", str(tmp_path))
        assert browser.bundled_browsers_dir() == str(tmp_path / "pw-browsers")

    def test_sets_the_env_var_playwright_reads(self, monkeypatch, tmp_path):
        from carrot import browser
        (tmp_path / "pw-browsers").mkdir()
        monkeypatch.setenv("CARROT_RESOURCES", str(tmp_path))
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        browser.use_bundled_browser()
        assert os_environ_path() == str(tmp_path / "pw-browsers")

    def test_never_overrides_an_explicit_choice(self, monkeypatch, tmp_path):
        """A developer pointing at their own browser tree should win."""
        from carrot import browser
        (tmp_path / "pw-browsers").mkdir()
        monkeypatch.setenv("CARROT_RESOURCES", str(tmp_path))
        monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "/somewhere/else")
        browser.use_bundled_browser()
        assert os_environ_path() == "/somewhere/else"

    def test_packaged_build_without_a_browser_says_so(self, monkeypatch, tmp_path):
        """And does not tell a user with no Python to run pip."""
        from carrot import browser
        pytest.importorskip("playwright")
        monkeypatch.setenv("CARROT_RESOURCES", str(tmp_path))
        monkeypatch.delenv("PLAYWRIGHT_BROWSERS_PATH", raising=False)
        monkeypatch.delenv("CARROT_CHROMIUM_PATH", raising=False)
        result = browser.is_available()
        assert result["available"] is False
        assert "pip install" not in result["hint"]

    def test_packaged_build_with_a_browser_is_available(self, monkeypatch, tmp_path):
        from carrot import browser
        pytest.importorskip("playwright")
        (tmp_path / "pw-browsers").mkdir()
        monkeypatch.setenv("CARROT_RESOURCES", str(tmp_path))
        assert browser.is_available()["available"] is True


def os_environ_path():
    import os
    return os.environ.get("PLAYWRIGHT_BROWSERS_PATH")


class TestFastUninstaller:
    """The NSIS uninstall override. It is compiled only on the Windows runner,
    so the cheap checks that can run here are worth having."""

    @property
    def source(self):
        return (ROOT / "gui" / "build" / "installer.nsh").read_text(encoding="utf-8")

    def test_is_referenced_by_the_build(self):
        import json
        cfg = json.loads((ROOT / "gui" / "package.json").read_text(encoding="utf-8"))
        assert cfg["build"]["nsis"]["include"] == "build/installer.nsh"

    def test_defines_the_macro_electron_builder_calls(self):
        assert "!macro customRemoveFiles" in self.source
        assert "!macroend" in self.source

    def test_uses_a_recursive_delete(self):
        """The whole point: one RMDir /r instead of ~100k individual deletes."""
        assert "RMDir /r" in self.source

    def test_recursive_delete_is_guarded(self):
        """An unguarded RMDir /r on an empty $INSTDIR deletes the parent."""
        assert 'StrCmp $INSTDIR ""' in self.source
        assert "IfFileExists" in self.source

    def test_removes_the_heavy_trees_by_name(self):
        for path in ("pw-browsers", "backend"):
            assert path in self.source

    def test_avoids_logiclib_and_internal_macros(self):
        """This file only compiles on CI, so it must not depend on includes
        electron-builder's template may or may not have pulled in."""
        assert "${if}" not in self.source.lower()
        assert "DEFAULT_customRemoveFiles" not in self.source


class TestLinuxAudio:
    """sounddevice is a binding, not a driver.

    It dlopen()s PortAudio at import time. The Windows and macOS wheels carry
    the library; the Linux wheel does not. Shipping the speech extra therefore
    made a previously unreachable failure mode reachable on Linux.
    """

    def test_missing_portaudio_is_handled(self):
        """It raises OSError, not ImportError — a handler that catches only
        ImportError turns a missing system library into a 500."""
        src = (ROOT / "carrot" / "speech" / "whisper_stt.py").read_text(encoding="utf-8")
        assert "except OSError" in src, (
            "importing sounddevice without PortAudio raises OSError; catching "
            "only ImportError lets it escape as a crash"
        )

    def test_deb_declares_portaudio(self):
        assert "libportaudio2" in _deb_depends()

    def test_deb_does_not_depend_on_packages_debian_removed(self):
        """electron-builder's default depends list still names gconf2,
        gconf-service and libappindicator1, all long gone from Debian and
        Ubuntu. A .deb that requires them cannot be installed at all."""
        for obsolete in ("gconf2", "gconf-service", "libappindicator1"):
            assert obsolete not in _deb_depends()

    def test_deb_keeps_the_electron_runtime_libraries(self):
        """depends replaces electron-builder's default rather than extending
        it, so dropping one of these silently ships a broken package."""
        depends = set(_deb_depends())
        for required in ("libnotify4", "libxtst6", "libnss3", "libgtk-3-0"):
            assert required in depends, f"the .deb no longer requires {required}"

    def test_depends_is_on_the_deb_target_not_the_linux_platform(self):
        """depends is a deb-target option. Putting it under `linux` fails
        schema validation — and electron-builder validates the whole config
        whatever it is building, so the misplacement broke the Windows and
        macOS jobs too, not just Linux."""
        cfg = _builder_config()
        assert "depends" not in cfg["linux"], \
            "depends belongs under build.deb; under build.linux it fails validation"
        assert "depends" in cfg.get("deb", {})


def _builder_config():
    import json
    return json.loads((ROOT / "gui" / "package.json").read_text(encoding="utf-8"))["build"]


def _deb_depends():
    return _builder_config().get("deb", {}).get("depends", [])


def test_builder_config_uses_only_known_platform_keys():
    """A stray key anywhere under `build` aborts every platform's packaging
    before it starts, so the failure never looks Linux-specific."""
    cfg = _builder_config()
    known_linux = {
        "appId", "artifactName", "asar", "asarUnpack", "category", "compression",
        "cscKeyPassword", "cscLink", "defaultArch", "description", "desktop",
        "detectUpdateChannel", "electronLanguages", "electronUpdaterCompatibility",
        "executableArgs", "executableName", "extraFiles", "extraResources",
        "fileAssociations", "files", "forceCodeSigning",
        "generateUpdatesFilesForAllChannels", "icon", "maintainer", "mimeTypes",
        "packageCategory", "protocols", "publish", "releaseInfo", "synopsis",
        "target", "vendor",
    }
    unknown = set(cfg.get("linux", {})) - known_linux
    assert not unknown, f"unknown keys under build.linux: {sorted(unknown)}"


def test_build_skips_the_headless_shell():
    """The agent runs headful, so the headless shell is a binary nothing can
    reach — it was 115 MB of download on the first CI run."""
    assert "--no-shell" in BUILD_SRC


class TestRunnerLabels:
    """A retired GitHub-hosted runner label does not fail — it queues.

    The job is never assigned a runner and sits there until the 24h timeout,
    so the run shows as "in progress" rather than red and the missing artifact
    is easy to miss. macos-13 was retired on 2025-12-04 and the Intel Mac
    build silently stopped producing anything for months.
    """

    # Labels GitHub has retired, mapped to what replaced them.
    RETIRED = {
        "macos-11": "macos-14 / macos-15",
        "macos-12": "macos-14 / macos-15",
        "macos-13": "macos-15-intel (Intel) or macos-14+ (Apple Silicon)",
        "ubuntu-18.04": "ubuntu-22.04 / ubuntu-latest",
        "ubuntu-20.04": "ubuntu-22.04 / ubuntu-latest",
        "windows-2016": "windows-latest",
        "windows-2019": "windows-latest",
    }

    def test_no_retired_runner_labels(self):
        for label, replacement in self.RETIRED.items():
            assert f"os: {label}\n" not in WORKFLOW_SRC, (
                f"'{label}' is a retired runner label — that job will queue "
                f"forever rather than fail. Use {replacement}."
            )

    def test_every_platform_still_has_a_job(self):
        """Losing a matrix entry is the other way a target goes missing."""
        for artifact in ("windows-x64", "mac-arm64", "mac-x64", "linux-x64"):
            assert f"artifact: {artifact}" in WORKFLOW_SRC, \
                f"no build job produces the {artifact} installer"


class TestQuickAskOverlay:
    """The Alt+Space panel. It is a file:// page in its own window, so most of
    what can go wrong is invisible to the rest of the test suite."""

    @property
    def main_js(self):
        return (ROOT / "gui" / "main.js").read_text(encoding="utf-8")

    @property
    def overlay(self):
        return (ROOT / "gui" / "public" / "overlay.html").read_text(encoding="utf-8")

    @property
    def preload(self):
        return (ROOT / "gui" / "preload.js").read_text(encoding="utf-8")

    def test_the_window_base_colour_is_cleared(self):
        """transparent:true does not clear Electron's default opaque white —
        it painted as a grey slab around the panel. Only an explicit
        fully-transparent backgroundColor removes it."""
        assert "backgroundColor: '#00000000'" in self.main_js

    def test_the_panel_is_big_enough_to_hold_its_controls(self):
        """620x92 fits one line of text and nothing else — no attachment
        chip, no workspace name."""
        width = int(re.search(r"const OVERLAY_WIDTH = (\d+)", self.main_js).group(1))
        height = int(re.search(r"const OVERLAY_HEIGHT = (\d+)", self.main_js).group(1))
        assert width >= 700 and height >= 130

    def test_it_can_grow_beyond_its_collapsed_height(self):
        max_height = int(re.search(r"const OVERLAY_MAX_HEIGHT = (\d+)", self.main_js).group(1))
        height = int(re.search(r"const OVERLAY_HEIGHT = (\d+)", self.main_js).group(1))
        assert max_height > height

    def test_attachments_and_workspaces_are_bridged(self):
        for call in ("pickAttachments", "readAttachment", "listWorkspaces"):
            assert call in self.preload, f"the overlay cannot reach {call}"
            
    def test_the_handlers_exist_for_every_bridged_call(self):
        for channel in ("pick-attachments", "read-attachment", "list-workspaces"):
            assert f"ipcMain.handle('{channel}'" in self.main_js

    def test_send_command_still_accepts_a_bare_string(self):
        """The overlay now sends an object; other callers still send a string,
        and a signature change should not silently break them."""
        assert "typeof command === 'string'" in self.main_js

    def test_a_workspace_choice_reaches_the_chat_api(self):
        from carrot import app as carrot_app

        assert "workspace_id" in carrot_app.ChatRequest.model_fields

    def test_attachments_are_size_capped_before_being_read(self):
        """Reading an arbitrary file into base64 in the main process is how a
        dropped video would freeze the panel."""
        assert "OVERLAY_MAX_ATTACHMENT_BYTES" in self.main_js

    def test_replies_are_escaped_not_injected(self):
        """The reply is model output; innerHTML with it would run whatever the
        model emitted, inside a window that holds the preload bridge."""
        assert "showReply(esc(" in self.overlay


class TestPackagingRetries:
    """Building a .dmg mounts a disk image and then ejects it. On a busy macOS
    runner the eject loses a race with Spotlight or the diskimages helper, and
    electron-builder exits non-zero — the same commit packaged cleanly on the
    arm64 runner in the same run, so it is timing, not the build."""

    def test_packaging_is_retried(self):
        assert "PACKAGE_ATTEMPTS" in BUILD_SRC
        assert build_installer.PACKAGE_ATTEMPTS > 1

    def test_a_failure_still_fails_the_build_eventually(self):
        """Retrying forever would turn a real breakage into a hung job."""
        assert "raise subprocess.CalledProcessError" in BUILD_SRC

    def test_a_stuck_volume_is_detached_before_retrying(self):
        """The retry would otherwise trip over the volume the failed attempt
        left mounted."""
        assert "hdiutil" in BUILD_SRC and "detach" in BUILD_SRC

    def test_the_retry_waits(self):
        assert build_installer.PACKAGE_RETRY_DELAY > 0
