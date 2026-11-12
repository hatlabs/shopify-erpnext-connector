"""Read Shopify's available levels and push sellable values with compare-and-set.

Every entry sent carries `changeFromQuantity`, the level just read, so a level
that moved since fails as stale instead of being overwritten. A value is sent
only when it differs from that level, and never to an item with no level at
the location: Shopify would read a null `changeFromQuantity` as "skip the
check".

A value not yet applied is kept in `pending_pushes`, one row per variant. A
newer value replaces it but keeps its `queued_at`, so `stale_pushes` measures
how long the variant has gone without a successful push.

Shopify applies all of a batch or none of it. When user errors name entries,
those entries are reported and the rest are resent as a new batch with a new
key. After a transport error the same batch is resent once with the same key,
so Shopify can recognise it if the first attempt was applied.
"""

import logging
import sqlite3
from collections import Counter
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from shopify_erpnext_connector.shopify import (
    CHANGE_FROM_QUANTITY_STALE,
    InventoryBatch,
    QuantityChange,
    ShopifyClient,
    ShopifyError,
    ShopifyGraphQLError,
    ShopifyHTTPError,
    ShopifyTransportError,
    ShopifyUserError,
    batch_changes,
    entry_index,
    new_idempotency_key,
)
from shopify_erpnext_connector.store import utc_timestamp

# The most IDs `nodes` accepts in one query.
LEVELS_PAGE_SIZE = 250
AVAILABLE = "available"

# Error codes recorded for failures that carry no Shopify code.
TRANSPORT_FAILURE = "transport"
UNEXPECTED_RESPONSE = "unexpected_response"
UNKNOWN_USER_ERROR = "unknown"

_log = logging.getLogger(__name__)

_LEVELS_QUERY = """\
query InventoryLevels($ids: [ID!]!, $locationId: ID!) {
  nodes(ids: $ids) {
    ... on InventoryItem {
      id
      inventoryLevel(locationId: $locationId) {
        quantities(names: ["available"]) { name quantity }
      }
      variant { inventoryPolicy }
    }
  }
}
"""


@dataclass(frozen=True)
class Level:
    """An inventory item's level at the location and its variant's policy.

    `available` is None when the item is not stocked at the location.
    `policy` is the variant's `inventoryPolicy`, such as DENY or CONTINUE.
    """

    available: int | None
    policy: str


def fetch_levels(
    shopify: ShopifyClient, inventory_item_ids: Sequence[str], location_id: str
) -> dict[str, Level]:
    """Read the level of each inventory item at the location, by item ID.

    An item Shopify does not know, such as one whose variant was deleted, is
    left out, so the other items' levels are still read.
    """
    ids = list(dict.fromkeys(inventory_item_ids))
    levels: dict[str, Level] = {}
    for start in range(0, len(ids), LEVELS_PAGE_SIZE):
        chunk = ids[start : start + LEVELS_PAGE_SIZE]
        data = shopify.execute(
            _LEVELS_QUERY, {"ids": chunk, "locationId": location_id}, idempotent=True
        )
        nodes = data.get("nodes")
        if not isinstance(nodes, list) or len(nodes) != len(chunk):
            raise _unexpected_levels()
        for requested, node in zip(chunk, nodes, strict=True):
            if node is None:
                continue
            item_id, level = _parse_level(node)
            if item_id != requested:
                raise _unexpected_levels()
            levels[item_id] = level
    return levels


def _parse_level(node: object) -> tuple[str, Level]:
    if not isinstance(node, dict):
        raise _unexpected_levels()
    item_id = node.get("id")
    variant = node.get("variant")
    policy = variant.get("inventoryPolicy") if isinstance(variant, dict) else None
    if not isinstance(item_id, str) or not isinstance(policy, str):
        raise _unexpected_levels()
    inventory_level = node.get("inventoryLevel")
    if inventory_level is None:
        return item_id, Level(None, policy)
    if not isinstance(inventory_level, dict):
        raise _unexpected_levels()
    return item_id, Level(_available(inventory_level.get("quantities")), policy)


def _available(quantities: object) -> int:
    if not isinstance(quantities, list):
        raise _unexpected_levels()
    for entry in quantities:
        if not isinstance(entry, dict):
            raise _unexpected_levels()
        if entry.get("name") == AVAILABLE:
            quantity = entry.get("quantity")
            # bool is an int subclass; a boolean is not a quantity.
            if not isinstance(quantity, int) or isinstance(quantity, bool):
                raise _unexpected_levels()
            return quantity
    raise _unexpected_levels()


