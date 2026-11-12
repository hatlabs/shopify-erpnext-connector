import json
import logging
from collections.abc import Callable, Mapping

import httpx
import pytest

from shopify_erpnext_connector.config import Config, Environment, Secret, Secrets
from shopify_erpnext_connector.pinning import PinningError
from shopify_erpnext_connector.retry import MAX_ATTEMPTS, RETRY_DELAY_SECONDS
from shopify_erpnext_connector.shopify import (
    API_VERSION,
    BATCH_LIMIT,
    MAX_THROTTLE_RETRIES,
    ChangeFromQuantityStaleError,
    InventoryBatch,
    QuantityChange,
    ShopifyClient,
    ShopifyGraphQLError,
    ShopifyHTTPError,
    ShopifyTransportError,
    ShopifyUserError,
    batch_changes,
)

SHOP_DOMAIN = "example.myshopify.com"
LOCATION_ID = "gid://shopify/Location/1234567890"
ACCESS_TOKEN = "test-shopify-token"
GRAPHQL_PATH = f"/admin/api/{API_VERSION}/graphql.json"
MAXIMUM_POINTS = 2000.0
RESTORE_RATE = 100.0

Handler = Callable[[httpx.Request], httpx.Response]


def make_config() -> Config:
    return Config(
        environment=Environment.STAGING,
        shop_domain=SHOP_DOMAIN,
        location_id=LOCATION_ID,
        erpnext_url="https://erpnext.example.com",
        secrets=Secrets(
            erpnext_api_key=Secret("test-erpnext-key"),
            erpnext_api_secret=Secret("test-erpnext-secret"),
            shopify_access_token=Secret(ACCESS_TOKEN),
            metrics_token=Secret("test-metrics-token"),
            operator_tokens={},
        ),
    )


def cost(requested: float, available: float) -> dict[str, object]:
    return {
        "cost": {
            "requestedQueryCost": requested,
            "actualQueryCost": requested,
            "throttleStatus": {
                "maximumAvailable": MAXIMUM_POINTS,
                "currentlyAvailable": available,
                "restoreRate": RESTORE_RATE,
            },
        }
    }


def graphql_response(
    data: Mapping[str, object] | None,
    *,
    requested: float = 10.0,
    available: float = MAXIMUM_POINTS,
    errors: list[dict[str, object]] | None = None,
) -> httpx.Response:
    body: dict[str, object] = {
        "data": data,
        "extensions": cost(requested, available),
    }
    if errors is not None:
        body["errors"] = errors
    return httpx.Response(200, json=body)


def set_quantities_response(
    user_errors: list[dict[str, object]] | None = None,
) -> httpx.Response:
    # Recorded shape: on any user error the group is null and nothing is applied.
    group = None if user_errors else {"id": "gid://shopify/InventoryAdjustmentGroup/1"}
    return graphql_response(
        {
            "inventorySetQuantities": {
                "inventoryAdjustmentGroup": group,
                "userErrors": user_errors or [],
            }
        }
    )


def user_error(code: str, index: int, field: str) -> dict[str, object]:
    return {
        "code": code,
        "field": ["input", "quantities", str(index), field],
        "message": "synthetic user error message",
    }


THROTTLED: list[dict[str, object]] = [
    {
        "message": "Throttled",
        "extensions": {
            "code": "THROTTLED",
        },
    }
]


class Recorder:
    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    def bodies(self) -> list[dict[str, object]]:
        return [json.loads(request.content) for request in self.requests]


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_client(
    handler: Handler,
    *,
    base_url: str = f"https://{SHOP_DOMAIN}",
    sleep: Sleeps | None = None,
) -> ShopifyClient:
    http = httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler))
    return ShopifyClient(make_config(), http, sleep=sleep or Sleeps())


def change(
    n: int, change_from: int | None = 0, location_id: str = LOCATION_ID
) -> QuantityChange:
    return QuantityChange(
        inventory_item_id=f"gid://shopify/InventoryItem/{1000 + n}",
        location_id=location_id,
        quantity=n % 7,
        change_from_quantity=change_from,
    )


