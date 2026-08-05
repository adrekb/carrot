"""Dual authentication: a developer API key, or the subscription you already pay for.

The security-relevant behaviours are the ones tested hardest: a login that
cannot be completed by a page that did not start it, a token that refreshes
before it expires rather than after, a dead session that clears itself instead
of failing every call, and secrets that never leave the process through the
config endpoint.
"""
import time
from unittest.mock import patch

import pytest

from carrot import config, dualauth, providers


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status
        self.text = str(payload)

    def json(self):
        return self._payload


@pytest.fixture
def oauth(isolated_db):
    dualauth.set_oauth_config(
        "anthropic",
        client_id="carrot-test",
        authorize_url="https://auth.example/authorize",
        token_url="https://auth.example/token",
    )
    dualauth._PENDING.clear()
    return True


class TestMode:
    def test_the_default_is_the_api_key_path(self, isolated_db):
        assert dualauth.mode("anthropic") == dualauth.MODE_API_KEY

    def test_an_unrecognized_stored_mode_degrades_to_the_key_path(self, isolated_db):
        # Degrading to the thing that has always worked beats degrading to the
        # one that needs a browser round trip.
        config.set_config("auth_modes", {"anthropic": "psychic"})
        assert dualauth.mode("anthropic") == dualauth.MODE_API_KEY

    def test_a_mode_can_be_set(self, isolated_db):
        dualauth.set_mode("anthropic", dualauth.MODE_SUBSCRIPTION)
        assert dualauth.mode("anthropic") == dualauth.MODE_SUBSCRIPTION

    def test_an_unknown_mode_is_refused(self, isolated_db):
        with pytest.raises(dualauth.AuthError):
            dualauth.set_mode("anthropic", "vibes")

    def test_a_provider_with_no_consumer_plan_cannot_use_subscription_mode(self, isolated_db):
        with pytest.raises(dualauth.AuthError) as caught:
            dualauth.set_mode("groq", dualauth.MODE_SUBSCRIPTION)
        assert "no consumer subscription" in str(caught.value)


class TestOAuthConfiguration:
    def test_missing_client_details_explain_what_to_do(self, isolated_db):
        with pytest.raises(dualauth.AuthError) as caught:
            dualauth.begin_login("anthropic")
        message = str(caught.value)
        assert "client_id" in message and "API key instead" in message

    def test_a_plaintext_endpoint_is_refused(self, isolated_db):
        # An http endpoint on the open internet puts the token on the wire in
        # the clear.
        with pytest.raises(dualauth.AuthError):
            dualauth.set_oauth_config("anthropic", token_url="http://auth.example/token")

    def test_a_loopback_redirect_is_allowed(self, isolated_db):
        dualauth.set_oauth_config("anthropic", redirect_uri="http://127.0.0.1:8181/api/auth/callback")
        assert dualauth.oauth_config("anthropic")["redirect_uri"].startswith("http://127.0.0.1")

    def test_an_unsupported_provider_has_no_oauth_config(self, isolated_db):
        with pytest.raises(dualauth.AuthError):
            dualauth.oauth_config("groq")


