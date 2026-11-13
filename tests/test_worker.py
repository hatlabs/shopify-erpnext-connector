import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from helpers import ERPNEXT_URL, LOCATION_ID, SHOP_DOMAIN, WAREHOUSE, make_config

from shopify_erpnext_connector.api import ENVIRONMENT_HEADER, ApiServer
from shopify_erpnext_connector.bindings import bound_item_code
from shopify_erpnext_connector.config import ApiConfig, Config, Secret
from shopify_erpnext_connector.decisions import (
    Decision,
    ReportRow,
    comparison_report,
    load_decision,
)
from shopify_erpnext_connector.erpnext import ErpNextClient
from shopify_erpnext_connector.notify import MAKE_METHOD
from shopify_erpnext_connector.orders import UNMIRRORED_GRACE
from shopify_erpnext_connector.poller import dirty_items
from shopify_erpnext_connector.problems import (
    ProblemKind,
    StoredProblem,
    get_problem,
    open_problems,
)
from shopify_erpnext_connector.shopify import CHANGE_FROM_QUANTITY_STALE, ShopifyClient
from shopify_erpnext_connector.store import (
    PUSH_HISTORY_RETENTION,
    open_store,
    set_dry_run,
    utc_timestamp,
)
from shopify_erpnext_connector.worker import (
    BINDING_PROBLEM_KINDS,
    EMAIL_CHECK_INTERVAL,
    FULL_PASS_INTERVAL,
    HELD_ACTION,
    MAX_RETRY_BACKOFF,
    SALE_SETTLE_HOLD,
    Worker,
)

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
SECOND = timedelta(seconds=1)
STALE_AFTER = make_config().worker.push_stale_after
POLL = make_config().worker.poll_interval
REJECTED_CODE = "INVALID_INVENTORY_ITEM"

SentEntry = tuple[str, int, int | None]


def variant_id(n: int) -> str:
    return f"gid://shopify/ProductVariant/{n}"


def inventory_item_id(n: int) -> str:
    return f"gid://shopify/InventoryItem/{n}"


def item_code(n: int) -> str:
    return f"TEST-ITEM-{n:03}"


def order_id(n: int) -> str:
    return str(1000 + n)


def graphql(data: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json={"data": data})


