import json
from collections.abc import Callable, Mapping

import httpx
import pytest
from helpers import LOCATION_ID, SHOP_DOMAIN, make_config

from shopify_erpnext_connector.push import LEVELS_PAGE_SIZE, Level, fetch_levels
from shopify_erpnext_connector.shopify import ShopifyClient, ShopifyError

Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    def bodies(self) -> list[dict[str, object]]:
        return [json.loads(request.content) for request in self.requests]

    def variables(self) -> list[dict[str, object]]:
        found: list[dict[str, object]] = []
        for body in self.bodies():
            variables = body["variables"]
            assert isinstance(variables, dict)
            found.append(variables)
        return found


def shopify_client(handler: Handler) -> ShopifyClient:
    http = httpx.Client(
        base_url=f"https://{SHOP_DOMAIN}", transport=httpx.MockTransport(handler)
    )
    return ShopifyClient(make_config(), http, sleep=lambda _: None)


def graphql(data: Mapping[str, object]) -> httpx.Response:
    return httpx.Response(200, json={"data": data})


def inventory_item_id(n: int) -> str:
    return f"gid://shopify/InventoryItem/{n}"


def level_node(
    n: int, available: int | None, policy: str = "DENY"
) -> dict[str, object]:
    level = (
        None
        if available is None
        else {"quantities": [{"name": "available", "quantity": available}]}
    )
    return {
        "id": inventory_item_id(n),
        "inventoryLevel": level,
        "variant": {"inventoryPolicy": policy},
    }


def levels_response(*nodes: object) -> httpx.Response:
    return graphql({"nodes": list(nodes)})


# Reading levels


def test_fetch_levels_reads_available_and_policy_at_the_location() -> None:
    recorder = Recorder(
        levels_response(level_node(1, 5), level_node(2, -2, policy="CONTINUE"))
    )

    levels = fetch_levels(
        shopify_client(recorder),
        [inventory_item_id(1), inventory_item_id(2)],
        LOCATION_ID,
    )

    assert levels == {
        inventory_item_id(1): Level(available=5, policy="DENY"),
        inventory_item_id(2): Level(available=-2, policy="CONTINUE"),
    }
    assert recorder.variables() == [
        {
            "ids": [inventory_item_id(1), inventory_item_id(2)],
            "locationId": LOCATION_ID,
        }
    ]


def test_item_not_stocked_at_the_location_has_no_available() -> None:
    recorder = Recorder(levels_response(level_node(1, None)))

    levels = fetch_levels(shopify_client(recorder), [inventory_item_id(1)], LOCATION_ID)

    assert levels == {inventory_item_id(1): Level(available=None, policy="DENY")}


def test_fetch_levels_splits_ids_into_requests_of_the_page_size() -> None:
    count = LEVELS_PAGE_SIZE + 1
    ids = [inventory_item_id(n) for n in range(count)]
    recorder = Recorder(
        levels_response(*(level_node(n, n) for n in range(LEVELS_PAGE_SIZE))),
        levels_response(level_node(LEVELS_PAGE_SIZE, 0)),
    )

    levels = fetch_levels(shopify_client(recorder), ids, LOCATION_ID)

    assert len(levels) == count
    assert [v["ids"] for v in recorder.variables()] == [
        ids[:LEVELS_PAGE_SIZE],
        ids[LEVELS_PAGE_SIZE:],
    ]


def test_fetch_levels_of_no_ids_sends_nothing() -> None:
    recorder = Recorder()

    assert fetch_levels(shopify_client(recorder), [], LOCATION_ID) == {}
    assert recorder.requests == []


def test_fetch_levels_retries_after_a_server_error() -> None:
    recorder = Recorder(httpx.Response(503), levels_response(level_node(1, 3)))

    levels = fetch_levels(shopify_client(recorder), [inventory_item_id(1)], LOCATION_ID)

    assert levels == {inventory_item_id(1): Level(available=3, policy="DENY")}
    assert len(recorder.requests) == 2


def test_inventory_item_shopify_does_not_know_is_left_out() -> None:
    recorder = Recorder(levels_response(None, level_node(2, 3)))

    levels = fetch_levels(
        shopify_client(recorder),
        [inventory_item_id(1), inventory_item_id(2)],
        LOCATION_ID,
    )

    assert levels == {inventory_item_id(2): Level(available=3, policy="DENY")}


def malformed(**changes: object) -> dict[str, object]:
    return {**level_node(1, 3), **changes}


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"nodes": None},
        {"nodes": [malformed(id=None)]},
        {"nodes": [malformed(id=inventory_item_id(9))]},
        {"nodes": [malformed(variant=None)]},
        {"nodes": [malformed(variant={"inventoryPolicy": None})]},
        {"nodes": [malformed(inventoryLevel={"quantities": None})]},
        {"nodes": [malformed(inventoryLevel={"quantities": []})]},
        {
            "nodes": [
                malformed(
                    inventoryLevel={
                        "quantities": [{"name": "available", "quantity": "3"}]
                    }
                )
            ]
        },
        {"nodes": [level_node(1, 3), level_node(1, 3)]},
    ],
)
def test_malformed_levels_response_raises(data: dict[str, object]) -> None:
    recorder = Recorder(graphql(data))

    with pytest.raises(ShopifyError):
        fetch_levels(shopify_client(recorder), [inventory_item_id(1)], LOCATION_ID)
