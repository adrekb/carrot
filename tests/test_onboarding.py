"""First-run onboarding, and the key-saving path it exercised.

"Which kind of setup do you want" and "which model should I download" are
different questions. Asking them together is what made first run confusing —
a new user met a list of quantized model names before anyone had said what a
model is. Onboarding runs in front of the bootstrap splash and answers the
first question; the splash still answers the second.
"""
import re
from pathlib import Path

import pytest

from carrot import config, providers

ROOT = Path(__file__).resolve().parent.parent
INDEX = (ROOT / "carrot" / "web" / "index.html").read_text(encoding="utf-8")
APP_JS = (ROOT / "carrot" / "web" / "js" / "app.js").read_text(encoding="utf-8")


class TestTheFlow:
    def test_it_only_shows_once(self, isolated_db):
        assert config.DEFAULTS["onboarding_done"] is False
        config.set_config("onboarding_done", True)
        assert config.get_config()["onboarding_done"] is True

    def test_all_three_paths_are_offered(self):
        for step in ("welcome", "choose", "explain", "key"):
            assert f'data-step="{step}"' in INDEX, f"no {step} step"

    def test_it_can_be_skipped(self):
        assert "onboard-skip" in INDEX
        assert "finishOnboarding(true)" in INDEX

    def test_skipping_does_not_then_show_the_model_splash(self):
        """Skip means skip; falling through to a download prompt would make
        the skip button a lie."""
        assert "if (!skipped && typeof checkBootstrap" in APP_JS

    def test_choosing_local_still_reaches_the_model_picker(self):
        """The local path needs the splash — that is where a model is chosen."""
        assert "onboardStep('local')" in INDEX
        assert "if (step === 'local') { finishOnboarding(false); return; }" in APP_JS

    def test_onboarding_runs_before_the_splash(self):
        assert "maybeShowOnboarding();" in APP_JS
        assert re.search(r"loadWorkspaces\(\);\s*\n\s*//[^\n]*\n\s*maybeShowOnboarding\(\);", APP_JS)

    def test_a_backend_it_cannot_reach_does_not_block_the_app(self):
        """If /api/config fails, showing an unfinishable wizard is worse than
        showing nothing."""
        assert "done = true;" in APP_JS


class TestTheExplainer:
    """The user asked for it to explain what an API key entails."""

    @property
    def prose(self):
        match = re.search(r'data-step="explain".*?</section>', INDEX, re.S)
        return match.group(0).lower()

    def test_it_says_you_pay_per_use(self):
        assert "pay for what you use" in self.prose

    def test_it_says_the_data_leaves_the_machine(self):
        """The privacy trade is the thing a local-first user most needs told."""
        assert "leave this computer" in self.prose

    def test_it_distinguishes_a_key_from_a_chat_subscription(self):
        """Paying for ChatGPT Plus or Claude.ai does not give you a key, and
        assuming otherwise is the most common way this goes wrong."""
        assert "not a subscription" in self.prose

    def test_it_says_to_treat_it_like_a_password(self):
        assert "like a password" in self.prose

    def test_it_says_a_key_is_not_required(self):
        assert "you do not need one" in self.prose


class TestKeyValidation:
    def test_a_key_is_probed_before_onboarding_completes(self):
        """Saving a key that does not work is worse than saving none: the
        failure then surfaces mid-answer."""
        assert "/test" in APP_JS
        assert "if (!probe.ok)" in APP_JS

    def test_listing_models_is_not_used_as_the_check(self):
        """list_models falls back to a cached list and returns an `error`
        field rather than raising, so a garbage key looked like success."""
        section = APP_JS[APP_JS.index("async function saveOnboardingKey"):]
        section = section[:section.index("async function finishOnboarding")]
        assert "/models" not in section

    def test_the_client_sends_the_field_the_api_expects(self):
        """It sent {key}; the endpoint takes {api_key}. Because the field had
        a default, the mismatch validated fine and cleared the stored key."""
        assert "api_key: key" in APP_JS


class TestKeyEndpoint:
    def test_a_key_round_trips(self, client, isolated_db):
        r = client.put("/api/router/providers/anthropic/key", json={"api_key": "sk-test"})
        assert r.status_code == 200 and r.json()["key_set"] is True

    def test_a_misnamed_field_is_rejected_rather_than_wiping_the_key(self, client, isolated_db):
        """The bug: api_key defaulted to "", so any body without it was a
        silent success that forgot a working key and returned 200."""
        client.put("/api/router/providers/anthropic/key", json={"api_key": "sk-test"})
        assert client.put("/api/router/providers/anthropic/key",
                          json={"key": "typo"}).status_code == 422
        assert providers.api_key("anthropic") == "sk-test"

    def test_clearing_is_still_possible_but_must_be_explicit(self, client, isolated_db):
        client.put("/api/router/providers/anthropic/key", json={"api_key": "sk-test"})
        r = client.put("/api/router/providers/anthropic/key", json={"api_key": ""})
        assert r.status_code == 200 and r.json()["key_set"] is False

    def test_an_unknown_provider_is_404(self, client, isolated_db):
        assert client.put("/api/router/providers/nope/key",
                          json={"api_key": "x"}).status_code == 404


class TestCalendarWidgetSize:
    def test_the_month_grid_is_capped(self):
        """The cells are aspect-ratio 1 in a seven-column grid, so their height
        is a seventh of the container width — across a full dashboard that made
        each day ~160px and the month filled the screen."""
        css = (ROOT / "carrot" / "web" / "css" / "style.css").read_text(encoding="utf-8")
        block = css[css.index("Calendar widget sizing"):]
        assert "max-width" in block
        assert "max-height" in block
