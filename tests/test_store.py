import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from shopify_erpnext_connector.store import (
    MIGRATIONS,
    PUSH_HISTORY_RETENTION,
    StoreError,
    migrate,
    open_store,
    purge_push_history,
    utc_timestamp,
)

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
VARIANT_ID = "gid://shopify/ProductVariant/1001"

PHASE_A_TABLES = {
    "watermark",
    "dirty_items",
    "bindings",
    "variant_quantities",
    "pending_pushes",
    "push_history",
    "problems",
    "decisions",
    "audit_log",
}


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "connector.sqlite3"


@pytest.fixture
def store(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_store(db_path)
    yield connection
    connection.close()


def user_version(connection: sqlite3.Connection) -> int:
    return connection.execute("PRAGMA user_version").fetchone()[0]


def tables(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")
    return {name for (name,) in rows}


def add_push(connection: sqlite3.Connection, attempted_at: datetime) -> None:
    connection.execute(
        "INSERT INTO push_history"
        " (variant_id, available, change_from, idempotency_key, attempted_at,"
        " outcome) VALUES (?, 5, 4, 'key', ?, 'ok')",
        (VARIANT_ID, utc_timestamp(attempted_at)),
    )


def push_times(connection: sqlite3.Connection) -> list[str]:
    rows = connection.execute(
        "SELECT attempted_at FROM push_history ORDER BY attempted_at"
    )
    return [attempted_at for (attempted_at,) in rows]


def test_open_creates_phase_a_tables_on_an_empty_file(
    store: sqlite3.Connection,
) -> None:
    assert tables(store) >= PHASE_A_TABLES
    assert user_version(store) == len(MIGRATIONS)


def test_reopening_a_current_database_is_a_no_op(db_path: Path) -> None:
    first = open_store(db_path)
    add_push(first, NOW)
    first.close()

    # The migrations use plain CREATE TABLE, so re-running one would raise.
    second = open_store(db_path)

    assert user_version(second) == len(MIGRATIONS)
    assert push_times(second) == [utc_timestamp(NOW)]
    second.close()


def test_file_database_uses_wal(store: sqlite3.Connection) -> None:
    assert store.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_database_newer_than_the_code_is_refused(db_path: Path) -> None:
    newer = sqlite3.connect(db_path)
    newer.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    newer.close()

    with pytest.raises(StoreError, match="newer"):
        open_store(db_path)


def test_failing_migration_leaves_version_and_schema_unchanged(
    db_path: Path,
) -> None:
    connection = sqlite3.connect(db_path, isolation_level=None)
    migrations = (
        "CREATE TABLE first (id INTEGER PRIMARY KEY);",
        "CREATE TABLE second (id INTEGER PRIMARY KEY); CREATE TABLE first (id);",
    )

    with pytest.raises(StoreError, match="migration 2"):
        migrate(connection, migrations)

    assert user_version(connection) == 1
    assert tables(connection) == {"first"}
    assert not connection.in_transaction
    connection.close()


def test_pending_migrations_apply_in_order(db_path: Path) -> None:
    connection = sqlite3.connect(db_path, isolation_level=None)
    migrate(connection, ("CREATE TABLE first (id INTEGER PRIMARY KEY);",))

    migrate(
        connection,
        (
            "CREATE TABLE first (id INTEGER PRIMARY KEY);",
            "ALTER TABLE first ADD COLUMN name TEXT;",
        ),
    )

    assert user_version(connection) == 2
    columns = {row[1] for row in connection.execute("PRAGMA table_info(first)")}
    assert columns == {"id", "name"}
    connection.close()


def test_purge_deletes_push_history_past_retention(
    store: sqlite3.Connection,
) -> None:
    cutoff = NOW - PUSH_HISTORY_RETENTION
    add_push(store, cutoff - timedelta(microseconds=1))
    add_push(store, cutoff - timedelta(days=30))
    add_push(store, cutoff)
    add_push(store, NOW - timedelta(days=1))

    deleted = purge_push_history(store, NOW)

    assert deleted == 2
    assert push_times(store) == [
        utc_timestamp(cutoff),
        utc_timestamp(NOW - timedelta(days=1)),
    ]


def test_retention_is_ninety_days() -> None:
    assert PUSH_HISTORY_RETENTION == timedelta(days=90)


def test_purge_keeps_decision_records_and_audit_log(
    store: sqlite3.Connection,
) -> None:
    long_ago = utc_timestamp(NOW - timedelta(days=400))
    store.execute(
        "INSERT INTO decisions (variant_id, decided_at, record) VALUES (?, ?, '{}')",
        (VARIANT_ID, long_ago),
    )
    store.execute(
        "INSERT INTO audit_log (recorded_at, operator, action) VALUES (?, ?, ?)",
        (long_ago, "alice", "push"),
    )

    purge_push_history(store, NOW)

    assert store.execute("SELECT count(*) FROM decisions").fetchone()[0] == 1
    assert store.execute("SELECT count(*) FROM audit_log").fetchone()[0] == 1


def test_utc_timestamp_converts_to_utc_with_fixed_width() -> None:
    helsinki_winter = timezone(timedelta(hours=2))
    moment = datetime(2026, 11, 12, 12, 0, tzinfo=helsinki_winter)

    assert utc_timestamp(moment) == "2026-11-12T10:00:00.000000Z"


def test_utc_timestamp_refuses_a_naive_datetime() -> None:
    with pytest.raises(ValueError):
        utc_timestamp(datetime(2026, 11, 12, 10, 0))
