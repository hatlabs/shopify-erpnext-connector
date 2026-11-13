"""Decision records and the cutover comparison report built from them.

A pass stores one decision record per variant it computed, replacing the
previous one, as JSON in the `decisions` table.
"""

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from shopify_erpnext_connector.availability import (
    ACTUAL_QTY,
    OLD_FORMULA,
    PLANNED_QTY,
    RESERVED_QTY,
    RESERVED_QTY_FOR_PRODUCTION,
    SELLABLE,
    UNMIRRORED,
)
from shopify_erpnext_connector.numeric import is_integer, is_number
from shopify_erpnext_connector.store import transaction, utc_timestamp


@dataclass(frozen=True)
class Decision:
    """The last computation for a variant, as stored in its decision record.

    `explanation` holds the Bin quantities and the values computed from them.
    `note` says when Shopify's value differed from what the connector last
    pushed, that is, it was changed outside the connector.
    """

    item_code: str
    explanation: Mapping[str, float | int]
    shopify_available: int | None
    last_pushed: int | None
    action: str
    note: str | None


@dataclass(frozen=True)
class DatedDecision:
    """A decision and when the pass that made it ran."""

    decided_at: str
    decision: Decision


@dataclass(frozen=True)
class ReportRow:
    """One bound variant in the cutover dry-run comparison."""

    variant_id: str
    item_code: str
    shopify_available: int | None
    computed: int
    old_formula: int
    actual_qty: float
    reserved_qty: float
    reserved_qty_for_production: float
    planned_qty: float
    unmirrored: int


def save_decisions(
    connection: sqlite3.Connection,
    decisions: Sequence[tuple[str, Decision]],
    now: datetime,
) -> None:
    """Store each (variant ID, decision), replacing the variant's previous one."""
    stamp = utc_timestamp(now)
    with transaction(connection):
        connection.executemany(
            "INSERT INTO decisions (variant_id, decided_at, record)"
            " VALUES (?, ?, ?) ON CONFLICT (variant_id) DO UPDATE SET"
            " decided_at = excluded.decided_at, record = excluded.record",
            [(variant_id, stamp, _encode(d)) for variant_id, d in decisions],
        )


def load_decision(connection: sqlite3.Connection, variant_id: str) -> Decision | None:
    dated = load_dated_decision(connection, variant_id)
    return None if dated is None else dated.decision


def load_dated_decision(
    connection: sqlite3.Connection, variant_id: str
) -> DatedDecision | None:
    row = connection.execute(
        "SELECT decided_at, record FROM decisions WHERE variant_id = ?",
        (variant_id,),
    ).fetchone()
    return None if row is None else DatedDecision(row[0], _decode(row[1]))


def comparison_report(connection: sqlite3.Connection) -> list[ReportRow]:
    """The cutover comparison, from the decision records of bound variants.

    A bound variant appears once a pass has computed it; after a completed
    full pass that is every bound variant.
    """
    rows = connection.execute(
        "SELECT d.variant_id, d.record FROM decisions AS d"
        " JOIN bindings AS b ON b.variant_id = d.variant_id ORDER BY b.item_code"
    ).fetchall()
    report: list[ReportRow] = []
    for variant_id, record in rows:
        decision = _decode(record)
        explanation = decision.explanation
        report.append(
            ReportRow(
                variant_id=variant_id,
                item_code=decision.item_code,
                shopify_available=decision.shopify_available,
                computed=int(explanation[SELLABLE]),
                old_formula=int(explanation[OLD_FORMULA]),
                actual_qty=explanation[ACTUAL_QTY],
                reserved_qty=explanation[RESERVED_QTY],
                reserved_qty_for_production=explanation[RESERVED_QTY_FOR_PRODUCTION],
                planned_qty=explanation[PLANNED_QTY],
                unmirrored=int(explanation[UNMIRRORED]),
            )
        )
    return report


def _encode(decision: Decision) -> str:
    return json.dumps(
        {
            "item_code": decision.item_code,
            "explanation": dict(decision.explanation),
            "shopify_available": decision.shopify_available,
            "last_pushed": decision.last_pushed,
            "action": decision.action,
            "note": decision.note,
        }
    )


def _decode(record: str) -> Decision:
    data: object = json.loads(record)
    if not isinstance(data, dict):
        raise ValueError("decision record: not an object")
    explanation = data.get("explanation")
    if not isinstance(explanation, dict) or not all(
        isinstance(k, str) and is_number(v) for k, v in explanation.items()
    ):
        raise ValueError("decision record: malformed explanation")
    item_code = data.get("item_code")
    action = data.get("action")
    note = data.get("note")
    if (
        not isinstance(item_code, str)
        or not isinstance(action, str)
        or not (note is None or isinstance(note, str))
    ):
        raise ValueError("decision record: malformed fields")
    return Decision(
        item_code=item_code,
        explanation={str(k): v for k, v in explanation.items()},
        shopify_available=_optional_int(data.get("shopify_available")),
        last_pushed=_optional_int(data.get("last_pushed")),
        action=action,
        note=note,
    )


def _optional_int(value: object) -> int | None:
    if value is None or is_integer(value):
        return value
    raise ValueError("decision record: malformed quantity")