class FakeShopify:
    """Variants, inventory levels and open orders of one shop at one location.

    Mutations check `changeFromQuantity` against the current level and apply
    all entries or none, as Shopify does.
    """

    def __init__(self) -> None:
        self.variants: dict[int, dict[str, object]] = {}
        self.levels: dict[str, tuple[int | None, str]] = {}
        self.orders: list[dict[str, object]] = []
        self.rejects: dict[str, str] = {}
        self.down = False
        self.mutations_down = False
        self.orders_down = False
        self.on_mutation: Callable[[], None] | None = None
        self.after_order_read: Callable[[], None] | None = None
        self.mutations: list[list[SentEntry]] = []
        self.variant_reads = 0
        self.order_reads = 0

    def add_variant(
        self, n: int, sku: str, available: int | None = 0, policy: str = "DENY"
    ) -> None:
        self.variants[n] = {"sku": sku, "status": "ACTIVE", "tracked": True}
        self.levels[inventory_item_id(n)] = (available, policy)

    def delete_variant(self, n: int) -> None:
        del self.variants[n]
        del self.levels[inventory_item_id(n)]

    def set_sku(self, n: int, sku: str) -> None:
        self.variants[n]["sku"] = sku

    def set_available(self, n: int, available: int) -> None:
        _, policy = self.levels[inventory_item_id(n)]
        self.levels[inventory_item_id(n)] = (available, policy)

    def available(self, n: int) -> int | None:
        return self.levels[inventory_item_id(n)][0]

    def add_order(
        self, n: int, lines: list[tuple[int, int]], created_at: datetime
    ) -> None:
        self.orders.append(
            {
                "legacyResourceId": order_id(n),
                "name": f"#{n}",
                "createdAt": created_at.isoformat(),
                "cancelledAt": None,
                "lineItems": {
                    "nodes": [
                        {"variant": {"id": variant_id(v)}, "unfulfilledQuantity": q}
                        for v, q in lines
                    ],
                    "pageInfo": {"hasNextPage": False},
                },
            }
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("synthetic outage", request=request)
        body = json.loads(request.content)
        query = body["query"]
        variables = body["variables"]
        if "inventorySetQuantities" in query:
            if self.mutations_down:
                raise httpx.ReadTimeout("synthetic timeout", request=request)
            return self._mutate(variables["input"]["quantities"])
        if "productVariants" in query:
            self.variant_reads += 1
            return self._variants()
        if "orders(" in query:
            self.order_reads += 1
            if self.orders_down:
                return httpx.Response(500, json={})
            response = graphql(
                {
                    "orders": {
                        "nodes": self.orders,
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            )
            if self.after_order_read is not None:
                self.after_order_read()
            return response
        if "InventoryLevels" in query:
            assert variables["locationId"] == LOCATION_ID
            return graphql({"nodes": [self._level(i) for i in variables["ids"]]})
        raise AssertionError(f"unexpected query: {query}")

    def _variants(self) -> httpx.Response:
        nodes = [
            {
                "id": variant_id(n),
                "sku": v["sku"],
                "inventoryItem": {"id": inventory_item_id(n), "tracked": v["tracked"]},
                "product": {"status": v["status"]},
            }
            for n, v in self.variants.items()
        ]
        return graphql(
            {
                "productVariants": {
                    "nodes": nodes,
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                }
            }
        )

    def _level(self, item_id: str) -> dict[str, object] | None:
        if item_id not in self.levels:
            return None
        available, policy = self.levels[item_id]
        level = (
            None
            if available is None
            else {"quantities": [{"name": "available", "quantity": available}]}
        )
        return {
            "id": item_id,
            "inventoryLevel": level,
            "variant": {"inventoryPolicy": policy},
        }

    def _mutate(self, quantities: list[dict[str, object]]) -> httpx.Response:
        if self.on_mutation is not None:
            self.on_mutation()
        entries: list[SentEntry] = []
        errors: list[dict[str, object]] = []
        for index, q in enumerate(quantities):
            item = str(q["inventoryItemId"])
            quantity = q["quantity"]
            change_from = q["changeFromQuantity"]
            assert isinstance(quantity, int)
            assert change_from is None or isinstance(change_from, int)
            entries.append((item, quantity, change_from))
            if item in self.rejects:
                errors.append(self._error(self.rejects[item], index))
            elif change_from != self.levels[item][0]:
                errors.append(self._error(CHANGE_FROM_QUANTITY_STALE, index))
        self.mutations.append(entries)
        if not errors:
            for item, quantity, _ in entries:
                self.levels[item] = (quantity, self.levels[item][1])
        group = None if errors else {"id": "gid://shopify/InventoryAdjustmentGroup/1"}
        return graphql(
            {
                "inventorySetQuantities": {
                    "inventoryAdjustmentGroup": group,
                    "userErrors": errors,
                }
            }
        )

    @staticmethod
    def _error(code: str, index: int) -> dict[str, object]:
        return {
            "code": code,
            "field": ["input", "quantities", str(index), "changeFromQuantity"],
            "message": "synthetic user error message",
        }


class FakeErpNext:
    """Items, Bins of one warehouse, Sales Orders and problem emails.

    `failing` names the reads that answer 500: "Bin poll", "Bin", "Item" or
    "Sales Order".
    """

    def __init__(self) -> None:
        self.items: list[dict[str, object]] = []
        self.bins: dict[str, dict[str, float | str]] = {}
        self.sales_order_lines: list[dict[str, object]] = []
        self.communications: dict[str, dict[str, object]] = {}
        self.make_calls = 0
        self.failing: set[str] = set()
        self.on_make: Callable[[], None] | None = None
        self._stamp = 0

    def add_item(
        self,
        code: str,
        *,
        disabled: int = 0,
        has_variants: int = 0,
        is_stock_item: int = 1,
    ) -> None:
        self.items.append(
            {
                "item_code": code,
                "disabled": disabled,
                "has_variants": has_variants,
                "is_stock_item": is_stock_item,
            }
        )

    def set_bin(
        self,
        code: str,
        *,
        actual: float = 0,
        reserved: float = 0,
        reserved_for_production: float = 0,
        planned: float = 0,
    ) -> None:
        self._stamp += 1
        self.bins[code] = {
            "actual_qty": actual,
            "reserved_qty": reserved,
            "reserved_qty_for_production": reserved_for_production,
            "planned_qty": planned,
            "modified": f"2026-11-12 09:00:00.{self._stamp:06}",
        }

    def latest_modified(self) -> str:
        return max(str(b["modified"]) for b in self.bins.values())

    def add_sales_order_line(self, order: int, code: str, qty: float) -> None:
        self.sales_order_lines.append(
            {
                "name": f"SAL-ORD-TEST-{order:05}",
                "shopify_order_id": order_id(order),
                "item_code": code,
                "stock_qty": qty,
                "delivered_qty": 0,
                "conversion_factor": 1,
                "warehouse": WAREHOUSE,
                "delivered_by_supplier": 0,
            }
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/api/method/{MAKE_METHOD}":
            return self._make()
        if path.startswith("/api/resource/Communication/"):
            name = path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"data": self.communications[name]})
        doctype = path.removeprefix("/api/resource/").replace("%20", " ")
        fields = json.loads(request.url.params["fields"])
        filters = json.loads(request.url.params["filters"])
        if doctype == "Bin":
            assert ["warehouse", "=", WAREHOUSE] in filters
            if "modified" in fields:
                return self._answer("Bin poll", self._bin_changes(filters))
            codes = _filter_values(filters, "item_code")
            return self._answer(
                "Bin",
                [
                    {"item_code": c, **{k: v for k, v in b.items() if k != "modified"}}
                    for c, b in self.bins.items()
                    if c in codes
                ],
            )
        if doctype == "Item":
            return self._answer("Item", self.items)
        if doctype == "Sales Order":
            ids = _filter_values(filters, "shopify_order_id")
            return self._answer(
                "Sales Order",
                [r for r in self.sales_order_lines if r["shopify_order_id"] in ids],
            )
        raise AssertionError(f"unexpected request: {path}")

    def _bin_changes(self, filters: list[list[object]]) -> list[dict[str, object]]:
        since = next((str(f[2]) for f in filters if f[0] == "modified"), "")
        changes = sorted(
            (str(b["modified"]), c)
            for c, b in self.bins.items()
            if str(b["modified"]) >= since
        )
        return [{"item_code": c, "modified": m} for m, c in changes]

    def _answer(self, read: str, rows: list[dict[str, object]]) -> httpx.Response:
        if read in self.failing:
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"data": rows})

    def _make(self) -> httpx.Response:
        if self.on_make is not None:
            self.on_make()
        self.make_calls += 1
        name = f"comm{self.make_calls:04}"
        self.communications[name] = {"name": name, "delivery_status": "Sent"}
        return httpx.Response(200, json={"message": {"name": name}})


def _filter_values(filters: list[list[object]], field: str) -> list[str]:
    for name, operator, value in filters:
        if name == field and operator == "in":
            assert isinstance(value, list)
            return [str(v) for v in value]
    raise AssertionError(f"no '{field} in' filter")


class World:
    def __init__(self, path: Path, config: Config) -> None:
        self.path = path
        self.config = config
        self.store = open_store(path)
        set_dry_run(self.store, False)
        self.shopify = FakeShopify()
        self.erpnext = FakeErpNext()
        self.worker = self.new_worker()

    def new_worker(self) -> Worker:
        shop_http = httpx.Client(
            base_url=f"https://{SHOP_DOMAIN}",
            transport=httpx.MockTransport(self.shopify),
        )
        erp_http = httpx.Client(
            base_url=ERPNEXT_URL, transport=httpx.MockTransport(self.erpnext)
        )
        return Worker(
            self.config,
            self.store,
            ShopifyClient(self.config, shop_http, sleep=lambda _: None),
            ErpNextClient(self.config, erp_http, sleep=lambda _: None),
            clock=lambda: NOW,
        )

    def bind(
        self,
        n: int,
        *,
        actual: float = 0,
        reserved: float = 0,
        planned: float = 0,
        available: int | None = 0,
        policy: str = "DENY",
    ) -> None:
        self.shopify.add_variant(n, item_code(n), available, policy)
        self.erpnext.add_item(item_code(n))
        self.erpnext.set_bin(
            item_code(n), actual=actual, reserved=reserved, planned=planned
        )

    def problem(self, kind: ProblemKind, subject: str) -> StoredProblem | None:
        stored = get_problem(self.store, kind, subject)
        return stored if stored is not None and stored.resolved_at is None else None

    def open_of(self, kind: ProblemKind) -> list[str]:
        return [p.subject for p in open_problems(self.store) if p.kind == kind]

    def watermark(self) -> str:
        return self.store.execute("SELECT bin_modified FROM watermark").fetchone()[0]


