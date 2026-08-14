"""Image and video generation across hosted and on-device backends.

Every backend is exercised against a fake transport rather than the real API:
what matters here is that Carrot sends the right shape, reads the right field
out of the answer, and turns a failure into a sentence the user can act on.
"""
import base64
from unittest.mock import patch

import pytest

from carrot import media


PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
B64 = base64.b64encode(PNG).decode()


class FakeResponse:
    def __init__(self, payload=None, content=b"", status=200, text=""):
        self._payload = payload
        self.content = content
        self.status_code = status
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


@pytest.fixture
def key(isolated_db):
    media.set_api_key("openai", "sk-test")
    media.set_api_key("stability", "st-test")
    media.set_api_key("replicate", "r8-test")
    media.set_api_key("fal", "fal-test")
    return True


class TestBackendRegistry:
    def test_local_backends_need_no_key(self, isolated_db):
        assert media.configured("automatic1111") is True

    def test_hosted_backends_need_one(self, isolated_db):
        assert media.configured("openai") is False

    def test_a_media_key_is_stored_separately_from_the_chat_key(self, isolated_db):
        media.set_api_key("openai", "sk-media")
        assert media.api_key("openai") == "sk-media"

    def test_a_chat_provider_key_is_reused(self, isolated_db):
        # Someone who already pasted an OpenAI key for chat should not have to
        # paste it again to make a picture.
        from carrot import providers

        providers.set_api_key("openai", "sk-chat")
        assert media.api_key("openai") == "sk-chat"

    def test_an_unknown_backend_is_an_error(self, isolated_db):
        with pytest.raises(media.MediaError):
            media.api_key("midjourney")

    def test_the_endpoint_can_be_overridden(self, isolated_db):
        media.set_endpoint("automatic1111", "http://192.168.1.9:7860/")
        assert media.base_url("automatic1111") == "http://192.168.1.9:7860"

    def test_backends_can_be_filtered_by_kind(self, isolated_db):
        video = {b["id"] for b in media.backends(media.KIND_VIDEO)}
        assert "replicate" in video
        assert "automatic1111" not in video


class TestDefaultBackend:
    def test_local_wins_when_several_are_set_up(self, isolated_db):
        media.set_api_key("openai", "sk-test")
        # Local costs nothing and sends nothing; it should never lose to a
        # cloud backend that merely also happens to work.
        assert media.default_backend(media.KIND_IMAGE) == "automatic1111"

    def test_an_explicit_choice_wins_over_the_heuristic(self, isolated_db):
        from carrot.config import set_config

        media.set_api_key("openai", "sk-test")
        set_config("media_backend_image", "openai")
        assert media.default_backend(media.KIND_IMAGE) == "openai"

    def test_a_choice_that_cannot_do_this_kind_is_ignored(self, isolated_db):
        from carrot.config import set_config

        set_config("media_backend_video", "automatic1111")
        media.set_api_key("replicate", "r8")
        assert media.default_backend(media.KIND_VIDEO) == "replicate"

    def test_no_video_backend_says_what_to_do(self, isolated_db):
        with pytest.raises(media.MediaError) as caught:
            media.default_backend(media.KIND_VIDEO)
        assert "Settings" in str(caught.value)


