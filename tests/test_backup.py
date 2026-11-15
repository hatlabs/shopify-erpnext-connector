import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from shopify_erpnext_connector.backup import (
    KEEP_BACKUPS,
    backup_directory,
    last_backup_at,
    prune_backups,
    take_backup,
)
from shopify_erpnext_connector.store import open_store, utc_timestamp

NOW = datetime(2026, 11, 12, 3, 0, tzinfo=UTC)
DAY = timedelta(days=1)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_store(tmp_path / "connector.sqlite3")
    yield connection
    connection.close()


def audit_row(connection: sqlite3.Connection, action: str) -> None:
    connection.execute(
        "INSERT INTO audit_log (recorded_at, operator, action) VALUES (?, ?, ?)",
        (utc_timestamp(NOW), "alice", action),
    )


def test_the_backup_directory_is_next_to_the_store(tmp_path: Path) -> None:
    assert backup_directory(tmp_path / "connector.sqlite3") == tmp_path / "backups"


def test_a_backup_holds_the_current_rows(
    store: sqlite3.Connection, tmp_path: Path
) -> None:
    audit_row(store, "first")
    audit_row(store, "second")
    directory = tmp_path / "backups"

    path = take_backup(store, directory, NOW)

    with closing(sqlite3.connect(path)) as copy:
        actions = [r[0] for r in copy.execute("SELECT action FROM audit_log")]
        mode = copy.execute("PRAGMA journal_mode").fetchone()[0]
    assert actions == ["first", "second"]
    # A self-contained file: a guest snapshot needs no -wal file beside it.
    assert mode == "delete"


def test_a_backup_leaves_only_its_dated_file(
    store: sqlite3.Connection, tmp_path: Path
) -> None:
    directory = tmp_path / "backups"

    path = take_backup(store, directory, NOW)

    assert list(directory.iterdir()) == [path]
    assert last_backup_at(directory) == NOW


def test_a_backup_replaces_a_partial_file_left_by_a_crash(
    store: sqlite3.Connection, tmp_path: Path
) -> None:
    directory = tmp_path / "backups"
    directory.mkdir()
    (directory / "backup.partial").write_bytes(b"not a database")
    audit_row(store, "kept")

    path = take_backup(store, directory, NOW)

    with closing(sqlite3.connect(path)) as copy:
        assert copy.execute("SELECT action FROM audit_log").fetchall() == [("kept",)]
    assert list(directory.iterdir()) == [path]


def test_no_backup_directory_means_no_last_backup(tmp_path: Path) -> None:
    assert last_backup_at(tmp_path / "backups") is None


def test_the_last_backup_is_the_newest_dated_file(
    store: sqlite3.Connection, tmp_path: Path
) -> None:
    directory = tmp_path / "backups"
    take_backup(store, directory, NOW - DAY)
    take_backup(store, directory, NOW)
    take_backup(store, directory, NOW - 2 * DAY)
    (directory / "connector-notadate.sqlite3").write_bytes(b"")
    (directory / "notes.txt").write_bytes(b"")

    assert last_backup_at(directory) == NOW


def test_pruning_keeps_the_newest_and_only_removes_backups(
    store: sqlite3.Connection, tmp_path: Path
) -> None:
    directory = tmp_path / "backups"
    paths = [take_backup(store, directory, NOW - n * DAY) for n in range(10)]
    others = [
        directory / "notes.txt",
        directory / "connector-notadate.sqlite3",
        directory / "backup.partial",
    ]
    for other in others:
        other.write_bytes(b"")

    prune_backups(directory)

    kept = sorted(directory.iterdir())
    assert kept == sorted(paths[:KEEP_BACKUPS] + others)
    assert KEEP_BACKUPS == 7


def test_pruning_with_few_backups_removes_nothing(
    store: sqlite3.Connection, tmp_path: Path
) -> None:
    directory = tmp_path / "backups"
    paths = [take_backup(store, directory, NOW - n * DAY) for n in range(3)]

    prune_backups(directory)

    assert sorted(directory.iterdir()) == sorted(paths)


def test_a_backup_into_an_unwritable_place_raises(
    store: sqlite3.Connection, tmp_path: Path
) -> None:
    blocker = tmp_path / "backups"
    blocker.write_bytes(b"a file where the directory should be")

    with pytest.raises(OSError):
        take_backup(store, blocker, NOW)


def test_a_failed_copy_removes_its_partial_file(
    store: sqlite3.Connection, tmp_path: Path
) -> None:
    directory = tmp_path / "backups"
    closed = open_store(tmp_path / "other.sqlite3")
    closed.close()

    with pytest.raises(sqlite3.Error):
        take_backup(closed, directory, NOW)

    assert list(directory.iterdir()) == []
    path = take_backup(store, directory, NOW)
    assert list(directory.iterdir()) == [path]
