"""PyInstaller entry point for the frozen Carrot backend.

The Electron shell launches this executable instead of a system Python —
end users never need Python installed. It initializes the database in the
per-user data directory (config._default_data_dir handles the frozen case)
and serves the API + web UI on 127.0.0.1.
"""
import multiprocessing


def main():
    # Frozen apps re-exec themselves for multiprocessing workers; without
    # this a spawned child would relaunch the whole server.
    multiprocessing.freeze_support()

    from carrot.database import init_db
    from carrot.config import get_config
    from carrot.app import app, note_bound_host, resolve_bind_host
    import uvicorn

    init_db()
    cfg = get_config()
    # The app is told what it bound to, because "am I reachable from the
    # network" is a question about this socket and not about what the config
    # would like next time — and the answer decides whether `/` hands out the
    # session token.
    uvicorn.run(
        app,
        host=note_bound_host(resolve_bind_host()),
        port=int(cfg.get("server_port", 8181)),
        log_level="warning",
    )


if __name__ == "__main__":
    main()
