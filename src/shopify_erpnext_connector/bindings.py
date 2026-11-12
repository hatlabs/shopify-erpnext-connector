"""Bind Shopify variants to ERPNext Items by SKU = item code.

The push set is the variants of active or unlisted products with inventory
tracking on.
Each is bound to exactly one Item or reported as unbound with a reason; an
unbound variant's Shopify quantity is left alone. Variants outside the push
set are neither bound nor reported.

A bound variant whose SKU changes stays unbound and reported until its SKU is
restored or it leaves the push set: a changed SKU may name a different
product, so the connector does not follow it on its own. A variant that lost
its binding for another reason, such as a disabled Item, keeps no record of
it, so a later SKU change binds like a new variant.
"""

import sqlite3
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from shopify_erpnext_connector.erpnext import ErpNextClient, ErpNextError
from shopify_erpnext_connector.shopify import ShopifyClient, ShopifyError
from shopify_erpnext_connector.store import utc_timestamp

# Shopify's maximum page size; a page costs about three points per variant.
VARIANT_PAGE_SIZE = 250
# Unlisted products are off the storefront but can still be bought by link.
SELLABLE_STATUSES = frozenset({"ACTIVE", "UNLISTED"})
ITEM_FIELDS = ("item_code", "disabled", "has_variants", "is_stock_item")

_VARIANTS_QUERY = """\
query PushSetVariants($first: Int!, $after: String) {
  productVariants(first: $first, after: $after) {
    nodes {
      id
      sku
      inventoryItem { id tracked }
      product { status }
    }
    pageInfo { hasNextPage endCursor }
  }
}
"""


@dataclass(frozen=True)
class ShopifyVariant:
    variant_id: str
    inventory_item_id: str
    sku: str
    product_status: str
    tracked: bool

    @property
    def in_push_set(self) -> bool:
        return self.product_status in SELLABLE_STATUSES and self.tracked


@dataclass(frozen=True)
class ErpItem:
    item_code: str
    disabled: bool
    has_variants: bool
    is_stock_item: bool


@dataclass(frozen=True)
class Binding:
    variant_id: str
    inventory_item_id: str
    item_code: str


class UnboundKind(StrEnum):
    EMPTY_SKU = "empty_sku"
    NO_ITEM = "no_item"
    ITEM_DISABLED = "item_disabled"
    TEMPLATE_ITEM = "template_item"
    NON_STOCK_ITEM = "non_stock_item"
    DUPLICATE_SKU = "duplicate_sku"
    NEAR_MATCH = "near_match"
    SKU_CHANGED = "sku_changed"


@dataclass(frozen=True)
class UnboundVariant:
    """A push-set variant left unbound, and why.

    `item_codes` holds the Item involved: the matching Item for the Item
    checks, the candidates for a near match, the previously bound Item for a
    SKU change. `other_variant_ids` holds the variants sharing a duplicate SKU.
    """

    kind: UnboundKind
    variant_id: str
    sku: str
    item_codes: tuple[str, ...] = ()
    other_variant_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class BindingResult:
    bindings: tuple[Binding, ...]
    unbound: tuple[UnboundVariant, ...]


def compute_bindings(
    variants: Iterable[ShopifyVariant],
    items: Iterable[ErpItem],
    previous: Mapping[str, str],
) -> BindingResult:
    """Bind the push set; `previous` maps variant ID to its last bound item code."""
    push_set = sorted(
        (v for v in variants if v.in_push_set), key=lambda v: v.variant_id
    )
    by_code = {i.item_code: i for i in items}
    by_normalized: dict[str, list[str]] = {}
    for code in sorted(by_code):
        by_normalized.setdefault(_normalize(code), []).append(code)
    sku_counts = Counter(v.sku for v in push_set if not _is_empty(v.sku))

    bindings: list[Binding] = []
    unbound: list[UnboundVariant] = []
    for v in push_set:
        bound_code = previous.get(v.variant_id)
        if bound_code is not None and bound_code != v.sku:
            unbound.append(_unbound(UnboundKind.SKU_CHANGED, v, (bound_code,)))
        elif _is_empty(v.sku):
            unbound.append(_unbound(UnboundKind.EMPTY_SKU, v))
        elif sku_counts[v.sku] > 1:
            others = tuple(
                o.variant_id for o in push_set if o.sku == v.sku and o is not v
            )
            unbound.append(
                UnboundVariant(
                    UnboundKind.DUPLICATE_SKU,
                    v.variant_id,
                    v.sku,
                    other_variant_ids=others,
                )
            )
        elif (match := by_code.get(v.sku)) is None:
            candidates = tuple(by_normalized.get(_normalize(v.sku), ()))
            kind = UnboundKind.NEAR_MATCH if candidates else UnboundKind.NO_ITEM
            unbound.append(_unbound(kind, v, candidates))
        elif (kind := _item_unusable(match)) is not None:
            unbound.append(_unbound(kind, v, (match.item_code,)))
        else:
            bindings.append(Binding(v.variant_id, v.inventory_item_id, v.sku))
    return BindingResult(tuple(bindings), tuple(unbound))


def _is_empty(sku: str) -> bool:
    return not sku.strip()


def _normalize(code: str) -> str:
    return "".join(code.split()).casefold()


def _item_unusable(item: ErpItem) -> UnboundKind | None:
    if item.disabled:
        return UnboundKind.ITEM_DISABLED
    if item.has_variants:
        return UnboundKind.TEMPLATE_ITEM
    if not item.is_stock_item:
        return UnboundKind.NON_STOCK_ITEM
    return None


def _unbound(
    kind: UnboundKind, variant: ShopifyVariant, item_codes: tuple[str, ...] = ()
) -> UnboundVariant:
    return UnboundVariant(kind, variant.variant_id, variant.sku, item_codes)


