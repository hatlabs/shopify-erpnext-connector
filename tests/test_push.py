import json
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from helpers import LOCATION_ID, SHOP_DOMAIN, make_config

from shopify_erpnext_connector.poller import mark_dirty
from shopify_erpnext_connector.problems import Problem, ProblemKind
from shopify_erpnext_connector.push import (
    LEVELS_PAGE_SIZE,
    TRANSPORT_FAILURE,
    Level,
    PushOutcome,
    PushTarget,
    drop_unbound_pending,
    fetch_levels,
    last_pushed_values,
    push,
    stale_pushes,
)
from shopify_erpnext_connector.shopify import (
    BATCH_LIMIT,
    CHANGE_FROM_QUANTITY_STALE,
    ShopifyClient,
    ShopifyError,
)
from shopify_erpnext_connector.store import open_store, utc_timestamp

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)

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


class TimeOut:
    """Stands in for a response: the request times out instead."""


Step = httpx.Response | TimeOut
SentEntry = tuple[str, int, int | None]


class Shopify:
    """Answers each request with the next step and records the request."""

    def __init__(self, *steps: Step) -> None:
        self.steps = list(steps)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        step = self.steps.pop(0)
        if isinstance(step, TimeOut):
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        return step

    def client(self) -> ShopifyClient:
        return shopify_client(self)

    def mutations(self) -> list[dict[str, object]]:
        """The variables of each inventory mutation sent."""
        found: list[dict[str, object]] = []
        for request in self.requests:
            variables = json.loads(request.content)["variables"]
            assert isinstance(variables, dict)
            if "input" in variables:
                found.append(variables)
        return found

    def keys(self) -> list[str]:
        return [str(m["idempotencyKey"]) for m in self.mutations()]

    def sent(self) -> list[list[SentEntry]]:
        """Per mutation: (inventory item, quantity, changeFromQuantity) entries."""
        batches: list[list[SentEntry]] = []
        for variables in self.mutations():
            mutation_input = variables["input"]
            assert isinstance(mutation_input, dict)
            batches.append(
                [
                    (q["inventoryItemId"], q["quantity"], q["changeFromQuantity"])
                    for q in mutation_input["quantities"]
                ]
            )
        return batches


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


# Pushing


def variant_id(n: int) -> str:
    return f"gid://shopify/ProductVariant/{n}"


def target(n: int, value: int, current: int | None) -> PushTarget:
    return PushTarget(
        variant_id=variant_id(n),
        inventory_item_id=inventory_item_id(n),
        value=value,
        current=current,
    )


def applied() -> httpx.Response:
    return graphql(
        {
            "inventorySetQuantities": {
                "inventoryAdjustmentGroup": {
                    "id": "gid://shopify/InventoryAdjustmentGroup/1"
                },
                "userErrors": [],
            }
        }
    )


def rejected(*errors: tuple[str, list[str]]) -> httpx.Response:
    # Recorded shape: on any user error the group is null and nothing is applied.
    return graphql(
        {
            "inventorySetQuantities": {
                "inventoryAdjustmentGroup": None,
                "userErrors": [
                    {
                        "code": code,
                        "field": field,
                        "message": "synthetic user error message",
                    }
                    for code, field in errors
                ],
            }
        }
    )


def at_entry(index: int) -> list[str]:
    return ["input", "quantities", str(index), "changeFromQuantity"]