class TestLoginFlow:
    def test_the_authorize_url_carries_pkce(self, oauth):
        started = dualauth.begin_login("anthropic")
        assert "code_challenge=" in started["url"]
        assert "code_challenge_method=S256" in started["url"]

    def test_the_verifier_never_reaches_the_url(self, oauth):
        # PKCE is pointless if the verifier travels with the challenge.
        started = dualauth.begin_login("anthropic")
        verifier = dualauth._PENDING[started["state"]]["verifier"]
        assert verifier not in started["url"]

    def test_the_verifier_is_never_written_to_config(self, oauth):
        started = dualauth.begin_login("anthropic")
        verifier = dualauth._PENDING[started["state"]]["verifier"]
        assert verifier not in str(config.get_config())

    def test_a_login_completes_and_stores_tokens(self, oauth):
        started = dualauth.begin_login("anthropic")
        with patch.object(dualauth.requests, "post", return_value=FakeResponse(
                {"access_token": "at-1", "refresh_token": "rt-1", "expires_in": 3600})):
            dualauth.complete_login(started["state"], "code-1")
        assert dualauth.signed_in("anthropic") is True
        assert dualauth.access_token("anthropic") == "at-1"

    def test_an_unknown_state_cannot_complete_a_login(self, oauth):
        # Otherwise any page that can reach the loopback port could finish a
        # sign-in on the user's behalf — which is what state is for.
        with pytest.raises(dualauth.AuthError) as caught:
            dualauth.complete_login("state-we-never-issued", "code-1")
        assert "did not start here" in str(caught.value)

    def test_a_state_cannot_be_replayed(self, oauth):
        started = dualauth.begin_login("anthropic")
        with patch.object(dualauth.requests, "post",
                          return_value=FakeResponse({"access_token": "at-1"})):
            dualauth.complete_login(started["state"], "code-1")
        with pytest.raises(dualauth.AuthError):
            dualauth.complete_login(started["state"], "code-1")

    def test_an_expired_pending_login_is_swept(self, oauth):
        started = dualauth.begin_login("anthropic")
        dualauth._PENDING[started["state"]]["started"] = time.time() - 10_000
        with pytest.raises(dualauth.AuthError):
            dualauth.complete_login(started["state"], "code-1")

    def test_a_missing_code_is_refused(self, oauth):
        started = dualauth.begin_login("anthropic")
        with pytest.raises(dualauth.AuthError):
            dualauth.complete_login(started["state"], "")

    def test_a_rejected_exchange_surfaces_the_providers_reason(self, oauth):
        started = dualauth.begin_login("anthropic")
        with patch.object(dualauth.requests, "post", return_value=FakeResponse(
                {"error_description": "the code has already been used"}, status=400)):
            with pytest.raises(dualauth.AuthError) as caught:
                dualauth.complete_login(started["state"], "code-1")
        assert "already been used" in str(caught.value)

    def test_a_response_with_no_token_is_an_error(self, oauth):
        started = dualauth.begin_login("anthropic")
        with patch.object(dualauth.requests, "post", return_value=FakeResponse({"ok": True})):
            with pytest.raises(dualauth.AuthError):
                dualauth.complete_login(started["state"], "code-1")


class TestTokenLifecycle:
    def sign_in(self, expires_in=3600, refresh="rt-1"):
        config.set_config("oauth_tokens", {"anthropic": {
            "access_token": "at-1", "refresh_token": refresh,
            "expires_at": time.time() + expires_in, "obtained_at": time.time(),
        }})

    def test_a_live_token_is_returned_as_is(self, oauth):
        self.sign_in()
        assert dualauth.access_token("anthropic") == "at-1"

    def test_a_token_about_to_expire_is_refreshed_first(self, oauth):
        # Refreshing *after* expiry means a long streaming request dies
        # halfway through; the margin is the whole point.
        self.sign_in(expires_in=30)
        with patch.object(dualauth.requests, "post", return_value=FakeResponse(
                {"access_token": "at-2", "expires_in": 3600})) as post:
            assert dualauth.access_token("anthropic") == "at-2"
        assert post.call_args.kwargs["data"]["grant_type"] == "refresh_token"

    def test_a_refresh_that_omits_a_new_refresh_token_keeps_the_old_one(self, oauth):
        self.sign_in(expires_in=30)
        with patch.object(dualauth.requests, "post",
                          return_value=FakeResponse({"access_token": "at-2", "expires_in": 3600})):
            dualauth.access_token("anthropic")
        assert config.get_config()["oauth_tokens"]["anthropic"]["refresh_token"] == "rt-1"

    def test_a_refused_refresh_clears_the_session(self, oauth):
        # Leaving a dead token in place makes every call fail identically with
        # no way for the UI to know it should offer "sign in" again.
        self.sign_in(expires_in=30)
        with patch.object(dualauth.requests, "post",
                          return_value=FakeResponse({"error": "invalid_grant"}, status=400)):
            with pytest.raises(dualauth.AuthError):
                dualauth.access_token("anthropic")
        assert dualauth.signed_in("anthropic") is False

    def test_an_expired_token_with_no_refresh_asks_for_a_new_sign_in(self, oauth):
        self.sign_in(expires_in=-100, refresh="")
        with pytest.raises(dualauth.AuthError) as caught:
            dualauth.access_token("anthropic")
        assert "sign in again" in str(caught.value)

    def test_a_token_with_no_stated_expiry_is_treated_as_live(self, oauth):
        config.set_config("oauth_tokens", {"anthropic": {
            "access_token": "at-1", "refresh_token": "", "expires_at": 0,
        }})
        assert dualauth.signed_in("anthropic") is True

    def test_signing_out_removes_the_session(self, oauth):
        self.sign_in()
        assert dualauth.sign_out("anthropic") is True
        assert dualauth.signed_in("anthropic") is False
        assert dualauth.sign_out("anthropic") is False

    def test_asking_for_a_token_when_signed_out_says_so(self, oauth):
        with pytest.raises(dualauth.AuthError) as caught:
            dualauth.access_token("anthropic")
        assert "not signed in" in str(caught.value)


