"""Local webhooks: the one door into Carrot that has no session behind it.

Home Assistant, a Stream Deck macro and a phone Shortcut can all make an HTTP
request but none of them can hold a session token. That makes this the most
security-sensitive surface in the app, so most of what is tested here is what
it refuses: a wrong token, a hook that does not exist, an action it was not
made for, a runaway automation, and an outbound target on the open internet.
"""
from unittest.mock import patch

import pytest

from carrot import config, webhooks


@pytest.fixture(autouse=True)
def clean_rate_limits():
    webhooks.reset_rate_limits()
    yield
    webhooks.reset_rate_limits()


@pytest.fixture
def hook(isolated_db):
    webhooks.set_enabled(True)
    return webhooks.create_hook("morning", webhooks.ACTION_NOTIFY, "Morning check")


class TestOffByDefault:
    def test_the_feature_starts_off(self, isolated_db):
        # A door into the app that opens itself is not a feature.
        assert webhooks.enabled() is False

    def test_a_hook_cannot_fire_while_the_feature_is_off(self, isolated_db):
        made = webhooks.create_hook("x", webhooks.ACTION_NOTIFY)
        webhooks.set_enabled(False)
        with pytest.raises(webhooks.WebhookError) as caught:
            webhooks.authenticate("x", made["token"])
        assert "turned off" in str(caught.value)

    def test_no_hooks_exist_until_one_is_made(self, isolated_db):
        assert webhooks.list_hooks() == []


class TestAuthentication:
    def test_the_right_token_authenticates(self, hook):
        assert webhooks.authenticate("morning", hook["token"])["id"] == "morning"

    def test_a_wrong_token_is_refused(self, hook):
        with pytest.raises(webhooks.WebhookError):
            webhooks.authenticate("morning", "not-the-token")

    def test_an_empty_token_is_refused(self, hook):
        with pytest.raises(webhooks.WebhookError):
            webhooks.authenticate("morning", "")

    def test_an_unknown_hook_is_refused(self, hook):
        with pytest.raises(webhooks.WebhookError):
            webhooks.authenticate("nonexistent", hook["token"])

    def test_a_wrong_hook_and_a_wrong_token_read_the_same(self, hook):
        # Telling a caller the hook exists is telling them what to keep
        # guessing at.
        with pytest.raises(webhooks.WebhookError) as wrong_hook:
            webhooks.authenticate("nope", hook["token"])
        with pytest.raises(webhooks.WebhookError) as wrong_token:
            webhooks.authenticate("morning", "nope")
        assert str(wrong_hook.value) == str(wrong_token.value)

    def test_the_comparison_is_constant_time(self):
        from pathlib import Path

        # `==` on a secret returns early on the first wrong character, which is
        # measurable over enough requests.
        source = (Path(__file__).resolve().parents[1] / "carrot" / "webhooks.py").read_text()
        assert "hmac.compare_digest" in source

    def test_rotating_a_token_invalidates_the_old_one(self, hook):
        old = hook["token"]
        webhooks.rotate_token("morning")
        with pytest.raises(webhooks.WebhookError):
            webhooks.authenticate("morning", old)

    def test_rotating_keeps_the_hook_doing_the_same_thing(self, hook):
        assert webhooks.rotate_token("morning")["action"] == webhooks.ACTION_NOTIFY


class TestTokensStayHidden:
    def test_the_listing_never_carries_tokens(self, hook):
        # A list view that renders secrets is a screenshot away from a leak.
        assert all("token" not in entry for entry in webhooks.list_hooks())

    def test_creation_is_the_one_time_the_token_is_shown(self, hook):
        assert hook["token"]

    def test_the_config_endpoint_redacts_them(self, client):
        webhooks.set_enabled(True)
        made = webhooks.create_hook("secret-hook", webhooks.ACTION_NOTIFY)
        assert made["token"] not in client.get("/api/config").text

    def test_the_listing_endpoint_does_not_leak_them(self, client):
        client.put("/api/webhooks/enabled", json={"enabled": True})
        made = client.post("/api/webhooks/hooks",
                           json={"id": "h", "action": "notify"}).json()
        assert made["token"] not in client.get("/api/webhooks").text


