"""Open Shopify orders that ERPNext has not mirrored as Sales Orders yet.

Shopify lowers `available` at the sale; ERPNext reserves stock only once
ecommerce-integrations has made the Sales Order. Until then the unmirrored
quantity is subtracted from sellable, or a push would give the sold unit back.
The match is per line: ecommerce-integrations maps lines through its own
Ecommerce Item rows, which can name another Item than the SKU binding.
"""

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from shopify_erpnext_connector.availability import quantity
from shopify_erpnext_connector.erpnext import ErpNextClient, ErpNextError
from shopify_erpnext_connector.numeric import is_integer, is_number
from shopify_erpnext_connector.problems import Problem, ProblemKind
from shopify_erpnext_connector.shopify import ShopifyClient, ShopifyError

# Shopify's maximum page sizes. Shopify refuses a query whose requested cost
# exceeds 1000 points; at these sizes it reported 277 (API 2026-10).
ORDER_PAGE_SIZE = 250
LINE_ITEM_PAGE_SIZE = 250
# Shopify order IDs per Sales Order request, keeping the `in` filter's URL short.
SALES_ORDER_CHUNK_SIZE = 100
# Far above the observed sale-to-Sales-Order delay of seconds.
UNMIRRORED_GRACE = timedelta(minutes=15)

SALES_ORDER_FIELDS = (
    "name",
    "shopify_order_id",
    "`tabSales Order Item`.item_code",
    "`tabSales Order Item`.stock_qty",
    "`tabSales Order Item`.delivered_qty",
    "`tabSales Order Item`.conversion_factor",
    "`tabSales Order Item`.warehouse",
    "`tabSales Order Item`.delivered_by_supplier",
)
# Sales Order statuses whose lines ERPNext does not count in a Bin's
# reserved quantity (`erpnext.stock.stock_balance.get_reserved_qty`).
UNRESERVED_STATUSES = ["Closed", "On Hold"]

_OPEN_ORDERS_QUERY = f"""\
query OpenOrders($first: Int!, $after: String) {{
  orders(first: $first, after: $after, query: "status:open") {{
    nodes {{
      legacyResourceId
      name
      createdAt
      cancelledAt
      lineItems(first: {LINE_ITEM_PAGE_SIZE}) {{
        nodes {{
          variant {{ id }}
          unfulfilledQuantity
        }}
        pageInfo {{ hasNextPage }}
      }}
    }}
    pageInfo {{ hasNextPage endCursor }}
  }}
}}
"""


@dataclass(frozen=True)
class OrderLine:
    variant_id: str
    unfulfilled: int


@dataclass(frozen=True)
class ShopifyOrder:
    """An open order; `order_id` is the numeric ID Sales Orders store."""

    order_id: str
    name: str
    created_at: datetime
    lines: tuple[OrderLine, ...]


@dataclass(frozen=True)
class SalesOrderLine:
    shopify_order_id: str
    item_code: str
    undelivered: Decimal


@dataclass(frozen=True)
class MatchedLine:
    """One bound variant of one order, its lines aggregated."""

    order_id: str
    order_name: str
    created_at: datetime
    variant_id: str
    item_code: str
    shopify_qty: int
    mirrored_qty: Decimal

    @property
    def unmirrored(self) -> int:
        # Rounded up: a part-mirrored unit is still sold.
        return max(0, math.ceil(self.shopify_qty - self.mirrored_qty))


@dataclass(frozen=True)
class OrderMatch:
    lines: tuple[MatchedLine, ...]

    @property
    def unmirrored_lines(self) -> tuple[MatchedLine, ...]:
        return tuple(line for line in self.lines if line.unmirrored > 0)

    @property
    def unmirrored_by_variant(self) -> dict[str, int]:
        """Unmirrored quantity per variant; variants with none are absent."""
        totals: Counter[str] = Counter()
        for line in self.unmirrored_lines:
            totals[line.variant_id] += line.unmirrored
        return dict(totals)

    def newest_orders_with(self, variant_id: str) -> list[str]:
        """Names of the open orders with the variant unfulfilled, newest first."""
        lines = [line for line in self.lines if line.variant_id == variant_id]
        lines.sort(key=lambda line: line.created_at, reverse=True)
        return [line.order_name for line in lines]