def _unexpected_levels() -> ShopifyError:
    return ShopifyError("inventory levels: unexpected response")


class PushOutcome(StrEnum):
    PUSHED = "pushed"
    UNCHANGED = "unchanged"
    STALE = "stale"
    FAILED = "failed"
    NO_LEVEL = "no_level"


@dataclass(frozen=True)
class PushTarget:
    """The value to set for a variant, and its `available` as just read."""

    variant_id: str
    inventory_item_id: str
    value: int
    current: int | None


@dataclass(frozen=True)
class PushResult:
    """The outcome per variant ID, and the error code of each stale or failed one."""

    outcomes: Mapping[str, PushOutcome]
    codes: Mapping[str, str]


@dataclass(frozen=True)
class _Outcome:
    target: PushTarget
    outcome: PushOutcome
    code: str | None = None


def push(
    connection: sqlite3.Connection,
    shopify: ShopifyClient,
    location_id: str,
    targets: Sequence[PushTarget],
    now: datetime,
) -> PushResult:
    """Push each target's value that differs from Shopify's, and record the result."""
    _require_unique(targets)
    stamp = utc_timestamp(now)
    results: dict[str, _Outcome] = {}
    to_send: list[PushTarget] = []
    with _transaction(connection):
        for target in targets:
            _record_read(connection, target, stamp)
            if target.value == target.current:
                connection.execute(
                    "DELETE FROM pending_pushes WHERE variant_id = ?",
                    (target.variant_id,),
                )
                # Shopify already holds the value, which confirms it as much as
                # a push would; the worker's sale hold compares against it.
                connection.execute(
                    "UPDATE variant_quantities SET last_pushed = ?, last_pushed_at = ?"
                    " WHERE variant_id = ?",
                    (target.value, stamp, target.variant_id),
                )
                results[target.variant_id] = _Outcome(target, PushOutcome.UNCHANGED)
                continue
            _queue(connection, target, stamp)
            if target.current is None:
                results[target.variant_id] = _Outcome(target, PushOutcome.NO_LEVEL)
            else:
                to_send.append(target)

    by_item = {t.inventory_item_id: t for t in to_send}
    changes = [
        QuantityChange(t.inventory_item_id, location_id, t.value, t.current)
        for t in to_send
    ]
    for batch in batch_changes(changes):
        for done in _push_batch(connection, shopify, batch, by_item, stamp):
            results[done.target.variant_id] = done

    return PushResult(
        outcomes={v: r.outcome for v, r in results.items()},
        codes={v: r.code for v, r in results.items() if r.code is not None},
    )


def _require_unique(targets: Sequence[PushTarget]) -> None:
    for ids in (
        [t.variant_id for t in targets],
        [t.inventory_item_id for t in targets],
    ):
        repeated = sorted(i for i, n in Counter(ids).items() if n > 1)
        if repeated:
            raise ValueError(f"listed more than once: {', '.join(repeated)}")


def _push_batch(
    connection: sqlite3.Connection,
    shopify: ShopifyClient,
    batch: InventoryBatch,
    by_item: Mapping[str, PushTarget],
    stamp: str,
) -> list[_Outcome]:
    """Send the batch, resending what Shopify did not reject, until done."""
    done: list[_Outcome] = []

    def finish(outcomes: list[_Outcome], key: str) -> None:
        _record_outcomes(connection, outcomes, key, stamp)
        done.extend(outcomes)

    while batch.changes:
        targets = [by_item[c.inventory_item_id] for c in batch.changes]
        try:
            _send(shopify, batch)
        except ShopifyUserError as error:
            rejected = _rejected(error, batch.changes)
            if rejected is None:
                code = _unplaced_code(error, batch.changes)
                finish(_all(targets, PushOutcome.FAILED, code), batch.idempotency_key)
                return done
            finish(
                [_Outcome(targets[i], *rejected[i]) for i in sorted(rejected)],
                batch.idempotency_key,
            )
            remaining = tuple(
                c for i, c in enumerate(batch.changes) if i not in rejected
            )
            batch = InventoryBatch(remaining, new_idempotency_key())
        except ShopifyError as error:
            code = _failure_code(error)
            _log.warning("inventory push of %d entries failed: %s", len(targets), code)
            finish(_all(targets, PushOutcome.FAILED, code), batch.idempotency_key)
            return done
        else:
            finish(_all(targets, PushOutcome.PUSHED), batch.idempotency_key)
            return done
    return done


