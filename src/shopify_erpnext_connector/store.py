"""The connector's SQLite store: schema migrations, WAL mode and retention.

Connections run in autocommit mode (`isolation_level=None`); callers group
writes with `transaction`.

Connector timestamps are UTC text from `utc_timestamp`, fixed width so that
text order is time order. The watermark is the exception: it holds ERPNext's
own Bin `modified` value, a naive time in the site's time zone, written
with six fractional digits.

Identifiers are text: Shopify GIDs and ERPNext item codes.
"""

import sqlite3
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

PUSH_HISTORY_RETENTION = timedelta(days=90)

# Applied in order; the database's `user_version` counts those applied. Never
# edit a released migration: append a new one.
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE watermark (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        bin_modified TEXT NOT NULL
    );

    CREATE TABLE dirty_items (
        item_code TEXT PRIMARY KEY,
        marked_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE bindings (
        variant_id TEXT PRIMARY KEY,
        inventory_item_id TEXT NOT NULL UNIQUE,
        item_code TEXT NOT NULL UNIQUE,
        bound_at TEXT NOT NULL
    );

    CREATE TABLE variant_quantities (
        variant_id TEXT PRIMARY KEY,
        last_read INTEGER,
        last_read_at TEXT,
        last_pushed INTEGER,
        last_pushed_at TEXT
    );

    -- One row per variant: a newer value replaces an unsent one. No idempotency
    -- key: a batch's key lives only in memory for an immediate resend, and a
    -- batch rebuilt after a restart is guarded by `change_from` instead.
    CREATE TABLE pending_pushes (
        variant_id TEXT PRIMARY KEY,
        available INTEGER NOT NULL,
        change_from INTEGER,
        queued_at TEXT NOT NULL
    );

    -- `idempotency_key` is the key of the batch the row was sent in, for diagnosis.
    CREATE TABLE push_history (
        id INTEGER PRIMARY KEY,
        variant_id TEXT NOT NULL,
        available INTEGER NOT NULL,
        change_from INTEGER,
        idempotency_key TEXT NOT NULL,
        attempted_at TEXT NOT NULL,
        outcome TEXT NOT NULL
    );
    CREATE INDEX push_history_attempted_at ON push_history (attempted_at);

    CREATE TABLE problems (
        kind TEXT NOT NULL,
        subject TEXT NOT NULL,
        fingerprint TEXT NOT NULL,
        opened_at TEXT NOT NULL,
        resolved_at TEXT,
        email_pending INTEGER NOT NULL CHECK (email_pending IN (0, 1)),
        communication_name TEXT,
        PRIMARY KEY (kind, subject)
    );

    -- The current decision per variant, overwritten; `record` is JSON.
    CREATE TABLE decisions (
        variant_id TEXT PRIMARY KEY,
        decided_at TEXT NOT NULL,
        record TEXT NOT NULL
    );

    CREATE TABLE audit_log (
        id INTEGER PRIMARY KEY,
        recorded_at TEXT NOT NULL,
        operator TEXT NOT NULL,
        action TEXT NOT NULL,
        detail TEXT
    );
    """,
    """
    -- A variant whose SKU changed since it was bound: the item it was bound
    -- to, kept while the change is reported so the variant stays unbound.
    CREATE TABLE sku_changes (
        variant_id TEXT PRIMARY KEY,
        bound_item_code TEXT NOT NULL,
        detected_at TEXT NOT NULL
    );
    """,
    """
    -- The problem's details as a JSON object, rendered into its email.
    ALTER TABLE problems ADD COLUMN details TEXT NOT NULL DEFAULT '{}';
    -- Sends of the current problem version, bounded so that a broken mail
    -- account does not repeat the email every pass.
    ALTER TABLE problems ADD COLUMN email_attempts INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE problems ADD COLUMN last_email_attempt_at TEXT;
    """,
    """
    -- Operator settings. Dry-run starts on, so a fresh install pushes nothing
    -- until an operator turns it off.
    CREATE TABLE settings (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        dry_run INTEGER NOT NULL CHECK (dry_run IN (0, 1))
    );
    INSERT INTO settings (id, dry_run) VALUES (1, 1);
    """,
)

_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class StoreError(Exception):
    """The store cannot be opened or migrated."""


def open_store(path: Path) -> sqlite3.Connection:
    """Open the store file in WAL mode and apply pending migrations."""
    connection = sqlite3.connect(path, isolation_level=None)
    try:
        mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if mode != "wal":
            raise StoreError(f"{path}: cannot enable WAL mode (got {mode!r})")
        migrate(connection, MIGRATIONS)
    except BaseException:
        connection.close()
        raise
    return connection


def migrate(connection: sqlite3.Connection, migrations: Sequence[str]) -> None:
    """Apply the migrations past the database's version, each in a transaction."""
    current = connection.execute("PRAGMA user_version").fetchone()[0]
    if current > len(migrations):
        raise StoreError(
            f"database schema version {current} is newer than this connector "
            f"supports ({len(migrations)})"
        )
    for version, script in enumerate(migrations[current:], start=current + 1):
        try:
            connection.executescript(
                f"BEGIN; {script}\n; PRAGMA user_version = {version}; COMMIT;"
            )
        except sqlite3.Error as error:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise StoreError(f"migration {version} failed: {error}") from error


def purge_push_history(connection: sqlite3.Connection, now: datetime) -> int:
    """Delete push history older than the retention period; return the count.

    A row exactly at the cutoff is kept. Decision records and the audit log
    are never purged.
    """
    cutoff = utc_timestamp(now - PUSH_HISTORY_RETENTION)
    cursor = connection.execute(
        "DELETE FROM push_history WHERE attempted_at < ?", (cutoff,)
    )
    return cursor.rowcount


@contextmanager
def transaction(connection: sqlite3.Connection) -> Generator[None]:
    """Commit the block's writes together, or roll them all back on an error."""
    connection.execute("BEGIN")
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def dry_run(connection: sqlite3.Connection) -> bool:
    """Whether the worker computes and records without pushing to Shopify."""
    return bool(connection.execute("SELECT dry_run FROM settings").fetchone()[0])


def set_dry_run(connection: sqlite3.Connection, on: bool) -> None:
    connection.execute("UPDATE settings SET dry_run = ?", (int(on),))


def utc_timestamp(moment: datetime) -> str:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return moment.astimezone(UTC).strftime(_TIMESTAMP_FORMAT)
