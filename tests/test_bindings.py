import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from shopify_erpnext_connector.bindings import (
    VARIANT_PAGE_SIZE,
    Binding,
    BindingResult,
    ErpItem,
    ShopifyVariant,
    UnboundKind,
    UnboundVariant,
    compute_bindings,
    fetch_items,
    fetch_variants,
    load_previous,
    refresh_bindings,
    save_bindings,
)
from shopify_erpnext_connector.config import (
    Config,
    EmailConfig,
    Environment,
    Secret,
    Secrets,
)
from shopify_erpnext_connector.erpnext import ErpNextClient, ErpNextError
from shopify_erpnext_connector.shopify import ShopifyClient, ShopifyError
from shopify_erpnext_connector.store import open_store, utc_timestamp

SHOP_DOMAIN = "example.myshopify.com"
ERPNEXT_URL = "https://erpnext.example.com"
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=1)

Handler = Callable[[httpx.Request], httpx.Response]


def make_config() -> Config:
    return Config(
        environment=Environment.STAGING,
        shop_domain=SHOP_DOMAIN,
        location_id="gid://shopify/Location/1234567890",
        erpnext_url=ERPNEXT_URL,
        email=EmailConfig(
            enabled=False,
            recipient="operator@example.com",
            sender="notifications@example.com",
        ),
        secrets=Secrets(
            erpnext_api_key=Secret("test-erpnext-key"),
            erpnext_api_secret=Secret("test-erpnext-secret"),
            shopify_access_token=Secret("test-shopify-token"),
            metrics_token=Secret("test-metrics-token"),
            operator_tokens={},
        ),
    )


def variant(
    number: int,
    sku: str,
    *,
    status: str = "ACTIVE",
    tracked: bool = True,
) -> ShopifyVariant:
    return ShopifyVariant(
        variant_id=f"gid://shopify/ProductVariant/{number}",
        inventory_item_id=f"gid://shopify/InventoryItem/{number}",
        sku=sku,
        product_status=status,
        tracked=tracked,
    )


def item(
    code: str,
    *,
    disabled: bool = False,
    has_variants: bool = False,
    is_stock_item: bool = True,
) -> ErpItem:
    return ErpItem(
        item_code=code,
        disabled=disabled,
        has_variants=has_variants,
        is_stock_item=is_stock_item,
    )


def binding_of(v: ShopifyVariant, item_code: str) -> Binding:
    return Binding(v.variant_id, v.inventory_item_id, item_code)


def only_unbound(result: BindingResult) -> UnboundVariant:
    assert len(result.unbound) == 1
    return result.unbound[0]


def test_variant_binds_to_the_item_with_the_same_code() -> None:
    v = variant(1, "COM-EXAMPLE-08-512")

    result = compute_bindings([v], [item("COM-EXAMPLE-08-512")], {})

    assert result.bindings == (binding_of(v, "COM-EXAMPLE-08-512"),)
    assert result.unbound == ()


def test_variant_without_an_item_is_reported() -> None:
    v = variant(1, "TEST-MISSING")

    result = compute_bindings([v], [item("TEST-OTHER")], {})

    assert result.bindings == ()
    assert only_unbound(result) == UnboundVariant(
        kind=UnboundKind.NO_ITEM,
        variant_id=v.variant_id,
        sku="TEST-MISSING",
    )


@pytest.mark.parametrize(
    ("erp_item", "kind"),
    [
        (item("TEST-ITEM", disabled=True), UnboundKind.ITEM_DISABLED),
        (item("TEST-ITEM", has_variants=True), UnboundKind.TEMPLATE_ITEM),
        (item("TEST-ITEM", is_stock_item=False), UnboundKind.NON_STOCK_ITEM),
    ],
)
def test_matching_item_that_cannot_hold_stock_is_reported_not_bound(
    erp_item: ErpItem, kind: UnboundKind
) -> None:
    v = variant(1, "TEST-ITEM")

    result = compute_bindings([v], [erp_item], {})

    assert result.bindings == ()
    assert only_unbound(result) == UnboundVariant(
        kind=kind, variant_id=v.variant_id, sku="TEST-ITEM", item_codes=("TEST-ITEM",)
    )


