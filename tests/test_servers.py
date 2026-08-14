"""Processes the agent starts and does not wait for.

`run_command` runs to completion with a timeout, which is right for a test run
and actively misleading for a dev server: `npm run dev` never exits, so it
produced sixty seconds of silence, a timeout, and an agent that reported the
project would not start. That is the one failure mode where a tool says the
opposite of the truth.

These tests are mostly about the ways a background process manager becomes
worse than not having one: a server outliving the app and holding a port, a
log that grows until the machine notices, and a pipe nobody drains — which
does not crash anything, it just silently stops the server from serving.
"""
import subprocess
import sys
import textwrap
import time

import pytest

from carrot import servers


@pytest.fixture(autouse=True)
def _no_leftovers():
    yield
    servers.stop_all()
    servers._servers.clear()


@pytest.fixture
def script(tmp_path):
    """A command that runs the given source as a file.

    A file rather than `python -c`: this interpreter's path contains both
    spaces and parentheses, and every child died instantly on the quoting
    before any of these tests were testing what they claimed to.
    """
    counter = {"n": 0}

    def make(source: str) -> str:
        counter["n"] += 1
        path = tmp_path / f"server_{counter['n']}.py"
        path.write_text(textwrap.dedent(source).strip() + "\n", encoding="utf-8")
        return f'"{sys.executable}" "{path}"'

    return make


