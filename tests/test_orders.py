import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from helpers import ERPNEXT_URL, SHOP_DOMAIN, WAREHOUSE, make_config

from shopify_erpnext_connector.erpnext import ErpNextClient, ErpNextError
from shopify_erpnext_connector.orders import (
    LINE_ITEM_PAGE_SIZE,
    ORDER_PAGE_SIZE,
    SALES_ORDER_CHUNK_SIZE,
    UNMIRRORED_GRACE,
    MatchedLine,
    OrderLine,
    SalesOrderLine,
    ShopifyOrder,
    fetch_open_orders,
    fetch_sales_order_lines,
    match_orders,
    unmirrored_problems,
)
from shopify_erpnext_connector.problems import Problem, ProblemKind
from shopify_erpnext_connector.shopify import ShopifyClient, ShopifyError

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
PAST_GRACE = NOW - UNMIRRORED_GRACE
INSIDE_GRACE = NOW - UNMIRRORED_GRACE + timedelta(seconds=1)

VARIANT_A = "gid://shopify/ProductVariant/1"
VARIANT_B = "gid://shopify/ProductVariant/2"
UNBOUND_VARIANT = "gid://shopify/ProductVariant/9"
ITEM_A = "TEST-ITEM-001"
ITEM_B = "TEST-ITEM-002"
BOUND = {VARIANT_A: ITEM_A, VARIANT_B: ITEM_B}

Handler = Callable[[httpx.Request], httpx.Response]


def order(
    number: int,
    *lines: tuple[str, int],
    created_at: datetime = PAST_GRACE,
) -> ShopifyOrder:
    return ShopifyOrder(
        order_id=str(1000 + number),
        name=f"#{number}",
        created_at=created_at,
        lines=tuple(OrderLine(variant_id, qty) for variant_id, qty in lines),
    )


def so_line(number: int, item_code: str, undelivered: str) -> SalesOrderLine:
    return SalesOrderLine(str(1000 + number), item_code, Decimal(undelivered))


# Matching


def test_order_without_a_sales_order_is_unmirrored() -> None:
    match = match_orders([order(1, (VARIANT_A, 2))], [], BOUND)

    assert match.unmirrored_by_variant == {VARIANT_A: 2}


def test_order_mirrored_by_its_sales_order_is_not_subtracted() -> None:
    match = match_orders([order(1, (VARIANT_A, 2))], [so_line(1, ITEM_A, "2")], BOUND)

    assert match.unmirrored_by_variant == {}
    assert match.unmirrored_lines == ()


def test_sales_order_line_for_another_item_leaves_the_line_unmirrored() -> None:
    match = match_orders([order(1, (VARIANT_A, 1))], [so_line(1, ITEM_B, "1")], BOUND)

    assert match.unmirrored_by_variant == {VARIANT_A: 1}


def test_sales_order_of_another_order_does_not_mirror() -> None:
    match = match_orders([order(1, (VARIANT_A, 1))], [so_line(2, ITEM_A, "1")], BOUND)

    assert match.unmirrored_by_variant == {VARIANT_A: 1}


def test_partially_mirrored_line_subtracts_the_difference() -> None:
    match = match_orders([order(1, (VARIANT_A, 3))], [so_line(1, ITEM_A, "1")], BOUND)

    assert match.unmirrored_by_variant == {VARIANT_A: 2}


def test_fractional_mirrored_quantity_rounds_unmirrored_up() -> None:
    match = match_orders([order(1, (VARIANT_A, 2))], [so_line(1, ITEM_A, "1.5")], BOUND)

    assert match.unmirrored_by_variant == {VARIANT_A: 1}


def test_unmirrored_sums_over_orders() -> None:
    orders = [order(1, (VARIANT_A, 1)), order(2, (VARIANT_A, 2), (VARIANT_B, 1))]

    match = match_orders(orders, [so_line(2, ITEM_B, "1")], BOUND)

    assert match.unmirrored_by_variant == {VARIANT_A: 3}


def test_two_lines_of_one_variant_in_an_order_aggregate() -> None:
    match = match_orders(
        [order(1, (VARIANT_A, 1), (VARIANT_A, 2))], [so_line(1, ITEM_A, "2")], BOUND
    )

    assert match.unmirrored_by_variant == {VARIANT_A: 1}
    assert match.unmirrored_lines == (
        MatchedLine(
            order_id="1001",
            order_name="#1",
            created_at=PAST_GRACE,
            variant_id=VARIANT_A,
            item_code=ITEM_A,
            shopify_qty=3,
            mirrored_qty=Decimal(2),
        ),
    )