@pytest.fixture
def world(tmp_path: Path) -> Iterator[World]:
    world = World(tmp_path / "connector.sqlite3", make_config())
    yield world
    world.store.close()


def emailing_world(tmp_path: Path) -> World:
    return World(tmp_path / "connector.sqlite3", make_config(email_enabled=True))


def decision(world: World, n: int) -> Decision:
    found = load_decision(world.store, variant_id(n))
    assert found is not None
    return found


# Computing and pushing


def test_sellable_quantity_is_pushed(world: World) -> None:
    world.bind(1, actual=5, reserved=2, available=0)

    world.worker.run_pass(NOW)

    assert world.shopify.mutations == [[(inventory_item_id(1), 3, 0)]]
    assert world.shopify.available(1) == 3
    assert dirty_items(world.store) == {}


def test_decision_record_explains_the_push(world: World) -> None:
    world.bind(1, actual=5, reserved=2, available=0)

    world.worker.run_pass(NOW)

    assert decision(world, 1) == Decision(
        item_code=item_code(1),
        explanation={
            "actual_qty": 5,
            "reserved_qty": 2,
            "reserved_qty_for_production": 0,
            "planned_qty": 0,
            "unmirrored": 0,
            "sellable": 3,
            "shortfall": 0,
            "old_formula": 3,
        },
        shopify_available=0,
        last_pushed=None,
        action="pushed",
        note=None,
    )


def test_a_bin_change_on_one_item_pushes_only_its_variant(world: World) -> None:
    world.bind(1, actual=5, available=5)
    world.bind(2, actual=4, available=4)
    world.worker.run_pass(NOW)
    assert world.shopify.mutations == []

    world.erpnext.set_bin(item_code(1), actual=6)
    world.worker.run_pass(NOW + 5 * SECOND)

    assert world.shopify.mutations == [[(inventory_item_id(1), 6, 5)]]


def test_a_value_equal_to_shopifys_is_not_pushed(world: World) -> None:
    world.bind(1, actual=3, available=3)

    world.worker.run_pass(NOW)

    assert world.shopify.mutations == []
    assert decision(world, 1).action == "unchanged"
    assert dirty_items(world.store) == {}


def test_reserved_beyond_actual_pushes_zero(world: World) -> None:
    world.bind(1, actual=2, reserved=5, available=2)

    world.worker.run_pass(NOW)

    assert world.shopify.mutations == [[(inventory_item_id(1), 0, 2)]]


def test_an_unmirrored_order_lowers_the_value_until_its_sales_order_appears(
    world: World,
) -> None:
    world.bind(1, actual=5, available=0)
    world.shopify.add_order(1, [(1, 2)], created_at=NOW - SECOND)

    world.worker.run_pass(NOW)
    assert world.shopify.mutations == [[(inventory_item_id(1), 3, 0)]]

    world.erpnext.add_sales_order_line(1, item_code(1), 2)
    world.erpnext.set_bin(item_code(1), actual=5, reserved=2)
    world.worker.run_pass(NOW + 5 * SECOND)

    assert len(world.shopify.mutations) == 1
    assert decision(world, 1).explanation["unmirrored"] == 0
    assert decision(world, 1).action == "unchanged"


def test_dirty_items_without_a_binding_are_cleared_and_left_alone(
    world: World,
) -> None:
    world.shopify.add_variant(2, "TEST-MISSING", available=7)
    world.erpnext.add_item(item_code(99))
    world.erpnext.set_bin(item_code(99), actual=4)

    world.worker.run_pass(NOW)

    assert dirty_items(world.store) == {}
    assert world.shopify.mutations == []
    assert world.shopify.available(2) == 7


def test_a_stale_level_is_re_read_and_pushed_once_more(world: World) -> None:
    world.bind(1, actual=3, available=10)

    def hand_edit_once() -> None:
        world.shopify.set_available(1, 9)
        world.shopify.on_mutation = None

    world.shopify.on_mutation = hand_edit_once

    world.worker.run_pass(NOW)

    assert world.shopify.mutations == [
        [(inventory_item_id(1), 3, 10)],
        [(inventory_item_id(1), 3, 9)],
    ]
    assert world.shopify.available(1) == 3
    assert decision(world, 1).action == "pushed"
    assert dirty_items(world.store) == {}


def test_a_sale_after_the_orders_read_is_not_put_back_on_sale(
    world: World,
) -> None:
    world.bind(1, actual=5, available=5)
    world.worker.run_pass(NOW)

    def sell_one_once() -> None:
        world.shopify.set_available(1, 4)
        world.shopify.add_order(1, [(1, 1)], created_at=NOW)
        world.shopify.after_order_read = None

    world.shopify.after_order_read = sell_one_once
    world.erpnext.set_bin(item_code(1), actual=5)
    world.worker.run_pass(NOW + 5 * SECOND)

    assert world.shopify.available(1) == 4
    assert decision(world, 1).action == "unchanged"


def sold_one_before_the_orders_search_shows_it(world: World) -> None:
    """Push 5, then sell one in Shopify; the order is not yet searchable."""
    world.bind(1, actual=5, available=0)
    world.worker.run_pass(NOW)
    world.shopify.set_available(1, 4)
    world.erpnext.set_bin(item_code(1), actual=5)
    world.shopify.mutations.clear()


def test_a_sale_missing_from_the_orders_search_holds_the_push(
    world: World,
) -> None:
    sold_one_before_the_orders_search_shows_it(world)

    world.worker.run_pass(NOW + POLL)

    assert world.shopify.mutations == []
    assert decision(world, 1).action == HELD_ACTION
    assert item_code(1) in dirty_items(world.store)

    world.shopify.add_order(1, [(1, 1)], created_at=NOW + POLL)
    world.worker.run_pass(NOW + 2 * POLL)

    assert world.shopify.mutations == []
    assert decision(world, 1).action == "unchanged"
    assert dirty_items(world.store) == {}


def test_a_sale_is_held_on_a_variant_that_never_needed_a_push(
    world: World,
) -> None:
    world.bind(1, actual=5, available=5)
    world.worker.run_pass(NOW)
    assert world.shopify.mutations == []
    world.shopify.set_available(1, 4)
    world.erpnext.set_bin(item_code(1), actual=5)

    world.worker.run_pass(NOW + POLL)

    assert world.shopify.mutations == []
    assert decision(world, 1).action == HELD_ACTION


def test_a_held_sale_whose_order_appears_pushes_the_lower_value(
    world: World,
) -> None:
    sold_one_before_the_orders_search_shows_it(world)
    world.worker.run_pass(NOW + POLL)

    world.shopify.add_order(1, [(1, 2)], created_at=NOW + POLL)
    world.worker.run_pass(NOW + 2 * POLL)

    assert world.shopify.mutations == [[(inventory_item_id(1), 3, 4)]]


