"""An endpoint that blocks must not be `async def`.

FastAPI runs an `async def` handler on the event loop and a plain `def` handler
in a threadpool. So an `async def` that calls blocking work does not slow that
one request down — it stops uvicorn serving anything at all for the duration.

`/api/terminal/execute` was `async def` and called `terminal.execute_command`,
which is a plain `subprocess.run` with a timeout measured in tens of seconds.
A `pip install` in the Code tab therefore froze the whole app, and the visible
symptom is not a slow terminal: it is unrelated panes failing with "Failed to
fetch" while the command itself is running perfectly well. The cause and the
symptom do not resemble each other, which is what makes it worth a test.
"""
import inspect

import pytest

from carrot import app as app_mod, terminal


# Endpoints that call known-blocking work, and so must be threadpool handlers.
BLOCKING_ROUTES = ["/api/terminal/execute"]


def endpoint_for(path):
    for route in app_mod.app.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"no route registered for {path}")


class TestBlockingWorkStaysOffTheEventLoop:

    @pytest.mark.parametrize("path", BLOCKING_ROUTES)
    def test_the_handler_is_not_a_coroutine(self, path):
        handler = endpoint_for(path)
        assert not inspect.iscoroutinefunction(handler), (
            f"{path} is `async def` and calls blocking work, so it runs on the "
            "event loop and stops uvicorn answering anything else while it runs"
        )

    def test_the_thing_it_calls_really_is_blocking(self):
        """The premise. If `terminal.execute_command` ever becomes a coroutine
        this test should fail and the one above be reconsidered, rather than
        both quietly describing something that stopped being true."""
        assert not inspect.iscoroutinefunction(terminal.execute_command)
        source = inspect.getsource(terminal.execute_command)
        assert "subprocess.run" in source
