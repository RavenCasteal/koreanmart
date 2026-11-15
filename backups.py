"""
Stage 18 - safe SQLite backup and restore (standard library only).

WHY NOT shutil.copy2():
  A raw file copy of a live SQLite database is only safe when nothing is
  writing. In rollback-journal mode a hot journal may still need recovery;
  in WAL mode committed pages can live ONLY in the -wal sidecar until the
  next checkpoint, so copying the main .db file alone can silently produce a
  backup that is missing the newest transactions (or is corrupt).

This module uses SQLite's ONLINE BACKUP API (sqlite3.Connection.backup()),
which reads a consistent snapshot through the source database's own pager:
committed WAL frames are included, uncommitted transactions are excluded, and
an open writer cannot tear the result. Restore goes the same way (backup FROM
a verified file INTO the live database), never by swapping files underneath
open connections.

Operational rule (documented, not enforceable from Python): close the app /
all other connections to this database before RESTORING. create_backup() is
safe while the app is running; restore_backup() needs exclusive access and
will fail with a friendly "database is locked" message instead of gambling.

Every restore first makes a "pre-restore" safety backup, so the operation is
reversible. Nothing here deletes an existing database file.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

import database
import permissions

BACKUP_DIR = database.BASE_DIR / "backups"
BACKUP_SUFFIX = ".db"
BUSY_TIMEOUT_MS = 5000


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_only(path: Path) -> sqlite3.Connection:
    """Source connection: reads committed data (incl. WAL), writes nothing."""
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.execute("PRAGMA query_only = ON")
    conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    return conn


def _unique_path(folder: Path, prefix: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    candidate = folder / f"{prefix}-{stamp}{BACKUP_SUFFIX}"
    counter = 1
    while candidate.exists():
        candidate = folder / f"{prefix}-{stamp}-{counter:02d}{BACKUP_SUFFIX}"
        counter += 1
    return candidate


def verify_backup(backup_path: Any) -> dict[str, Any]:
    """Validate a backup file before it is ever used as a restore source."""
    path = Path(backup_path).resolve()
    if not path.is_file():
        raise ValueError("That backup file does not exist.")
    # as_uri() already includes the "file:" scheme and percent-encodes spaces
    # and non-ASCII characters. Prefixing another "file:" produces an invalid
    # URI, and Path.as_uri() raises on a relative path, so resolve() first.
    conn = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    try:
        integrity = str(conn.execute("PRAGMA integrity_check").fetchone()[0])
        tables = int(conn.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchone()[0])
        user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        foreign_keys = list(conn.execute("PRAGMA foreign_key_check"))
    except sqlite3.Error as exc:
        raise ValueError(f"That file is not a readable SQLite database: {exc}") from None
    finally:
        conn.close()
    return {
        "ok": integrity == "ok" and tables > 0 and not foreign_keys,
        "path": str(path),
        "integrity": integrity,
        "tables": tables,
        "foreign_key_errors": len(foreign_keys),
        "user_version": user_version,
        "size_bytes": path.stat().st_size,
        "sha256": _sha256(path),
    }


def create_backup(
    *,
    db_path: Any = None,
    backup_dir: Any = None,
    actor: Any = None,
    prefix: str = "ramen_inventory",
) -> dict[str, Any]:
    """Create a consistent, verified backup. Safe while the app is running.

    Writes to a temporary file first, verifies it, then atomically renames it
    into place, so a failed or half-written backup can never be mistaken for
    a good one.
    """
    permissions.require(actor, permissions.CAP_MANAGE_BACKUPS)
    source_path = Path(db_path) if db_path is not None else database.DB_PATH
    if not source_path.is_file():
        raise ValueError("The database file to back up does not exist.")
    folder = Path(backup_dir) if backup_dir is not None else BACKUP_DIR
    folder.mkdir(parents=True, exist_ok=True)
    target = _unique_path(folder, prefix)
    temporary = folder / f".{target.name}.tmp"
    if temporary.exists():
        temporary.unlink()

    source = _read_only(source_path)
    destination = sqlite3.connect(str(temporary), timeout=5.0)
    try:
        source.backup(destination)
    except sqlite3.Error as exc:
        raise ValueError(f"Could not back up the database: {exc}") from None
    finally:
        destination.close()
        source.close()

    report = verify_backup(temporary)
    if not report["ok"]:
        raise ValueError(
            "Backup verification failed; the temporary file was kept for "
            f"inspection at {temporary}."
        )
    os.replace(str(temporary), str(target))
    report["path"] = str(target)
    report["sha256"] = _sha256(target)
    report["size_bytes"] = target.stat().st_size
    return report


def list_backups(
    *, backup_dir: Any = None, actor: Any = None
) -> list[dict[str, Any]]:
    """Newest first, with integrity status (for the manager's picker)."""
    permissions.require(actor, permissions.CAP_MANAGE_BACKUPS)
    folder = Path(backup_dir) if backup_dir is not None else BACKUP_DIR
    if not folder.is_dir():
        return []
    result: list[dict[str, Any]] = []
    for path in sorted(folder.glob(f"*{BACKUP_SUFFIX}"), reverse=True):
        if path.name.startswith("."):
            continue
        try:
            info = verify_backup(path)
        except ValueError:
            info = {"ok": False, "integrity": "unreadable"}
        result.append(
            {
                "path": str(path),
                "name": path.name,
                "ok": bool(info["ok"]),
                "size_bytes": path.stat().st_size,
            }
        )
    return result


def restore_backup(
    backup_path: Any,
    *,
    db_path: Any = None,
    backup_dir: Any = None,
    actor: Any = None,
    safety_backup: bool = True,
) -> dict[str, Any]:
    """Restore a VERIFIED backup into the live database via the backup API.

    A pre-restore backup of the current database is taken first (unless the
    caller explicitly disables it for a unit test), so no restore is ever a
    one-way trip. The destination file is never deleted or replaced.
    """
    permissions.require(actor, permissions.CAP_MANAGE_BACKUPS)
    source_path = Path(backup_path)
    report = verify_backup(source_path)
    if not report["ok"]:
        raise ValueError(
            "Refusing to restore: the backup failed integrity or foreign-key "
            "verification."
        )
    destination_path = Path(db_path) if db_path is not None else database.DB_PATH
    if not destination_path.is_file():
        raise ValueError("The destination database file does not exist.")
    folder = Path(backup_dir) if backup_dir is not None else BACKUP_DIR
    safety: dict[str, Any] | None = None
    if safety_backup:
        safety = create_backup(
            db_path=destination_path,
            backup_dir=folder,
            actor=actor,
            prefix="pre-restore",
        )

    source = _read_only(source_path)
    destination = sqlite3.connect(str(destination_path), timeout=5.0)
    try:
        destination.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        # Fold any live WAL frames into the main file before we overwrite it.
        destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        source.backup(destination)
        destination.commit()
        # After the restore, leave no stale WAL frames behind.
        destination.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        integrity = str(
            destination.execute("PRAGMA integrity_check").fetchone()[0]
        )
    except sqlite3.Error as exc:
        raise ValueError(
            f"Could not restore the database (is the app still open?): {exc}"
        ) from None
    finally:
        destination.close()
        source.close()

    if integrity != "ok":
        raise ValueError(
            "Restore completed but integrity check failed. A pre-restore "
            "backup is available - restore it before using the app again."
        )
    return {
        "ok": True,
        "restored_from": str(source_path),
        "restored_sha256": report["sha256"],
        "integrity": integrity,
        "safety_backup": None if safety is None else safety["path"],
        "database": str(destination_path),
    }