@pytest.fixture
def store(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_store(tmp_path / "connector.sqlite3")
    yield connection
    connection.close()


def pending(store: sqlite3.Connection) -> list[tuple[str, int, int | None, str]]:
    return store.execute(
        "SELECT variant_id, available, change_from, queued_at"
        " FROM pending_pushes ORDER BY variant_id"
    ).fetchall()


def quantities(
    store: sqlite3.Connection,
) -> list[tuple[str, int | None, str | None, int | None, str | None]]:
    return store.execute(
        "SELECT variant_id, last_read, last_read_at, last_pushed, last_pushed_at"
        " FROM variant_quantities ORDER BY variant_id"
    ).fetchall()


def history(
    store: sqlite3.Connection,
) -> list[tuple[str, int, int | None, str, str, str]]:
    return store.execute(
        "SELECT variant_id, available, change_from, idempotency_key, attempted_at,"
        " outcome FROM push_history ORDER BY id"
    ).fetchall()


def test_value_equal_to_shopifys_is_not_pushed(store: sqlite3.Connection) -> None:
    shopify = Shopify()
    store.execute(
        "INSERT INTO pending_pushes VALUES (?, 4, 2, ?)",
        (variant_id(1), utc_timestamp(NOW)),
    )

    result = push(store, shopify.client(), LOCATION_ID, [target(1, 4, 4)], LATER)

    assert result.outcomes == {variant_id(1): PushOutcome.UNCHANGED}
    assert shopify.requests == []
    assert pending(store) == []
    stamp = utc_timestamp(LATER)
    assert quantities(store) == [(variant_id(1), 4, stamp, 4, stamp)]
    assert history(store) == []


def test_differing_value_is_pushed_against_the_level_read(
    store: sqlite3.Connection,
) -> None:
    shopify = Shopify(applied())

    result = push(store, shopify.client(), LOCATION_ID, [target(1, 7, 4)], NOW)

    assert result.outcomes == {variant_id(1): PushOutcome.PUSHED}
    assert result.codes == {}
    assert shopify.sent() == [[(inventory_item_id(1), 7, 4)]]
    mutation_input = shopify.mutations()[0]["input"]
    assert isinstance(mutation_input, dict)
    assert mutation_input["quantities"][0]["locationId"] == LOCATION_ID
    stamp = utc_timestamp(NOW)
    assert pending(store) == []
    assert quantities(store) == [(variant_id(1), 4, stamp, 7, stamp)]
    assert history(store) == [(variant_id(1), 7, 4, shopify.keys()[0], stamp, "pushed")]


def test_unchanged_variant_records_the_value_as_confirmed(
    store: sqlite3.Connection,
) -> None:
    push(store, Shopify().client(), LOCATION_ID, [target(1, 7, 7)], LATER)

    stamp = utc_timestamp(LATER)
    assert quantities(store) == [(variant_id(1), 7, stamp, 7, stamp)]


def test_item_without_a_level_is_not_pushed_and_stays_pending(
    store: sqlite3.Connection,
) -> None:
    shopify = Shopify()

    result = push(store, shopify.client(), LOCATION_ID, [target(1, 3, None)], NOW)

    assert result.outcomes == {variant_id(1): PushOutcome.NO_LEVEL}
    assert shopify.requests == []
    assert pending(store) == [(variant_id(1), 3, None, utc_timestamp(NOW))]
    assert history(store) == []


def test_no_targets_send_nothing(store: sqlite3.Connection) -> None:
    shopify = Shopify()

    result = push(store, shopify.client(), LOCATION_ID, [], NOW)

    assert result.outcomes == {}
    assert shopify.requests == []


def test_a_variant_listed_twice_is_refused(store: sqlite3.Connection) -> None:
    with pytest.raises(ValueError, match="ProductVariant/1"):
        push(
            store,
            Shopify().client(),
            LOCATION_ID,
            [target(1, 3, 2), target(1, 4, 2)],
            NOW,
        )


def test_stale_entry_is_dropped_and_the_rest_resent_under_a_new_key(
    store: sqlite3.Connection,
) -> None:
    shopify = Shopify(
        rejected((CHANGE_FROM_QUANTITY_STALE, at_entry(1))),
        applied(),
    )
    targets = [target(1, 5, 1), target(2, 6, 2), target(3, 7, 3)]

    result = push(store, shopify.client(), LOCATION_ID, targets, NOW)

    assert result.outcomes == {
        variant_id(1): PushOutcome.PUSHED,
        variant_id(2): PushOutcome.STALE,
        variant_id(3): PushOutcome.PUSHED,
    }
    assert result.codes == {variant_id(2): CHANGE_FROM_QUANTITY_STALE}
    assert shopify.sent() == [
        [
            (inventory_item_id(1), 5, 1),
            (inventory_item_id(2), 6, 2),
            (inventory_item_id(3), 7, 3),
        ],
        [(inventory_item_id(1), 5, 1), (inventory_item_id(3), 7, 3)],
    ]
    first, second = shopify.keys()
    assert first != second
    stamp = utc_timestamp(NOW)
    assert pending(store) == [(variant_id(2), 6, 2, stamp)]
    assert history(store) == [
        (variant_id(2), 6, 2, first, stamp, "stale"),
        (variant_id(1), 5, 1, second, stamp, "pushed"),
        (variant_id(3), 7, 3, second, stamp, "pushed"),
    ]


def test_entry_with_another_user_error_fails_and_the_rest_is_resent(
    store: sqlite3.Connection,
) -> None:
    shopify = Shopify(
        rejected(("INVALID_INVENTORY_ITEM", at_entry(1))),
        applied(),
    )
    targets = [target(1, 5, 1), target(2, 6, 2), target(3, 7, 3)]

    result = push(store, shopify.client(), LOCATION_ID, targets, NOW)

    assert result.outcomes == {
        variant_id(1): PushOutcome.PUSHED,
        variant_id(2): PushOutcome.FAILED,
        variant_id(3): PushOutcome.PUSHED,
    }
    assert result.codes == {variant_id(2): "INVALID_INVENTORY_ITEM"}
    assert shopify.sent()[1] == [
        (inventory_item_id(1), 5, 1),
        (inventory_item_id(3), 7, 3),
    ]
    assert pending(store) == [(variant_id(2), 6, 2, utc_timestamp(NOW))]
    assert history(store)[0][5] == "failed"


@pytest.mark.parametrize("stale_first", [True, False])
def test_entry_both_stale_and_otherwise_rejected_fails_with_the_other_code(
    store: sqlite3.Connection, stale_first: bool
) -> None:
    errors = [
        (CHANGE_FROM_QUANTITY_STALE, at_entry(0)),
        ("INVALID_INVENTORY_ITEM", at_entry(0)),
    ]
    shopify = Shopify(rejected(*(errors if stale_first else errors[::-1])))

    result = push(store, shopify.client(), LOCATION_ID, [target(1, 5, 1)], NOW)

    assert result.outcomes == {variant_id(1): PushOutcome.FAILED}
    assert result.codes == {variant_id(1): "INVALID_INVENTORY_ITEM"}


def test_rejections_over_several_rounds_each_use_a_new_key(
    store: sqlite3.Connection,
) -> None:
    # Whether Shopify names every bad entry at once is unverified, so a
    # resend may be rejected for another entry.
    shopify = Shopify(
        rejected((CHANGE_FROM_QUANTITY_STALE, at_entry(0))),
        rejected(("INVALID_INVENTORY_ITEM", at_entry(1))),
        applied(),
    )
    targets = [target(1, 5, 1), target(2, 6, 2), target(3, 7, 3)]

    result = push(store, shopify.client(), LOCATION_ID, targets, NOW)

    assert result.outcomes == {
        variant_id(1): PushOutcome.STALE,
        variant_id(2): PushOutcome.PUSHED,
        variant_id(3): PushOutcome.FAILED,
    }
    assert shopify.sent()[2] == [(inventory_item_id(2), 6, 2)]
    assert len(set(shopify.keys())) == len(shopify.requests)


def test_every_entry_rejected_sends_nothing_more(store: sqlite3.Connection) -> None:
    shopify = Shopify(
        rejected(
            (CHANGE_FROM_QUANTITY_STALE, at_entry(0)),
            (CHANGE_FROM_QUANTITY_STALE, at_entry(1)),
        )
    )

    result = push(
        store, shopify.client(), LOCATION_ID, [target(1, 5, 1), target(2, 6, 2)], NOW
    )

    assert set(result.outcomes.values()) == {PushOutcome.STALE}
    assert len(shopify.requests) == 1


def test_user_error_naming_no_entry_fails_the_whole_batch(
    store: sqlite3.Connection,
) -> None:
    shopify = Shopify(
        rejected(
            (CHANGE_FROM_QUANTITY_STALE, at_entry(0)),
            ("INVALID_LOCATION", ["input"]),
        )
    )

    result = push(
        store, shopify.client(), LOCATION_ID, [target(1, 5, 1), target(2, 6, 2)], NOW
    )

    assert result.outcomes == {
        variant_id(1): PushOutcome.FAILED,
        variant_id(2): PushOutcome.FAILED,
    }
    assert result.codes == {
        variant_id(1): "INVALID_LOCATION",
        variant_id(2): "INVALID_LOCATION",
    }
    assert len(shopify.requests) == 1
    assert [row[0] for row in pending(store)] == [variant_id(1), variant_id(2)]
    assert [row[5] for row in history(store)] == ["failed", "failed"]


def test_transport_error_resends_the_same_batch_with_the_same_key(
    store: sqlite3.Connection,
) -> None:
    shopify = Shopify(TimeOut(), applied())

    result = push(store, shopify.client(), LOCATION_ID, [target(1, 7, 4)], NOW)

    assert result.outcomes == {variant_id(1): PushOutcome.PUSHED}
    first, second = shopify.keys()
    assert first == second
    assert shopify.sent()[0] == shopify.sent()[1]
    assert pending(store) == []
    assert history(store) == [
        (variant_id(1), 7, 4, first, utc_timestamp(NOW), "pushed")
    ]


def test_two_transport_errors_fail_the_batch_and_keep_it_pending(
    store: sqlite3.Connection,
) -> None:
    shopify = Shopify(TimeOut(), TimeOut())

    result = push(store, shopify.client(), LOCATION_ID, [target(1, 7, 4)], NOW)

    assert result.outcomes == {variant_id(1): PushOutcome.FAILED}
    assert result.codes == {variant_id(1): TRANSPORT_FAILURE}
    assert len(shopify.requests) == 2
    stamp = utc_timestamp(NOW)
    assert pending(store) == [(variant_id(1), 7, 4, stamp)]
    assert quantities(store) == [(variant_id(1), 4, stamp, None, None)]
    assert history(store) == [(variant_id(1), 7, 4, shopify.keys()[0], stamp, "failed")]


def test_user_error_after_a_transport_error_is_handled_like_any_other(
    store: sqlite3.Connection,
) -> None:
    shopify = Shopify(
        TimeOut(),
        rejected((CHANGE_FROM_QUANTITY_STALE, at_entry(0))),
        applied(),
    )

    result = push(
        store, shopify.client(), LOCATION_ID, [target(1, 5, 1), target(2, 6, 2)], NOW
    )

    assert result.outcomes == {
        variant_id(1): PushOutcome.STALE,
        variant_id(2): PushOutcome.PUSHED,
    }
    first, resent, rebuilt = shopify.keys()
    assert first == resent != rebuilt


ACCESS_DENIED = httpx.Response(
    200,
    json={
        "data": None,
        "errors": [{"message": "synthetic", "extensions": {"code": "ACCESS_DENIED"}}],
    },
)


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(503), "http_503"),
        (ACCESS_DENIED, "ACCESS_DENIED"),
        (graphql({"inventorySetQuantities": None}), "unexpected_response"),
    ],
)
def test_failed_batch_stays_pending_and_is_not_resent(
    store: sqlite3.Connection, response: httpx.Response, code: str
) -> None:
    shopify = Shopify(response)

    result = push(store, shopify.client(), LOCATION_ID, [target(1, 7, 4)], NOW)

    assert result.outcomes == {variant_id(1): PushOutcome.FAILED}
    assert result.codes == {variant_id(1): code}
    assert len(shopify.requests) == 1
    assert pending(store) == [(variant_id(1), 7, 4, utc_timestamp(NOW))]
    assert [row[5] for row in history(store)] == ["failed"]