class TestHooksDoOneThing:
    def test_a_hook_is_bound_to_its_action_at_creation(self, hook):
        # The point of the token is that it cannot become a shell.
        assert webhooks.get_hook("morning")["action"] == webhooks.ACTION_NOTIFY

    def test_an_unknown_action_is_refused(self, isolated_db):
        with pytest.raises(webhooks.WebhookError) as caught:
            webhooks.create_hook("bad", "run_shell_command")
        assert "unknown action" in str(caught.value)

    def test_the_action_list_is_closed(self):
        # Anything that could run a command or write a file is absent by design.
        assert set(webhooks.ACTIONS) == {"notify", "ask", "brief", "note", "reminder"}

    def test_a_payload_cannot_change_which_action_runs(self, hook):
        # A leaked token does the one thing its hook was made for. Naming a
        # different action in the body must not redirect it.
        result = webhooks.fire(webhooks.get_hook("morning"),
                               {"action": "ask", "question": "x", "title": "still a notify"})
        assert result["action"] == webhooks.ACTION_NOTIFY
        assert "answer" not in result

    def test_an_invalid_id_is_refused(self, isolated_db):
        with pytest.raises(webhooks.WebhookError):
            webhooks.create_hook("Not An Id!", webhooks.ACTION_NOTIFY)

    def test_a_duplicate_id_is_refused(self, hook):
        with pytest.raises(webhooks.WebhookError):
            webhooks.create_hook("morning", webhooks.ACTION_NOTIFY)

    def test_there_is_a_ceiling_on_hooks(self, isolated_db):
        for index in range(webhooks.MAX_HOOKS):
            webhooks.create_hook(f"h{index}", webhooks.ACTION_NOTIFY)
        with pytest.raises(webhooks.WebhookError):
            webhooks.create_hook("one-too-many", webhooks.ACTION_NOTIFY)


class TestRateLimiting:
    def test_a_runaway_automation_is_refused(self, hook):
        # Firing every second is a misconfiguration, not a user, and left alone
        # it would drive a model in a loop.
        for _ in range(webhooks.RATE_LIMIT_PER_MINUTE):
            webhooks.check_rate("morning")
        with pytest.raises(webhooks.WebhookError) as caught:
            webhooks.check_rate("morning")
        assert "misconfigured" in str(caught.value)

    def test_normal_use_is_unaffected(self, hook):
        for _ in range(5):
            webhooks.check_rate("morning")

    def test_the_limit_is_per_hook(self, isolated_db):
        webhooks.set_enabled(True)
        webhooks.create_hook("a", webhooks.ACTION_NOTIFY)
        webhooks.create_hook("b", webhooks.ACTION_NOTIFY)
        for _ in range(webhooks.RATE_LIMIT_PER_MINUTE):
            webhooks.check_rate("a")
        webhooks.check_rate("b")  # b is unaffected


class TestActions:
    def test_notify_raises_a_notification(self, hook):
        result = webhooks.fire(webhooks.get_hook("morning"), {"title": "Leaving now"})
        assert result["title"] == "Leaving now"

    def test_notify_needs_something_to_say(self, hook):
        with pytest.raises(webhooks.WebhookError):
            webhooks.fire(webhooks.get_hook("morning"), {})

    def test_ask_returns_the_answer(self, isolated_db):
        webhooks.set_enabled(True)
        webhooks.create_hook("q", webhooks.ACTION_ASK)

        def fake(resolved, messages, tools=None):
            yield {"type": "text", "text": "It is going to rain."}

        from carrot import router as router_mod

        with patch.object(router_mod, "stream_events", fake), \
             patch.object(router_mod, "route", lambda **k: None):
            result = webhooks.fire(webhooks.get_hook("q"), {"question": "weather?"})
        assert result["answer"] == "It is going to rain."

    def test_brief_never_fails_on_a_missing_subsystem(self, isolated_db):
        # A calendar that is not configured should not break the whole brief.
        webhooks.set_enabled(True)
        webhooks.create_hook("b", webhooks.ACTION_BRIEF)
        result = webhooks.fire(webhooks.get_hook("b"), {})
        assert set(result["brief"]) >= {"events", "reminders", "notifications"}

    def test_a_note_can_be_filed(self, isolated_db):
        webhooks.set_enabled(True)
        webhooks.create_hook("n", webhooks.ACTION_NOTE)
        result = webhooks.fire(webhooks.get_hook("n"),
                               {"title": "Bin day", "content": "Tuesdays"})
        assert result["title"] == "Bin day"

    def test_a_reminder_can_be_created(self, isolated_db):
        webhooks.set_enabled(True)
        webhooks.create_hook("r", webhooks.ACTION_REMINDER)
        assert webhooks.fire(webhooks.get_hook("r"), {"title": "Move the car"})["title"]

    def test_defaults_are_merged_under_the_payload(self, isolated_db):
        webhooks.set_enabled(True)
        webhooks.create_hook("d", webhooks.ACTION_NOTIFY, defaults={"title": "Default"})
        assert webhooks.fire(webhooks.get_hook("d"), {})["title"] == "Default"
        assert webhooks.fire(webhooks.get_hook("d"), {"title": "Live"})["title"] == "Live"

    def test_firing_is_counted(self, hook):
        webhooks.fire(webhooks.get_hook("morning"), {"title": "x"})
        assert webhooks.get_hook("morning")["fires"] == 1

    def test_an_enormous_body_is_truncated(self, hook):
        result = webhooks.fire(webhooks.get_hook("morning"), {"title": "x" * 9000})
        assert len(result["title"]) <= 200