def test_template_item_is_reported_even_when_flagged_as_stock_item() -> None:
    # The template-row case: a template Item must never be pushed as 0.
    v = variant(1, "SOM-EXAMPLE")
    template = item("SOM-EXAMPLE", has_variants=True, is_stock_item=True)

    result = compute_bindings([v], [template], {})

    assert result.bindings == ()
    assert only_unbound(result).kind is UnboundKind.TEMPLATE_ITEM


def test_disabled_takes_precedence_over_template() -> None:
    v = variant(1, "TEST-ITEM")
    erp_item = item("TEST-ITEM", disabled=True, has_variants=True)

    result = compute_bindings([v], [erp_item], {})

    assert only_unbound(result).kind is UnboundKind.ITEM_DISABLED


def test_two_variants_with_one_sku_are_both_reported_and_neither_bound() -> None:
    first, second = variant(1, "TEST-DUP"), variant(2, "TEST-DUP")
    lone = variant(3, "TEST-LONE")

    result = compute_bindings(
        [first, second, lone], [item("TEST-DUP"), item("TEST-LONE")], {}
    )

    assert result.bindings == (binding_of(lone, "TEST-LONE"),)
    assert set(result.unbound) == {
        UnboundVariant(
            kind=UnboundKind.DUPLICATE_SKU,
            variant_id=first.variant_id,
            sku="TEST-DUP",
            other_variant_ids=(second.variant_id,),
        ),
        UnboundVariant(
            kind=UnboundKind.DUPLICATE_SKU,
            variant_id=second.variant_id,
            sku="TEST-DUP",
            other_variant_ids=(first.variant_id,),
        ),
    }


def test_unlisted_product_variant_binds() -> None:
    unlisted = variant(1, "TEST-ITEM", status="UNLISTED")

    result = compute_bindings([unlisted], [item("TEST-ITEM")], {})

    assert result.bindings == (binding_of(unlisted, "TEST-ITEM"),)


def test_duplicate_sku_counts_only_push_set_variants() -> None:
    active = variant(1, "TEST-ITEM")
    draft = variant(2, "TEST-ITEM", status="DRAFT")

    result = compute_bindings([active, draft], [item("TEST-ITEM")], {})

    assert result.bindings == (binding_of(active, "TEST-ITEM"),)
    assert result.unbound == ()


@pytest.mark.parametrize(
    "sku",
    ["cx-example-pw300", " CX-EXAMPLE-PW300", "CX-EXAMPLE-PW300 ", "CX-EXAMPLE- PW300"],
)
def test_sku_differing_only_in_case_or_whitespace_is_a_near_match(sku: str) -> None:
    v = variant(1, sku)

    result = compute_bindings([v], [item("CX-EXAMPLE-PW300"), item("TEST-OTHER")], {})

    assert result.bindings == ()
    assert only_unbound(result) == UnboundVariant(
        kind=UnboundKind.NEAR_MATCH,
        variant_id=v.variant_id,
        sku=sku,
        item_codes=("CX-EXAMPLE-PW300",),
    )


def test_exact_match_wins_over_a_near_match() -> None:
    v = variant(1, "TEST-ITEM")

    result = compute_bindings([v], [item("test-item"), item("TEST-ITEM")], {})

    assert result.bindings == (binding_of(v, "TEST-ITEM"),)


def test_near_match_lists_every_candidate_sorted() -> None:
    v = variant(1, "test-item")

    result = compute_bindings([v], [item("TEST-ITEM"), item("Test-Item")], {})

    assert only_unbound(result).item_codes == ("TEST-ITEM", "Test-Item")


@pytest.mark.parametrize("sku", ["", "   "])
def test_variant_with_an_empty_sku_is_reported(sku: str) -> None:
    v = variant(1, sku)

    result = compute_bindings([v], [item("TEST-ITEM")], {})

    assert result.bindings == ()
    assert only_unbound(result) == UnboundVariant(
        kind=UnboundKind.EMPTY_SKU, variant_id=v.variant_id, sku=sku
    )


def test_empty_skus_are_not_reported_as_duplicates_of_each_other() -> None:
    result = compute_bindings([variant(1, ""), variant(2, "")], [], {})

    assert {u.kind for u in result.unbound} == {UnboundKind.EMPTY_SKU}