def variables(body: Mapping[str, object]) -> dict[str, object]:
    value = body["variables"]
    assert isinstance(value, dict)
    return value


def entries(body: Mapping[str, object]) -> list[dict[str, object]]:
    mutation_input = variables(body)["input"]
    assert isinstance(mutation_input, dict)
    return mutation_input["quantities"]


def sent_keys(recorder: Recorder) -> list[object]:
    return [variables(body)["idempotencyKey"] for body in recorder.bodies()]


def test_execute_posts_to_the_pinned_api_version_with_the_token() -> None:
    recorder = Recorder(graphql_response({"shop": {"name": "Example"}}))

    data = make_client(recorder).execute(
        "query Shop($first: Int!) { shop { name } }", {"first": 1}
    )

    assert data == {"shop": {"name": "Example"}}
    (request,) = recorder.requests
    assert request.method == "POST"
    assert request.url.host == SHOP_DOMAIN
    assert request.url.path == GRAPHQL_PATH
    assert request.headers.get("X-Shopify-Access-Token") == ACCESS_TOKEN
    assert recorder.bodies()[0] == {
        "query": "query Shop($first: Int!) { shop { name } }",
        "variables": {"first": 1},
    }


@pytest.mark.parametrize(
    "base_url", ["https://other.example.myshopify.com", "http://example.myshopify.com"]
)
def test_request_to_unpinned_shop_raises_before_network_io(base_url: str) -> None:
    recorder = Recorder()

    with pytest.raises(PinningError):
        make_client(recorder, base_url=base_url).execute("{ shop { name } }")
    assert recorder.requests == []


def test_mutation_for_unpinned_location_raises_before_network_io() -> None:
    recorder = Recorder()
    other = "gid://shopify/Location/999"
    (batch,) = batch_changes([change(1), change(2, location_id=other)])

    with pytest.raises(PinningError):
        make_client(recorder).set_quantities(batch)
    assert recorder.requests == []


def test_300_changes_are_sent_as_mutations_of_250_and_50() -> None:
    changes = [change(n) for n in range(300)]
    recorder = Recorder(set_quantities_response(), set_quantities_response())
    client = make_client(recorder)

    batches = batch_changes(changes)
    for batch in batches:
        client.set_quantities(batch)

    assert [len(entries(body)) for body in recorder.bodies()] == [BATCH_LIMIT, 50]
    sent = [
        entry["inventoryItemId"]
        for body in recorder.bodies()
        for entry in entries(body)
    ]
    assert sent == [c.inventory_item_id for c in changes]
    keys = sent_keys(recorder)
    assert keys == [batch.idempotency_key for batch in batches]
    assert len(set(keys)) == len(keys)


def test_batching_nothing_gives_no_batches() -> None:
    assert batch_changes([]) == []


def test_every_batch_gets_a_fresh_key() -> None:
    changes = [change(1)]

    assert batch_changes(changes)[0].idempotency_key != (
        batch_changes(changes)[0].idempotency_key
    )


def test_resending_a_batch_reuses_its_key() -> None:
    batch = InventoryBatch(changes=(change(1),), idempotency_key="in-memory-key")
    recorder = Recorder(set_quantities_response(), set_quantities_response())
    client = make_client(recorder)

    client.set_quantities(batch)
    client.set_quantities(batch)

    keys = sent_keys(recorder)
    assert keys == ["in-memory-key", "in-memory-key"]


def test_oversized_batch_is_refused() -> None:
    changes = tuple(change(n) for n in range(BATCH_LIMIT + 1))

    with pytest.raises(ValueError):
        make_client(Recorder()).set_quantities(InventoryBatch(changes, "key"))