class TestOnDeviceBackends:
    def test_automatic1111_returns_the_decoded_image(self, isolated_db):
        with patch.object(media.requests, "post",
                          return_value=FakeResponse({"images": [B64]})) as post:
            result = media.generate("a rabbit", backend="automatic1111")
        assert result["local"] is True
        assert result["files"][0]["bytes"] == len(PNG)
        assert "/sdapi/v1/txt2img" in post.call_args[0][0]

    def test_the_prompt_and_size_reach_the_local_server(self, isolated_db):
        with patch.object(media.requests, "post",
                          return_value=FakeResponse({"images": [B64]})) as post:
            media.generate("a rabbit", backend="automatic1111", width=512, height=768)
        body = post.call_args.kwargs["json"]
        assert body["prompt"] == "a rabbit"
        assert (body["width"], body["height"]) == (512, 768)

    def test_a_refused_local_connection_says_it_is_not_running(self, isolated_db):
        with patch.object(media.requests, "post",
                          side_effect=media.requests.ConnectionError("refused")):
            with pytest.raises(media.MediaError) as caught:
                media.generate("x", backend="automatic1111")
        assert "Is it running" in str(caught.value)

    def test_comfyui_without_a_workflow_explains_how_to_get_one(self, isolated_db):
        with pytest.raises(media.MediaError) as caught:
            media.generate("x", backend="comfyui")
        assert "Save (API Format)" in str(caught.value)

    def test_the_comfy_workflow_gets_the_users_prompt(self, isolated_db):
        graph = {"3": {"class_type": "CLIPTextEncode", "inputs": {"text": "placeholder"}}}
        filled = media._fill_comfy_prompt(graph, "a rabbit in a field")
        assert filled["3"]["inputs"]["text"] == "a rabbit in a field"
        assert graph["3"]["inputs"]["text"] == "placeholder"  # original untouched


class TestHostedBackends:
    def test_openai_reads_base64_data(self, key):
        with patch.object(media.requests, "post",
                          return_value=FakeResponse({"data": [{"b64_json": B64}]})):
            result = media.generate("a rabbit", backend="openai")
        assert result["backend"] == "openai" and result["local"] is False

    def test_openai_falls_back_to_downloading_a_url(self, key):
        with patch.object(media.requests, "post",
                          return_value=FakeResponse({"data": [{"url": "https://x/i.png"}]})), \
             patch.object(media.requests, "get", return_value=FakeResponse(content=PNG)):
            result = media.generate("a rabbit", backend="openai")
        assert result["files"][0]["bytes"] == len(PNG)

    def test_openai_sends_the_key_as_a_bearer(self, key):
        with patch.object(media.requests, "post",
                          return_value=FakeResponse({"data": [{"b64_json": B64}]})) as post:
            media.generate("x", backend="openai")
        assert post.call_args.kwargs["headers"]["Authorization"] == "Bearer sk-test"

    def test_stability_returns_raw_bytes(self, key):
        with patch.object(media.requests, "post", return_value=FakeResponse(content=PNG)):
            result = media.generate("a rabbit", backend="stability")
        assert result["files"][0]["bytes"] == len(PNG)

    def test_replicate_polls_until_the_job_finishes(self, key):
        started = {"status": "processing", "urls": {"get": "https://api/p/1"}}
        finished = {"status": "succeeded", "output": ["https://x/out.png"]}
        with patch.object(media.requests, "post", return_value=FakeResponse(started)), \
             patch.object(media.requests, "get",
                          side_effect=[FakeResponse(finished), FakeResponse(content=PNG)]), \
             patch.object(media.time, "sleep", lambda _: None):
            result = media.generate("a rabbit", backend="replicate")
        assert result["files"][0]["bytes"] == len(PNG)

    def test_a_failed_replicate_job_surfaces_its_reason(self, key):
        started = {"status": "processing", "urls": {"get": "https://api/p/1"}}
        failed = {"status": "failed", "error": "NSFW content detected"}
        with patch.object(media.requests, "post", return_value=FakeResponse(started)), \
             patch.object(media.requests, "get", return_value=FakeResponse(failed)), \
             patch.object(media.time, "sleep", lambda _: None):
            with pytest.raises(media.MediaError) as caught:
                media.generate("x", backend="replicate")
        assert "NSFW" in str(caught.value)

    def test_a_stuck_job_gives_up_rather_than_hanging_the_turn(self, key):
        forever = {"status": "processing", "urls": {"get": "https://api/p/1"}}
        with patch.object(media.requests, "post", return_value=FakeResponse(forever)), \
             patch.object(media.requests, "get", return_value=FakeResponse(forever)), \
             patch.object(media.time, "sleep", lambda _: None), \
             patch.object(media, "POLL_CEILING_IMAGE", 0):
            with pytest.raises(media.MediaError) as caught:
                media.generate("x", backend="replicate")
        assert "did not finish" in str(caught.value)

    def test_fal_uses_its_own_key_scheme(self, key):
        with patch.object(media.requests, "post",
                          return_value=FakeResponse({"images": [{"url": "https://x/i.png"}]})) as post, \
             patch.object(media.requests, "get", return_value=FakeResponse(content=PNG)):
            media.generate("x", backend="fal")
        # fal uses "Key <token>", not "Bearer".
        assert post.call_args.kwargs["headers"]["Authorization"] == "Key fal-test"

    def test_video_uses_the_video_default_model(self, key):
        started = {"status": "succeeded", "output": "https://x/out.mp4"}
        with patch.object(media.requests, "post", return_value=FakeResponse(started)) as post, \
             patch.object(media.requests, "get", return_value=FakeResponse(content=b"mp4data")):
            result = media.generate("a rabbit running", kind=media.KIND_VIDEO,
                                    backend="replicate")
        assert media.BACKENDS["replicate"]["default_video_model"] in post.call_args[0][0]
        assert result["files"][0]["name"].endswith(".mp4")


