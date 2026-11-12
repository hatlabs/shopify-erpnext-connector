"""Bin change polling and the dirty set it feeds.

Each poll re-reads the Bins modified within `POLL_OVERLAP` of the watermark.
ERPNext stamps a Bin before its transaction commits, so a change can become
visible with a `modified` older than one already seen; the overlap catches
it. Pairs already seen are kept in memory so a re-read Bin is not marked
again. Bin `modified` is a naive time in the ERPNext site's time zone, so the
watermark only ever comes from returned values, never from this host's clock.

A dirty item's `attempts` counts its failed pushes and is persisted for the
operator status to show. When a failed item is retried is decided by the
worker's in-memory backoff, not by this count.
"""

import sqlite3
from collections.abc import Collection, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from shopify_erpnext_connector.erpnext import ErpNextClient, ErpNextError, Filter
from shopify_erpnext_connector.store import utc_timestamp

# About ten times the largest stamp-to-commit gap measured on ERPNext.
POLL_OVERLAP = timedelta(seconds=60)

_MODIFIED_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
_MODIFIED_FORMAT_WHOLE_SECONDS = "%Y-%m-%d %H:%M:%S"


class SeenBins:
    """The (item code, `modified`) pairs already read within the overlap."""

    def __init__(self) -> None:
        self._pairs: set[tuple[str, datetime]] = set()

    def contains(self, item_code: str, modified: datetime) -> bool:
        return (item_code, modified) in self._pairs

    def add(self, pairs: Collection[tuple[str, datetime]]) -> None:
        self._pairs.update(pairs)

    def forget_before(self, cutoff: datetime) -> None:
        self._pairs = {pair for pair in self._pairs if pair[1] >= cutoff}


@dataclass(frozen=True)
class DirtyItem:
    marked_at: str
    attempts: int


def poll_bins(
    connection: sqlite3.Connection,
    erpnext: ErpNextClient,
    warehouse: str,
    seen: SeenBins,
    now: datetime,
) -> set[str]:
    """Mark the items whose Bin in `warehouse` changed; return their codes.

    Without a watermark every Bin of the warehouse is read. The watermark
    advances to the latest `modified` read, never backwards, in the same
    transaction that marks the items.
    """
    current = _read_watermark(connection)
    filters: list[Filter] = [("warehouse", "=", warehouse)]
    if current is not None:
        filters.append(("modified", ">=", _format(current - POLL_OVERLAP)))
    rows = erpnext.get_list(
        "Bin",
        fields=("item_code", "modified"),
        filters=filters,
        order_by="modified asc",
    )
    pairs = [_parse_row(row) for row in rows]

    changed = {code for code, modified in pairs if not seen.contains(code, modified)}
    stamps = [modified for _, modified in pairs]
    if current is not None:
        stamps.append(current)
    latest = max(stamps, default=None)
    with _transaction(connection):
        _insert_dirty(connection, changed, now)
        if latest is not None and latest != current:
            connection.execute(
                "INSERT INTO watermark (id, bin_modified) VALUES (1, ?)"
                " ON CONFLICT (id) DO UPDATE SET bin_modified = excluded.bin_modified",
                (_format(latest),),
            )

    seen.add(pairs)
    if latest is not None:
        seen.forget_before(latest - POLL_OVERLAP)
    return changed


def mark_dirty(
    connection: sqlite3.Connection, item_codes: Collection[str], now: datetime
) -> None:
    """Mark items dirty; an item already dirty keeps its time and attempts."""
    with _transaction(connection):
        _insert_dirty(connection, item_codes, now)


def clear_dirty(connection: sqlite3.Connection, item_codes: Collection[str]) -> None:
    with _transaction(connection):
        connection.executemany(
            "DELETE FROM dirty_items WHERE item_code = ?",
            [(code,) for code in item_codes],
        )


def record_failed_attempt(
    connection: sqlite3.Connection, item_codes: Collection[str]
) -> None:
    with _transaction(connection):
        connection.executemany(
            "UPDATE dirty_items SET attempts = attempts + 1 WHERE item_code = ?",
            [(code,) for code in item_codes],
        )


def dirty_items(connection: sqlite3.Connection) -> dict[str, DirtyItem]:
    rows = connection.execute(
        "SELECT item_code, marked_at, attempts FROM dirty_items ORDER BY item_code"
    ).fetchall()
    return {code: DirtyItem(marked_at, attempts) for code, marked_at, attempts in rows}


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Generator[None]:
    connection.execute("BEGIN")
    try:
        yield
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def _insert_dirty(
    connection: sqlite3.Connection, item_codes: Collection[str], now: datetime
) -> None:
    stamp = utc_timestamp(now)
    connection.executemany(
        "INSERT INTO dirty_items (item_code, marked_at) VALUES (?, ?)"
        " ON CONFLICT (item_code) DO NOTHING",
        [(code, stamp) for code in sorted(item_codes)],
    )


def _read_watermark(connection: sqlite3.Connection) -> datetime | None:
    row = connection.execute("SELECT bin_modified FROM watermark").fetchone()
    return None if row is None else _parse_modified(row[0])


def _parse_row(row: Mapping[str, object]) -> tuple[str, datetime]:
    code = row.get("item_code")
    modified = row.get("modified")
    if not isinstance(code, str) or not isinstance(modified, str):
        raise _unexpected_bin()
    try:
        return code, _parse_modified(modified)
    except ValueError as error:
        raise _unexpected_bin() from error


def _parse_modified(value: str) -> datetime:
    fmt = _MODIFIED_FORMAT if "." in value else _MODIFIED_FORMAT_WHOLE_SECONDS
    return datetime.strptime(value, fmt)


def _format(moment: datetime) -> str:
    return moment.strftime(_MODIFIED_FORMAT)


def _unexpected_bin() -> ErpNextError:
    return ErpNextError("Bin change list: unexpected row")