def test_mutation_sets_available_with_change_from_quantity() -> None:
    recorder = Recorder(set_quantities_response())
    (batch,) = batch_changes([change(1, change_from=4), change(2, change_from=None)])

    make_client(recorder).set_quantities(batch)

    body = recorder.bodies()[0]
    query = body["query"]
    assert isinstance(query, str)
    assert "inventorySetQuantities" in query
    assert "@idempotent(key: $idempotencyKey)" in query
    mutation_input = variables(body)["input"]
    assert isinstance(mutation_input, dict)
    assert mutation_input["name"] == "available"
    assert entries(body) == [
        {
            "inventoryItemId": "gid://shopify/InventoryItem/1001",
            "locationId": LOCATION_ID,
            "quantity": 1,
            "changeFromQuantity": 4,
        },
        {
            "inventoryItemId": "gid://shopify/InventoryItem/1002",
            "locationId": LOCATION_ID,
            "quantity": 2,
            "changeFromQuantity": None,
        },
    ]


def test_user_errors_raise_with_codes_and_the_failed_entries() -> None:
    changes = [change(0), change(1), change(2)]
    recorder = Recorder(
        set_quantities_response(
            [user_error("INVALID_INVENTORY_ITEM", 1, "inventoryItemId")]
        )
    )

    with pytest.raises(ShopifyUserError) as excinfo:
        make_client(recorder).set_quantities(batch_changes(changes)[0])

    assert not isinstance(excinfo.value, ChangeFromQuantityStaleError)
    assert excinfo.value.codes == ("INVALID_INVENTORY_ITEM",)
    assert excinfo.value.failed_changes == (changes[1],)
    assert excinfo.value.user_errors[0].field == (
        "input",
        "quantities",
        "1",
        "inventoryItemId",
    )


def test_stale_change_from_quantity_is_a_distinct_error() -> None:
    changes = [change(0), change(1)]
    recorder = Recorder(
        set_quantities_response(
            [user_error("CHANGE_FROM_QUANTITY_STALE", 1, "changeFromQuantity")]
        )
    )

    with pytest.raises(ChangeFromQuantityStaleError) as excinfo:
        make_client(recorder).set_quantities(batch_changes(changes)[0])

    assert excinfo.value.codes == ("CHANGE_FROM_QUANTITY_STALE",)
    assert excinfo.value.failed_changes == (changes[1],)


def test_stale_mixed_with_another_user_error_is_not_only_stale() -> None:
    changes = [change(0), change(1), change(2)]
    recorder = Recorder(
        set_quantities_response(
            [
                user_error("CHANGE_FROM_QUANTITY_STALE", 0, "changeFromQuantity"),
                user_error("INVALID_LOCATION", 2, "locationId"),
            ]
        )
    )

    with pytest.raises(ShopifyUserError) as excinfo:
        make_client(recorder).set_quantities(batch_changes(changes)[0])

    assert not isinstance(excinfo.value, ChangeFromQuantityStaleError)
    assert excinfo.value.codes == ("CHANGE_FROM_QUANTITY_STALE", "INVALID_LOCATION")
    assert excinfo.value.failed_changes == (changes[0], changes[2])


def test_top_level_graphql_errors_raise_with_their_codes() -> None:
    recorder = Recorder(
        graphql_response(
            None,
            errors=[
                {
                    "message": "Access denied for locations field.",
                    "extensions": {"code": "ACCESS_DENIED"},
                }
            ],
        )
    )

    with pytest.raises(ShopifyGraphQLError) as excinfo:
        make_client(recorder).execute("{ locations(first: 1) { nodes { id } } }")
    assert excinfo.value.codes == ("ACCESS_DENIED",)


def test_throttle_status_near_the_limit_delays_the_next_call() -> None:
    recorder = Recorder(
        graphql_response({"shop": None}, requested=50.0, available=30.0),
        graphql_response({"shop": None}),
    )
    sleeps = Sleeps()
    client = make_client(recorder, sleep=sleeps)

    client.execute("{ shop { name } }")
    assert sleeps.calls == []
    client.execute("{ shop { name } }")

    assert sleeps.calls == [pytest.approx((50.0 - 30.0) / RESTORE_RATE)]
    assert len(recorder.requests) == 2