@pytest.mark.parametrize(
    "excluded",
    [
        variant(1, "TEST-ITEM", status="DRAFT"),
        variant(1, "TEST-ITEM", status="ARCHIVED"),
        variant(1, "TEST-ITEM", tracked=False),
        variant(1, "TEST-MISSING", status="DRAFT"),
        variant(1, "", status="ARCHIVED"),
    ],
)
def test_variants_outside_the_push_set_are_neither_bound_nor_reported(
    excluded: ShopifyVariant,
) -> None:
    result = compute_bindings([excluded], [item("TEST-ITEM")], {})

    assert result == BindingResult(bindings=(), unbound=())


def test_variant_whose_sku_changed_since_bound_is_reported_not_bound() -> None:
    v = variant(1, "TEST-NEW")

    result = compute_bindings(
        [v], [item("TEST-OLD"), item("TEST-NEW")], {v.variant_id: "TEST-OLD"}
    )

    assert result.bindings == ()
    assert only_unbound(result) == UnboundVariant(
        kind=UnboundKind.SKU_CHANGED,
        variant_id=v.variant_id,
        sku="TEST-NEW",
        item_codes=("TEST-OLD",),
    )


def test_sku_cleared_since_bound_is_a_sku_change() -> None:
    v = variant(1, "")

    result = compute_bindings([v], [item("TEST-OLD")], {v.variant_id: "TEST-OLD"})

    assert only_unbound(result).kind is UnboundKind.SKU_CHANGED


def test_variant_with_an_unchanged_sku_rebinds() -> None:
    v = variant(1, "TEST-ITEM")

    result = compute_bindings([v], [item("TEST-ITEM")], {v.variant_id: "TEST-ITEM"})

    assert result.bindings == (binding_of(v, "TEST-ITEM"),)


def test_bound_variant_whose_item_is_disabled_loses_its_binding() -> None:
    v = variant(1, "TEST-ITEM")

    result = compute_bindings(
        [v], [item("TEST-ITEM", disabled=True)], {v.variant_id: "TEST-ITEM"}
    )

    assert result.bindings == ()
    assert only_unbound(result).kind is UnboundKind.ITEM_DISABLED


def test_changed_variant_still_counts_toward_a_duplicate() -> None:
    changed = variant(1, "TEST-ITEM")
    other = variant(2, "TEST-ITEM")

    result = compute_bindings(
        [changed, other], [item("TEST-ITEM")], {changed.variant_id: "TEST-OLD"}
    )

    assert result.bindings == ()
    kinds = {u.variant_id: u.kind for u in result.unbound}
    assert kinds == {
        changed.variant_id: UnboundKind.SKU_CHANGED,
        other.variant_id: UnboundKind.DUPLICATE_SKU,
    }


def test_compute_bindings_is_deterministic_in_variant_order() -> None:
    variants = [variant(n, f"TEST-{n:03d}") for n in (3, 1, 2)]
    items = [item(f"TEST-{n:03d}") for n in (1, 2, 3)]

    result = compute_bindings(variants, items, {})

    assert [b.variant_id for b in result.bindings] == [
        f"gid://shopify/ProductVariant/{n}" for n in (1, 2, 3)
    ]


def variant_node(
    number: int,
    sku: str | None,
    *,
    status: str = "ACTIVE",
    tracked: bool = True,
) -> dict[str, object]:
    return {
        "id": f"gid://shopify/ProductVariant/{number}",
        "sku": sku,
        "inventoryItem": {
            "id": f"gid://shopify/InventoryItem/{number}",
            "tracked": tracked,
        },
        "product": {"status": status},
    }


def variants_page(
    nodes: list[dict[str, object]], end_cursor: str | None
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": {
                "productVariants": {
                    "nodes": nodes,
                    "pageInfo": {
                        "hasNextPage": end_cursor is not None,
                        "endCursor": end_cursor,
                    },
                }
            }
        },
    )


class Recorder:
    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    def bodies(self) -> list[dict[str, object]]:
        return [json.loads(request.content) for request in self.requests]


