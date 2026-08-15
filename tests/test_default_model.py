"""There is no model that is right for every machine.

The default was `gemma4:e4b` for everyone who has ever run Carrot. It needs
6 GB, which makes it wrong twice: it thrashes on an 8 GB laptop where a 3B
would have been quick and pleasant, and it is a toy on a workstation with a
24 GB card that could run something four times better. The Hub has known how
to size a model to a machine since it was written. The default never asked it.
"""
from unittest.mock import patch

import pytest

from carrot import bootstrap, config, hub


def specs(budget, backend="cpu", ram=None):
    return {"os": "Linux", "cpu": "cpu", "cpu_cores": 8,
            "ram_gb": ram or budget * 2, "gpu": None, "vram_gb": 0,
            "backend": backend, "model_budget_gb": budget}


def machine(budget, backend="cpu"):
    return patch.object(hub, "detect_specs", lambda refresh=False: specs(budget, backend))


class TestItSizesTheModelToTheMachine:
    def test_a_small_laptop_gets_something_that_actually_runs(self):
        with machine(3.0):
            picked = hub.default_model()
        entry = next(m for m in hub.BUNDLED_CATALOG if m["id"] == picked["id"])
        assert entry["min_mem_gb"] <= 3.0

    def test_a_big_machine_is_not_handed_a_4b(self):
        """The other half of the bug, and the quieter one — nobody files a
        report saying their answers could have been better."""
        with machine(24.0, "cuda"):
            picked = hub.default_model()
        entry = next(m for m in hub.BUNDLED_CATALOG if m["id"] == picked["id"])
        assert entry["params_b"] > 4.0

    def test_it_says_why(self):
        with machine(12.0, "cuda"):
            picked = hub.default_model()
        assert "12.0 GB" in picked["why"]
        assert picked["fallback"] is False

    def test_the_default_is_never_a_download_nobody_agreed_to(self):
        """`recommend()["best"]` on 64 GB of unified memory is a 43 GB pull.
        As the thing a user who skipped the picker silently gets, that is a
        first launch that appears to hang for an hour."""
        with machine(64.0, "metal"):
            picked = hub.default_model()
        entry = next(m for m in hub.BUNDLED_CATALOG if m["id"] == picked["id"])
        assert entry["download_gb"] <= hub.FIRST_RUN_DOWNLOAD_CEILING_GB

    def test_the_machines_real_ceiling_is_still_offered(self):
        """The cap is on the silent default, not on what the user may choose —
        the splash still preselects the strongest thing that fits."""
        with machine(64.0, "metal"):
            best = hub.recommend(hub.BUNDLED_CATALOG, specs(64.0, "metal"))["best"]
            assert best["download_gb"] > hub.FIRST_RUN_DOWNLOAD_CEILING_GB

    def test_a_machine_too_small_for_anything_still_gets_an_answer(self):
        with machine(0.5):
            picked = hub.default_model()
        assert picked["id"]

    def test_hardware_detection_failing_is_not_a_failed_launch(self):
        """This sits under bootstrap, which runs before there is a config
        database to write an error into."""
        def boom(refresh=False):
            raise OSError("no nvidia-smi, no /proc, nothing")

        with patch.object(hub, "detect_specs", boom):
            picked = hub.default_model()
        assert picked["id"] == hub.FALLBACK_MODEL
        assert picked["fallback"] is True


