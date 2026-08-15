"""The suite must not touch the database the developer actually uses.

This is a test about the harness rather than about Carrot, and it exists
because the harness was wrong in a way nothing else could catch. Isolation
used to be opt-in: a test that did not request the `isolated_db` fixture did
not run without a database, it ran against `carrot/data/carrot.db` — the real
one — and so did every other test that had not opted in. A few hundred tests
sharing one mutable file is a suite whose result depends on what ran before
it and on what the last run left behind, which is exactly what intermittent
failures on unrelated files look like.

The fixture is autouse now. These tests fail if that is ever undone.
"""
import os

from carrot import config, database


def _real_data_dir():
    """Where Carrot keeps data when nobody has redirected it."""
    return os.path.normcase(os.path.abspath(config._default_data_dir()))


def test_the_database_is_not_the_real_one():
    """No fixture requested, and it still must not be the developer's data."""
    assert not os.path.normcase(os.path.abspath(database.DB_PATH)).startswith(
        _real_data_dir()
    ), f"the suite is writing to the real database at {database.DB_PATH}"


def test_the_config_module_agrees_about_where_data_lives():
    """`config.CARROT_DIR` is read at call time by several writers. If the
    redirect misses it, config rows land in the real database while everything
    else goes to the temporary one — the worst version, because most of the
    suite would still pass."""
    assert os.path.normcase(os.path.abspath(config.CARROT_DIR)) != _real_data_dir()


def test_the_data_directory_is_not_inside_the_tests_tmp_path(tmp_path, isolated_db):
    """Because `tmp_path` is a git repository in a good many tests.

    They run `git init` there and then `git add -A`, which tracks whatever is
    sitting in the directory — and a live SQLite file committed into a
    checkpoint is one that a restore then tries to overwrite while the suite
    still has it open. That fails on Windows and nowhere else, and only when a
    connection happens to be open, which is a flake rather than a failure.
    """
    assert not os.path.exists(tmp_path / "_carrot")
    assert os.path.commonpath(
        [os.path.abspath(tmp_path), os.path.abspath(isolated_db)]
    ) != os.path.abspath(tmp_path)


def test_writing_config_stays_in_the_temporary_database(isolated_db):
    config.set_config("_suite_isolation_probe", "written")
    assert config.get_config()["_suite_isolation_probe"] == "written"
    assert os.path.exists(isolated_db)


def test_config_works_on_a_database_nobody_initialised(temp_data_dir):
    """`get_config` and `set_config` used to open the file directly, which
    meant they were the one pair of callers that could meet a database with no
    schema and raise `no such table: config` instead of creating it."""
    assert not os.path.exists(temp_data_dir)
    config.set_config("_suite_isolation_probe", "written")
    assert config.get_config()["_suite_isolation_probe"] == "written"


class TestTheOneStatementThatCouldNotWait:
    """`PRAGMA journal_mode=WAL` does not honour the busy timeout.

    Changing journal mode needs a lock no busy handler is consulted for, so
    with any other connection mid-transaction it returns SQLITE_BUSY
    immediately — not after thirty seconds, after zero. Every other statement
    in `get_db` waits; that one never did, and it was the last flake: a full
    run failing once every few runs, on a different unrelated file each time,
    always at setup, always `database is locked`.
    """

    def test_an_overlapping_transaction_does_not_kill_a_new_connection(self):
        import sqlite3

        from carrot import database

        database.init_db()                    # schema first, so get_db only reads
        holder = sqlite3.connect(database.DB_PATH, timeout=30)
        holder.execute("BEGIN IMMEDIATE")
        holder.execute("INSERT INTO config (key, value) VALUES ('probe', '1')")
        try:
            conn = database.get_db()          # must not raise
            conn.close()
        finally:
            holder.rollback()
            holder.close()

    def test_it_is_not_set_again_once_the_file_is_already_in_wal(self):
        """WAL is a property of the file, not of a connection. After the first
        connection in a database's life nothing should try to change it, which
        is what makes the race vanish rather than merely get caught."""
        import sqlite3

        from carrot import database

        database.init_db()
        conn = database.get_db()
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        conn.close()

        # A connection that reports WAL and explodes if anyone sets it.
        class Already:
            def execute(self, sql, *args):
                if "journal_mode=" in sql.replace(" ", ""):
                    raise AssertionError(f"tried to set journal mode again: {sql}")

                class Row:
                    def fetchone(self):
                        return ("wal",)
                return Row()

        database._ensure_wal(Already())

    def test_losing_the_race_is_not_an_error(self):
        """Journal mode is a performance property, not a correctness one.
        Failing a chat turn to report that a pragma had to wait is the wrong
        trade in every direction."""
        import sqlite3

        from carrot import database

        class Stubborn:
            def execute(self, sql, *args):
                if "journal_mode=" in sql.replace(" ", ""):
                    raise sqlite3.OperationalError("database is locked")

                class Row:
                    def fetchone(self):
                        return ("delete",)
                return Row()

        database._ensure_wal(Stubborn())      # must not raise


def test_notes_are_not_the_developers_own(tmp_path):
    """They are files, and they bind their directory at import time, so
    patching `config.CARROT_DIR` does not move them. A listing test passes
    either way — it just happens to be listing somebody's real writing."""
    from carrot import notes

    assert not os.path.normcase(os.path.abspath(notes.NOTES_DIR)).startswith(
        _real_data_dir()
    ), f"the suite is reading real notes at {notes.NOTES_DIR}"


def test_a_note_written_by_a_test_lands_in_the_temporary_directory(isolated_db):
    from carrot import notes

    made = notes.create_note("probe", "hello")
    assert [n["title"] for n in notes.list_notes()] == ["probe"]
    assert os.path.abspath(notes.get_note_path(made["id"])).startswith(
        os.path.abspath(notes.NOTES_DIR))