def test_ample_throttle_budget_does_not_delay() -> None:
    recorder = Recorder(
        graphql_response({"shop": None}, requested=50.0, available=1000.0),
        graphql_response({"shop": None}),
    )
    sleeps = Sleeps()
    client = make_client(recorder, sleep=sleeps)

    client.execute("{ shop { name } }")
    client.execute("{ shop { name } }")

    assert sleeps.calls == []


def test_throttled_request_waits_and_retries() -> None:
    recorder = Recorder(
        graphql_response(None, requested=200.0, available=40.0, errors=THROTTLED),
        graphql_response({"shop": {"name": "Example"}}),
    )
    sleeps = Sleeps()

    data = make_client(recorder, sleep=sleeps).execute("{ shop { name } }")

    assert data == {"shop": {"name": "Example"}}
    assert sleeps.calls == [pytest.approx((200.0 - 40.0) / RESTORE_RATE)]
    assert len(recorder.requests) == 2


def test_throttled_mutation_is_retried_with_the_same_key() -> None:
    recorder = Recorder(
        graphql_response(None, requested=200.0, available=40.0, errors=THROTTLED),
        set_quantities_response(),
    )
    (batch,) = batch_changes([change(1)])

    make_client(recorder).set_quantities(batch)

    keys = sent_keys(recorder)
    assert keys == [batch.idempotency_key, batch.idempotency_key]


def test_persistent_throttling_gives_up_after_the_bound() -> None:
    responses = [
        graphql_response(None, requested=200.0, available=0.0, errors=THROTTLED)
    ] * (MAX_THROTTLE_RETRIES + 1)
    recorder = Recorder(*responses)

    with pytest.raises(ShopifyGraphQLError) as excinfo:
        make_client(recorder).execute("{ shop { name } }")

    assert excinfo.value.codes == ("THROTTLED",)
    assert len(recorder.requests) == MAX_THROTTLE_RETRIES + 1


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_error_status_raises_without_retrying_by_default(status: int) -> None:
    recorder = Recorder(httpx.Response(status, json={"errors": "synthetic"}))

    with pytest.raises(ShopifyHTTPError) as excinfo:
        make_client(recorder).execute("{ shop { name } }")
    assert excinfo.value.status == status
    assert len(recorder.requests) == 1