def shopify_client(handler: Handler) -> ShopifyClient:
    http = httpx.Client(
        base_url=f"https://{SHOP_DOMAIN}", transport=httpx.MockTransport(handler)
    )
    return ShopifyClient(make_config(), http, sleep=lambda _: None)


def test_fetch_variants_follows_the_cursor_across_pages() -> None:
    recorder = Recorder(
        variants_page([variant_node(1, "TEST-001")], "cursor-1"),
        variants_page([variant_node(2, None, status="DRAFT", tracked=False)], None),
    )

    variants = fetch_variants(shopify_client(recorder))

    assert variants == [
        variant(1, "TEST-001"),
        variant(2, "", status="DRAFT", tracked=False),
    ]
    cursors = [body["variables"] for body in recorder.bodies()]
    assert cursors == [
        {"first": VARIANT_PAGE_SIZE, "after": None},
        {"first": VARIANT_PAGE_SIZE, "after": "cursor-1"},
    ]


def test_fetch_variants_retries_a_page_after_a_server_error() -> None:
    recorder = Recorder(
        httpx.Response(503),
        variants_page([variant_node(1, "TEST-001")], None),
    )

    assert fetch_variants(shopify_client(recorder)) == [variant(1, "TEST-001")]
    assert len(recorder.requests) == 2


def test_fetch_variants_queries_no_customer_or_product_detail_fields() -> None:
    recorder = Recorder(variants_page([], None))

    fetch_variants(shopify_client(recorder))

    query = str(recorder.bodies()[0]["query"])
    for field in ("title", "customer", "price", "displayName"):
        assert field not in query


@pytest.mark.parametrize(
    "broken",
    [
        {"productVariants": None},
        {"productVariants": {"nodes": [{"id": 1}], "pageInfo": {}}},
        {
            "productVariants": {
                "nodes": [],
                "pageInfo": {"hasNextPage": True, "endCursor": None},
            }
        },
    ],
)
def test_fetch_variants_rejects_an_unexpected_shape(broken: object) -> None:
    recorder = Recorder(httpx.Response(200, json={"data": broken}))

    with pytest.raises(ShopifyError):
        fetch_variants(shopify_client(recorder))


def page_info_response(
    nodes: list[dict[str, object]], page_info: dict[str, object]
) -> httpx.Response:
    return httpx.Response(
        200,
        json={"data": {"productVariants": {"nodes": nodes, "pageInfo": page_info}}},
    )


def test_fetch_variants_rejects_a_variant_listed_on_two_pages() -> None:
    recorder = Recorder(
        variants_page([variant_node(1, "TEST-001")], "cursor-1"),
        variants_page([variant_node(1, "TEST-001")], None),
    )

    with pytest.raises(ShopifyError):
        fetch_variants(shopify_client(recorder))


@pytest.mark.parametrize("has_next_page", [None, "true", 1])
def test_fetch_variants_rejects_a_has_next_page_that_is_not_a_boolean(
    has_next_page: object,
) -> None:
    recorder = Recorder(
        page_info_response(
            [variant_node(1, "TEST-001")],
            {"hasNextPage": has_next_page, "endCursor": "cursor-1"},
        )
    )

    with pytest.raises(ShopifyError):
        fetch_variants(shopify_client(recorder))


def test_fetch_variants_rejects_a_cursor_that_does_not_advance() -> None:
    recorder = Recorder(
        variants_page([variant_node(1, "TEST-001")], "cursor-1"),
        variants_page([variant_node(2, "TEST-002")], "cursor-1"),
    )

    with pytest.raises(ShopifyError):
        fetch_variants(shopify_client(recorder))


def erpnext_client(handler: Handler) -> ErpNextClient:
    http = httpx.Client(base_url=ERPNEXT_URL, transport=httpx.MockTransport(handler))
    return ErpNextClient(make_config(), http, sleep=lambda _: None)