class TestFindingTheAddress:
    @pytest.mark.parametrize("line,expected", [
        ("  ➜  Local:   http://localhost:5173/", "http://localhost:5173/"),
        ("Serving at http://127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("Starting development server at http://0.0.0.0:8000/", "http://localhost:8000/"),
        ("Listening on port 3000", "http://localhost:3000"),
        ("running at 4173", "http://localhost:4173"),
    ])
    def test_the_address_a_line_announces(self, line, expected):
        assert servers.find_url(line) == expected

    def test_a_bare_number_in_a_traceback_is_not_a_port(self):
        """`line 3000` and `:3000` look identical to a loose pattern, and a
        card offering to open http://localhost:47 is worse than no card."""
        assert servers.find_url('  File "app.py", line 3000, in handler') == ""
        assert servers.find_url("TypeError: expected 8000 arguments") == ""

    def test_zero_zero_zero_zero_is_rewritten(self):
        """It means 'every interface' to the server and nothing to a browser."""
        assert "0.0.0.0" not in servers.find_url("http://0.0.0.0:8000/")


class TestRunningOne:
    def test_a_server_keeps_running_after_the_call_returns(self, script):
        started = servers.start(script("import time; print('up'); time.sleep(30)"), cwd=".")
        assert started["running"] is True
        assert servers.get(started["id"])["running"] is True

    def test_the_address_it_prints_is_picked_up(self, script):
        started = servers.start(
            script("import time; print('Local: http://localhost:5173/'); time.sleep(30)"),
            cwd=".")
        settled = servers.wait_for_url(started["id"], timeout=15)
        assert settled["url"] == "http://localhost:5173/"

    def test_waiting_gives_up_on_a_server_that_exits(self, script):
        """A command that fails immediately must not hold the turn for the
        whole timeout — the agent needs the error, not a wait."""
        started = servers.start(script("raise SystemExit(3)"), cwd=".")
        began = time.time()
        settled = servers.wait_for_url(started["id"], timeout=20)
        assert settled["running"] is False
        assert time.time() - began < 15

    def test_the_output_is_kept_and_readable(self, script):
        started = servers.start(script("print('boom: no such module')"), cwd=".")
        servers.wait_for_url(started["id"], timeout=15)
        assert "boom: no such module" in servers.logs(started["id"])["log"]

    def test_the_log_is_a_ring_not_a_list(self, script):
        """A watch-mode bundler logs on every keystroke. Left unbounded, a
        server running all day is a memory leak with a URL."""
        record = servers.start(script("pass"), cwd=".")
        buffer = servers._servers[record["id"]]["_log"]
        for n in range(servers.MAX_LOG_LINES * 2):
            buffer.append(f"line {n}")
        assert len(buffer) == servers.MAX_LOG_LINES
        assert buffer[-1] == f"line {servers.MAX_LOG_LINES * 2 - 1}"

    def test_a_chatty_server_does_not_block_on_its_own_pipe(self, script):
        """The failure that is invisible: an undrained pipe fills its OS
        buffer and the child blocks on its next print. The server does not
        crash — it just stops responding, which reads as a hang in the app."""
        started = servers.start(
            script("""
            import sys, time
            for n in range(4000):
                print('x' * 80)
            sys.stdout.flush()
            print('STILL ALIVE')
            time.sleep(30)
            """), cwd=".")
        deadline = time.time() + 25
        while time.time() < deadline:
            if "STILL ALIVE" in servers.logs(started["id"], lines=5)["log"]:
                break
            time.sleep(0.2)
        assert "STILL ALIVE" in servers.logs(started["id"], lines=5)["log"]
        assert servers.get(started["id"])["running"] is True


class TestStopping:
    def test_stopping_actually_stops_it(self, script):
        started = servers.start(script("import time; time.sleep(60)"), cwd=".")
        process = servers._servers[started["id"]]["_process"]
        servers.stop(started["id"])
        assert process.poll() is not None
        assert servers.get(started["id"])["running"] is False

    def test_nothing_survives_the_app(self, script):
        """A dev server still holding port 5173 after Carrot has closed is a
        process the user did not start and cannot see."""
        for _ in range(3):
            servers.start(script("import time; time.sleep(60)"), cwd=".")
        assert servers.stop_all() == 3
        assert all(not s["running"] for s in servers.list_servers())

    def test_a_server_that_exited_on_its_own_is_noticed(self, script):
        started = servers.start(script("pass"), cwd=".")
        deadline = time.time() + 15
        while time.time() < deadline and servers.get(started["id"])["running"]:
            time.sleep(0.1)
        assert servers.list_servers()[-1]["running"] is False

    def test_stopping_something_that_is_not_there_says_so(self):
        assert "no such server" in servers.stop("nope")["error"]


def test_a_runaway_cannot_start_an_unlimited_number(script):
    """The agent that starts a server every turn because it forgot the last one."""
    for _ in range(servers.MAX_SERVERS):
        servers.start(script("import time; time.sleep(60)"), cwd=".")
    refused = servers.start(script("import time; time.sleep(60)"), cwd=".")
    assert "already running" in refused.get("error", "")


# ===== The tools =====

class TestTheTools:
    def test_starting_one_is_gated_like_running_any_other_command(self):
        """It is run_command that does not return. Anything less than the same
        gate would make 'read-only' a mode you can execute code in."""
        from carrot import agent_tools, coder

        assert agent_tools.TOOLS["start_server"]["risk"] == "high"
        assert agent_tools.TOOLS["start_server"]["mutating"] is True
        assert "start_server" in coder.WRITE_TOOLS
        assert "start_server" not in coder.tools_for_mode(
            ["start_server", "read_file"], coder.MODE_PLAN)

    def test_the_approval_prompt_says_it_keeps_running(self):
        """The command alone looks like any other; the part being approved is
        that nothing will stop it."""
        from carrot import agent_tools

        summary = agent_tools._summarize_call("start_server", {"command": "npm run dev"})
        assert "npm run dev" in summary
        assert "running" in summary.lower()

    def test_the_url_reaches_the_ui_as_an_event(self, isolated_db, monkeypatch, script):
        """A link the user is meant to click should not arrive as a sentence
        buried in a tool result."""
        from carrot import agent_tools

        monkeypatch.setattr(agent_tools, "workspace_root", lambda: ".")
        events = []
        out = agent_tools._tool_start_server(
            script("import time; print('Local: http://localhost:5173/'); time.sleep(30)"),
            emit=events.append)
        assert "http://localhost:5173/" in out
        assert events and events[0]["server"]["url"] == "http://localhost:5173/"

    def test_a_server_that_dies_reports_its_output_not_a_timeout(self, isolated_db, monkeypatch, script):
        """The whole reason this exists: through run_command this was sixty
        seconds of nothing and a timeout that said nothing about the cause."""
        from carrot import agent_tools

        monkeypatch.setattr(agent_tools, "workspace_root", lambda: ".")
        out = agent_tools._tool_start_server(
            script("import sys; sys.stdout.write('ENOENT: no such file\\n'); raise SystemExit(1)"))
        assert "exited" in out
        assert "ENOENT" in out

    def test_logs_default_to_the_most_recent_server(self, isolated_db, monkeypatch, script):
        from carrot import agent_tools

        monkeypatch.setattr(agent_tools, "workspace_root", lambda: ".")
        agent_tools._tool_start_server(script("import time; print('hello there'); time.sleep(30)"))
        deadline = time.time() + 15
        while time.time() < deadline and "hello there" not in agent_tools._tool_server_logs():
            time.sleep(0.2)
        assert "hello there" in agent_tools._tool_server_logs()

    def test_stopping_with_no_id_stops_everything(self, isolated_db, monkeypatch, script):
        from carrot import agent_tools

        monkeypatch.setattr(agent_tools, "workspace_root", lambda: ".")
        agent_tools._tool_start_server(script("import time; time.sleep(60)"))
        assert "stopped 1" in agent_tools._tool_stop_server()


def test_the_panel_can_ask_what_is_running(client, isolated_db):
    """A server outlives the conversation that started it, so a reload must
    not lose the user's only handle on a process holding one of their ports."""
    body = client.get("/api/coder/servers").json()
    assert "servers" in body
    assert client.post("/api/coder/servers/nope/stop").status_code == 404