class TestOutboundTargets:
    def test_a_private_address_is_allowed(self, isolated_db):
        # This is the one place in Carrot where the local network is the
        # intended destination rather than an SSRF target.
        webhooks.check_outbound_url("http://192.168.1.40:8123/api/webhook/carrot")

    def test_loopback_is_allowed(self, isolated_db):
        webhooks.check_outbound_url("http://127.0.0.1:8123/hook")

    def test_a_dot_local_name_is_allowed(self, isolated_db):
        webhooks.check_outbound_url("http://homeassistant.local:8123/hook")

    def test_a_public_address_is_refused(self, isolated_db):
        with pytest.raises(webhooks.WebhookError) as caught:
            webhooks.check_outbound_url("http://93.184.216.34/collect")
        assert "public address" in str(caught.value)

    def test_a_public_hostname_is_refused(self, isolated_db):
        # Otherwise Carrot's notifications become an exfiltration channel that
        # looks like a feature.
        with pytest.raises(webhooks.WebhookError):
            webhooks.check_outbound_url("https://evil.example.com/collect")

    def test_a_non_http_scheme_is_refused(self, isolated_db):
        with pytest.raises(webhooks.WebhookError):
            webhooks.check_outbound_url("file:///etc/passwd")

    def test_a_target_can_be_added_and_removed(self, isolated_db):
        target = webhooks.add_target("http://127.0.0.1:8123/hook", ["notification"])
        assert webhooks.remove_target(target["id"]) is True
        assert webhooks.remove_target(target["id"]) is False

    def test_targets_only_get_the_events_they_asked_for(self, isolated_db):
        webhooks.set_enabled(True)
        webhooks.add_target("http://127.0.0.1:8123/hook", ["research_done"])
        with patch.object(webhooks.requests, "post") as post:
            webhooks.notify_targets("notification", {"title": "x"})
        assert not post.called

    def test_a_dead_smart_home_box_is_not_a_carrot_error(self, isolated_db):
        webhooks.set_enabled(True)
        webhooks.add_target("http://127.0.0.1:8123/hook", ["notification"])
        with patch.object(webhooks.requests, "post",
                          side_effect=webhooks.requests.ConnectionError("refused")):
            results = webhooks.notify_targets("notification", {"title": "x"})
        assert "error" in results[0]

    def test_nothing_is_sent_while_the_feature_is_off(self, isolated_db):
        webhooks.add_target("http://127.0.0.1:8123/hook", ["notification"])
        webhooks.set_enabled(False)
        with patch.object(webhooks.requests, "post") as post:
            webhooks.notify_targets("notification", {"title": "x"})
        assert not post.called