class TestCredentialSelection:
    def test_api_key_mode_returns_the_key(self, isolated_db):
        providers.set_api_key("anthropic", "sk-ant-1")
        assert dualauth.credential("anthropic") == ("api_key", "sk-ant-1")

    def test_subscription_mode_returns_a_bearer_token(self, oauth):
        dualauth.set_mode("anthropic", dualauth.MODE_SUBSCRIPTION)
        config.set_config("oauth_tokens", {"anthropic": {
            "access_token": "at-1", "expires_at": time.time() + 3600,
        }})
        # The scheme matters: an OAuth token sent as x-api-key fails in a way
        # nobody can debug from the error message.
        assert dualauth.credential("anthropic") == ("bearer", "at-1")

    def test_has_credential_follows_the_mode(self, oauth):
        providers.set_api_key("anthropic", "sk-ant-1")
        dualauth.set_mode("anthropic", dualauth.MODE_SUBSCRIPTION)
        # A key is present, but this provider is in subscription mode and has
        # no session — so it is not usable, and saying otherwise would produce
        # a confusing failure at call time.
        assert dualauth.has_credential("anthropic") is False

    def test_a_subscribed_provider_counts_as_configured(self, oauth):
        dualauth.set_mode("anthropic", dualauth.MODE_SUBSCRIPTION)
        config.set_config("oauth_tokens", {"anthropic": {
            "access_token": "at-1", "expires_at": time.time() + 3600,
        }})
        provider = providers.get_provider("anthropic")
        assert provider["configured"] is True and provider["key_set"] is False


class TestSecretsStayInside:
    def test_tokens_are_redacted_from_the_config_endpoint(self, client):
        config.set_config("oauth_tokens", {"anthropic": {"access_token": "at-secret"}})
        body = client.get("/api/config").text
        assert "at-secret" not in body

    def test_media_keys_are_redacted_too(self, client):
        config.set_config("media_keys", {"openai": "sk-media-secret"})
        assert "sk-media-secret" not in client.get("/api/config").text


class TestAuthEndpoints:
    def test_status_lists_every_provider(self, client):
        body = client.get("/api/auth/status").json()
        assert any(p["provider"] == "anthropic" for p in body["providers"])

    def test_a_provider_reports_whether_subscription_is_supported(self, client):
        assert client.get("/api/auth/status/anthropic").json()["subscription_supported"] is True
        assert client.get("/api/auth/status/groq").json()["subscription_supported"] is False

    def test_the_mode_can_be_switched(self, client):
        body = client.put("/api/auth/mode/anthropic", json={"mode": "subscription"})
        assert body.json()["mode"] == "subscription"

    def test_an_impossible_mode_switch_is_a_400(self, client):
        assert client.put("/api/auth/mode/groq",
                          json={"mode": "subscription"}).status_code == 400

    def test_login_without_oauth_details_is_a_400_that_explains(self, client):
        body = client.post("/api/auth/login/anthropic")
        assert body.status_code == 400 and "client_id" in body.json()["detail"]

    def test_the_callback_renders_a_page_a_human_can_read(self, client):
        body = client.get("/api/auth/callback?error=access_denied")
        assert body.status_code == 400 and "Sign-in failed" in body.text

    def test_the_callback_rejects_a_state_it_never_issued(self, client):
        body = client.get("/api/auth/callback?code=x&state=forged")
        assert body.status_code == 400

    def test_the_callback_is_reachable_without_a_session_token(self, unauthenticated_client):
        # The provider redirects the system browser here; it has no token and
        # cannot be given one.
        assert unauthenticated_client.get("/api/auth/callback?error=x").status_code == 400

    def test_the_rest_of_the_auth_api_still_needs_a_token(self, unauthenticated_client):
        assert unauthenticated_client.get("/api/auth/status").status_code == 401


class TestNoCookieScraping:
    """The line this feature does not cross, asserted rather than promised."""

    def test_subscription_mode_is_oauth_and_nothing_else(self):
        from pathlib import Path

        source = (Path(__file__).resolve().parents[1] / "carrot" / "dualauth.py").read_text()
        lowered = source.lower()
        # No browser cookie jars, no session-key replay, no headless driving.
        for forbidden in ("browser_cookie", "cookiejar", "sessionkey", "session_key",
                          "playwright", "selenium", "webdriver"):
            assert forbidden not in lowered.replace("`sessionkey`", ""), forbidden
