"""Full export and import of a Carrot instance.

Everything Carrot knows lives in one SQLite file plus a handful of directories.
This packages all of it into a single archive the user owns — for moving to a
new machine, keeping an offline copy, or rolling back a bad import.

The database is copied with SQLite's own backup API rather than by reading the
file, so an export taken while Carrot is running is a consistent snapshot rather
than a half-written page.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import zipfile
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import database

MANIFEST_NAME = "carrot-manifest.json"
ARCHIVE_FORMAT_VERSION = 1

DB_ENTRY = "data/carrot.db"

# Directory name in the archive -> path on disk, resolved at call time so tests
# that repoint the data directory are honoured.
def _data_root() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


def _directories() -> Dict[str, str]:
    root = _data_root()
    return {
        "data/notes": os.path.join(root, "notes"),
        "data/skills": os.path.join(root, "skills"),
        "data/briefings": os.path.join(root, "briefings"),
        "data/config": os.path.join(root, "config"),
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_counts() -> Dict[str, int]:
    """Row counts for the manifest, so an import can be sanity-checked."""
    counts = {}
    conn = database.get_db()
    for table in (
        "conversations", "messages", "memories", "goals", "reminders",
        "documents", "document_chunks", "vectors", "notifications", "widgets",
    ):
        try:
            counts[table] = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        except sqlite3.OperationalError:
            counts[table] = 0
    conn.close()
    return counts


def _snapshot_db(destination: str):
    """Consistent copy of the live database via the SQLite backup API."""
    source = sqlite3.connect(database.DB_PATH)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def export_archive(destination: Optional[str] = None, include_vectors: bool = True) -> Dict[str, Any]:
    """Write a full backup archive and return a description of it.

    ``include_vectors`` False drops the embedding table — it is the bulk of the
    archive and is fully recoverable by re-running the backfill.
    """
    if destination is None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = os.path.join(_data_root(), "backups", f"carrot-backup-{stamp}.zip")
    os.makedirs(os.path.dirname(os.path.abspath(destination)), exist_ok=True)

    staged_db = destination + ".db.tmp"
    _snapshot_db(staged_db)
    if not include_vectors:
        conn = sqlite3.connect(staged_db)
        try:
            conn.execute("DELETE FROM vectors")
            conn.commit()
            conn.execute("VACUUM")
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()

    manifest = {
        "format_version": ARCHIVE_FORMAT_VERSION,
        "created_at": _now(),
        "includes_vectors": include_vectors,
        "counts": _table_counts(),
        "directories": [],
    }

    try:
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.write(staged_db, DB_ENTRY)
            for entry_name, path in _directories().items():
                if not os.path.isdir(path):
                    continue
                manifest["directories"].append(entry_name)
                for dirpath, _, filenames in os.walk(path):
                    for filename in filenames:
                        full = os.path.join(dirpath, filename)
                        relative = os.path.relpath(full, path)
                        archive.write(full, os.path.join(entry_name, relative))
            archive.writestr(MANIFEST_NAME, json.dumps(manifest, indent=2))
    finally:
        try:
            os.remove(staged_db)
        except OSError:
            pass

    return {
        "path": os.path.abspath(destination),
        "size_bytes": os.path.getsize(destination),
        "manifest": manifest,
    }


def inspect_archive(path: str) -> Dict[str, Any]:
    """Read an archive's manifest without importing it."""
    if not os.path.isfile(path):
        return {"valid": False, "error": "archive not found"}
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if MANIFEST_NAME not in names:
                return {"valid": False, "error": "not a Carrot archive (no manifest)"}
            manifest = json.loads(archive.read(MANIFEST_NAME))
            if DB_ENTRY not in names:
                return {"valid": False, "error": "archive is missing the database"}
    except (zipfile.BadZipFile, json.JSONDecodeError, KeyError) as exc:
        return {"valid": False, "error": f"unreadable archive: {exc}"}

    if manifest.get("format_version", 0) > ARCHIVE_FORMAT_VERSION:
        return {
            "valid": False,
            "error": (
                f"archive format v{manifest['format_version']} is newer than this "
                f"build supports (v{ARCHIVE_FORMAT_VERSION})"
            ),
            "manifest": manifest,
        }
    return {"valid": True, "manifest": manifest}