class TestGenerateContract:
    def test_an_empty_prompt_is_refused(self, isolated_db):
        with pytest.raises(media.MediaError):
            media.generate("   ", backend="automatic1111")

    def test_asking_a_backend_for_a_kind_it_cannot_do(self, isolated_db):
        with pytest.raises(media.MediaError) as caught:
            media.generate("x", kind=media.KIND_VIDEO, backend="automatic1111")
        assert "cannot generate video" in str(caught.value)

    def test_an_unconfigured_hosted_backend_says_where_to_fix_it(self, isolated_db):
        with pytest.raises(media.MediaError) as caught:
            media.generate("x", backend="openai")
        assert "Settings" in str(caught.value)

    def test_an_unknown_kind_is_refused(self, isolated_db):
        with pytest.raises(media.MediaError):
            media.generate("x", kind="hologram", backend="automatic1111")

    def test_a_rejected_key_is_reported_as_such(self, key):
        with patch.object(media.requests, "post",
                          return_value=FakeResponse({"error": {"message": "bad key"}}, status=401)):
            with pytest.raises(media.MediaError) as caught:
                media.generate("x", backend="openai")
        assert "API key was rejected" in str(caught.value)

    def test_rate_limiting_is_named(self, key):
        with patch.object(media.requests, "post", return_value=FakeResponse({}, status=429)):
            with pytest.raises(media.MediaError) as caught:
                media.generate("x", backend="openai")
        assert "rate limited" in str(caught.value)

    def test_an_image_becomes_an_artifact_so_it_shows_in_chat(self, isolated_db):
        with patch.object(media.requests, "post", return_value=FakeResponse({"images": [B64]})):
            result = media.generate("a rabbit", backend="automatic1111",
                                    conversation_id="conv-1")
        assert result["artifact"]["kind"] == "image"

    def test_an_oversized_file_is_refused(self, isolated_db):
        with pytest.raises(media.MediaError):
            media.save(b"0" * (media.MAX_BYTES + 1))

    def test_empty_data_is_refused(self, isolated_db):
        with pytest.raises(media.MediaError):
            media.save(b"")