class TestOneAnswerForTheWholeApp:
    def test_the_users_choice_always_wins(self, isolated_db):
        config.set_config("ollama_model", "mistral:7b")
        assert hub.configured_or_default_model() == "mistral:7b"

    def test_an_unchosen_model_is_empty_rather_than_a_guess(self):
        """`gemma4:e4b` sat in DEFAULTS, so every `.get("ollama_model", …)`
        fallback in the app was dead code — the guess arrived before it."""
        assert config.DEFAULTS["ollama_model"] == ""

    def test_nothing_configured_falls_through_to_the_machine(self, isolated_db):
        with machine(3.0):
            assert hub.configured_or_default_model() == hub.default_model()["id"]

    def test_bootstrap_targets_the_same_model(self, isolated_db):
        with machine(3.0):
            assert bootstrap.get_target_model() == hub.configured_or_default_model()

    @pytest.mark.parametrize("module,attr", [
        ("carrot.router", "local_model"),
        ("carrot.ollama_client", "OllamaClient"),
    ])
    def test_no_module_keeps_its_own_literal(self, module, attr):
        """They each had `"gemma4:e4b"` written into them, which agreed only
        because they were all wrong in the same way."""
        import importlib
        from pathlib import Path
        source = Path(importlib.import_module(module).__file__).read_text(encoding="utf-8")
        resolver = source[source.index(attr):]
        assert '"gemma4:e4b"' not in resolver[:2000]


class TestSayingWhatTheMachineCannotDo:
    def test_a_small_machine_is_warned_before_it_disappoints(self):
        with machine(3.0):
            report = hub.feasibility()
        assert report["warning"]
        assert not report["on_device_only"]

    def test_the_warning_names_the_work_not_the_hardware(self):
        """"Your GPU is small" is not actionable. "Writing code will not work
        well here" is the sentence someone can decide against."""
        with machine(3.0):
            report = hub.feasibility()
        assert "code" in report["warning"].lower()

    def test_it_says_what_to_do_about_it(self):
        with machine(3.0):
            report = hub.feasibility()
        assert "cloud" in report["warning"].lower()

    def test_a_capable_machine_is_not_nagged(self):
        with machine(48.0, "cuda"):
            report = hub.feasibility()
        assert report["warning"] == ""
        assert report["on_device_only"] is True

    def test_each_limit_carries_its_own_numbers(self):
        with machine(3.0):
            report = hub.feasibility()
        limited = [t for t in report["tasks"] if t["verdict"] != "on_device"]
        assert limited and all("GB" in t["detail"] for t in limited)

    def test_partly_fitting_is_slow_rather_than_impossible(self):
        """Ollama offloads layers to the CPU rather than refusing, and telling
        someone a thing is impossible when it is merely slow is a worse error
        than the reverse."""
        with machine(6.5):
            verdicts = {t["use_case"]: t["verdict"] for t in hub.feasibility()["tasks"]}
        assert "slow" in verdicts.values()


class TestTheSplashShowsIt:
    def read(self, *parts):
        from pathlib import Path
        return (Path(__file__).resolve().parents[1] / "carrot" / "web"
                ).joinpath(*parts).read_text(encoding="utf-8")

    def test_the_payload_carries_the_warning(self):
        """In the request the splash already makes. A warning that needs a
        second round-trip arrives after the screen it was for."""
        with machine(3.0), patch.object(hub, "fetch_hf_trending", lambda: []):
            overview = hub.hub_overview()
        assert overview["feasibility"]["warning"]
        assert overview["default_model"]["id"]

    def test_the_splash_has_somewhere_to_draw_it(self):
        assert 'id="splash-feasibility"' in self.read("index.html")

    def test_the_splash_draws_it(self):
        assert "splash-feasibility" in self.read("js", "app.js")

    def test_skipping_the_picker_does_not_skip_the_sizing(self):
        """The one path taken by users who did not want to think about it was
        the path that ignored their hardware entirely."""
        html = self.read("index.html")
        assert "stock default" not in html

    def test_the_splash_can_scroll_once_it_has_something_to_say(self):
        """Three limitations is 289 px of prose. The card grew past a 720 px
        laptop viewport and, with nothing scrollable, "Set up now" went below
        the fold with no way to reach it."""
        css = self.read("css", "style.css")
        splash = css[css.index("#splash {"):]
        block = splash[:splash.index("}")]
        assert "overflow-y: auto" in block
        # Plain centring pushes the top of an oversized flex item above the
        # scroll origin, which cuts off the heading instead of the footer.
        assert "safe center" in block