def test_a_hand_decrease_without_an_order_is_pushed_after_the_hold(
    world: World,
) -> None:
    sold_one_before_the_orders_search_shows_it(world)
    held_at = NOW + POLL
    world.worker.run_pass(held_at)
    world.worker.run_pass(held_at + SALE_SETTLE_HOLD - SECOND)
    assert world.shopify.mutations == []

    world.worker.run_pass(held_at + SALE_SETTLE_HOLD)

    assert world.shopify.mutations == [[(inventory_item_id(1), 5, 4)]]
    assert decision(world, 1).action == "pushed"
    assert dirty_items(world.store) == {}


def test_a_level_stale_twice_stays_dirty_for_the_next_cycle(world: World) -> None:
    world.bind(1, actual=3, available=10)

    def sell_one() -> None:
        current = world.shopify.available(1)
        assert current is not None
        world.shopify.set_available(1, current - 1)

    world.shopify.on_mutation = sell_one

    world.worker.run_pass(NOW)

    assert len(world.shopify.mutations) == 2
    assert dirty_items(world.store)[item_code(1)].attempts == 1
    assert decision(world, 1).action == "stale"


def test_a_failed_re_read_for_a_stale_retry_keeps_the_other_outcomes(
    world: World,
) -> None:
    world.bind(1, actual=3, available=10)
    world.bind(2, actual=4, available=0)

    def hand_edit_then_erpnext_fails() -> None:
        if world.shopify.available(1) == 10:
            world.shopify.set_available(1, 9)
            world.erpnext.failing.add("Bin")

    world.shopify.on_mutation = hand_edit_then_erpnext_fails

    world.worker.run_pass(NOW)

    assert world.shopify.available(2) == 4
    assert decision(world, 2).action == "pushed"
    assert list(dirty_items(world.store)) == [item_code(1)]
    assert world.worker.last_pass_at is None


def test_a_failing_variant_keeps_retrying_while_others_push(world: World) -> None:
    world.bind(1, actual=3, available=0)
    world.bind(2, actual=3, available=0)
    world.shopify.rejects[inventory_item_id(1)] = REJECTED_CODE

    world.worker.run_pass(NOW)

    assert world.shopify.available(2) == 3
    assert dirty_items(world.store)[item_code(1)].attempts == 1
    assert decision(world, 1).action == f"failed: {REJECTED_CODE}"

    world.erpnext.set_bin(item_code(2), actual=4)
    world.worker.run_pass(NOW + 5 * SECOND)

    assert world.shopify.available(2) == 4
    assert dirty_items(world.store)[item_code(1)].attempts == 2
    assert world.watermark() == world.erpnext.latest_modified()


def sent_items(world: World) -> set[str]:
    return {item for batch in world.shopify.mutations for item, _, _ in batch}


def test_a_failing_variant_is_retried_at_growing_intervals(world: World) -> None:
    world.bind(1, actual=3, available=0)
    world.bind(2, actual=3, available=0)
    world.shopify.rejects[inventory_item_id(1)] = REJECTED_CODE
    retried_at: list[int] = []

    for step in range(8):
        world.shopify.mutations.clear()
        world.erpnext.set_bin(item_code(2), actual=10 + step)
        world.worker.run_pass(NOW + step * POLL)
        assert world.shopify.available(2) == 10 + step
        if inventory_item_id(1) in sent_items(world):
            retried_at.append(step)

    assert retried_at == [0, 1, 3, 7]
    assert dirty_items(world.store)[item_code(1)].attempts == 4


def test_a_variant_waiting_on_its_backoff_does_not_read_orders(
    world: World,
) -> None:
    world.bind(1, actual=3, available=0)
    world.shopify.rejects[inventory_item_id(1)] = REJECTED_CODE
    world.worker.run_pass(NOW)
    world.worker.run_pass(NOW + POLL)
    reads = world.shopify.order_reads

    world.worker.run_pass(NOW + 2 * POLL)

    assert world.shopify.order_reads == reads
    assert world.shopify.mutations[-1][0][0] == inventory_item_id(1)
    assert len(world.shopify.mutations) == 2


def test_the_retry_wait_doubles_up_to_the_cap(world: World) -> None:
    world.bind(1, actual=3, available=0)
    world.shopify.rejects[inventory_item_id(1)] = REJECTED_CODE
    world.worker.run_pass(NOW)
    waits = [POLL * 2**n for n in range(8)] + [MAX_RETRY_BACKOFF] * 2
    assert waits[7] < MAX_RETRY_BACKOFF < POLL * 2**8
    last = NOW

    for wait in waits:
        world.shopify.mutations.clear()
        world.worker.run_pass(last + wait - SECOND)
        assert world.shopify.mutations == []
        world.worker.run_pass(last + wait)
        assert inventory_item_id(1) in sent_items(world)
        last += wait

    assert last - NOW < FULL_PASS_INTERVAL


def test_a_full_pass_retries_a_variant_waiting_on_its_backoff(
    world: World,
) -> None:
    world.bind(1, actual=3, available=0)
    world.shopify.rejects[inventory_item_id(1)] = REJECTED_CODE
    world.worker.run_pass(NOW)
    world.worker.run_pass(NOW + POLL)
    set_dry_run(world.store, True)
    world.worker.run_pass(NOW + 2 * POLL)
    assert len(world.shopify.mutations) == 2

    set_dry_run(world.store, False)
    world.worker.run_pass(NOW + 2 * POLL + SECOND)

    assert world.worker.last_pass_full
    assert len(world.shopify.mutations) == 3


def test_recovery_clears_the_backoff(world: World) -> None:
    world.bind(1, actual=3, available=0)
    world.shopify.rejects[inventory_item_id(1)] = REJECTED_CODE
    world.worker.run_pass(NOW)
    world.worker.run_pass(NOW + POLL)
    del world.shopify.rejects[inventory_item_id(1)]
    world.worker.run_pass(NOW + 3 * POLL)
    assert world.shopify.available(1) == 3

    world.shopify.rejects[inventory_item_id(1)] = REJECTED_CODE
    world.erpnext.set_bin(item_code(1), actual=4)
    world.worker.run_pass(NOW + 4 * POLL)
    world.shopify.mutations.clear()
    world.worker.run_pass(NOW + 5 * POLL)

    assert inventory_item_id(1) in sent_items(world)


