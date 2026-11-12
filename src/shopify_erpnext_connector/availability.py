"""Sellable stock of a bound Item from its Bin in the connector's warehouse.

Sellable is what may be offered on Shopify: stock on hand less what Sales
Orders and Work Orders have reserved and less the Shopify orders ERPNext has
not mirrored yet. Planned production is not sellable, but it does cover
demand, so it counts only toward the oversell shortfall.
"""

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal

from shopify_erpnext_connector.erpnext import ErpNextClient, ErpNextError
from shopify_erpnext_connector.numeric import is_number

# Item codes per Bin request, keeping the `in` filter's URL short.
BIN_CHUNK_SIZE = 100

# Keys of `Sellable.explanation`, which decision records store.
ACTUAL_QTY = "actual_qty"
RESERVED_QTY = "reserved_qty"
RESERVED_QTY_FOR_PRODUCTION = "reserved_qty_for_production"
PLANNED_QTY = "planned_qty"
UNMIRRORED = "unmirrored"
SELLABLE = "sellable"
SHORTFALL = "shortfall"
OLD_FORMULA = "old_formula"

BIN_FIELDS = (
    "item_code",
    ACTUAL_QTY,
    RESERVED_QTY,
    RESERVED_QTY_FOR_PRODUCTION,
    PLANNED_QTY,
)


def quantity(value: float) -> Decimal:
    """ERPNext's float as the decimal it was entered as.

    Binary float arithmetic can land just below a whole number, which floor
    would turn into a lost unit.
    """
    return Decimal(str(value))


@dataclass(frozen=True)
class BinQuantities:
    actual: float
    reserved: float
    reserved_for_production: float
    planned: float


ZERO_BIN = BinQuantities(0, 0, 0, 0)


@dataclass(frozen=True)
class Sellable:
    """The quantities of one computation, kept for the decision record.

    `shortfall` is the demand stock and planned production cannot cover,
    0 when there is no oversell. `old_formula` is what ecommerce-integrations
    would push.
    """

    bin: BinQuantities
    unmirrored: int
    sellable: int
    shortfall: int
    old_formula: int

    def explanation(self) -> dict[str, float | int]:
        return {
            ACTUAL_QTY: self.bin.actual,
            RESERVED_QTY: self.bin.reserved,
            RESERVED_QTY_FOR_PRODUCTION: self.bin.reserved_for_production,
            PLANNED_QTY: self.bin.planned,
            UNMIRRORED: self.unmirrored,
            SELLABLE: self.sellable,
            SHORTFALL: self.shortfall,
            OLD_FORMULA: self.old_formula,
        }


def compute_sellable(bin: BinQuantities, unmirrored: int) -> Sellable:
    free = (
        quantity(bin.actual)
        - quantity(bin.reserved)
        - quantity(bin.reserved_for_production)
        - unmirrored
    )
    covered = free + quantity(bin.planned)
    return Sellable(
        bin=bin,
        unmirrored=unmirrored,
        sellable=max(0, math.floor(free)),
        shortfall=max(0, math.ceil(-covered)),
        # ecommerce-integrations pushes cint(actual) - cint(reserved).
        old_formula=int(bin.actual) - int(bin.reserved),
    )


def fetch_bins(
    client: ErpNextClient, item_codes: Sequence[str], warehouse: str
) -> dict[str, BinQuantities]:
    """The Bin of each item in `warehouse`; an item without one has zeros."""
    bins = dict.fromkeys(item_codes, ZERO_BIN)
    codes = list(bins)
    for start in range(0, len(codes), BIN_CHUNK_SIZE):
        rows = client.get_list(
            "Bin",
            fields=BIN_FIELDS,
            filters=[
                ("warehouse", "=", warehouse),
                ("item_code", "in", codes[start : start + BIN_CHUNK_SIZE]),
            ],
        )
        for row in rows:
            code, quantities = _parse_bin(row)
            bins[code] = quantities
    return bins


def _parse_bin(row: Mapping[str, object]) -> tuple[str, BinQuantities]:
    code = row.get("item_code")
    values = [row.get(field) for field in BIN_FIELDS[1:]]
    if not isinstance(code, str):
        raise _unexpected_bin()
    numbers: list[float] = []
    for value in values:
        if not is_number(value):
            raise _unexpected_bin()
        numbers.append(value)
    actual, reserved, reserved_for_production, planned = numbers
    return code, BinQuantities(actual, reserved, reserved_for_production, planned)


def _unexpected_bin() -> ErpNextError:
    return ErpNextError("Bin list: unexpected row")