def test_two_sales_order_lines_of_one_item_sum() -> None:
    match = match_orders(
        [order(1, (VARIANT_A, 3))],
        [so_line(1, ITEM_A, "1"), so_line(1, ITEM_A, "2")],
        BOUND,
    )

    assert match.unmirrored_by_variant == {}


def test_lines_of_unbound_variants_are_ignored() -> None:
    match = match_orders([order(1, (UNBOUND_VARIANT, 5))], [], BOUND)

    assert match.unmirrored_by_variant == {}
    assert match.lines == ()


def test_newest_orders_with_a_variant_lists_names_newest_first() -> None:
    orders = [
        order(1, (VARIANT_A, 1), created_at=NOW - timedelta(hours=2)),
        order(2, (VARIANT_B, 1), created_at=NOW - timedelta(hours=1)),
        order(3, (VARIANT_A, 1), created_at=NOW),
    ]
    so_lines = [so_line(3, ITEM_A, "1")]

    match = match_orders(orders, so_lines, BOUND)

    assert match.newest_orders_with(VARIANT_A) == ["#3", "#1"]
    assert match.newest_orders_with(UNBOUND_VARIANT) == []


# Unmirrored problems


def test_order_unmirrored_past_grace_raises_one_problem() -> None:
    orders = [order(1, (VARIANT_A, 2), (VARIANT_B, 1))]
    match = match_orders(orders, [so_line(1, ITEM_B, "1")], BOUND)

    problems = unmirrored_problems(match, NOW, UNMIRRORED_GRACE)

    assert problems == [
        Problem(
            ProblemKind.UNMIRRORED_ORDER,
            "1001",
            {"Order": "#1", f"Item {ITEM_A}": "Shopify 2, Sales Order 0"},
        )
    ]
    assert match.unmirrored_by_variant == {VARIANT_A: 2}


def test_problem_lists_every_unmirrored_line_of_the_order() -> None:
    match = match_orders(
        [order(1, (VARIANT_B, 1), (VARIANT_A, 2))],
        [so_line(1, ITEM_B, "1.5"), so_line(1, ITEM_A, "0.5")],
        BOUND,
    )

    (problem,) = unmirrored_problems(match, NOW, UNMIRRORED_GRACE)

    assert list(problem.details.items()) == [
        ("Order", "#1"),
        (f"Item {ITEM_A}", "Shopify 2, Sales Order 0.5"),
    ]


def test_mismatched_item_past_grace_names_the_order() -> None:
    match = match_orders([order(7, (VARIANT_A, 1))], [so_line(7, ITEM_B, "1")], BOUND)

    (problem,) = unmirrored_problems(match, NOW, UNMIRRORED_GRACE)

    assert problem.subject == "1007"
    assert problem.details["Order"] == "#7"


def test_order_inside_grace_raises_no_problem_but_is_subtracted() -> None:
    match = match_orders([order(1, (VARIANT_A, 1), created_at=INSIDE_GRACE)], [], BOUND)

    assert unmirrored_problems(match, NOW, UNMIRRORED_GRACE) == []
    assert match.unmirrored_by_variant == {VARIANT_A: 1}


def test_mirrored_order_past_grace_raises_no_problem() -> None:
    match = match_orders([order(1, (VARIANT_A, 1))], [so_line(1, ITEM_A, "1")], BOUND)

    assert unmirrored_problems(match, NOW, UNMIRRORED_GRACE) == []


# Shopify fetch


class Recorder:
    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    def bodies(self) -> list[dict[str, object]]:
        return [json.loads(request.content) for request in self.requests]

    def filters(self, index: int) -> list[list[object]]:
        return json.loads(self.requests[index].url.params["filters"])


def shopify_client(handler: Handler) -> ShopifyClient:
    http = httpx.Client(
        base_url=f"https://{SHOP_DOMAIN}", transport=httpx.MockTransport(handler)
    )
    return ShopifyClient(make_config(), http, sleep=lambda _: None)