def test_fetch_items_reads_the_rule_fields_of_every_item() -> None:
    rows = [
        {"item_code": "TEST-001", "disabled": 0, "has_variants": 0, "is_stock_item": 1},
        {"item_code": "TEST-002", "disabled": 1, "has_variants": 1, "is_stock_item": 0},
    ]
    recorder = Recorder(httpx.Response(200, json={"data": rows}))

    items = fetch_items(erpnext_client(recorder))

    assert items == [
        item("TEST-001"),
        item("TEST-002", disabled=True, has_variants=True, is_stock_item=False),
    ]
    request = recorder.requests[0]
    assert request.url.path == "/api/resource/Item"
    assert json.loads(request.url.params["fields"]) == [
        "item_code",
        "disabled",
        "has_variants",
        "is_stock_item",
    ]
    assert request.url.params["limit_page_length"] == "0"


@pytest.mark.parametrize(
    "row",
    [
        {"item_code": None, "disabled": 0, "has_variants": 0, "is_stock_item": 1},
        {
            "item_code": "TEST-001",
            "disabled": "no",
            "has_variants": 0,
            "is_stock_item": 1,
        },
        {"item_code": "TEST-001", "disabled": 0, "has_variants": 0},
    ],
)
def test_fetch_items_rejects_an_unexpected_row(row: dict[str, object]) -> None:
    recorder = Recorder(httpx.Response(200, json={"data": [row]}))

    with pytest.raises(ErpNextError):
        fetch_items(erpnext_client(recorder))