def match_orders(
    orders: Iterable[ShopifyOrder],
    so_lines: Iterable[SalesOrderLine],
    bound: Mapping[str, str],
) -> OrderMatch:
    """Match each order's bound variants to its Sales Order lines.

    `bound` maps variant ID to item code. A variant's lines count as mirrored
    only by Sales Order lines of its bound item; lines of unbound variants
    are ignored.
    """
    mirrored: defaultdict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for so_line in so_lines:
        mirrored[so_line.shopify_order_id, so_line.item_code] += so_line.undelivered

    matched: list[MatchedLine] = []
    for order in orders:
        unfulfilled: Counter[str] = Counter()
        for line in order.lines:
            if line.variant_id in bound:
                unfulfilled[line.variant_id] += line.unfulfilled
        for variant_id, shopify_qty in unfulfilled.items():
            item_code = bound[variant_id]
            matched.append(
                MatchedLine(
                    order_id=order.order_id,
                    order_name=order.name,
                    created_at=order.created_at,
                    variant_id=variant_id,
                    item_code=item_code,
                    shopify_qty=shopify_qty,
                    mirrored_qty=mirrored[order.order_id, item_code],
                )
            )
    return OrderMatch(tuple(matched))


def unmirrored_problems(
    match: OrderMatch, now: datetime, grace: timedelta
) -> list[Problem]:
    """One problem per order still unmirrored `grace` after it was created."""
    by_order: dict[str, list[MatchedLine]] = {}
    for line in match.unmirrored_lines:
        if now - line.created_at >= grace:
            by_order.setdefault(line.order_id, []).append(line)
    problems: list[Problem] = []
    for order_id, lines in by_order.items():
        details = {"Order": lines[0].order_name}
        for line in sorted(lines, key=lambda line: line.item_code):
            details[f"Item {line.item_code}"] = (
                f"Shopify {line.shopify_qty}, Sales Order {_format(line.mirrored_qty)}"
            )
        problems.append(Problem(ProblemKind.UNMIRRORED_ORDER, order_id, details))
    return problems


def _format(value: Decimal) -> str:
    return format(value.normalize(), "f")


def fetch_open_orders(client: ShopifyClient) -> list[ShopifyOrder]:
    """Open, not cancelled orders with their unfulfilled variant lines.

    A line without a variant (a custom item or a deleted variant) is dropped:
    it cannot be bound, so it never lowers a pushed quantity.
    """
    orders: list[ShopifyOrder] = []
    after: str | None = None
    while True:
        data = client.execute(
            _OPEN_ORDERS_QUERY,
            {"first": ORDER_PAGE_SIZE, "after": after},
            idempotent=True,
        )
        connection = data.get("orders")
        if not isinstance(connection, dict):
            raise _unexpected_orders()
        nodes = connection.get("nodes")
        page_info = connection.get("pageInfo")
        if not isinstance(nodes, list) or not isinstance(page_info, dict):
            raise _unexpected_orders()
        for node in nodes:
            order = _parse_order(node)
            if order is not None:
                orders.append(order)
        has_next_page = page_info.get("hasNextPage")
        if not isinstance(has_next_page, bool):
            raise _unexpected_orders()
        if not has_next_page:
            return orders
        cursor = page_info.get("endCursor")
        if not isinstance(cursor, str) or cursor == after:
            raise _unexpected_orders()
        after = cursor


def _parse_order(node: object) -> ShopifyOrder | None:
    """The order, or None if it is cancelled."""
    if not isinstance(node, dict):
        raise _unexpected_orders()
    order_id = node.get("legacyResourceId")
    name = node.get("name")
    created_at = node.get("createdAt")
    cancelled_at = node.get("cancelledAt")
    line_items = node.get("lineItems")
    if (
        not isinstance(order_id, str)
        or not order_id.isdigit()
        or not isinstance(name, str)
        or not isinstance(created_at, str)
        or not (cancelled_at is None or isinstance(cancelled_at, str))
        or not isinstance(line_items, dict)
    ):
        raise _unexpected_orders()
    if cancelled_at is not None:
        return None
    lines = _parse_lines(line_items, name)
    return ShopifyOrder(order_id, name, _parse_time(created_at), lines)