def line_node(variant_id: str | None, unfulfilled: int) -> dict[str, object]:
    variant = None if variant_id is None else {"id": variant_id}
    return {"variant": variant, "unfulfilledQuantity": unfulfilled}


def order_node(
    number: int,
    *lines: dict[str, object],
    created_at: str = "2026-11-12T09:30:00Z",
    cancelled_at: str | None = None,
    more_lines: bool = False,
) -> dict[str, object]:
    return {
        "legacyResourceId": str(1000 + number),
        "name": f"#{number}",
        "createdAt": created_at,
        "cancelledAt": cancelled_at,
        "lineItems": {
            "nodes": list(lines),
            "pageInfo": {"hasNextPage": more_lines},
        },
    }


def orders_page(
    nodes: list[dict[str, object]], end_cursor: str | None
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": {
                "orders": {
                    "nodes": nodes,
                    "pageInfo": {
                        "hasNextPage": end_cursor is not None,
                        "endCursor": end_cursor,
                    },
                }
            }
        },
    )


def test_fetch_open_orders_follows_the_cursor_across_pages() -> None:
    recorder = Recorder(
        orders_page([order_node(1, line_node(VARIANT_A, 2))], "cursor-1"),
        orders_page([order_node(2, line_node(VARIANT_B, 1))], None),
    )

    orders = fetch_open_orders(shopify_client(recorder))

    created = datetime(2026, 11, 12, 9, 30, tzinfo=UTC)
    assert orders == [
        order(1, (VARIANT_A, 2), created_at=created),
        order(2, (VARIANT_B, 1), created_at=created),
    ]
    variables = [body["variables"] for body in recorder.bodies()]
    assert variables == [
        {"first": ORDER_PAGE_SIZE, "after": None},
        {"first": ORDER_PAGE_SIZE, "after": "cursor-1"},
    ]


@pytest.mark.parametrize("has_next_page", [None, "true", 1])
def test_fetch_open_orders_rejects_a_has_next_page_that_is_not_a_boolean(
    has_next_page: object,
) -> None:
    page = {"hasNextPage": has_next_page, "endCursor": "cursor-1"}
    response = httpx.Response(
        200,
        json={"data": {"orders": {"nodes": [], "pageInfo": page}}},
    )

    with pytest.raises(ShopifyError):
        fetch_open_orders(shopify_client(Recorder(response)))


def test_fetch_open_orders_rejects_a_cursor_that_does_not_advance() -> None:
    recorder = Recorder(
        orders_page([order_node(1, line_node(VARIANT_A, 2))], "cursor-1"),
        orders_page([order_node(2, line_node(VARIANT_B, 1))], "cursor-1"),
    )

    with pytest.raises(ShopifyError):
        fetch_open_orders(shopify_client(recorder))


def test_fetch_open_orders_asks_for_open_orders_only() -> None:
    recorder = Recorder(orders_page([], None))

    fetch_open_orders(shopify_client(recorder))

    query = str(recorder.bodies()[0]["query"])
    assert 'query: "status:open"' in query
    assert f"lineItems(first: {LINE_ITEM_PAGE_SIZE})" in query


def test_fetch_open_orders_queries_no_customer_or_price_fields() -> None:
    recorder = Recorder(orders_page([], None))

    fetch_open_orders(shopify_client(recorder))

    query = str(recorder.bodies()[0]["query"]).lower()
    for field in (
        "customer",
        "email",
        "phone",
        "address",
        "price",
        "total",
        "title",
        "note",
    ):
        assert field not in query


def test_fetch_open_orders_keeps_the_creation_time_zone() -> None:
    node = order_node(
        1, line_node(VARIANT_A, 1), created_at="2026-11-12T11:30:00+02:00"
    )
    recorder = Recorder(orders_page([node], None))

    (fetched,) = fetch_open_orders(shopify_client(recorder))

    assert fetched.created_at == datetime(2026, 11, 12, 9, 30, tzinfo=UTC)
    assert fetched.created_at.tzinfo is not None


def test_cancelled_orders_are_skipped() -> None:
    recorder = Recorder(
        orders_page(
            [
                order_node(
                    1,
                    line_node(VARIANT_A, 1),
                    cancelled_at="2026-11-12T09:40:00Z",
                )
            ],
            None,
        )
    )

    assert fetch_open_orders(shopify_client(recorder)) == []