def test_a_variant_without_a_level_at_the_location_stays_dirty(
    world: World,
) -> None:
    world.bind(1, actual=3, available=None)

    world.worker.run_pass(NOW)

    assert world.shopify.mutations == []
    assert decision(world, 1).action == "no level"
    assert dirty_items(world.store)[item_code(1)].attempts == 1


def test_a_deleted_variant_is_skipped_and_rebound_on_the_next_cycle(
    world: World,
) -> None:
    world.bind(1, actual=3, available=3)
    world.bind(2, actual=4, available=4)
    world.worker.run_pass(NOW)
    world.shopify.delete_variant(2)
    world.erpnext.set_bin(item_code(1), actual=5)
    world.erpnext.set_bin(item_code(2), actual=6)

    world.worker.run_pass(NOW + 5 * SECOND)

    assert world.shopify.mutations == [[(inventory_item_id(1), 5, 3)]]
    assert decision(world, 2).action == "inventory item not found"
    assert dirty_items(world.store)[item_code(2)].attempts == 1
    assert not world.worker.last_pass_full

    world.worker.run_pass(NOW + 10 * SECOND)

    assert world.worker.last_pass_full
    assert dirty_items(world.store) == {}
    assert world.open_of(ProblemKind.STALE_PUSH) == []


def stale_push(world: World) -> StoredProblem:
    problem = world.problem(ProblemKind.STALE_PUSH, LOCATION_ID)
    assert problem is not None
    return problem


def test_shopify_down_past_the_threshold_raises_one_problem(world: World) -> None:
    world.bind(1, actual=3, available=0)
    world.bind(2, actual=3, available=0)
    world.shopify.mutations_down = True
    world.worker.run_pass(NOW)

    world.shopify.down = True
    world.erpnext.set_bin(item_code(1), actual=2)
    world.worker.run_pass(NOW + STALE_AFTER + SECOND)

    assert world.open_of(ProblemKind.STALE_PUSH) == [LOCATION_ID]
    assert stale_push(world).details["Variants"] == (
        f"{variant_id(1)}, {variant_id(2)}"
    )

    world.shopify.down = False
    world.shopify.mutations_down = False
    world.shopify.mutations.clear()
    world.worker.run_pass(NOW + STALE_AFTER + SECOND + 2 * POLL)

    assert world.shopify.mutations == [
        [(inventory_item_id(1), 2, 0), (inventory_item_id(2), 3, 0)]
    ]
    assert world.open_of(ProblemKind.STALE_PUSH) == []


def test_reads_failing_past_the_threshold_count_the_dirty_variants(
    world: World,
) -> None:
    world.bind(1, actual=3, available=3)
    world.bind(2, actual=4, available=0)
    world.bind(3, actual=5, available=5)
    world.shopify.mutations_down = True
    world.worker.run_pass(NOW)
    world.shopify.down = True
    world.erpnext.set_bin(item_code(1), actual=2)
    world.worker.run_pass(NOW + 5 * SECOND)

    world.worker.run_pass(NOW + 5 * SECOND + STALE_AFTER)

    assert stale_push(world).details["Variants"] == (
        f"{variant_id(1)}, {variant_id(2)}"
    )


def test_an_outage_is_emailed_once_as_the_unpushed_variants_change(
    tmp_path: Path,
) -> None:
    world = emailing_world(tmp_path)
    world.bind(1, actual=3, available=0)
    world.bind(2, actual=3, available=0)
    world.bind(3, actual=3, available=3)
    world.shopify.mutations_down = True
    world.worker.run_pass(NOW)
    world.erpnext.set_bin(item_code(3), actual=4)
    world.worker.run_pass(NOW + POLL)

    world.worker.run_pass(NOW + STALE_AFTER)
    assert stale_push(world).details["Unpushed variants"] == "2"
    assert world.erpnext.make_calls == 1

    world.worker.run_pass(NOW + STALE_AFTER + EMAIL_CHECK_INTERVAL)
    assert stale_push(world).details["Unpushed variants"] == "3"
    assert world.erpnext.make_calls == 1

    world.shopify.mutations_down = False
    world.worker.run_pass(NOW + FULL_PASS_INTERVAL)

    assert world.open_of(ProblemKind.STALE_PUSH) == []
    assert world.erpnext.make_calls == 1
    world.store.close()


def test_an_unbound_variants_unpushed_value_is_dropped_on_a_full_pass(
    world: World,
) -> None:
    world.bind(1, actual=3, available=0)
    world.shopify.mutations_down = True
    world.worker.run_pass(NOW)
    world.worker.run_pass(NOW + STALE_AFTER + SECOND)
    assert world.open_of(ProblemKind.STALE_PUSH) == [LOCATION_ID]

    world.erpnext.items.clear()
    world.worker.run_pass(NOW + FULL_PASS_INTERVAL)

    assert world.worker.last_pass_full
    assert world.open_of(ProblemKind.STALE_PUSH) == []
    assert world.store.execute("SELECT count(*) FROM pending_pushes").fetchone() == (0,)
    assert dirty_items(world.store) == {}


def test_pushes_and_emails_happen_under_the_writer_lock(tmp_path: Path) -> None:
    world = emailing_world(tmp_path)
    world.bind(1, actual=3, available=0)
    world.shopify.add_variant(2, "TEST-MISSING")
    held: list[bool] = []
    world.shopify.on_mutation = lambda: held.append(world.worker.writer_lock.locked())
    world.erpnext.on_make = lambda: held.append(world.worker.writer_lock.locked())

    world.worker.run_pass(NOW)

    assert held == [True, True]
    assert not world.worker.writer_lock.locked()
    world.store.close()


def test_problem_emails_are_handled_at_most_once_per_interval(
    tmp_path: Path,
) -> None:
    world = emailing_world(tmp_path)
    world.bind(1, actual=3, available=3)
    world.worker.run_pass(NOW)

    world.shopify.add_order(1, [(1, 5)], created_at=NOW)
    world.erpnext.set_bin(item_code(1), actual=3)
    world.worker.run_pass(NOW + EMAIL_CHECK_INTERVAL - SECOND)
    assert world.open_of(ProblemKind.OVERSELL) == [variant_id(1)]
    assert world.erpnext.make_calls == 0

    world.worker.run_pass(NOW + EMAIL_CHECK_INTERVAL)

    assert world.erpnext.make_calls == 1
    world.store.close()


# Dry-run


def test_dry_run_pushes_nothing_and_records_the_decision(world: World) -> None:
    set_dry_run(world.store, True)
    world.bind(1, actual=3, available=0)

    world.worker.run_pass(NOW)

    assert world.shopify.mutations == []
    assert decision(world, 1).action == "dry run"
    assert dirty_items(world.store) == {}


