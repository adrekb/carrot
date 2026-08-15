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