def test_fulfilled_lines_and_lines_without_a_variant_are_dropped() -> None:
    recorder = Recorder(
        orders_page(
            [
                order_node(
                    1,
                    line_node(VARIANT_A, 0),
                    line_node(None, 3),
                    line_node(VARIANT_B, 1),
                )
            ],
            None,
        )
    )

    (fetched,) = fetch_open_orders(shopify_client(recorder))

    assert fetched.lines == (OrderLine(VARIANT_B, 1),)


def test_order_with_more_lines_than_one_page_raises() -> None:
    recorder = Recorder(
        orders_page([order_node(1, line_node(VARIANT_A, 1), more_lines=True)], None)
    )

    with pytest.raises(ShopifyError):
        fetch_open_orders(shopify_client(recorder))


@pytest.mark.parametrize(
    "broken",
    [
        {"orders": None},
        {"orders": {"nodes": [{"name": "#1"}], "pageInfo": {}}},
        {"orders": {"nodes": [], "pageInfo": {"hasNextPage": True, "endCursor": None}}},
        {
            "orders": {
                "nodes": [{**order_node(1), "legacyResourceId": "gid://x"}],
                "pageInfo": {"hasNextPage": False},
            }
        },
        {
            "orders": {
                "nodes": [order_node(1, created_at="2026-11-12T09:30:00")],
                "pageInfo": {"hasNextPage": False},
            }
        },
        {
            "orders": {
                "nodes": [order_node(1, created_at="yesterday")],
                "pageInfo": {"hasNextPage": False},
            }
        },
        {
            "orders": {
                "nodes": [
                    order_node(1, {"variant": {"id": 1}, "unfulfilledQuantity": 1})
                ],
                "pageInfo": {"hasNextPage": False},
            }
        },
        {
            "orders": {
                "nodes": [order_node(1, {"variant": None, "unfulfilledQuantity": "1"})],
                "pageInfo": {"hasNextPage": False},
            }
        },
        {
            "orders": {
                "nodes": [{**order_node(1), "lineItems": None}],
                "pageInfo": {"hasNextPage": False},
            }
        },
    ],
)
def test_fetch_open_orders_rejects_an_unexpected_shape(broken: object) -> None:
    recorder = Recorder(httpx.Response(200, json={"data": broken}))

    with pytest.raises(ShopifyError):
        fetch_open_orders(shopify_client(recorder))


# ERPNext fetch


def erpnext_client(handler: Handler) -> ErpNextClient:
    http = httpx.Client(base_url=ERPNEXT_URL, transport=httpx.MockTransport(handler))
    return ErpNextClient(make_config(), http, sleep=lambda _: None)


def so_row(
    number: int,
    item_code: str,
    stock_qty: float,
    delivered_qty: float = 0,
    conversion_factor: float = 1,
    warehouse: str = WAREHOUSE,
    delivered_by_supplier: int = 0,
) -> dict[str, object]:
    return {
        "name": f"SAL-ORD-TEST-{number:05}",
        "shopify_order_id": str(1000 + number),
        "item_code": item_code,
        "stock_qty": stock_qty,
        "delivered_qty": delivered_qty,
        "conversion_factor": conversion_factor,
        "warehouse": warehouse,
        "delivered_by_supplier": delivered_by_supplier,
    }


def rows(*so_rows: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json={"data": list(so_rows)})


def test_fetch_sales_order_lines_reads_submitted_open_sales_orders() -> None:
    recorder = Recorder(rows(so_row(1, ITEM_A, 2.0)))

    lines = fetch_sales_order_lines(erpnext_client(recorder), ["1001"], WAREHOUSE)

    assert lines == [so_line(1, ITEM_A, "2")]
    request = recorder.requests[0]
    assert request.url.path == "/api/resource/Sales Order"
    assert json.loads(request.url.params["fields"]) == [
        "name",
        "shopify_order_id",
        "`tabSales Order Item`.item_code",
        "`tabSales Order Item`.stock_qty",
        "`tabSales Order Item`.delivered_qty",
        "`tabSales Order Item`.conversion_factor",
        "`tabSales Order Item`.warehouse",
        "`tabSales Order Item`.delivered_by_supplier",
    ]
    assert recorder.filters(0) == [
        ["docstatus", "=", 1],
        ["status", "not in", ["Closed", "On Hold"]],
        ["shopify_order_id", "in", ["1001"]],
    ]