def _parse_lines(line_items: Mapping[str, object], name: str) -> tuple[OrderLine, ...]:
    nodes = line_items.get("nodes")
    page_info = line_items.get("pageInfo")
    if not isinstance(nodes, list) or not isinstance(page_info, dict):
        raise _unexpected_orders()
    more = page_info.get("hasNextPage")
    if not isinstance(more, bool):
        raise _unexpected_orders()
    if more:
        # Counting only the first page would undercount the order's demand.
        raise ShopifyError(
            f"orders: order {name} has more than {LINE_ITEM_PAGE_SIZE} lines"
        )
    lines: list[OrderLine] = []
    for node in nodes:
        if not isinstance(node, dict):
            raise _unexpected_orders()
        variant = node.get("variant")
        unfulfilled = node.get("unfulfilledQuantity")
        if not is_integer(unfulfilled):
            raise _unexpected_orders()
        if variant is None:
            continue
        variant_id = variant.get("id") if isinstance(variant, dict) else None
        if not isinstance(variant_id, str):
            raise _unexpected_orders()
        if unfulfilled > 0:
            lines.append(OrderLine(variant_id, unfulfilled))
    return tuple(lines)


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise _unexpected_orders() from None
    if parsed.tzinfo is None:
        raise _unexpected_orders()
    return parsed


def _unexpected_orders() -> ShopifyError:
    return ShopifyError("orders: unexpected response")


def fetch_sales_order_lines(
    client: ErpNextClient, shopify_order_ids: Sequence[str], warehouse: str
) -> list[SalesOrderLine]:
    """Lines these orders' Sales Orders reserve in `warehouse`'s Bin.

    The conditions are ERPNext's own for a Bin's reserved quantity: a
    submitted Sales Order not Closed or On Hold, a line in that warehouse and
    not delivered by the supplier. Any other line is not subtracted in the
    Bin, so it must stay unmirrored here.
    """
    ids = list(dict.fromkeys(shopify_order_ids))
    lines: list[SalesOrderLine] = []
    for start in range(0, len(ids), SALES_ORDER_CHUNK_SIZE):
        rows = client.get_list(
            "Sales Order",
            fields=SALES_ORDER_FIELDS,
            filters=[
                ("docstatus", "=", 1),
                ("status", "not in", UNRESERVED_STATUSES),
                ("shopify_order_id", "in", ids[start : start + SALES_ORDER_CHUNK_SIZE]),
            ],
        )
        for row in rows:
            line = _parse_sales_order_line(row, warehouse)
            if line is not None:
                lines.append(line)
    return lines


def _parse_sales_order_line(
    row: Mapping[str, object], warehouse: str
) -> SalesOrderLine | None:
    order_id = row.get("shopify_order_id")
    item_code = row.get("item_code")
    line_warehouse = row.get("warehouse")
    by_supplier = row.get("delivered_by_supplier")
    numbers = [row.get(f) for f in ("stock_qty", "delivered_qty", "conversion_factor")]
    if (
        not isinstance(order_id, str)
        or not isinstance(item_code, str)
        or not (line_warehouse is None or isinstance(line_warehouse, str))
        or by_supplier not in (0, 1, None)
    ):
        raise _unexpected_sales_order()
    if line_warehouse != warehouse or by_supplier == 1:
        return None
    stock_qty, delivered_qty, conversion_factor = (_number(n) for n in numbers)
    # A line reserves only its undelivered part, in the stock UOM.
    undelivered = stock_qty - delivered_qty * conversion_factor
    return SalesOrderLine(order_id, item_code, max(Decimal(0), undelivered))


def _number(value: object) -> Decimal:
    if not is_number(value):
        raise _unexpected_sales_order()
    return quantity(value)


def _unexpected_sales_order() -> ErpNextError:
    return ErpNextError("Sales Order list: unexpected row")
