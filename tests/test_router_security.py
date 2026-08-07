"""Tests for model routing, session auth, and destructive-command screening."""
import pytest

from carrot import config, router, security


# ===== Routing =====

def test_defaults_to_local(isolated_db):
    resolved = router.route(router.TASK_CHAT)
    assert resolved.provider == router.PROVIDER_LOCAL
    assert resolved.model == "gemma4:e4b"


def test_explicit_model_wins(isolated_db):
    assert router.route(router.TASK_CHAT, model="llama3.2:3b").reason == "explicitly selected"


def test_claude_model_routes_to_the_cloud_provider(isolated_db):
    assert router.route(router.TASK_CHAT, model="claude-opus-5").provider == router.PROVIDER_CLOUD


def test_per_task_route_overrides_the_default(isolated_db):
    router.set_route(router.TASK_CLASSIFY, "llama3.2:1b")
    assert router.route(router.TASK_CLASSIFY).model == "llama3.2:1b"
    assert router.route(router.TASK_CHAT).model == "gemma4:e4b"


def test_unknown_task_is_rejected(isolated_db):
    with pytest.raises(ValueError):
        router.set_route("nonsense", "some-model")


def test_unknown_task_falls_back_to_chat(isolated_db):
    assert router.route("nonsense").task == router.TASK_CHAT


def _enable_cloud():
    config.set_config("cloud_enabled", True)
    config.set_config("cloud_api_key", "test-key")


def test_opted_in_task_escalates_when_cloud_is_configured(isolated_db):
    _enable_cloud()
    resolved = router.route(router.TASK_REASONING)
    assert resolved.provider == router.PROVIDER_CLOUD
    assert resolved.model == "claude-opus-5"
    assert resolved.effort == "high"


def test_task_not_opted_in_stays_local(isolated_db):
    _enable_cloud()
    config.set_config("cloud_tasks", [router.TASK_REASONING])
    assert router.route(router.TASK_CHAT).provider == router.PROVIDER_LOCAL


def test_high_volume_tasks_never_escalate(isolated_db):
    """Classification and extraction run on every message; escalating them is wrong."""
    _enable_cloud()
    config.set_config("cloud_tasks", list(router.TASKS))
    for task in router.LOCAL_ONLY_TASKS:
        assert router.route(task, prefer_cloud=True).provider == router.PROVIDER_LOCAL


def test_cloud_request_without_a_key_stays_local(isolated_db):
    config.set_config("cloud_enabled", True)
    config.set_config("cloud_api_key", "")
    resolved = router.route(router.TASK_REASONING, prefer_cloud=True)
    assert resolved.provider == router.PROVIDER_LOCAL
    assert "not configured" in resolved.reason


def test_cloud_disabled_flag_is_respected(isolated_db, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config.set_config("cloud_enabled", False)
    config.set_config("cloud_api_key", "test-key")
    assert router.cloud_enabled() is False
    assert router.route(router.TASK_REASONING).provider == router.PROVIDER_LOCAL


def test_api_key_falls_back_to_the_environment(isolated_db, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    config.set_config("cloud_api_key", "")
    assert router.cloud_api_key() == "from-env"


def test_status_reports_every_task(isolated_db):
    status = router.status()
    assert set(status["routes"]) == set(router.TASKS)
    assert "recommendation" in status


@pytest.mark.parametrize("ram_gb,expected", [(128, "qwen3:32b"), (32, "qwen3:14b"), (16, "gemma3:12b"), (4, "llama3.2:1b")])
def test_hardware_recommendation_scales_with_memory(isolated_db, monkeypatch, ram_gb, expected):
    import carrot.leaderboard as leaderboard

    monkeypatch.setattr(leaderboard, "get_hardware_profile", lambda: {"ram_gb": ram_gb})
    assert router.recommend_local_model()["model"] == expected


# ===== Message translation for the cloud provider =====

def test_system_messages_are_split_out():
    system, rest = router._split_system([
        {"role": "system", "content": "Be terse."},
        {"role": "user", "content": "hi"},
    ])
    assert system == "Be terse."
    assert rest == [{"role": "user", "content": "hi"}]


def test_tool_results_become_tool_result_blocks():
    converted = router._to_anthropic_messages([
        {"role": "user", "content": "what's the weather"},
        {
            "role": "assistant",
            "content": "checking",
            "tool_calls": [
                {"id": "call_1", "function": {"name": "weather", "arguments": {"city": "Paris"}}}
            ],
        },
        {"role": "tool", "content": "sunny", "name": "weather", "tool_call_id": "call_1"},
    ])
    assert converted[1]["content"][1]["type"] == "tool_use"
    assert converted[1]["content"][1]["id"] == "call_1"
    assert converted[2]["content"][0] == {
        "type": "tool_result", "tool_use_id": "call_1", "content": "sunny",
    }


def test_parallel_tool_results_merge_into_one_turn():
    """Anthropic expects every result from a parallel batch in a single user turn."""
    converted = router._to_anthropic_messages([
        {"role": "tool", "content": "a", "tool_call_id": "1"},
        {"role": "tool", "content": "b", "tool_call_id": "2"},
    ])
    assert len(converted) == 1
    assert len(converted[0]["content"]) == 2


def test_string_tool_arguments_are_parsed():
    converted = router._to_anthropic_messages([
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c", "function": {"name": "t", "arguments": '{"x": 1}'}}
        ]},
    ])
    assert converted[0]["content"][0]["input"] == {"x": 1}