@pytest.mark.parametrize(
    "row",
    [
        so_row(1, "TEST-ITEM-001", 2, warehouse="Other - TEST"),
        so_row(1, "TEST-ITEM-001", 2, delivered_by_supplier=1),
    ],
    ids=["other warehouse", "delivered by supplier"],
)
def test_line_that_erpnext_does_not_reserve_in_the_bin_is_left_out(
    row: dict[str, object],
) -> None:
    recorder = Recorder(rows(row))

    assert fetch_sales_order_lines(erpnext_client(recorder), ["1001"], WAREHOUSE) == []


def test_delivered_quantity_reduces_undelivered() -> None:
    recorder = Recorder(rows(so_row(1, ITEM_A, 5, delivered_qty=2)))

    (line,) = fetch_sales_order_lines(erpnext_client(recorder), ["1001"], WAREHOUSE)

    assert line.undelivered == 3


def test_delivered_quantity_is_converted_to_the_stock_uom() -> None:
    # Two boxes of ten, one box delivered.
    recorder = Recorder(
        rows(so_row(1, ITEM_A, 20, delivered_qty=1, conversion_factor=10))
    )

    (line,) = fetch_sales_order_lines(erpnext_client(recorder), ["1001"], WAREHOUSE)

    assert line.undelivered == 10


def test_over_delivered_line_counts_as_zero() -> None:
    recorder = Recorder(rows(so_row(1, ITEM_A, 1, delivered_qty=2)))

    (line,) = fetch_sales_order_lines(erpnext_client(recorder), ["1001"], WAREHOUSE)

    assert line.undelivered == 0


def test_fetch_sales_order_lines_of_no_orders_sends_no_request() -> None:
    recorder = Recorder()

    assert fetch_sales_order_lines(erpnext_client(recorder), [], WAREHOUSE) == []
    assert recorder.requests == []


def test_fetch_sales_order_lines_splits_a_long_id_list_into_chunks() -> None:
    ids = [str(1000 + n) for n in range(SALES_ORDER_CHUNK_SIZE + 1)]
    recorder = Recorder(rows(so_row(0, ITEM_A, 1)), rows())

    lines = fetch_sales_order_lines(erpnext_client(recorder), ids, WAREHOUSE)

    assert lines == [so_line(0, ITEM_A, "1")]
    assert recorder.filters(0)[2] == ["shopify_order_id", "in", ids[:-1]]
    assert recorder.filters(1)[2] == ["shopify_order_id", "in", ids[-1:]]


@pytest.mark.parametrize(
    "row",
    [
        {**so_row(1, ITEM_A, 1), "shopify_order_id": None},
        {**so_row(1, ITEM_A, 1), "item_code": None},
        {**so_row(1, ITEM_A, 1), "stock_qty": "1"},
        {**so_row(1, ITEM_A, 1), "delivered_qty": False},
        {"name": "SAL-ORD-TEST-00001", "shopify_order_id": "1001"},
    ],
)
def test_fetch_sales_order_lines_rejects_an_unexpected_row(
    row: dict[str, object],
) -> None:
    recorder = Recorder(rows(row))

    with pytest.raises(ErpNextError):
        fetch_sales_order_lines(erpnext_client(recorder), ["1001"], WAREHOUSE)


# Both sides together


def test_open_order_without_a_sales_order_flows_into_a_problem() -> None:
    shopify = shopify_client(
        Recorder(
            orders_page(
                [
                    order_node(1, line_node(VARIANT_A, 2)),
                    order_node(2, line_node(VARIANT_A, 1)),
                ],
                None,
            )
        )
    )
    erpnext_recorder = Recorder(rows(so_row(2, ITEM_A, 1)))
    erpnext = erpnext_client(erpnext_recorder)

    orders = fetch_open_orders(shopify)
    so_lines = fetch_sales_order_lines(erpnext, [o.order_id for o in orders], WAREHOUSE)
    match = match_orders(orders, so_lines, BOUND)
    problems = unmirrored_problems(match, NOW, UNMIRRORED_GRACE)

    requested = erpnext_recorder.filters(0)[2]
    assert requested == ["shopify_order_id", "in", ["1001", "1002"]]
    assert match.unmirrored_by_variant == {VARIANT_A: 2}
    assert [p.subject for p in problems] == ["1001"]
