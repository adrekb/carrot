"""Tests for cross-platform packaging: data-dir resolution and the
per-OS/per-GPU Ollama artifact selection used by the one-click installer."""
import os
import sys

from carrot import bootstrap, config


# ===== Per-user data directory (frozen app must not write into install dir) =====

def test_data_dir_env_override_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("CARROT_DATA_DIR", str(tmp_path / "portable"))
    assert config._default_data_dir() == str(tmp_path / "portable")


def test_data_dir_checkout_default(monkeypatch):
    monkeypatch.delenv("CARROT_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert config._default_data_dir().endswith(os.path.join("carrot", "data"))


def test_data_dir_frozen_is_user_writable(monkeypatch):
    monkeypatch.delenv("CARROT_DATA_DIR", raising=False)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    d = config._default_data_dir()
    home = os.path.expanduser("~")
    assert d.startswith(home) or d.startswith(os.environ.get("APPDATA", home))
    # Never inside the package/install directory.
    assert not d.startswith(os.path.dirname(os.path.abspath(config.__file__)))


# ===== Ollama artifact selection =====

def _fake_platform(monkeypatch, system, machine="x86_64"):
    monkeypatch.setattr(bootstrap.platform, "system", lambda: system)
    monkeypatch.setattr(bootstrap.platform, "machine", lambda: machine)


def test_windows_uses_official_installer(monkeypatch):
    _fake_platform(monkeypatch, "Windows")
    assert bootstrap.ollama_artifact_url() == bootstrap.OLLAMA_DOWNLOAD_URL
    assert bootstrap.ollama_rocm_addon_url() is None  # installer already has ROCm


def test_macos_uses_darwin_tarball_any_arch(monkeypatch):
    for arch in ("arm64", "x86_64"):
        _fake_platform(monkeypatch, "Darwin", arch)
        assert bootstrap.ollama_artifact_url().endswith("ollama-darwin.tgz")
        assert bootstrap.ollama_rocm_addon_url() is None  # Metal is built in


def test_linux_tarball_matches_arch(monkeypatch):
    _fake_platform(monkeypatch, "Linux", "x86_64")
    assert bootstrap.ollama_artifact_url().endswith("ollama-linux-amd64.tgz")
    assert bootstrap.ollama_rocm_addon_url().endswith("ollama-linux-amd64-rocm.tgz")
    _fake_platform(monkeypatch, "Linux", "aarch64")
    assert bootstrap.ollama_artifact_url().endswith("ollama-linux-arm64.tgz")
    assert bootstrap.ollama_rocm_addon_url() is None  # no ROCm build for arm64


# ===== Managed (no-sudo) install =====

def test_managed_ollama_binary_is_discovered(monkeypatch, tmp_path):
    managed = tmp_path / "ollama"
    (managed / "bin").mkdir(parents=True)
    exe = managed / "bin" / "ollama"
    exe.write_text("#!/bin/sh\n")
    monkeypatch.setattr(bootstrap, "MANAGED_OLLAMA_DIR", str(managed))
    monkeypatch.setattr(bootstrap.shutil, "which", lambda name: None)
    assert bootstrap.get_ollama_executable() == str(exe)


def test_install_ollama_unix_extracts_and_chmods(monkeypatch, tmp_path):
    import tarfile

    # Build a fake ollama-linux tarball: bin/ollama + lib/ollama/libfoo.
    src = tmp_path / "src"
    (src / "bin").mkdir(parents=True)
    (src / "lib" / "ollama").mkdir(parents=True)
    (src / "bin" / "ollama").write_text("binary")
    (src / "lib" / "ollama" / "libfoo.so").write_text("lib")
    archive = tmp_path / "ollama-linux-amd64.tgz"
    with tarfile.open(archive, "w:gz") as tf:
        tf.add(src / "bin", arcname="bin")
        tf.add(src / "lib", arcname="lib")

    managed = tmp_path / "managed"
    monkeypatch.setattr(bootstrap, "MANAGED_OLLAMA_DIR", str(managed))
    monkeypatch.setattr(bootstrap, "ollama_artifact_url",
                        lambda: "https://example.test/ollama-linux-amd64.tgz")
    monkeypatch.setattr(bootstrap, "ollama_rocm_addon_url", lambda: None)

    def fake_download(dest, progress_cb=None, url=""):
        import shutil as sh
        sh.copy(archive, dest)
        return True
    monkeypatch.setattr(bootstrap, "download_installer", fake_download)

    assert bootstrap.install_ollama_unix() is True
    exe = managed / "bin" / "ollama"
    assert exe.exists()
    assert os.access(exe, os.X_OK)
    assert (managed / "lib" / "ollama" / "libfoo.so").exists()


def test_install_ollama_unix_fails_cleanly_when_offline(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap, "MANAGED_OLLAMA_DIR", str(tmp_path / "managed"))
    monkeypatch.setattr(bootstrap, "download_installer",
                        lambda dest, progress_cb=None, url="": False)
    events = []
    assert bootstrap.install_ollama_unix(progress_cb=events.append) is False


# ===== Asset cache busting =====
#
# An update that keeps serving the previous build's JavaScript is
# indistinguishable from the fix never shipping, so this is load-bearing.

def test_index_fingerprints_js_and_css_urls(client):
    html = client.get("/").text
    import re as _re
    refs = _re.findall(r'(?:src|href)="(/(?:js|css)/[^"]+)"', html)
    assert refs, "no app assets referenced"
    assert all("?v=" in ref for ref in refs), [r for r in refs if "?v=" not in r]
    # One fingerprint for the whole page.
    assert len({r.split("?v=")[1] for r in refs}) == 1


def test_fingerprint_changes_when_an_asset_changes(monkeypatch, tmp_path):
    from carrot import app as app_mod
    web = tmp_path / "web"
    (web / "js").mkdir(parents=True)
    (web / "css").mkdir()
    asset = web / "js" / "app.js"
    asset.write_text("v1")
    monkeypatch.setattr(app_mod, "WEB_DIR", str(web))

    first = app_mod._asset_fingerprint()
    asset.write_text("a much longer second version")
    assert app_mod._asset_fingerprint() != first


def test_app_code_revalidates_but_fonts_stay_cacheable(client):
    """Stale JS breaks updates; fonts are immutable and large."""
    js = client.get("/js/app.js")
    assert "no-cache" in js.headers.get("cache-control", "")
    font = client.get("/assets/fonts/InterVariable.woff2")
    if font.status_code == 200:
        assert "immutable" in font.headers.get("cache-control", "")