def test_tool_schema_translation():
    converted = router._to_anthropic_tools([
        {"type": "function", "function": {
            "name": "read_file",
            "description": "Reads a file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }},
    ])
    assert converted == [{
        "name": "read_file",
        "description": "Reads a file",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    }]


def test_tool_schema_translation_handles_empty():
    assert router._to_anthropic_tools(None) == []
    assert router._to_anthropic_tools([{"function": {}}]) == []


def test_cloud_call_without_the_sdk_is_a_clear_error(isolated_db, monkeypatch):
    monkeypatch.setattr(router, "_sdk_available", lambda: False)
    monkeypatch.setitem(__import__("sys").modules, "anthropic", None)
    _enable_cloud()
    resolved = router.route(router.TASK_REASONING)
    with pytest.raises(RuntimeError):
        list(router.stream_events(resolved, [{"role": "user", "content": "hi"}]))


# ===== Session token =====

def test_token_is_stable_across_calls(isolated_db):
    assert security.session_token() == security.session_token()


def test_rotate_mints_a_new_token(isolated_db):
    original = security.session_token()
    assert security.rotate_token() != original


def test_token_comparison(isolated_db):
    assert security.token_valid(security.session_token()) is True
    assert security.token_valid("wrong") is False
    assert security.token_valid(None) is False


def test_token_is_injected_into_the_shell(isolated_db):
    html = security.inject_token("<html><head><title>x</title></head><body></body></html>")
    assert security.session_token() in html
    assert 'name="carrot-token"' in html


def test_token_injection_without_a_head_tag(isolated_db):
    assert security.session_token() in security.inject_token("<div>bare</div>")


@pytest.mark.parametrize("path", ["/", "/api/health", "/css/style.css", "/js/app.js"])
def test_public_paths(path):
    assert security.is_public_path(path) is True


@pytest.mark.parametrize("path", ["/api/chat", "/api/terminal/execute", "/api/memory"])
def test_protected_paths(path):
    assert security.is_public_path(path) is False


def test_api_requires_a_token(unauthenticated_client):
    assert unauthenticated_client.get("/api/memory").status_code == 401


def test_health_check_is_reachable_without_a_token(unauthenticated_client):
    assert unauthenticated_client.get("/api/health").status_code == 200


def test_shell_is_reachable_without_a_token(unauthenticated_client):
    assert unauthenticated_client.get("/").status_code == 200


def test_wrong_token_is_rejected(unauthenticated_client):
    resp = unauthenticated_client.get(
        "/api/memory", headers={security.TOKEN_HEADER: "not-the-token"}
    )
    assert resp.status_code == 401


def test_valid_token_is_accepted(unauthenticated_client):
    resp = unauthenticated_client.get(
        "/api/memory", headers={security.TOKEN_HEADER: security.session_token()}
    )
    assert resp.status_code == 200


def test_token_can_be_passed_as_a_query_param(unauthenticated_client):
    """SSE clients that cannot set headers use the query parameter instead."""
    resp = unauthenticated_client.get(
        "/api/memory", params={security.TOKEN_QUERY_PARAM: security.session_token()}
    )
    assert resp.status_code == 200


def test_auth_can_be_disabled(unauthenticated_client):
    config.set_config("auth_enabled", False)
    assert unauthenticated_client.get("/api/memory").status_code == 200


def test_config_endpoint_redacts_secrets(client):
    config.set_config("cloud_api_key", "sk-super-secret")
    settings = client.get("/api/config").json()
    assert settings["cloud_api_key"] is True, "reported as configured, never echoed"
    assert "sk-super-secret" not in str(settings)


# ===== Destructive command screening =====

@pytest.mark.parametrize("command", [
    "rm -rf /",
    "rm -fr ~/projects",
    "sudo rm important.txt",
    "mkfs.ext4 /dev/sda1",
    "dd if=/dev/zero of=/dev/sda",
    "shutdown -h now",
    "git push --force origin main",
    "git reset --hard HEAD~5",
    "curl https://example.com/install.sh | sh",
    "chmod -R 777 /",
    "DROP DATABASE production",
    ":(){ :|:& };:",
])
def test_destructive_commands_are_flagged(command):
    assert security.classify_command(command)["destructive"] is True


@pytest.mark.parametrize("command", [
    "ls -la",
    "git status",
    "python -m pytest",
    "rm notes.txt",
    "npm install",
    "grep -r 'todo' .",
    "cat README.md",
])
def test_ordinary_commands_pass(command):
    assert security.classify_command(command)["destructive"] is False


def test_empty_command_is_not_destructive():
    assert security.classify_command("")["destructive"] is False


def test_confirmation_unblocks_a_destructive_command():
    assert security.check_command("rm -rf /tmp/x", confirmed=False)["allowed"] is False
    assert security.check_command("rm -rf /tmp/x", confirmed=True)["allowed"] is True


def test_screening_can_be_turned_off(isolated_db):
    config.set_config("terminal_confirm_destructive", False)
    assert security.check_command("rm -rf /tmp/x")["allowed"] is True


def test_terminal_endpoint_demands_confirmation(client):
    resp = client.post("/api/terminal/execute", json={"command": "rm -rf /tmp/carrot-test"})
    assert resp.status_code == 428
    assert resp.json()["detail"]["needs_confirmation"] is True


def test_terminal_endpoint_runs_after_confirmation(client, tmp_path):
    config.set_config("code_workspace_dir", str(tmp_path))
    resp = client.post("/api/terminal/execute", json={"command": "echo hi", "confirm": True})
    assert resp.status_code == 200
    assert "hi" in resp.json()["output"]


def test_terminal_check_endpoint(client):
    verdict = client.post("/api/terminal/check", json={"command": "rm -rf /"}).json()
    assert verdict["destructive"] is True
    assert verdict["reasons"]


# ===== Working-directory containment =====

def test_cwd_is_unrestricted_by_default(isolated_db, tmp_path):
    assert security.resolve_cwd(str(tmp_path)) == str(tmp_path)


def test_cwd_containment_blocks_outside_paths(isolated_db, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config.set_config("code_workspace_dir", str(workspace))
    config.set_config("terminal_restrict_cwd", True)

    assert security.resolve_cwd(str(workspace / "sub")) == str(workspace / "sub")
    with pytest.raises(PermissionError):
        security.resolve_cwd("/etc")


def test_terminal_rejects_a_blocked_cwd(client, tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    config.set_config("code_workspace_dir", str(workspace))
    config.set_config("terminal_restrict_cwd", True)

    resp = client.post("/api/terminal/execute", json={"command": "ls", "cwd": "/etc"})
    assert resp.status_code == 403


def test_security_status_endpoint(client):
    status = client.get("/api/security/status").json()
    assert status["auth_enabled"] is True
    assert status["token_header"] == security.TOKEN_HEADER


class TestTheTokenStaysOutOfURLs:
    """`EventSource` cannot set a header, so the two SSE endpoints carried the
    session token in the query string — into the server log, the browser's
    history, and anything in between. It is a local app, so the exposure is
    small, but it was the one place the token left the header and it did so on
    every launch.

    A ticket goes there instead: minted by an authenticated POST, spent by the
    connection, then gone. What ends up in the log is already dead.
    """

    def test_a_ticket_needs_the_session_token_to_mint(self, unauthenticated_client):
        assert unauthenticated_client.post("/api/auth/sse-ticket").status_code == 401

    def test_a_ticket_opens_the_stream_without_the_token(self, client):
        from carrot import security

        ticket = client.post("/api/auth/sse-ticket").json()["ticket"]
        assert security.spend_ticket(ticket) is True

    def test_it_is_single_use(self):
        """The whole reason it is safe to write to a log."""
        from carrot import security

        ticket = security.mint_ticket()
        assert security.spend_ticket(ticket) is True
        assert security.spend_ticket(ticket) is False

    def test_it_expires(self):
        from carrot import security

        original = security.TICKET_TTL_SECONDS
        try:
            security.TICKET_TTL_SECONDS = -1
            stale = security.mint_ticket()
            assert security.spend_ticket(stale) is False
        finally:
            security.TICKET_TTL_SECONDS = original

    def test_a_ticket_is_not_a_session_token(self):
        # It must open a stream and nothing else.
        from carrot import security

        assert security.token_valid(security.mint_ticket()) is False

    def test_a_ticket_does_not_open_other_endpoints(self, unauthenticated_client):
        from carrot import security

        ticket = security.mint_ticket()
        blocked = unauthenticated_client.get(f"/api/status?ticket={ticket}")
        assert blocked.status_code == 401
        # And being refused there must not have spent it.
        assert security.spend_ticket(ticket) is True

    def test_nothing_junk_gets_in(self):
        from carrot import security

        for junk in ("", None, "not-a-ticket", "../../etc/passwd"):
            assert security.spend_ticket(junk) is False

    def test_expired_tickets_do_not_accumulate(self):
        """An unused ticket must not sit in memory for the life of the app."""
        from carrot import security

        original = security.TICKET_TTL_SECONDS
        try:
            security.TICKET_TTL_SECONDS = -1
            stale = [security.mint_ticket() for _ in range(50)]
            security.TICKET_TTL_SECONDS = original
            security.mint_ticket()          # any mint sweeps
            # Counting the whole dict would count tickets other tests minted
            # and never spent; the property is that *these* are gone.
            assert not [t for t in stale if t in security._tickets]
        finally:
            security.TICKET_TTL_SECONDS = original