def test_more_targets_than_a_batch_holds_are_sent_in_several_batches(
    store: sqlite3.Connection,
) -> None:
    count = BATCH_LIMIT + 1
    shopify = Shopify(applied(), applied())

    result = push(
        store,
        shopify.client(),
        LOCATION_ID,
        [target(n, n + 1, n) for n in range(count)],
        NOW,
    )

    assert [len(batch) for batch in shopify.sent()] == [BATCH_LIMIT, 1]
    assert len(set(shopify.keys())) == 2
    assert len(result.outcomes) == count
    assert set(result.outcomes.values()) == {PushOutcome.PUSHED}


def test_a_failed_batch_does_not_stop_the_next(store: sqlite3.Connection) -> None:
    count = BATCH_LIMIT + 1
    shopify = Shopify(httpx.Response(503), applied())

    result = push(
        store,
        shopify.client(),
        LOCATION_ID,
        [target(n, n + 1, n) for n in range(count)],
        NOW,
    )

    outcomes = list(result.outcomes.values())
    assert outcomes.count(PushOutcome.FAILED) == BATCH_LIMIT
    assert outcomes.count(PushOutcome.PUSHED) == 1
    assert len(pending(store)) == BATCH_LIMIT


def test_pending_values_collapse_to_the_latest_while_shopify_is_down(
    store: sqlite3.Connection,
) -> None:
    down = Shopify(httpx.Response(503), httpx.Response(503))

    push(store, down.client(), LOCATION_ID, [target(1, 5, 2)], NOW)
    push(store, down.client(), LOCATION_ID, [target(1, 4, 3)], LATER)

    assert pending(store) == [(variant_id(1), 4, 3, utc_timestamp(NOW))]


