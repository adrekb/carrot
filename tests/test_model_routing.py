"""Picking a model must send the turn to the model you picked.

From a reported session: the user selected Mistral, the picker snapped back to
`gemma4:e4b` on the next refresh, the trace announced `mistral-medium
(on-device)`, and the turn produced nothing. Three separate faults —

1. `_infer_provider` mapped every non-Claude name to Ollama, so a hosted model
   was routed local and mislabelled "(on-device)".
2. The picker's label was read from `active_model` (the Ollama default) rather
   than from the resolved chat route, so a pinned cloud model looked unset.
3. A local route naming an unpulled model failed as an empty answer instead of
   saying which model was missing.
"""
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from carrot import app as A, router as router_mod


class TestInferProvider:
    """Only used when the caller did not name a provider — but it has to be
    right, because being wrong sends the turn somewhere it cannot be served."""

    def test_claude_still_goes_to_anthropic(self):
        assert router_mod._infer_provider("claude-opus-5") == "anthropic"

    def test_an_ollama_tag_is_local(self):
        assert router_mod._infer_provider("gemma4:e4b") == router_mod.PROVIDER_LOCAL

    def test_a_locally_pulled_mistral_is_not_the_hosted_one(self):
        # The tag form is what `ollama pull` produces. It must stay local even
        # when a Mistral provider is configured.
        with patch.object(router_mod.providers_mod, "usable", lambda p: True):
            assert router_mod._infer_provider("mistral:7b") == router_mod.PROVIDER_LOCAL

    def test_a_hosted_mistral_name_goes_to_mistral(self):
        with patch.object(router_mod.providers_mod, "usable", lambda p: p == "mistral"):
            assert router_mod._infer_provider("mistral-medium") == "mistral"

    def test_an_unconfigured_family_falls_back_to_local(self):
        # Guessing "mistral" for someone with no Mistral key would swap a wrong
        # label for a dead end; local is the honest fallback.
        with patch.object(router_mod.providers_mod, "usable", lambda p: False):
            assert router_mod._infer_provider("mistral-medium") == router_mod.PROVIDER_LOCAL

    @pytest.mark.parametrize("model", ["gpt-4o", "chatgpt-4o-latest", "o3-mini"])
    def test_openai_families(self, model):
        with patch.object(router_mod.providers_mod, "usable", lambda p: p == "openai"):
            assert router_mod._infer_provider(model) == "openai"

    def test_vendor_slash_model_is_openrouter(self):
        with patch.object(router_mod.providers_mod, "usable", lambda p: p == "openrouter"):
            assert router_mod._infer_provider("mistralai/mistral-large") == "openrouter"

    def test_an_empty_name_is_local(self):
        assert router_mod._infer_provider("") == router_mod.PROVIDER_LOCAL


class TestRouteLabelling:
    """`local` drives the "(on-device)" badge in the trace, so it must follow
    the provider's kind and nothing else."""

    def test_a_hosted_model_is_not_marked_local(self):
        with patch.object(router_mod.providers_mod, "usable", lambda p: p == "mistral"):
            resolved = router_mod.route(task="chat", model="mistral-medium")
        assert resolved.provider == "mistral"
        assert resolved.local is False
        assert resolved.as_dict()["local"] is False

    def test_an_explicit_provider_beats_any_guess(self):
        resolved = router_mod.route(task="chat", model="anything-at-all", provider="openai")
        assert resolved.provider == "openai"


class TestMissingLocalModel:
    """Naming a model Ollama never pulled used to surface as "(no response)"."""

    class Client:
        def __init__(self, names):
            self.names = names

        def list_models(self):
            return [{"name": n} for n in self.names]

    def test_an_uninstalled_model_is_rejected_with_its_name(self):
        with pytest.raises(HTTPException) as caught:
            A._require_installed_model(self.Client(["gemma4:e4b"]), "mistral-medium")
        assert caught.value.status_code == 400
        assert "mistral-medium" in caught.value.detail

    def test_an_installed_model_passes(self):
        A._require_installed_model(self.Client(["gemma4:e4b"]), "gemma4:e4b")

    def test_a_bare_name_matches_its_tag(self):
        # Ollama resolves a bare name to a tag; rejecting it would be wrong.
        A._require_installed_model(self.Client(["llama3.2:3b"]), "llama3.2")

    def test_a_failed_listing_never_blocks_a_turn(self):
        class Broken:
            def list_models(self):
                raise RuntimeError("ollama went away")

        A._require_installed_model(Broken(), "anything")

    def test_an_empty_listing_never_blocks_a_turn(self):
        A._require_installed_model(self.Client([]), "anything")


class TestPickerSendsItsProvider:
    """The frontend must name the provider rather than leave it to inference."""

    def read(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        return (root / "carrot" / "web" / "js" / "app.js").read_text(encoding="utf-8")

    def test_the_chat_body_carries_the_provider(self):
        # Auto is the one case that deliberately sends none — it has not picked
        # a model yet, and naming one would outrank the classifier.
        assert "provider: autoModel ? null : currentProvider," in self.read()

    def test_the_label_follows_the_resolved_chat_route(self):
        source = self.read()
        assert "data.chat_local === false" in source
        assert "currentModel = data.chat_model" in source

    def test_selecting_a_remote_model_records_its_provider(self):
        assert "currentProvider = provider;" in self.read()

    def test_selecting_a_local_model_records_ollama(self):
        assert "currentProvider = 'ollama';" in self.read()
