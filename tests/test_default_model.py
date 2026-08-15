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


class TestThePickerIsWhereYouFindMore:
    """The Hub is not a place you go. It is a section of the thing you already
    open to pick a model."""

    def test_the_hardcoded_six_are_gone(self):
        """`SUGGESTED_MODELS` was the last hardware-blind recommendation in
        the app, and it was on the screen users actually look at."""
        from carrot import app
        assert not hasattr(app, "SUGGESTED_MODELS")

    def test_what_fits_comes_first(self):
        with machine(6.0):
            rows = hub.find_more()
        fits = [r for r in rows if r["runs_here"]]
        assert rows[:len(fits)] == fits

    def test_a_model_that_will_not_run_is_kept_and_says_why(self):
        """A short list with no explanation reads as Carrot having few
        models. "needs 24 GB, this machine has 6" reads as a machine having
        limits, which is the true one."""
        with machine(6.0):
            rows = hub.find_more()
        unfit = [r for r in rows if not r["runs_here"]]
        assert unfit
        assert all("GB" in r["why_not"] for r in unfit)

    def test_the_least_attainable_is_not_the_headline_of_wont_run(self):
        """Sorted by how close it is, so the first row someone reads there is
        the one an upgrade would actually buy them."""
        with machine(6.0):
            unfit = [r for r in hub.find_more() if not r["runs_here"]]
        assert unfit[0]["min_mem_gb"] == min(r["min_mem_gb"] for r in unfit)

    def test_what_you_already_have_is_not_offered_again(self):
        with machine(6.0):
            rows = hub.find_more(installed={"gemma4:e4b"})
        assert not any(r["name"] == "gemma4:e4b" for r in rows)

    def test_a_bigger_machine_is_offered_bigger_models(self):
        with machine(6.0):
            small = {r["name"] for r in hub.find_more() if r["runs_here"]}
        with machine(48.0, "cuda"):
            large = {r["name"] for r in hub.find_more() if r["runs_here"]}
        assert small < large

    def test_the_picker_draws_the_fit(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "carrot" / "web" / "js"
              / "app.js").read_text(encoding="utf-8")
        assert "m-fit-" in js and "why_not" in js


class TestAnInstallThatAlreadyHasTheOldDefault:
    def test_a_model_bootstrap_wrote_is_resized(self, isolated_db):
        """Fixing the default for new installs and leaving every existing one
        on the wrong model would fix nothing for anybody who has Carrot."""
        config.set_config("ollama_model", hub.RETIRED_DEFAULT)
        with machine(48.0, "cuda"):
            changed = hub.resize_stale_default()
        assert changed and changed != hub.RETIRED_DEFAULT
        assert config.get_config()["ollama_model"] == changed

    def test_it_runs_at_most_once(self, isolated_db):
        config.set_config("ollama_model", hub.RETIRED_DEFAULT)
        with machine(48.0, "cuda"):
            hub.resize_stale_default()
            assert hub.resize_stale_default() is None

    def test_a_model_the_user_picked_is_left_alone(self, isolated_db):
        config.set_config("ollama_model", "mistral:7b")
        with machine(48.0, "cuda"):
            assert hub.resize_stale_default() is None
        assert config.get_config()["ollama_model"] == "mistral:7b"

    def test_it_does_nothing_when_the_old_default_is_still_the_right_answer(self, isolated_db):
        config.set_config("ollama_model", hub.RETIRED_DEFAULT)
        with machine(6.0):
            assert hub.resize_stale_default() is None
        assert config.get_config()["ollama_model"] == hub.RETIRED_DEFAULT

    def test_it_never_moves_someone_onto_the_blind_fallback(self, isolated_db):
        """Detection failing is not a reason to rewrite a working config."""
        config.set_config("ollama_model", hub.RETIRED_DEFAULT)

        def boom(refresh=False):
            raise OSError("no detection")

        with patch.object(hub, "detect_specs", boom):
            assert hub.resize_stale_default() is None
        assert config.get_config()["ollama_model"] == hub.RETIRED_DEFAULT

    def test_it_runs_at_startup(self):
        from pathlib import Path
        app_src = (Path(__file__).resolve().parents[1] / "carrot" / "app.py"
                   ).read_text(encoding="utf-8")
        startup = app_src[app_src.index("def _startup():"):]
        assert "resize_stale_default" in startup[:1200]