def test_recovery_pushes_only_the_latest_value(store: sqlite3.Connection) -> None:
    down = Shopify(httpx.Response(503), httpx.Response(503))
    push(store, down.client(), LOCATION_ID, [target(1, 5, 2)], NOW)
    push(store, down.client(), LOCATION_ID, [target(1, 4, 2)], LATER)

    recovered = Shopify(applied())
    push(store, recovered.client(), LOCATION_ID, [target(1, 3, 2)], LATER)

    assert recovered.sent() == [[(inventory_item_id(1), 3, 2)]]
    assert pending(store) == []
    assert [(row[1], row[5]) for row in history(store)] == [
        (5, "failed"),
        (4, "failed"),
        (3, "pushed"),
    ]


def test_last_pushed_values_are_read_by_variant(store: sqlite3.Connection) -> None:
    push(store, Shopify(applied()).client(), LOCATION_ID, [target(1, 7, 4)], NOW)
    push(store, Shopify().client(), LOCATION_ID, [target(2, 3, 3)], NOW)
    down = Shopify(httpx.Response(503))
    push(store, down.client(), LOCATION_ID, [target(3, 5, 1)], NOW)

    assert last_pushed_values(store) == {variant_id(1): 7, variant_id(2): 3}


def test_unbound_variants_lose_their_pending_values(
    store: sqlite3.Connection,
) -> None:
    store.execute(
        "INSERT INTO bindings VALUES (?, ?, 'TEST-ITEM-001', ?)",
        (variant_id(1), inventory_item_id(1), utc_timestamp(NOW)),
    )
    push(
        store,
        Shopify().client(),
        LOCATION_ID,
        [target(1, 3, None), target(2, 4, None)],
        NOW,
    )

    drop_unbound_pending(store)

    assert [row[0] for row in pending(store)] == [variant_id(1)]


