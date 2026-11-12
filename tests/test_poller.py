import json
import sqlite3
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from helpers import ERPNEXT_URL, make_config

from shopify_erpnext_connector.erpnext import ErpNextClient, ErpNextError
from shopify_erpnext_connector.poller import (
    POLL_OVERLAP,
    DirtyItem,
    SeenBins,
    clear_dirty,
    dirty_items,
    mark_dirty,
    poll_bins,
    record_failed_attempt,
)
from shopify_erpnext_connector.store import open_store, utc_timestamp

WAREHOUSE = "Stores - TEST"
NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=1)

Handler = Callable[[httpx.Request], httpx.Response]


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


def rows(*bins: tuple[str, str]) -> httpx.Response:
    data = [{"item_code": code, "modified": modified} for code, modified in bins]
    return httpx.Response(200, json={"data": data})


@pytest.fixture
def store(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_store(tmp_path / "connector.sqlite3")
    yield connection
    connection.close()


def watermark(store: sqlite3.Connection) -> str | None:
    row = store.execute("SELECT bin_modified FROM watermark").fetchone()
    return None if row is None else row[0]


def set_watermark(store: sqlite3.Connection, value: str) -> None:
    store.execute("INSERT INTO watermark (id, bin_modified) VALUES (1, ?)", (value,))


def poll(
    store: sqlite3.Connection,
    seen: SeenBins,
    *responses: httpx.Response,
    now: datetime = NOW,
) -> tuple[set[str], Recorder]:
    recorder = Recorder(*responses)
    changed = poll_bins(store, erpnext_client(recorder), WAREHOUSE, seen, now)
    return changed, recorder


# Polling


def test_first_poll_marks_every_bin_and_sets_the_watermark(
    store: sqlite3.Connection,
) -> None:
    changed, recorder = poll(
        store,
        SeenBins(),
        rows(
            ("TEST-ITEM-001", "2026-11-11 17:29:40.100000"),
            ("TEST-ITEM-002", "2026-11-11 17:29:42.431978"),
        ),
    )

    assert changed == {"TEST-ITEM-001", "TEST-ITEM-002"}
    assert dirty_items(store) == {
        "TEST-ITEM-001": DirtyItem(marked_at=utc_timestamp(NOW), attempts=0),
        "TEST-ITEM-002": DirtyItem(marked_at=utc_timestamp(NOW), attempts=0),
    }
    assert watermark(store) == "2026-11-11 17:29:42.431978"
    assert recorder.filters(0) == [["warehouse", "=", WAREHOUSE]]


def test_poll_requests_item_and_modified_ordered_by_modified(
    store: sqlite3.Connection,
) -> None:
    _, recorder = poll(store, SeenBins(), rows())

    request = recorder.requests[0]
    assert request.url.path == "/api/resource/Bin"
    assert json.loads(request.url.params["fields"]) == ["item_code", "modified"]
    assert request.url.params["order_by"] == "modified asc"
    assert request.url.params["limit_page_length"] == "0"


def test_poll_reads_from_the_watermark_less_the_overlap(
    store: sqlite3.Connection,
) -> None:
    set_watermark(store, "2026-11-11 17:30:00.250000")

    _, recorder = poll(store, SeenBins(), rows())

    assert POLL_OVERLAP == timedelta(seconds=60)
    assert recorder.filters(0) == [
        ["warehouse", "=", WAREHOUSE],
        ["modified", ">=", "2026-11-11 17:29:00.250000"],
    ]


def test_a_change_on_one_item_marks_only_that_item(store: sqlite3.Connection) -> None:
    seen = SeenBins()
    first = ("TEST-ITEM-001", "2026-11-11 17:29:40.000000")
    poll(store, seen, rows(first, ("TEST-ITEM-002", "2026-11-11 17:29:41.000000")))
    clear_dirty(store, ["TEST-ITEM-001", "TEST-ITEM-002"])

    changed, _ = poll(
        store,
        seen,
        rows(first, ("TEST-ITEM-002", "2026-11-11 17:29:50.000000")),
        now=LATER,
    )

    assert changed == {"TEST-ITEM-002"}
    assert set(dirty_items(store)) == {"TEST-ITEM-002"}
    assert watermark(store) == "2026-11-11 17:29:50.000000"


def test_a_bin_already_seen_in_the_overlap_is_not_marked_again(
    store: sqlite3.Connection,
) -> None:
    seen = SeenBins()
    bin_row = ("TEST-ITEM-001", "2026-11-11 17:29:42.431978")
    poll(store, seen, rows(bin_row))
    clear_dirty(store, ["TEST-ITEM-001"])

    changed, _ = poll(store, seen, rows(bin_row), now=LATER)

    assert changed == set()
    assert dirty_items(store) == {}


def test_a_late_commit_with_an_earlier_stamp_is_seen_on_the_next_poll(
    store: sqlite3.Connection,
) -> None:
    seen = SeenBins()
    early = ("TEST-ITEM-001", "2026-11-11 17:29:42.900000")
    poll(store, seen, rows(early))
    clear_dirty(store, ["TEST-ITEM-001"])
    # Stamped before the watermark in the same second but committed after the
    # first poll read.
    late = ("TEST-ITEM-002", "2026-11-11 17:29:42.100000")

    changed, _ = poll(store, seen, rows(late, early), now=LATER)

    assert changed == {"TEST-ITEM-002"}
    assert watermark(store) == "2026-11-11 17:29:42.900000"


def test_a_new_seen_set_after_a_restart_marks_the_overlap_again(
    store: sqlite3.Connection,
) -> None:
    bin_row = ("TEST-ITEM-001", "2026-11-11 17:29:42.431978")
    poll(store, SeenBins(), rows(bin_row))
    clear_dirty(store, ["TEST-ITEM-001"])

    changed, _ = poll(store, SeenBins(), rows(bin_row), now=LATER)

    assert changed == {"TEST-ITEM-001"}


def test_a_poll_returning_nothing_keeps_the_watermark(
    store: sqlite3.Connection,
) -> None:
    set_watermark(store, "2026-11-11 17:29:42.431978")

    changed, _ = poll(store, SeenBins(), rows())

    assert changed == set()
    assert watermark(store) == "2026-11-11 17:29:42.431978"


def test_the_watermark_never_moves_backwards(store: sqlite3.Connection) -> None:
    set_watermark(store, "2026-11-11 17:29:42.431978")

    poll(store, SeenBins(), rows(("TEST-ITEM-001", "2026-11-11 17:29:10.000000")))

    assert watermark(store) == "2026-11-11 17:29:42.431978"


def test_a_first_poll_of_an_empty_warehouse_sets_no_watermark(
    store: sqlite3.Connection,
) -> None:
    changed, _ = poll(store, SeenBins(), rows())

    assert changed == set()
    assert watermark(store) is None


def test_modified_without_fractional_seconds_is_accepted(
    store: sqlite3.Connection,
) -> None:
    seen = SeenBins()
    poll(store, seen, rows(("TEST-ITEM-001", "2026-11-11 17:29:42")))
    clear_dirty(store, ["TEST-ITEM-001"])

    changed, recorder = poll(
        store, seen, rows(("TEST-ITEM-001", "2026-11-11 17:29:42.000000")), now=LATER
    )

    assert changed == set()
    assert watermark(store) == "2026-11-11 17:29:42.000000"
    assert recorder.filters(0)[1] == ["modified", ">=", "2026-11-11 17:28:42.000000"]


def test_seen_pairs_older_than_the_overlap_are_forgotten(
    store: sqlite3.Connection,
) -> None:
    seen = SeenBins()
    old = ("TEST-ITEM-001", "2026-11-11 17:00:00.000000")
    poll(store, seen, rows(old))
    poll(store, seen, rows(("TEST-ITEM-002", "2026-11-11 17:30:00.000000")))
    clear_dirty(store, ["TEST-ITEM-001", "TEST-ITEM-002"])

    assert not seen.contains("TEST-ITEM-001", datetime(2026, 11, 11, 17))
    assert seen.contains("TEST-ITEM-002", datetime(2026, 11, 11, 17, 30))


def test_re_marking_keeps_an_existing_dirty_row(store: sqlite3.Connection) -> None:
    mark_dirty(store, ["TEST-ITEM-001"], NOW)
    record_failed_attempt(store, ["TEST-ITEM-001"])

    changed, _ = poll(
        store,
        SeenBins(),
        rows(("TEST-ITEM-001", "2026-11-11 17:29:42.431978")),
        now=LATER,
    )

    assert changed == {"TEST-ITEM-001"}
    assert dirty_items(store) == {
        "TEST-ITEM-001": DirtyItem(marked_at=utc_timestamp(NOW), attempts=1)
    }


@pytest.mark.parametrize(
    "row",
    [
        {"item_code": None, "modified": "2026-11-11 17:29:42.431978"},
        {"item_code": "TEST-ITEM-002", "modified": None},
        {"item_code": "TEST-ITEM-002", "modified": "yesterday"},
        {"item_code": "TEST-ITEM-002"},
    ],
)
def test_a_malformed_row_raises_and_writes_nothing(
    store: sqlite3.Connection, row: dict[str, object]
) -> None:
    seen = SeenBins()
    good = {"item_code": "TEST-ITEM-001", "modified": "2026-11-11 17:29:50.000000"}
    recorder = Recorder(httpx.Response(200, json={"data": [good, row]}))

    with pytest.raises(ErpNextError):
        poll_bins(store, erpnext_client(recorder), WAREHOUSE, seen, NOW)

    assert dirty_items(store) == {}
    assert watermark(store) is None
    assert not seen.contains("TEST-ITEM-001", datetime(2026, 11, 11, 17, 29, 50))


def test_a_failed_request_writes_nothing(store: sqlite3.Connection) -> None:
    set_watermark(store, "2026-11-11 17:29:42.431978")

    with pytest.raises(ErpNextError):
        poll(store, SeenBins(), httpx.Response(403))

    assert dirty_items(store) == {}
    assert watermark(store) == "2026-11-11 17:29:42.431978"


# Dirty set


def test_mark_dirty_records_the_time_and_no_attempts(
    store: sqlite3.Connection,
) -> None:
    mark_dirty(store, ["TEST-ITEM-001", "TEST-ITEM-002"], NOW)

    assert dirty_items(store) == {
        "TEST-ITEM-001": DirtyItem(marked_at=utc_timestamp(NOW), attempts=0),
        "TEST-ITEM-002": DirtyItem(marked_at=utc_timestamp(NOW), attempts=0),
    }


def test_mark_dirty_keeps_an_existing_row(store: sqlite3.Connection) -> None:
    mark_dirty(store, ["TEST-ITEM-001"], NOW)
    record_failed_attempt(store, ["TEST-ITEM-001"])

    mark_dirty(store, ["TEST-ITEM-001"], LATER)

    assert dirty_items(store) == {
        "TEST-ITEM-001": DirtyItem(marked_at=utc_timestamp(NOW), attempts=1)
    }


def test_record_failed_attempt_increments_attempts(
    store: sqlite3.Connection,
) -> None:
    mark_dirty(store, ["TEST-ITEM-001", "TEST-ITEM-002"], NOW)

    record_failed_attempt(store, ["TEST-ITEM-001"])
    record_failed_attempt(store, ["TEST-ITEM-001"])

    assert dirty_items(store)["TEST-ITEM-001"].attempts == 2
    assert dirty_items(store)["TEST-ITEM-002"].attempts == 0


def test_record_failed_attempt_ignores_an_item_not_dirty(
    store: sqlite3.Connection,
) -> None:
    record_failed_attempt(store, ["TEST-ITEM-001"])

    assert dirty_items(store) == {}


def test_clear_dirty_removes_only_the_named_items(store: sqlite3.Connection) -> None:
    mark_dirty(store, ["TEST-ITEM-001", "TEST-ITEM-002"], NOW)

    clear_dirty(store, ["TEST-ITEM-001", "TEST-ITEM-003"])

    assert set(dirty_items(store)) == {"TEST-ITEM-002"}