class TestTheGeneralPickIsAGeneralist:
    def test_recommended_is_not_a_code_specialist(self):
        """On a 12 GB card it offered Qwen 2.5 Coder 14B under the word
        RECOMMENDED — a specialist handed to somebody who never said they
        write code. "Strongest that fits" is a different question from "best
        all-rounder", and it was answering the first one."""
        with machine(12.0, "cuda"):
            best = hub.recommend(hub.BUNDLED_CATALOG, specs(12.0, "cuda"))["best"]
        assert "chat" in best["use_cases"]

    def test_the_specialist_is_still_one_row_down(self):
        with machine(12.0, "cuda"):
            recs = hub.recommend(hub.BUNDLED_CATALOG, specs(12.0, "cuda"))
        assert "coder" in recs["by_use_case"]["coding"]["id"]

    def test_a_machine_with_only_specialists_still_gets_a_pick(self):
        """Better a specialist than an empty screen."""
        only = [m for m in hub.BUNDLED_CATALOG if "chat" not in m["use_cases"]]
        best = hub.recommend(only, specs(48.0, "cuda"))["best"]
        assert best is not None


class TestFindingModelsThatExistToday:
    """The bundled catalog is a snapshot taken when the build was cut, so on
    release day it already recommends last quarter's models."""

    def test_the_splash_offers_to_go_and_look(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1] / "carrot" / "web"
        assert 'id="splash-find"' in (root / "index.html").read_text(encoding="utf-8")
        assert "splashFindForMachine" in (root / "js" / "app.js").read_text(encoding="utf-8")

    def test_it_is_a_button_and_not_automatic(self):
        """The first screen should not depend on the network, and a
        local-first app should not reach out before it is asked."""
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "carrot" / "web" / "js"
              / "app.js").read_text(encoding="utf-8")
        render = js[js.index("function renderSplashPicks"):js.index("function renderSplashCards")]
        assert "hub/search" not in render

    def test_the_splash_names_no_model_until_it_has_looked(self):
        """Naming a model on the setup screen from a list compiled months ago
        is a confident claim about the present that a shipped binary cannot
        support. The screen offers to go and look instead."""
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "carrot" / "web" / "js"
              / "app.js").read_text(encoding="utf-8")
        render = js[js.index("function renderSplashPicks"):js.index("function renderSplashCards")]
        assert "renderSplashCards(" not in render

    def test_the_built_in_list_is_the_offline_fallback_and_says_so(self):
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "carrot" / "web" / "js"
              / "app.js").read_text(encoding="utf-8")
        find = js[js.index("async function splashFindForMachine"):]
        assert "splashBundledPicks()" in find
        assert "out of date" in find

    def test_the_picker_labels_a_built_in_row_as_one(self, isolated_db):
        with machine(6.0):
            rows = hub.find_more()
        assert rows and all(r["source"] == "bundled" for r in rows)

    def test_the_picker_can_ask_for_the_current_list(self, isolated_db):
        found = [{"id": "org/Fresh-7B-GGUF", "downloads": 5000}]
        with machine(12.0, "cuda"), patch.object(hub, "_hf_api_get", lambda p, k: found):
            rows = hub.find_more(live=True)
        assert rows and rows[0]["source"] == "huggingface"
        assert any("Fresh-7B" in r["name"] for r in rows)

    def test_asking_for_current_falls_back_rather_than_emptying_the_list(self, isolated_db):
        """Offline is not a reason to show nothing — it is a reason to say
        which list you are looking at."""
        with machine(6.0), patch.object(hub, "_hf_api_get", lambda p, k: None):
            rows = hub.find_more(live=True)
        assert rows and all(r["source"] == "bundled" for r in rows)

    def test_the_popup_does_not_wait_on_the_network_to_draw(self):
        """`live` is opt-in: the models you already have must not be gated on
        a Hugging Face round-trip."""
        from pathlib import Path
        js = (Path(__file__).resolve().parents[1] / "carrot" / "web" / "js"
              / "app.js").read_text(encoding="utf-8")
        loader = js[js.index("async function loadModels"):js.index("async function loadModels") + 500]
        assert "(opts || {}).live" in loader

    def test_live_results_are_still_sized_to_this_machine(self, isolated_db):
        """Current *and* runnable. A trending list that ignores the machine is
        the same mistake as a stale list that ignores it."""
        rows = [{"id": "org/Big-70B-GGUF", "downloads": 9000},
                {"id": "org/Small-3B-GGUF", "downloads": 8000}]
        with machine(6.0), patch.object(hub, "_hf_api_get", lambda p, k: rows):
            found = hub.live_search()
        ids = [m["id"] for m in found["results"]]
        assert any("Small-3B" in i for i in ids)
        assert not any("Big-70B" in i for i in ids)

    def test_being_offline_says_so_rather_than_showing_nothing(self, isolated_db):
        with machine(6.0), patch.object(hub, "_hf_api_get", lambda p, k: None):
            found = hub.live_search()
        assert found["source"] == "offline" and found["detail"]