def test_levels_read_then_pushed(store: sqlite3.Connection) -> None:
    shopify = Shopify(
        levels_response(level_node(1, 4), level_node(2, 6), level_node(3, None)),
        applied(),
    )
    client = shopify.client()
    sellable = {1: 5, 2: 6, 3: 1}

    levels = fetch_levels(client, [inventory_item_id(n) for n in sellable], LOCATION_ID)
    targets = [
        target(n, value, levels[inventory_item_id(n)].available)
        for n, value in sellable.items()
    ]
    result = push(store, client, LOCATION_ID, targets, NOW)

    assert result.outcomes == {
        variant_id(1): PushOutcome.PUSHED,
        variant_id(2): PushOutcome.UNCHANGED,
        variant_id(3): PushOutcome.NO_LEVEL,
    }
    assert shopify.sent() == [[(inventory_item_id(1), 5, 4)]]


# Stale pushes

THRESHOLD = timedelta(minutes=30)


def fail_pushes(store: sqlite3.Connection, targets: list[PushTarget]) -> None:
    down = Shopify(httpx.Response(503))
    push(store, down.client(), LOCATION_ID, targets, NOW)


def stale(store: sqlite3.Connection, now: datetime = NOW + THRESHOLD) -> Problem | None:
    return stale_pushes(store, now, THRESHOLD, location_id=LOCATION_ID)