def time_out_every_time(attempts: list[httpx.Request]) -> Handler:
    def time_out(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ReadTimeout("timed out", request=request)

    return time_out


def test_transport_error_raises_without_retrying_by_default() -> None:
    attempts: list[httpx.Request] = []

    with pytest.raises(ShopifyTransportError):
        make_client(time_out_every_time(attempts)).execute("{ shop { name } }")
    assert len(attempts) == 1


def test_idempotent_query_is_retried_after_a_server_error() -> None:
    recorder = Recorder(
        httpx.Response(503, json={"errors": "synthetic"}),
        graphql_response({"shop": {"name": "Example"}}),
    )
    sleeps = Sleeps()

    data = make_client(recorder, sleep=sleeps).execute(
        "{ shop { name } }", idempotent=True
    )

    assert data == {"shop": {"name": "Example"}}
    assert len(recorder.requests) == 2
    assert sleeps.calls == [RETRY_DELAY_SECONDS]


def test_idempotent_query_gives_up_after_the_bound() -> None:
    responses = [httpx.Response(502, json={"errors": "synthetic"})] * MAX_ATTEMPTS
    recorder = Recorder(*responses)
    sleeps = Sleeps()

    with pytest.raises(ShopifyHTTPError) as excinfo:
        make_client(recorder, sleep=sleeps).execute(
            "{ shop { name } }", idempotent=True
        )

    assert excinfo.value.status == 502
    assert len(recorder.requests) == MAX_ATTEMPTS
    assert sleeps.calls == [RETRY_DELAY_SECONDS * 2**n for n in range(MAX_ATTEMPTS - 1)]


def test_idempotent_query_is_retried_after_a_read_timeout() -> None:
    attempts: list[httpx.Request] = []

    def time_out_once(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return graphql_response({"shop": {"name": "Example"}})

    data = make_client(time_out_once).execute("{ shop { name } }", idempotent=True)

    assert data == {"shop": {"name": "Example"}}
    assert len(attempts) == 2


def test_idempotent_query_transport_errors_give_up_after_the_bound() -> None:
    attempts: list[httpx.Request] = []

    with pytest.raises(ShopifyTransportError):
        make_client(time_out_every_time(attempts)).execute(
            "{ shop { name } }", idempotent=True
        )
    assert len(attempts) == MAX_ATTEMPTS


@pytest.mark.parametrize("status", [401, 403, 404])
def test_idempotent_query_client_error_is_not_retried(status: int) -> None:
    recorder = Recorder(httpx.Response(status, json={"errors": "synthetic"}))

    with pytest.raises(ShopifyHTTPError):
        make_client(recorder).execute("{ shop { name } }", idempotent=True)
    assert len(recorder.requests) == 1


def test_mutation_is_not_retried_after_a_server_error() -> None:
    recorder = Recorder(httpx.Response(503, json={"errors": "synthetic"}))
    sleeps = Sleeps()

    with pytest.raises(ShopifyHTTPError):
        make_client(recorder, sleep=sleeps).set_quantities(
            batch_changes([change(1)])[0]
        )
    assert len(recorder.requests) == 1
    assert sleeps.calls == []


def test_mutation_is_not_retried_after_a_read_timeout() -> None:
    attempts: list[httpx.Request] = []

    with pytest.raises(ShopifyTransportError):
        make_client(time_out_every_time(attempts)).set_quantities(
            batch_changes([change(1)])[0]
        )
    assert len(attempts) == 1


def test_mutation_that_changes_nothing_succeeds() -> None:
    # Shopify answers a set whose quantities already match with no group.
    recorder = Recorder(
        graphql_response(
            {
                "inventorySetQuantities": {
                    "inventoryAdjustmentGroup": None,
                    "userErrors": [],
                }
            }
        )
    )

    make_client(recorder).set_quantities(batch_changes([change(1)])[0])

    assert len(recorder.requests) == 1


def test_logs_and_errors_carry_no_token_or_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    recorder = Recorder(
        set_quantities_response(
            [user_error("CHANGE_FROM_QUANTITY_STALE", 0, "changeFromQuantity")]
        ),
        httpx.Response(500, json={"errors": "synthetic server detail"}),
    )
    client = make_client(recorder)
    batch = batch_changes([change(1)])[0]

    with pytest.raises(ShopifyUserError) as user_excinfo:
        client.set_quantities(batch)
    with pytest.raises(ShopifyHTTPError) as http_excinfo:
        client.set_quantities(batch)

    texts = [caplog.text, str(user_excinfo.value), str(http_excinfo.value)]
    for text in texts:
        assert ACCESS_TOKEN not in text
        assert "synthetic server detail" not in text
        assert "InventoryItem/1001" not in text
        assert batch.idempotency_key not in text
    assert f"POST {SHOP_DOMAIN} {GRAPHQL_PATH} 500" in caplog.text


@pytest.mark.parametrize("status", [301, 302, 307])
def test_redirect_raises_without_following(status: int) -> None:
    requests: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host != SHOP_DOMAIN:
            return graphql_response({"shop": {"name": "Elsewhere"}})
        location = f"https://other.example.myshopify.com{GRAPHQL_PATH}"
        return httpx.Response(status, headers={"Location": location})

    # A client configured to follow redirects must not carry the token elsewhere.
    http = httpx.Client(
        base_url=f"https://{SHOP_DOMAIN}",
        follow_redirects=True,
        transport=httpx.MockTransport(redirect),
    )
    client = ShopifyClient(make_config(), http, sleep=Sleeps())

    with pytest.raises(ShopifyHTTPError) as excinfo:
        client.execute("{ shop { name } }")

    assert excinfo.value.status == status
    assert [r.url.host for r in requests] == [SHOP_DOMAIN]