class TestTheLiveListIsNotJustPopular:
    """Current is necessary and not sufficient. HF's GGUF index sorted by
    trend is mostly community roleplay merges, and the most downloaded thing
    that fits any machine is a speech model."""

    def test_a_speech_model_is_not_an_assistant(self):
        """Asked for the best match for this machine, the first answer was
        `nemotron-3.5-asr-streaming-0.6b` — pressing Set up now would have
        installed speech-to-text as somebody's assistant."""
        assert not hub._is_an_assistant("nvidia/nemotron-3.5-asr-streaming-0.6b")
        assert not hub._is_an_assistant("BAAI/bge-large-en-v1.5-GGUF")
        assert not hub._is_an_assistant("openai/whisper-large-v3-GGUF")

    def test_real_models_survive_the_filter(self):
        for keep in ("Qwen/Qwen3.5-9B-Instruct-GGUF", "meta-llama/Llama-4-8B-GGUF",
                     "Qwen/Qwen2.5-Coder-7B-GGUF", "google/gemma-3-12b-it"):
            assert hub._is_an_assistant(keep), keep

    def test_it_asks_hugging_face_for_instruct_rather_than_filtering_after(self):
        seen = {}

        def capture(params, key):
            seen.update(params)
            return []

        with machine(12.0, "cuda"), patch.object(hub, "_hf_api_get", capture):
            hub.live_search(workload="assistant")
        assert seen.get("search") == "instruct"

    def test_the_coding_path_still_asks_for_coders(self):
        seen = {}

        def capture(params, key):
            seen.update(params)
            return []

        with machine(12.0, "cuda"), patch.object(hub, "_hf_api_get", capture):
            hub.live_search(workload="write code")
        assert seen.get("search") == "coder"

    def test_a_google_instruction_tuned_tag_reads_as_chat(self):
        """`-it` is as common as the word, and without it `gemma-3-12b-it`
        reads as base weights — which answer a question by continuing it."""
        entry = hub._hf_repo_to_entry({"id": "google/gemma-3-12b-it", "downloads": 10})
        assert "chat" in entry["use_cases"]

    def test_fitting_is_not_the_same_as_being_worth_running(self):
        """A 2.6B and a 9B both score `great` on a 12 GB card, and popularity
        decided — so the best match for a machine with room to spare was a
        model a third of what it could hold."""
        small = {"fit": "great", "params_b": 2.6, "downloads": 250_000, "use_cases": []}
        large = {"fit": "great", "params_b": 9.0, "downloads": 250_000, "use_cases": []}
        profile = {"use_cases": [], "modalities": []}
        assert hub._rank_key(large, profile) > hub._rank_key(small, profile)

    def test_a_tight_model_is_not_rewarded_for_being_too_big(self):
        tight = {"fit": "tight", "params_b": 30.0, "downloads": 250_000, "use_cases": []}
        good = {"fit": "great", "params_b": 8.0, "downloads": 250_000, "use_cases": []}
        profile = {"use_cases": [], "modalities": []}
        assert hub._rank_key(good, profile) > hub._rank_key(tight, profile)


class TestTheQuantDescentHasAFloor:
    """An 8B at Q8_0 beats a 24B squeezed to Q2_K, and costs the same memory.
    Two-bit is not the same model made smaller; it is a worse model wearing
    the big one's name — and the number in the name is what is being chosen
    on."""

    def test_two_bit_is_not_on_the_ladder(self):
        assert not any(name.startswith("Q2") for name, _ in hub.QUANT_LADDER)

    def test_the_floor_is_three_bit(self):
        assert hub.QUANT_LADDER[-1][0] == "Q3_K_M"

    def test_a_24b_does_not_squeeze_onto_a_12gb_card(self):
        """It used to be planned at Q2_K and reported as a *good* fit — the
        best match this machine was offered."""
        plan = hub.quant_plan(24.0, 12.0)
        assert plan["quant"] == "Q3_K_M"
        assert plan["fit"] in ("tight", "too_big")

    def test_the_smaller_model_at_a_higher_bit_rate_wins_instead(self):
        big = hub.quant_plan(24.0, 12.0)
        small = hub.quant_plan(8.0, 12.0)
        assert small["fit"] in ("great", "good")
        assert {"great": 0, "good": 1, "tight": 2, "too_big": 3}[small["fit"]] \
            < {"great": 0, "good": 1, "tight": 2, "too_big": 3}[big["fit"]]
        assert small["quant"] == "Q8_0"

    def test_a_16gb_card_does_get_the_24b(self):
        """The floor is not a ban on big models — it is a ban on pretending a
        two-bit one is the same thing."""
        plan = hub.quant_plan(24.0, 16.0)
        assert plan["quant"] == "Q3_K_M" and plan["fit"] in ("great", "good")

    def test_room_to_spare_still_buys_quality_not_just_size(self):
        """A 24 GB card should run a 24B at more than the floor."""
        assert hub.quant_plan(24.0, 24.0)["quant"] in ("Q6_K", "Q5_K_M", "Q8_0")

    def test_a_model_that_needs_two_bit_to_fit_is_reported_as_too_big(self):
        assert hub.quant_plan(70.0, 12.0)["fit"] == "too_big"