def item_code(n: int) -> str:
    return f"TEST-ITEM-{n:03d}"


def bind(store: sqlite3.Connection, n: int) -> None:
    store.execute(
        "INSERT INTO bindings (variant_id, inventory_item_id, item_code, bound_at)"
        " VALUES (?, ?, ?, ?)",
        (variant_id(n), inventory_item_id(n), item_code(n), utc_timestamp(NOW)),
    )


def test_variants_unpushed_past_the_threshold_are_one_problem(
    store: sqlite3.Connection,
) -> None:
    fail_pushes(store, [target(2, 6, 1), target(1, 5, 1)])

    problem = stale(store)

    assert problem == Problem(
        ProblemKind.STALE_PUSH,
        LOCATION_ID,
        {},
        context={
            "Unpushed variants": "2",
            "Unpushed since": "2026-11-12 10:00 UTC",
            "Variants": f"{variant_id(1)}, {variant_id(2)}",
        },
    )


def test_stale_push_lists_at_most_ten_variants(store: sqlite3.Connection) -> None:
    fail_pushes(store, [target(n, 5, 1) for n in range(12)])

    problem = stale(store)

    assert problem is not None
    assert problem.context["Unpushed variants"] == "12"
    assert problem.context["Variants"] == ", ".join(
        sorted(variant_id(n) for n in range(12))[:10]
    )


def test_variant_unpushed_for_less_than_the_threshold_is_no_problem(
    store: sqlite3.Connection,
) -> None:
    fail_pushes(store, [target(1, 5, 1)])

    assert stale(store, NOW + THRESHOLD - timedelta(microseconds=1)) is None


def test_stale_push_fingerprint_stays_as_the_variants_change(
    store: sqlite3.Connection,
) -> None:
    fail_pushes(store, [target(1, 5, 1)])
    first = stale(store, LATER + THRESHOLD)
    down = Shopify(httpx.Response(503))
    push(store, down.client(), LOCATION_ID, [target(1, 4, 1), target(2, 3, 1)], LATER)

    second = stale(store, LATER + THRESHOLD)

    assert first is not None
    assert second is not None
    assert second.context["Unpushed variants"] == "2"
    assert second.fingerprint == first.fingerprint


def test_pushed_variant_is_no_longer_a_stale_push(store: sqlite3.Connection) -> None:
    fail_pushes(store, [target(1, 5, 1)])
    push(store, Shopify(applied()).client(), LOCATION_ID, [target(1, 5, 1)], LATER)

    assert stale(store) is None


def test_variant_waiting_past_the_threshold_without_a_pending_value_is_stale(
    store: sqlite3.Connection,
) -> None:
    bind(store, 1)
    bind(store, 2)
    mark_dirty(store, [item_code(1)], NOW)
    mark_dirty(store, [item_code(2)], LATER)

    problem = stale(store)

    assert problem is not None
    assert problem.context["Variants"] == variant_id(1)


def test_dirty_item_that_is_not_bound_is_no_stale_push(
    store: sqlite3.Connection,
) -> None:
    mark_dirty(store, [item_code(1)], NOW)

    assert stale(store) is None


def test_variant_both_waiting_and_pending_counts_once_since_the_earlier(
    store: sqlite3.Connection,
) -> None:
    fail_pushes(store, [target(1, 5, 1)])
    bind(store, 1)
    mark_dirty(store, [item_code(1)], NOW - timedelta(minutes=10))

    problem = stale(store)

    assert problem is not None
    assert problem.context["Unpushed variants"] == "1"
    assert problem.context["Unpushed since"] == "2026-11-12 09:50 UTC"
