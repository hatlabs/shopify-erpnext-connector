"""Online backups of the store into a directory next to it.

A guest-level backup copies the store file while the connector writes to it,
which can catch it mid-write. The connector therefore keeps dated copies made
with SQLite's online backup, each a consistent self-contained database, in
the same volume. A backup's time is in its file name, so the last one is
known after a restart.
"""

import os
import re
import sqlite3
from contextlib import closing, suppress
from datetime import UTC, datetime
from pathlib import Path

KEEP_BACKUPS = 7
BACKUP_DIRECTORY_NAME = "backups"

_NAME_FORMAT = "connector-%Y%m%dT%H%M%SZ.sqlite3"
_NAME = re.compile(r"connector-\d{8}T\d{6}Z\.sqlite3")
# Outside `_NAME`, so neither pruning nor the last-backup time sees it.
_PARTIAL_NAME = "backup.partial"


def backup_directory(store_path: Path) -> Path:
    return store_path.parent / BACKUP_DIRECTORY_NAME


def take_backup(source: sqlite3.Connection, directory: Path, now: datetime) -> Path:
    """Copy the store into a file named by `now`; return its path.

    The copy is written under a temporary name, synced and renamed, so a
    dated file is always complete.
    """
    if not directory.exists():
        directory.mkdir()
        # The new directory's own entry must survive a crash too, or the
        # first backup is lost with it.
        _fsync_directory(directory.parent)
    partial = directory / _PARTIAL_NAME
    partial.unlink(missing_ok=True)
    path = directory / now.astimezone(UTC).strftime(_NAME_FORMAT)
    try:
        with closing(sqlite3.connect(partial, isolation_level=None)) as copy:
            # One step copies a snapshot under a read transaction; in WAL mode
            # writers do not wait for it, so the writer lock is not needed.
            source.backup(copy)
            copy.execute("PRAGMA journal_mode = DELETE")
        with partial.open("rb") as file:
            os.fsync(file.fileno())
        partial.replace(path)
    except BaseException:
        # On a full volume the partial copy holds space the store needs
        # until the next attempt.
        with suppress(OSError):
            partial.unlink(missing_ok=True)
        raise
    _fsync_directory(directory)
    return path


def last_backup_at(directory: Path) -> datetime | None:
    """The time of the newest backup in `directory`; None if there is none."""
    backups = _backups(directory)
    return _backup_time(backups[0]) if backups else None


def prune_backups(directory: Path) -> None:
    """Delete all but the newest `KEEP_BACKUPS` backups; other files stay."""
    for path in _backups(directory)[KEEP_BACKUPS:]:
        path.unlink(missing_ok=True)


def _backups(directory: Path) -> list[Path]:
    """The backups in `directory`, newest first."""
    # An unreadable directory has no backups to report; writing a backup into
    # it fails and is logged.
    try:
        names = [p for p in directory.iterdir() if _NAME.fullmatch(p.name)]
    except OSError:
        return []
    return sorted(names, key=lambda p: p.name, reverse=True)


def _backup_time(path: Path) -> datetime:
    return datetime.strptime(path.name, _NAME_FORMAT).replace(tzinfo=UTC)


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