def test_dry_run_raises_no_staleness_problem(world: World) -> None:
    world.bind(1, actual=3, available=0)
    world.shopify.mutations_down = True
    world.worker.run_pass(NOW)

    set_dry_run(world.store, True)
    world.worker.run_pass(NOW + STALE_AFTER + SECOND)

    assert world.open_of(ProblemKind.STALE_PUSH) == []


def test_turning_dry_run_off_pushes_every_bound_variant_next_cycle(
    world: World,
) -> None:
    set_dry_run(world.store, True)
    world.bind(1, actual=3, available=0)
    world.bind(2, actual=4, available=0)
    world.worker.run_pass(NOW)
    assert world.shopify.mutations == []

    set_dry_run(world.store, False)
    world.worker.run_pass(NOW + 5 * SECOND)

    assert world.shopify.mutations == [
        [(inventory_item_id(1), 3, 0), (inventory_item_id(2), 4, 0)]
    ]
    assert world.worker.last_pass_full

    world.worker.run_pass(NOW + 10 * SECOND)

    assert not world.worker.last_pass_full


# Full passes


def test_a_full_pass_runs_at_start_and_then_hourly(world: World) -> None:
    world.bind(1, actual=3, available=3)

    world.worker.run_pass(NOW)
    assert (world.shopify.variant_reads, world.worker.last_pass_full) == (1, True)

    world.worker.run_pass(NOW + FULL_PASS_INTERVAL - SECOND)
    assert (world.shopify.variant_reads, world.worker.last_pass_full) == (1, False)

    world.worker.run_pass(NOW + FULL_PASS_INTERVAL)
    assert (world.shopify.variant_reads, world.worker.last_pass_full) == (2, True)
    assert world.worker.last_pass_at == NOW + FULL_PASS_INTERVAL


def test_a_requested_full_pass_runs_next_and_once(world: World) -> None:
    world.bind(1, actual=3, available=3)
    world.worker.run_pass(NOW)

    world.worker.request_full_pass()
    world.worker.run_pass(NOW + SECOND)
    assert (world.shopify.variant_reads, world.worker.last_pass_full) == (2, True)

    world.worker.run_pass(NOW + 2 * SECOND)
    assert (world.shopify.variant_reads, world.worker.last_pass_full) == (2, False)


def test_last_full_pass_at_is_the_start_of_the_last_completed_full_pass(
    world: World,
) -> None:
    world.bind(1, actual=3, available=3)
    assert world.worker.last_full_pass_at is None

    world.worker.run_pass(NOW)
    world.worker.run_pass(NOW + SECOND)

    assert world.worker.last_full_pass_at == NOW
    assert world.worker.last_pass_at == NOW + SECOND


def test_a_hand_edit_is_overwritten_and_noted_without_a_problem(
    world: World,
) -> None:
    world.bind(1, actual=3, available=0)
    world.worker.run_pass(NOW)

    world.shopify.set_available(1, 7)
    world.worker.run_pass(NOW + FULL_PASS_INTERVAL)

    assert world.shopify.mutations[-1] == [(inventory_item_id(1), 3, 7)]
    record = decision(world, 1)
    assert (record.shopify_available, record.last_pushed) == (7, 3)
    assert record.note is not None
    assert "changed outside the connector" in record.note
    assert open_problems(world.store) == []


def test_a_full_pass_purges_push_history_past_retention(world: World) -> None:
    kept, purged = NOW - PUSH_HISTORY_RETENTION, NOW - 2 * PUSH_HISTORY_RETENTION
    for attempted_at in (kept, purged):
        world.store.execute(
            "INSERT INTO push_history (variant_id, available, change_from,"
            " idempotency_key, attempted_at, outcome) VALUES (?, 1, 0, 'k', ?, ?)",
            (variant_id(1), utc_timestamp(attempted_at), "pushed"),
        )

    world.worker.run_pass(NOW)

    rows = world.store.execute("SELECT attempted_at FROM push_history").fetchall()
    assert rows == [(utc_timestamp(kept),)]


def test_no_note_when_shopify_holds_the_last_pushed_value(world: World) -> None:
    world.bind(1, actual=3, available=0)
    world.worker.run_pass(NOW)

    world.erpnext.set_bin(item_code(1), actual=4)
    world.worker.run_pass(NOW + 5 * SECOND)

    record = decision(world, 1)
    assert (record.shopify_available, record.last_pushed) == (3, 3)
    assert record.note is None


def test_restart_runs_a_full_pass_and_sends_no_duplicate_email(
    tmp_path: Path,
) -> None:
    world = emailing_world(tmp_path)
    world.bind(1, actual=3, available=3)
    world.shopify.add_variant(2, "TEST-MISSING")
    world.worker.run_pass(NOW)
    assert world.erpnext.make_calls == 1

    restarted = world.new_worker()
    restarted.run_pass(NOW + 10 * SECOND)

    assert world.shopify.variant_reads == 2
    assert restarted.last_pass_full
    assert world.erpnext.make_calls == 1
    world.store.close()


def test_binding_problems_are_recorded_on_a_full_pass(world: World) -> None:
    world.shopify.add_variant(1, "")
    world.shopify.add_variant(2, "TEST-MISSING")
    world.shopify.add_variant(3, item_code(3).lower())
    world.erpnext.add_item(item_code(3))
    world.shopify.add_variant(4, item_code(4))
    world.shopify.add_variant(5, item_code(4))
    world.erpnext.add_item(item_code(4))
    world.shopify.add_variant(6, item_code(6))
    world.erpnext.add_item(item_code(6), has_variants=1)
    world.bind(9, actual=1, available=1)
    world.worker.run_pass(NOW)

    world.shopify.set_sku(9, item_code(10))
    world.worker.run_pass(NOW + FULL_PASS_INTERVAL)

    assert sorted(world.open_of(ProblemKind.UNBOUND_SKU)) == [
        variant_id(1),
        variant_id(2),
        variant_id(3),
    ]
    assert sorted(world.open_of(ProblemKind.AMBIGUOUS_SKU)) == [
        variant_id(4),
        variant_id(5),
        variant_id(6),
    ]
    assert world.open_of(ProblemKind.CHANGED_SKU) == [variant_id(9)]
    near = world.problem(ProblemKind.UNBOUND_SKU, variant_id(3))
    assert near is not None
    assert near.details["SKU"] == item_code(3).lower()
    assert near.details["Similar item codes"] == item_code(3)
    duplicate = world.problem(ProblemKind.AMBIGUOUS_SKU, variant_id(4))
    assert duplicate is not None
    assert duplicate.details["Other variants"] == variant_id(5)
    changed = world.problem(ProblemKind.CHANGED_SKU, variant_id(9))
    assert changed is not None
    assert changed.details == {
        "SKU": item_code(10),
        "Reason": "The SKU changed since the variant was bound",
        "Bound item": item_code(9),
    }