def fetch_variants(client: ShopifyClient) -> list[ShopifyVariant]:
    """Every product variant, with the fields that decide the push set."""
    variants: list[ShopifyVariant] = []
    after: str | None = None
    while True:
        data = client.execute(
            _VARIANTS_QUERY,
            {"first": VARIANT_PAGE_SIZE, "after": after},
            idempotent=True,
        )
        connection = data.get("productVariants")
        if not isinstance(connection, dict):
            raise _unexpected_variants()
        nodes = connection.get("nodes")
        page_info = connection.get("pageInfo")
        if not isinstance(nodes, list) or not isinstance(page_info, dict):
            raise _unexpected_variants()
        variants.extend(_parse_variant(node) for node in nodes)
        # A short or repeated listing would drop or double-count bindings on
        # save, so anything but a clean page sequence fails the refresh.
        if len({v.variant_id for v in variants}) != len(variants):
            raise _unexpected_variants()
        has_next_page = page_info.get("hasNextPage")
        if not isinstance(has_next_page, bool):
            raise _unexpected_variants()
        if not has_next_page:
            return variants
        cursor = page_info.get("endCursor")
        if not isinstance(cursor, str) or cursor == after:
            raise _unexpected_variants()
        after = cursor


def _parse_variant(node: object) -> ShopifyVariant:
    if not isinstance(node, dict):
        raise _unexpected_variants()
    variant_id = node.get("id")
    sku = node.get("sku")
    inventory_item = node.get("inventoryItem")
    product = node.get("product")
    if not isinstance(inventory_item, dict) or not isinstance(product, dict):
        raise _unexpected_variants()
    inventory_item_id = inventory_item.get("id")
    tracked = inventory_item.get("tracked")
    status = product.get("status")
    if (
        not isinstance(variant_id, str)
        or not (sku is None or isinstance(sku, str))
        or not isinstance(inventory_item_id, str)
        or not isinstance(tracked, bool)
        or not isinstance(status, str)
    ):
        raise _unexpected_variants()
    return ShopifyVariant(variant_id, inventory_item_id, sku or "", status, tracked)


def _unexpected_variants() -> ShopifyError:
    return ShopifyError("productVariants: unexpected response")


def fetch_items(client: ErpNextClient) -> list[ErpItem]:
    """Every Item, with the fields the binding rules read."""
    return [_parse_item(row) for row in client.get_list("Item", fields=ITEM_FIELDS)]


def _parse_item(row: Mapping[str, object]) -> ErpItem:
    code = row.get("item_code")
    flags = [row.get(field) for field in ITEM_FIELDS[1:]]
    if not isinstance(code, str) or not all(f in (0, 1) for f in flags):
        raise ErpNextError("Item list: unexpected row")
    disabled, has_variants, is_stock_item = (bool(f) for f in flags)
    return ErpItem(code, disabled, has_variants, is_stock_item)


def load_previous(connection: sqlite3.Connection) -> dict[str, str]:
    """Variant ID to the item code it was last bound to, held changes included."""
    rows = connection.execute(
        "SELECT variant_id, item_code FROM bindings"
        " UNION ALL SELECT variant_id, bound_item_code FROM sku_changes"
    )
    return dict(rows.fetchall())


def load_bindings(connection: sqlite3.Connection) -> list[Binding]:
    """The stored bindings, ordered by item code."""
    rows = connection.execute(
        "SELECT variant_id, inventory_item_id, item_code FROM bindings"
        " ORDER BY item_code"
    ).fetchall()
    return [Binding(*row) for row in rows]


def save_bindings(
    connection: sqlite3.Connection, result: BindingResult, now: datetime
) -> None:
    """Make the stored bindings and held SKU changes those of `result`.

    An unchanged binding keeps its `bound_at`, and a held change its
    `detected_at`. Rows of variants not bound, or not changed, are removed.
    """
    stamp = utc_timestamp(now)
    wanted = {(b.variant_id, b.inventory_item_id, b.item_code) for b in result.bindings}
    changes = [u for u in result.unbound if u.kind is UnboundKind.SKU_CHANGED]
    connection.execute("BEGIN")
    try:
        stored = connection.execute(
            "SELECT variant_id, inventory_item_id, item_code FROM bindings"
        ).fetchall()
        connection.executemany(
            "DELETE FROM bindings WHERE variant_id = ?",
            [(row[0],) for row in stored if row not in wanted],
        )
        connection.executemany(
            "INSERT INTO bindings (variant_id, inventory_item_id, item_code, bound_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT (variant_id) DO NOTHING",
            [(*row, stamp) for row in sorted(wanted)],
        )
        _replace_changes(connection, changes, stamp)
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def _replace_changes(
    connection: sqlite3.Connection, changes: Sequence[UnboundVariant], stamp: str
) -> None:
    held = [u.variant_id for u in changes]
    placeholders = ", ".join("?" * len(held))
    connection.execute(
        f"DELETE FROM sku_changes WHERE variant_id NOT IN ({placeholders})", held
    )
    connection.executemany(
        "INSERT INTO sku_changes (variant_id, bound_item_code, detected_at)"
        " VALUES (?, ?, ?) ON CONFLICT (variant_id) DO NOTHING",
        [(u.variant_id, u.item_codes[0], stamp) for u in changes],
    )


def refresh_bindings(
    connection: sqlite3.Connection,
    shopify: ShopifyClient,
    erpnext: ErpNextClient,
    now: datetime,
) -> BindingResult:
    """Read both sides, recompute the bindings and store them."""
    result = compute_bindings(
        fetch_variants(shopify), fetch_items(erpnext), load_previous(connection)
    )
    save_bindings(connection, result, now)
    return result