class TestMediaEndpoints:
    def test_backends_are_listed(self, client):
        body = client.get("/api/media").json()
        assert any(b["id"] == "automatic1111" for b in body["backends"])

    def test_a_key_can_be_saved(self, client):
        assert client.put("/api/media/backends/openai/key",
                          json={"api_key": "sk-x"}).json()["key_set"] is True

    def test_saving_a_key_for_an_unknown_backend_is_a_404(self, client):
        assert client.put("/api/media/backends/nope/key",
                          json={"api_key": "x"}).status_code == 404

    def test_a_key_field_typo_is_rejected_rather_than_wiping_the_key(self, client):
        # `api_key` has no default, so a body naming the wrong field is a 422
        # instead of quietly storing an empty string.
        assert client.put("/api/media/backends/openai/key",
                          json={"key": "sk-x"}).status_code == 422

    def test_the_default_backend_can_be_set(self, client):
        body = client.put("/api/media/default",
                          json={"backend": "automatic1111", "kind": "image"})
        assert body.json()["backend"] == "automatic1111"

    def test_a_default_that_cannot_do_the_kind_is_a_400(self, client):
        assert client.put("/api/media/default",
                          json={"backend": "automatic1111", "kind": "video"}).status_code == 400

    def test_a_generation_failure_is_a_400_with_the_reason(self, client):
        body = client.post("/api/media/generate", json={"prompt": "x", "backend": "openai"})
        assert body.status_code == 400 and "Settings" in body.json()["detail"]


class TestImageToolIsRegistered:
    def test_the_tool_exists(self):
        from carrot import agent_tools

        assert "generate_image" in agent_tools.TOOLS

    def test_it_emits_the_marker_the_chat_renders(self, isolated_db):
        from carrot import agent_tools

        with patch.object(media.requests, "post", return_value=FakeResponse({"images": [B64]})):
            out = agent_tools._tool_generate_image("a rabbit", conversation_id="c1")
        assert "[[carrot:artifact:" in out

    def test_a_failure_comes_back_as_text_not_an_exception(self, isolated_db):
        from carrot import agent_tools

        assert agent_tools._tool_generate_image("x", backend="openai").startswith("error:")


class TestSettingsWiring:
    """The panels have to be drawn and loaded, or the feature does not exist."""

    def read(self, *parts):
        from pathlib import Path

        # Explicit encoding. Without it this reads a UTF-8 file as cp1252 on
        # Windows, so the test's subject — whether a token is defined —
        # stopped mattering the day somebody wrote an arrow in a comment.
        return Path(__file__).resolve().parents[1].joinpath(
            "carrot", "web", *parts).read_text(encoding="utf-8")

    def test_both_panels_are_in_the_markup(self):
        html = self.read("index.html")
        assert 'id="media-panel"' in html and 'id="auth-panel"' in html

    def test_the_script_is_loaded(self):
        assert "/js/studio.js" in self.read("index.html")

    def test_both_panels_load_with_the_settings_tab(self):
        js = self.read("js", "dashboard.js")
        assert "loadAuthPanel()" in js and "loadMediaPanel()" in js

    def test_the_icons_the_panels_use_are_defined(self):
        html = self.read("index.html")
        assert 'symbol id="i-key"' in html and 'symbol id="i-image"' in html

    def test_sign_in_opens_the_real_browser(self):
        # An embedded window asking for a provider password is indistinguishable
        # from a phishing page.
        js = self.read("js", "studio.js")
        assert "openExternal" in js and "window.open" in js

    def test_every_css_token_the_new_panels_use_is_defined(self):
        import re

        css = self.read("css", "style.css")
        block = css.split("/* ===== Sign-in modes and generated media ===== */")[1]
        used = set(re.findall(r"var\((--[a-z0-9-]+)", block))
        defined = set(re.findall(r"^\s*(--[a-z0-9-]+):", css, re.M))
        assert used <= defined, f"undefined CSS tokens: {sorted(used - defined)}"


class TestElectronBridge:
    def read(self, name):
        from pathlib import Path
        # Explicit encoding: without it this reads a UTF-8 asset as cp1252 on
        # Windows, and the first arrow or en dash in preload.js raises a
        # UnicodeDecodeError before the bridge is ever checked.
        return (Path(__file__).resolve().parents[1] / "gui" / name).read_text(
            encoding="utf-8")

    def test_open_external_is_exposed(self):
        assert "openExternal" in self.read("preload.js")

    def test_only_http_urls_are_opened(self):
        # Otherwise the renderer could ask the OS to launch anything.
        main = self.read("main.js")
        handler = main.split("ipcMain.handle('open-external'")[1][:400]
        assert "^https?:" in handler