def test_a_binding_problem_resolves_once_the_variant_binds(world: World) -> None:
    world.shopify.add_variant(2, item_code(2))
    world.worker.run_pass(NOW)
    assert world.open_of(ProblemKind.UNBOUND_SKU) == [variant_id(2)]

    world.erpnext.add_item(item_code(2))
    world.worker.run_pass(NOW + FULL_PASS_INTERVAL)

    assert world.open_of(ProblemKind.UNBOUND_SKU) == []


# Problem checks


def test_an_order_unmirrored_past_grace_raises_one_problem(world: World) -> None:
    world.bind(1, actual=5, available=4)
    world.shopify.add_order(1, [(1, 1)], created_at=NOW - UNMIRRORED_GRACE)

    world.worker.run_pass(NOW)

    assert world.open_of(ProblemKind.UNMIRRORED_ORDER) == [order_id(1)]
    assert decision(world, 1).explanation["unmirrored"] == 1


def test_oversell_on_a_stop_at_zero_variant_names_the_newest_orders(
    world: World,
) -> None:
    world.bind(1, actual=1, available=0)
    for n in range(1, 7):
        world.shopify.add_order(n, [(1, 1)], created_at=NOW - (10 - n) * SECOND)

    world.worker.run_pass(NOW)

    problem = world.problem(ProblemKind.OVERSELL, variant_id(1))
    assert problem is not None
    assert problem.details == {
        "Item": item_code(1),
        "Short by": "5",
        "Shopify inventory policy": "Stops selling at zero",
        "Newest Shopify orders": "#6, #5, #4, #3, #2",
    }


def test_a_standing_oversell_is_emailed_once_as_its_orders_change(
    tmp_path: Path,
) -> None:
    world = emailing_world(tmp_path)
    world.bind(1, actual=0, available=0)
    world.shopify.add_order(1, [(1, 1)], created_at=NOW - SECOND)
    world.worker.run_pass(NOW)
    assert world.erpnext.make_calls == 1

    world.shopify.add_order(2, [(1, 2)], created_at=NOW)
    world.erpnext.set_bin(item_code(1), actual=0)
    world.worker.run_pass(NOW + timedelta(minutes=2))

    assert world.erpnext.make_calls == 1
    problem = world.problem(ProblemKind.OVERSELL, variant_id(1))
    assert problem is not None
    assert problem.details["Short by"] == "3"
    assert problem.details["Newest Shopify orders"] == "#2, #1"
    world.store.close()


def test_an_incremental_pass_leaves_another_variants_oversell_open(
    tmp_path: Path,
) -> None:
    world = emailing_world(tmp_path)
    world.bind(1, actual=0, reserved=1, available=0)
    world.bind(2, actual=3, available=3)
    world.shopify.add_order(1, [(1, 1)], created_at=NOW - SECOND)
    world.erpnext.add_sales_order_line(1, item_code(1), 1)
    world.worker.run_pass(NOW)
    assert world.erpnext.make_calls == 1

    world.erpnext.set_bin(item_code(2), actual=4)
    world.worker.run_pass(NOW + 5 * SECOND)
    assert world.open_of(ProblemKind.OVERSELL) == [variant_id(1)]

    world.worker.run_pass(NOW + FULL_PASS_INTERVAL)

    assert world.worker.last_pass_full
    assert world.open_of(ProblemKind.OVERSELL) == [variant_id(1)]
    assert world.erpnext.make_calls == 1
    world.store.close()


def test_oversell_on_a_keep_selling_variant_is_worded_as_a_build_need(
    world: World,
) -> None:
    world.bind(1, actual=0, available=-2, policy="CONTINUE")
    world.shopify.add_order(1, [(1, 2)], created_at=NOW - SECOND)

    world.worker.run_pass(NOW)

    problem = world.problem(ProblemKind.OVERSELL, variant_id(1))
    assert problem is not None
    assert problem.details["Shopify inventory policy"] == (
        "Keeps selling at zero: a build is needed"
    )
    assert problem.details["Newest Shopify orders"] == "#1"


def test_demand_covered_by_planned_production_is_no_oversell(world: World) -> None:
    world.bind(1, actual=1, planned=5, available=0)
    world.shopify.add_order(1, [(1, 3)], created_at=NOW - SECOND)

    world.worker.run_pass(NOW)

    assert world.open_of(ProblemKind.OVERSELL) == []


def test_oversell_resolves_once_stock_covers_the_orders(world: World) -> None:
    world.bind(1, actual=0, available=0)
    world.shopify.add_order(1, [(1, 1)], created_at=NOW - SECOND)
    world.worker.run_pass(NOW)
    assert world.open_of(ProblemKind.OVERSELL) == [variant_id(1)]

    world.erpnext.set_bin(item_code(1), actual=1)
    world.worker.run_pass(NOW + 5 * SECOND)

    assert world.open_of(ProblemKind.OVERSELL) == []


# Failures


def test_an_erpnext_error_mid_pass_keeps_the_dirty_items(world: World) -> None:
    world.bind(1, actual=3, available=3)
    world.worker.run_pass(NOW)

    world.erpnext.set_bin(item_code(1), actual=4)
    world.erpnext.failing.add("Bin")
    world.worker.run_pass(NOW + 5 * SECOND)

    assert item_code(1) in dirty_items(world.store)
    assert world.shopify.mutations == []
    assert world.worker.last_pass_at == NOW

    world.erpnext.failing.clear()
    world.worker.run_pass(NOW + 10 * SECOND)

    assert world.shopify.mutations == [[(inventory_item_id(1), 4, 3)]]
    assert dirty_items(world.store) == {}


def test_a_failed_full_pass_is_retried_after_a_doubling_wait(world: World) -> None:
    world.bind(1, actual=3, available=0)
    world.erpnext.failing.add("Item")
    world.worker.run_pass(NOW)
    assert world.worker.last_pass_at is None
    world.worker.run_pass(NOW + POLL)
    assert world.shopify.variant_reads == 2

    world.erpnext.failing.clear()
    world.worker.run_pass(NOW + 3 * POLL - SECOND)

    assert world.shopify.variant_reads == 2
    assert not world.worker.last_pass_full
    assert world.shopify.available(1) == 0

    world.worker.run_pass(NOW + 3 * POLL)

    assert world.worker.last_pass_full
    assert world.shopify.available(1) == 3