def _send(shopify: ShopifyClient, batch: InventoryBatch) -> None:
    try:
        shopify.set_quantities(batch)
    except ShopifyTransportError:
        _log.warning("inventory push failed in transport, resending once")
        shopify.set_quantities(batch)


def _rejected(
    error: ShopifyUserError, changes: tuple[QuantityChange, ...]
) -> dict[int, tuple[PushOutcome, str]] | None:
    """The outcome and code per rejected entry; None if an error names no entry."""
    rejected: dict[int, tuple[PushOutcome, str]] = {}
    for user_error in error.user_errors:
        index = entry_index(user_error, changes)
        if index is None:
            return None
        code = user_error.code or UNKNOWN_USER_ERROR
        if code == CHANGE_FROM_QUANTITY_STALE:
            rejected.setdefault(index, (PushOutcome.STALE, code))
            continue
        earlier = rejected.get(index)
        # Any other error outweighs staleness: a re-read would not fix it.
        if earlier is None or earlier[0] is PushOutcome.STALE:
            rejected[index] = (PushOutcome.FAILED, code)
    return rejected


def _unplaced_code(error: ShopifyUserError, changes: tuple[QuantityChange, ...]) -> str:
    for user_error in error.user_errors:
        if entry_index(user_error, changes) is None:
            return user_error.code or UNKNOWN_USER_ERROR
    return UNKNOWN_USER_ERROR


def _failure_code(error: ShopifyError) -> str:
    if isinstance(error, ShopifyTransportError):
        return TRANSPORT_FAILURE
    if isinstance(error, ShopifyHTTPError):
        return f"http_{error.status}"
    if isinstance(error, ShopifyGraphQLError):
        return ",".join(error.codes)
    return UNEXPECTED_RESPONSE


def _all(
    targets: Sequence[PushTarget], outcome: PushOutcome, code: str | None = None
) -> list[_Outcome]:
    return [_Outcome(t, outcome, code) for t in targets]


def last_pushed_values(connection: sqlite3.Connection) -> dict[str, int]:
    """The value last pushed, or confirmed already in Shopify, per variant."""
    rows = connection.execute(
        "SELECT variant_id, last_pushed FROM variant_quantities"
        " WHERE last_pushed IS NOT NULL"
    ).fetchall()
    return dict(rows)


def drop_unbound_pending(connection: sqlite3.Connection) -> None:
    """Drop the pending values of variants no longer bound: they are never sent."""
    connection.execute(
        "DELETE FROM pending_pushes"
        " WHERE variant_id NOT IN (SELECT variant_id FROM bindings)"
    )


def _record_read(
    connection: sqlite3.Connection, target: PushTarget, stamp: str
) -> None:
    connection.execute(
        "INSERT INTO variant_quantities (variant_id, last_read, last_read_at)"
        " VALUES (?, ?, ?) ON CONFLICT (variant_id) DO UPDATE SET"
        " last_read = excluded.last_read, last_read_at = excluded.last_read_at",
        (target.variant_id, target.current, stamp),
    )


def _queue(connection: sqlite3.Connection, target: PushTarget, stamp: str) -> None:
    connection.execute(
        "INSERT INTO pending_pushes (variant_id, available, change_from, queued_at)"
        " VALUES (?, ?, ?, ?) ON CONFLICT (variant_id) DO UPDATE SET"
        " available = excluded.available, change_from = excluded.change_from",
        (target.variant_id, target.value, target.current, stamp),
    )


def _record_outcomes(
    connection: sqlite3.Connection,
    outcomes: Sequence[_Outcome],
    key: str,
    stamp: str,
) -> None:
    with _transaction(connection):
        connection.executemany(
            "INSERT INTO push_history (variant_id, available, change_from,"
            " idempotency_key, attempted_at, outcome) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    o.target.variant_id,
                    o.target.value,
                    o.target.current,
                    key,
                    stamp,
                    o.outcome,
                )
                for o in outcomes
            ],
        )
        pushed = [o.target for o in outcomes if o.outcome is PushOutcome.PUSHED]
        connection.executemany(
            "DELETE FROM pending_pushes WHERE variant_id = ?",
            [(t.variant_id,) for t in pushed],
        )
        connection.executemany(
            "UPDATE variant_quantities SET last_pushed = ?, last_pushed_at = ?"
            " WHERE variant_id = ?",
            [(t.value, stamp, t.variant_id) for t in pushed],
        )


@contextmanager
def _transaction(connection: sqlite3.Connection) -> Generator[None]:
    connection.execute("BEGIN")
    try:
        yield
    except BaseException:
        connection.execute("ROLLBACK")
        raise
    connection.execute("COMMIT")