class TestFiringOverHttp:
    def enable(self, client):
        client.put("/api/webhooks/enabled", json={"enabled": True})
        return client.post("/api/webhooks/hooks",
                           json={"id": "morning", "action": "notify"}).json()

    def test_a_bearer_token_works(self, client):
        made = self.enable(client)
        body = client.post("/api/hooks/morning", json={"title": "Leaving"},
                           headers={"Authorization": f"Bearer {made['token']}"})
        assert body.status_code == 200

    def test_the_carrot_header_works(self, client):
        made = self.enable(client)
        body = client.post("/api/hooks/morning", json={"title": "Leaving"},
                           headers={"X-Carrot-Token": made["token"]})
        assert body.status_code == 200

    def test_a_token_in_the_body_works(self, client):
        # curl, Home Assistant and Shortcuts each find a different one of the
        # three easiest; refusing two would make the feature look broken.
        made = self.enable(client)
        body = client.post("/api/hooks/morning",
                           json={"title": "Leaving", "token": made["token"]})
        assert body.status_code == 200

    def test_a_get_works_for_tools_that_cannot_post(self, client):
        client.put("/api/webhooks/enabled", json={"enabled": True})
        made = client.post("/api/webhooks/hooks",
                           json={"id": "brief", "action": "brief"}).json()
        body = client.get(f"/api/hooks/brief?token={made['token']}")
        assert body.status_code == 200 and "brief" in body.json()

    def test_no_token_is_a_401(self, client):
        self.enable(client)
        assert client.post("/api/hooks/morning", json={"title": "x"}).status_code == 401

    def test_a_wrong_token_is_a_401(self, client):
        self.enable(client)
        body = client.post("/api/hooks/morning", json={"title": "x"},
                           headers={"Authorization": "Bearer wrong"})
        assert body.status_code == 401

    def test_firing_needs_no_session_token(self, unauthenticated_client, client):
        # The whole point: Home Assistant has no session and cannot be given one.
        made = self.enable(client)
        body = unauthenticated_client.post(
            "/api/hooks/morning", json={"title": "Leaving"},
            headers={"Authorization": f"Bearer {made['token']}"})
        assert body.status_code == 200

    def test_managing_hooks_still_needs_a_session(self, unauthenticated_client):
        # Being able to fire a hook must not mean being able to make one.
        assert unauthenticated_client.get("/api/webhooks").status_code == 401
        assert unauthenticated_client.post(
            "/api/webhooks/hooks", json={"id": "x", "action": "notify"}).status_code == 401

    def test_a_runaway_caller_gets_a_429(self, client):
        made = self.enable(client)
        headers = {"Authorization": f"Bearer {made['token']}"}
        for _ in range(webhooks.RATE_LIMIT_PER_MINUTE):
            client.post("/api/hooks/morning", json={"title": "x"}, headers=headers)
        body = client.post("/api/hooks/morning", json={"title": "x"}, headers=headers)
        assert body.status_code == 429

    def test_the_state_endpoint_describes_the_actions(self, client):
        body = client.get("/api/webhooks").json()
        assert {a["id"] for a in body["actions"]} == set(webhooks.ACTIONS)

    def test_a_public_target_is_a_400(self, client):
        body = client.post("/api/webhooks/targets", json={"url": "https://evil.example.com/x"})
        assert body.status_code == 400

    def test_deleting_an_unknown_hook_is_a_404(self, client):
        assert client.delete("/api/webhooks/hooks/ghost").status_code == 404


class TestSettingsPanel:
    def read(self, *parts):
        from pathlib import Path
        return Path(__file__).resolve().parents[1].joinpath("carrot", "web", *parts).read_text()

    def test_the_panel_exists(self):
        assert 'id="hooks-panel"' in self.read("index.html")

    def test_there_is_an_explicit_switch(self):
        # The one door with no session behind it should look like a decision.
        assert 'id="hooks-enabled"' in self.read("index.html")

    def test_the_panel_loads_with_settings(self):
        assert "loadHooksPanel()" in self.read("js", "dashboard.js")

    def test_the_token_is_shown_once_with_a_working_example(self):
        # The next thing anyone does is paste it into Home Assistant; hunting
        # for the right curl incantation is where people give up.
        js = self.read("js", "studio.js")
        assert "shown once" in js and "curl -X POST" in js

    def test_deleting_and_rotating_ask_first(self):
        js = self.read("js", "studio.js")
        rotate = js.split("async function rotateHook")[1][:300]
        remove = js.split("async function deleteHook")[1][:300]
        assert "confirm(" in rotate and "confirm(" in remove

    def test_every_css_token_the_panel_uses_is_defined(self):
        import re

        css = self.read("css", "style.css")
        block = css.split("/* ===== Local webhooks ===== */")[1]
        used = set(re.findall(r"var\((--[a-z0-9-]+)", block))
        defined = set(re.findall(r"^\s*(--[a-z0-9-]+):", css, re.M))
        assert used <= defined, f"undefined CSS tokens: {sorted(used - defined)}"