def test_nothing_is_pushed_before_the_first_full_pass_succeeds(
    world: World,
) -> None:
    world.bind(1, actual=3, available=3)
    world.worker.run_pass(NOW)
    restarted = world.new_worker()
    world.erpnext.failing.add("Item")
    restarted.run_pass(NOW + FULL_PASS_INTERVAL)

    world.erpnext.set_bin(item_code(1), actual=4)
    restarted.run_pass(NOW + FULL_PASS_INTERVAL + SECOND)

    assert world.shopify.mutations == []
    assert item_code(1) in dirty_items(world.store)
    assert restarted.last_pass_at is None

    world.erpnext.failing.clear()
    restarted.run_pass(NOW + FULL_PASS_INTERVAL + POLL)

    assert restarted.last_pass_full
    assert world.shopify.mutations == [[(inventory_item_id(1), 4, 3)]]


def test_a_failed_pass_backs_off_the_items_it_was_computing(world: World) -> None:
    world.bind(1, actual=3, available=3)
    world.bind(2, actual=4, available=4)
    world.worker.run_pass(NOW)
    world.shopify.orders_down = True
    world.worker.run_pass(NOW + FULL_PASS_INTERVAL)
    assert len(dirty_items(world.store)) == 2
    reads = world.shopify.order_reads

    world.worker.run_pass(NOW + FULL_PASS_INTERVAL + SECOND)
    world.worker.run_pass(NOW + FULL_PASS_INTERVAL + POLL - SECOND)

    assert world.shopify.order_reads == reads

    world.shopify.orders_down = False
    world.worker.run_pass(NOW + FULL_PASS_INTERVAL + POLL)

    assert world.shopify.order_reads > reads
    assert dirty_items(world.store) == {}


def test_incremental_passes_continue_while_a_full_pass_waits(world: World) -> None:
    world.bind(1, actual=3, available=3)
    world.worker.run_pass(NOW)
    world.erpnext.failing.add("Item")
    world.worker.run_pass(NOW + FULL_PASS_INTERVAL)

    world.erpnext.set_bin(item_code(1), actual=4)
    world.worker.run_pass(NOW + FULL_PASS_INTERVAL + SECOND)

    assert not world.worker.last_pass_full
    assert world.shopify.mutations == [[(inventory_item_id(1), 4, 3)]]


def test_a_failed_full_pass_leaves_last_full_pass_at_unchanged(
    world: World,
) -> None:
    world.bind(1, actual=3, available=3)
    world.worker.run_pass(NOW)
    world.erpnext.failing.add("Item")
    world.worker.run_pass(NOW + FULL_PASS_INTERVAL)

    world.worker.run_pass(NOW + FULL_PASS_INTERVAL + SECOND)

    assert world.worker.last_full_pass_at == NOW
    assert world.worker.last_pass_at == NOW + FULL_PASS_INTERVAL + SECOND


def test_run_passes_until_stopped(world: World) -> None:
    world.bind(1, actual=3, available=0)
    stop = threading.Event()
    world.shopify.on_mutation = stop.set

    world.worker.run(stop)

    assert world.worker.last_pass_at == NOW
    assert world.shopify.available(1) == 3


# Comparison report


def test_comparison_report_lists_each_bound_variant(world: World) -> None:
    set_dry_run(world.store, True)
    world.bind(1, actual=5.5, reserved=2, available=4)
    world.bind(2, actual=0, reserved=1, available=0)
    world.shopify.add_variant(3, "TEST-MISSING", available=9)
    world.shopify.add_order(1, [(1, 1)], created_at=NOW - SECOND)

    world.worker.run_pass(NOW)

    assert comparison_report(world.store) == [
        ReportRow(
            variant_id=variant_id(1),
            item_code=item_code(1),
            shopify_available=4,
            computed=2,
            old_formula=3,
            actual_qty=5.5,
            reserved_qty=2,
            reserved_qty_for_production=0,
            planned_qty=0,
            unmirrored=1,
        ),
        ReportRow(
            variant_id=variant_id(2),
            item_code=item_code(2),
            shopify_available=0,
            computed=0,
            old_formula=-1,
            actual_qty=0,
            reserved_qty=1,
            reserved_qty_for_production=0,
            planned_qty=0,
            unmirrored=0,
        ),
    ]
    assert world.shopify.mutations == []


def test_binding_problem_kinds_are_unbound_ambiguous_then_changed() -> None:
    assert BINDING_PROBLEM_KINDS == (
        ProblemKind.UNBOUND_SKU,
        ProblemKind.AMBIGUOUS_SKU,
        ProblemKind.CHANGED_SKU,
    )


# Operator commands through the API


OPERATOR_TOKEN = "test-operator-token"
# Kept apart from its value so the fixture check's header pattern stays meaningful.
AUTH_HEADER = "Authorization"


def test_an_accepted_sku_change_binds_the_variant_to_its_new_sku_next_pass(
    world: World,
) -> None:
    world.bind(9, actual=4, available=0)
    world.worker.run_pass(NOW)
    world.shopify.set_sku(9, item_code(10))
    world.erpnext.add_item(item_code(10))
    world.erpnext.set_bin(item_code(10), actual=2)
    world.worker.run_pass(NOW + FULL_PASS_INTERVAL)
    assert world.open_of(ProblemKind.CHANGED_SKU) == [variant_id(9)]
    config = replace(
        world.config,
        store_path=world.path,
        api=ApiConfig(port=0),
        secrets=replace(
            world.config.secrets, operator_tokens={"alice": Secret(OPERATOR_TOKEN)}
        ),
    )
    server = ApiServer(config, world.worker)
    server.start()
    try:
        response = httpx.post(
            f"http://127.0.0.1:{server.port}/bindings/accept-sku-change",
            json={"variant": variant_id(9)},
            headers={
                AUTH_HEADER: f"Bearer {OPERATOR_TOKEN}",
                ENVIRONMENT_HEADER: world.config.environment,
            },
        )
    finally:
        server.close()
    assert response.status_code == 200

    world.worker.run_pass(NOW + FULL_PASS_INTERVAL + SECOND)

    assert world.worker.last_pass_full
    assert bound_item_code(world.store, variant_id(9)) == item_code(10)
    assert world.open_of(ProblemKind.CHANGED_SKU) == []
    assert world.shopify.available(9) == 2
    operators = world.store.execute("SELECT operator FROM audit_log").fetchall()
    assert operators == [("alice",)]