@pytest.fixture
def store(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_store(tmp_path / "connector.sqlite3")
    yield connection
    connection.close()


def binding_rows(store: sqlite3.Connection) -> list[tuple[str, str, str, str]]:
    return store.execute(
        "SELECT variant_id, inventory_item_id, item_code, bound_at"
        " FROM bindings ORDER BY variant_id"
    ).fetchall()


def run_pass(
    store: sqlite3.Connection,
    variants: list[ShopifyVariant],
    items: list[ErpItem],
    now: datetime = NOW,
) -> BindingResult:
    result = compute_bindings(variants, items, load_previous(store))
    save_bindings(store, result, now)
    return result


def test_save_bindings_stores_each_binding_with_its_time(
    store: sqlite3.Connection,
) -> None:
    v = variant(1, "TEST-ITEM")

    run_pass(store, [v], [item("TEST-ITEM")])

    assert binding_rows(store) == [
        (v.variant_id, v.inventory_item_id, "TEST-ITEM", utc_timestamp(NOW))
    ]


def test_rebinding_is_idempotent_across_passes(store: sqlite3.Connection) -> None:
    variants = [variant(1, "TEST-001"), variant(2, "TEST-002")]
    items = [item("TEST-001"), item("TEST-002")]
    first = run_pass(store, variants, items, NOW)
    rows = binding_rows(store)

    second = run_pass(store, variants, items, LATER)

    assert second == first
    assert binding_rows(store) == rows


def test_bindings_outside_the_new_push_set_are_removed(
    store: sqlite3.Connection,
) -> None:
    kept = variant(1, "TEST-KEPT")
    run_pass(
        store, [kept, variant(2, "TEST-GONE")], [item("TEST-KEPT"), item("TEST-GONE")]
    )

    run_pass(
        store,
        [kept, variant(2, "TEST-GONE", status="ARCHIVED")],
        [item("TEST-KEPT"), item("TEST-GONE")],
    )

    assert [row[0] for row in binding_rows(store)] == [kept.variant_id]


def test_item_moving_to_another_variant_replaces_the_old_binding(
    store: sqlite3.Connection,
) -> None:
    old, new = variant(1, "TEST-ITEM"), variant(2, "TEST-ITEM")
    run_pass(store, [old], [item("TEST-ITEM")])

    run_pass(
        store,
        [variant(1, "TEST-ITEM", status="DRAFT"), new],
        [item("TEST-ITEM")],
        LATER,
    )

    assert binding_rows(store) == [
        (new.variant_id, new.inventory_item_id, "TEST-ITEM", utc_timestamp(LATER))
    ]


def test_sku_change_stays_reported_until_the_sku_is_restored(
    store: sqlite3.Connection,
) -> None:
    items = [item("TEST-OLD"), item("TEST-NEW")]
    run_pass(store, [variant(1, "TEST-OLD")], items)

    changed = run_pass(store, [variant(1, "TEST-NEW")], items, LATER)
    still_changed = run_pass(
        store, [variant(1, "TEST-NEW")], items, LATER + timedelta(hours=1)
    )

    for result in (changed, still_changed):
        assert result.bindings == ()
        assert only_unbound(result).kind is UnboundKind.SKU_CHANGED
        assert only_unbound(result).item_codes == ("TEST-OLD",)
    assert binding_rows(store) == []
    held = store.execute(
        "SELECT variant_id, bound_item_code, detected_at FROM sku_changes"
    ).fetchall()
    assert held == [
        ("gid://shopify/ProductVariant/1", "TEST-OLD", utc_timestamp(LATER))
    ]

    restored = run_pass(store, [variant(1, "TEST-OLD")], items, LATER)

    assert restored.unbound == ()
    assert [row[2] for row in binding_rows(store)] == ["TEST-OLD"]
    assert load_previous(store) == {"gid://shopify/ProductVariant/1": "TEST-OLD"}


def test_held_sku_change_does_not_block_another_variant_taking_the_item(
    store: sqlite3.Connection,
) -> None:
    items = [item("TEST-OLD"), item("TEST-NEW")]
    run_pass(store, [variant(1, "TEST-OLD")], items)
    run_pass(store, [variant(1, "TEST-NEW")], items)

    result = run_pass(store, [variant(1, "TEST-NEW"), variant(2, "TEST-OLD")], items)

    assert result.bindings == (binding_of(variant(2, "TEST-OLD"), "TEST-OLD"),)
    assert only_unbound(result).kind is UnboundKind.SKU_CHANGED


def test_held_sku_change_is_dropped_when_the_variant_leaves_the_push_set(
    store: sqlite3.Connection,
) -> None:
    items = [item("TEST-OLD"), item("TEST-NEW")]
    run_pass(store, [variant(1, "TEST-OLD")], items)
    run_pass(store, [variant(1, "TEST-NEW")], items)

    run_pass(store, [variant(1, "TEST-NEW", status="ARCHIVED")], items)

    assert load_previous(store) == {}


def test_sku_change_after_the_binding_was_lost_binds_the_new_item(
    store: sqlite3.Connection,
) -> None:
    run_pass(store, [variant(1, "TEST-OLD")], [item("TEST-OLD"), item("TEST-NEW")])
    run_pass(
        store,
        [variant(1, "TEST-OLD")],
        [item("TEST-OLD", disabled=True), item("TEST-NEW")],
    )

    result = run_pass(
        store,
        [variant(1, "TEST-NEW")],
        [item("TEST-OLD", disabled=True), item("TEST-NEW")],
    )

    assert result.bindings == (binding_of(variant(1, "TEST-NEW"), "TEST-NEW"),)
    assert result.unbound == ()


def test_save_bindings_rolls_back_on_failure(store: sqlite3.Connection) -> None:
    run_pass(store, [variant(1, "TEST-001")], [item("TEST-001")])
    rows = binding_rows(store)
    # Two bindings for one item violate the UNIQUE constraint mid-write.
    clash = BindingResult(
        bindings=(
            binding_of(variant(2, "TEST-002"), "TEST-002"),
            binding_of(variant(3, "TEST-002"), "TEST-002"),
        ),
        unbound=(),
    )

    with pytest.raises(sqlite3.IntegrityError):
        save_bindings(store, clash, LATER)

    assert not store.in_transaction
    assert binding_rows(store) == rows


def test_refresh_bindings_fetches_computes_and_saves(
    store: sqlite3.Connection,
) -> None:
    shopify = shopify_client(
        Recorder(
            variants_page(
                [variant_node(1, "TEST-001"), variant_node(2, "TEST-MISSING")], None
            )
        )
    )
    rows: list[Mapping[str, object]] = [
        {"item_code": "TEST-001", "disabled": 0, "has_variants": 0, "is_stock_item": 1}
    ]
    erpnext = erpnext_client(Recorder(httpx.Response(200, json={"data": rows})))

    result = refresh_bindings(store, shopify, erpnext, NOW)

    assert result.bindings == (binding_of(variant(1, "TEST-001"), "TEST-001"),)
    assert [u.kind for u in result.unbound] == [UnboundKind.NO_ITEM]
    assert [row[2] for row in binding_rows(store)] == ["TEST-001"]