def _is_safe_member(name: str) -> bool:
    """Reject archive entries that would write outside the data directory.

    Archives are user-supplied files; a crafted one could otherwise carry a
    ``../../`` path and overwrite anything the process can write.
    """
    if name == MANIFEST_NAME:
        return True
    if name.startswith("/") or ".." in name.replace("\\", "/").split("/"):
        return False
    return name == DB_ENTRY or any(name.startswith(prefix + "/") for prefix in _directories())


def import_archive(path: str, safety_copy: bool = True) -> Dict[str, Any]:
    """Replace this instance's data with an archive's contents.

    This is a **replace**, not a merge: the imported database becomes the
    database. Unless ``safety_copy`` is off, the current state is exported first,
    so a mistaken import is one restore away from being undone.
    """
    inspection = inspect_archive(path)
    if not inspection["valid"]:
        return {"success": False, "error": inspection["error"]}

    result: Dict[str, Any] = {"success": False, "manifest": inspection["manifest"]}

    if safety_copy:
        try:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            backup = export_archive(
                os.path.join(_data_root(), "backups", f"pre-import-{stamp}.zip")
            )
            result["safety_copy"] = backup["path"]
        except Exception as exc:
            return {"success": False, "error": f"could not take a safety copy: {exc}"}

    root = _data_root()
    os.makedirs(root, exist_ok=True)
    staged_db = os.path.join(root, "carrot.import.db")

    try:
        with zipfile.ZipFile(path) as archive:
            members = [n for n in archive.namelist() if not n.endswith("/")]
            rejected = [n for n in members if not _is_safe_member(n)]
            if rejected:
                return {
                    "success": False,
                    "error": f"archive contains unsafe paths: {rejected[:3]}",
                }

            with archive.open(DB_ENTRY) as source, open(staged_db, "wb") as target:
                shutil.copyfileobj(source, target)

            # Verify the staged file is a usable database before replacing the
            # live one — a truncated archive must not destroy working data.
            probe = sqlite3.connect(staged_db)
            try:
                probe.execute("SELECT COUNT(*) FROM conversations").fetchone()
            finally:
                probe.close()

            for entry_name, target_dir in _directories().items():
                extracted = [n for n in members if n.startswith(entry_name + "/")]
                if not extracted:
                    continue
                if os.path.isdir(target_dir):
                    shutil.rmtree(target_dir)
                os.makedirs(target_dir, exist_ok=True)
                for name in extracted:
                    relative = os.path.relpath(name, entry_name)
                    destination = os.path.join(target_dir, relative)
                    os.makedirs(os.path.dirname(destination), exist_ok=True)
                    with archive.open(name) as source, open(destination, "wb") as handle:
                        shutil.copyfileobj(source, handle)

        # Swap the database in last: everything else is already in place, so a
        # failure before this point leaves the original instance working.
        for suffix in ("-wal", "-shm"):
            stale = database.DB_PATH + suffix
            if os.path.exists(stale):
                os.remove(stale)
        shutil.move(staged_db, database.DB_PATH)
        database.init_db()
    except (sqlite3.DatabaseError, OSError, KeyError, zipfile.BadZipFile) as exc:
        if os.path.exists(staged_db):
            os.remove(staged_db)
        return {**result, "error": f"import failed: {exc}"}

    result["success"] = True
    result["counts"] = _table_counts()
    return result


def list_backups() -> List[Dict[str, Any]]:
    """Archives previously written to the default backup directory."""
    backup_dir = os.path.join(_data_root(), "backups")
    if not os.path.isdir(backup_dir):
        return []
    entries = []
    for filename in sorted(os.listdir(backup_dir), reverse=True):
        if not filename.endswith(".zip"):
            continue
        full = os.path.join(backup_dir, filename)
        entries.append(
            {
                "name": filename,
                "path": full,
                "size_bytes": os.path.getsize(full),
                "created_at": datetime.fromtimestamp(
                    os.path.getmtime(full), timezone.utc
                ).isoformat(),
            }
        )
    return entries
