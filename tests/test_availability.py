import json
from collections.abc import Callable

import httpx
import pytest
from helpers import ERPNEXT_URL, make_config

from shopify_erpnext_connector.availability import (
    BIN_CHUNK_SIZE,
    BinQuantities,
    compute_sellable,
    fetch_bins,
)
from shopify_erpnext_connector.erpnext import ErpNextClient, ErpNextError

WAREHOUSE = "Stores - TEST"

Handler = Callable[[httpx.Request], httpx.Response]


def bin_of(
    actual: float = 0,
    reserved: float = 0,
    reserved_for_production: float = 0,
    planned: float = 0,
) -> BinQuantities:
    return BinQuantities(
        actual=actual,
        reserved=reserved,
        reserved_for_production=reserved_for_production,
        planned=planned,
    )


def test_sellable_is_actual_less_reserved() -> None:
    assert compute_sellable(bin_of(actual=5, reserved=2), 0).sellable == 3


def test_sellable_is_floored_at_zero_when_reserved_exceeds_actual() -> None:
    assert compute_sellable(bin_of(actual=1, reserved=3), 0).sellable == 0


def test_sellable_subtracts_reserved_for_production() -> None:
    quantities = bin_of(actual=10, reserved=2, reserved_for_production=3)

    result = compute_sellable(quantities, 0)

    assert result.sellable == 5


def test_sellable_subtracts_unmirrored_orders() -> None:
    assert compute_sellable(bin_of(actual=5, reserved=1), 2).sellable == 2


def test_sellable_ignores_planned_quantity() -> None:
    assert compute_sellable(bin_of(actual=2, planned=10), 0).sellable == 2


def test_fractional_sellable_is_floored() -> None:
    assert compute_sellable(bin_of(actual=2.5, reserved=0.3), 0).sellable == 2


def test_float_noise_does_not_lose_a_unit() -> None:
    # In binary floats 2.3 - 0.1 - 0.2 is 1.9999999999999998.
    result = compute_sellable(
        bin_of(actual=2.3, reserved=0.1, reserved_for_production=0.2), 0
    )

    assert result.sellable == 2


def test_no_shortfall_when_stock_covers_demand() -> None:
    assert compute_sellable(bin_of(actual=5, reserved=5), 0).shortfall == 0


def test_shortfall_when_demand_exceeds_stock_without_a_work_order() -> None:
    result = compute_sellable(bin_of(actual=1, reserved=3), 0)

    assert result.shortfall == 2


def test_unmirrored_orders_count_toward_the_shortfall() -> None:
    assert compute_sellable(bin_of(actual=1, reserved=1), 2).shortfall == 2


def test_planned_quantity_covers_the_shortfall_but_not_sellable() -> None:
    result = compute_sellable(bin_of(actual=1, reserved=3, planned=2), 0)

    assert result.shortfall == 0
    assert result.sellable == 0


def test_fractional_shortfall_is_rounded_up() -> None:
    assert compute_sellable(bin_of(actual=1, reserved=1.5), 0).shortfall == 1


def test_old_formula_truncates_and_goes_negative() -> None:
    quantities = bin_of(actual=1.9, reserved=3.7, reserved_for_production=5)

    result = compute_sellable(quantities, 0)

    assert result.old_formula == -2


def test_explanation_lists_the_quantities_used() -> None:
    result = compute_sellable(
        bin_of(actual=5, reserved=2, reserved_for_production=1, planned=4), 1
    )

    assert result.explanation() == {
        "actual_qty": 5,
        "reserved_qty": 2,
        "reserved_qty_for_production": 1,
        "planned_qty": 4,
        "unmirrored": 1,
        "sellable": 1,
        "shortfall": 0,
        "old_formula": 3,
    }


class Recorder:
    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)

    def filters(self, index: int) -> list[list[object]]:
        return json.loads(self.requests[index].url.params["filters"])


def erpnext_client(handler: Handler) -> ErpNextClient:
    http = httpx.Client(base_url=ERPNEXT_URL, transport=httpx.MockTransport(handler))
    return ErpNextClient(make_config(), http, sleep=lambda _: None)


def bin_row(code: str, actual: float, reserved: float = 0) -> dict[str, object]:
    return {
        "item_code": code,
        "actual_qty": actual,
        "reserved_qty": reserved,
        "reserved_qty_for_production": 0,
        "planned_qty": 0,
    }


def rows(*bins: dict[str, object]) -> httpx.Response:
    return httpx.Response(200, json={"data": list(bins)})


def test_fetch_bins_reads_the_bins_of_the_items_in_the_warehouse() -> None:
    recorder = Recorder(rows(bin_row("TEST-ITEM-001", 5.0, 2)))

    bins = fetch_bins(erpnext_client(recorder), ["TEST-ITEM-001"], WAREHOUSE)

    assert bins == {"TEST-ITEM-001": bin_of(actual=5, reserved=2)}
    request = recorder.requests[0]
    assert request.url.path == "/api/resource/Bin"
    assert json.loads(request.url.params["fields"]) == [
        "item_code",
        "actual_qty",
        "reserved_qty",
        "reserved_qty_for_production",
        "planned_qty",
    ]
    assert recorder.filters(0) == [
        ["warehouse", "=", WAREHOUSE],
        ["item_code", "in", ["TEST-ITEM-001"]],
    ]


def test_item_without_a_bin_gets_zero_quantities() -> None:
    recorder = Recorder(rows())

    bins = fetch_bins(erpnext_client(recorder), ["TEST-ITEM-001"], WAREHOUSE)

    assert bins == {"TEST-ITEM-001": bin_of()}


def test_fetch_bins_of_no_items_sends_no_request() -> None:
    recorder = Recorder()

    assert fetch_bins(erpnext_client(recorder), [], WAREHOUSE) == {}
    assert recorder.requests == []


def test_fetch_bins_splits_a_long_item_list_into_chunks() -> None:
    codes = [f"TEST-ITEM-{n:03}" for n in range(BIN_CHUNK_SIZE + 1)]
    recorder = Recorder(rows(bin_row(codes[0], 1)), rows(bin_row(codes[-1], 2)))

    bins = fetch_bins(erpnext_client(recorder), codes, WAREHOUSE)

    assert len(recorder.requests) == 2
    assert recorder.filters(0)[1] == ["item_code", "in", codes[:BIN_CHUNK_SIZE]]
    assert recorder.filters(1)[1] == ["item_code", "in", codes[BIN_CHUNK_SIZE:]]
    assert bins[codes[0]] == bin_of(actual=1)
    assert bins[codes[-1]] == bin_of(actual=2)
    assert len(bins) == len(codes)


@pytest.mark.parametrize(
    "row",
    [
        {**bin_row("TEST-ITEM-001", 1), "item_code": None},
        {**bin_row("TEST-ITEM-001", 1), "actual_qty": "5"},
        {**bin_row("TEST-ITEM-001", 1), "reserved_qty": True},
        {"item_code": "TEST-ITEM-001", "actual_qty": 1},
    ],
)
def test_fetch_bins_rejects_an_unexpected_row(row: dict[str, object]) -> None:
    recorder = Recorder(rows(row))

    with pytest.raises(ErpNextError):
        fetch_bins(erpnext_client(recorder), ["TEST-ITEM-001"], WAREHOUSE)
