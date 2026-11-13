import json
import logging
import socket
import sqlite3
import threading
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from pathlib import Path

import httpx
import pytest
from helpers import FakeWorker, make_config

from shopify_erpnext_connector import api
from shopify_erpnext_connector.api import (
    ENVIRONMENT_HEADER,
    MAX_BODY_BYTES,
    METRICS_CONTENT_TYPE,
    PASS_STALE_AFTER,
    RECORD_PUSH_HISTORY,
    ApiServer,
    audit,
)
from shopify_erpnext_connector.config import ApiConfig, Environment, Secret
from shopify_erpnext_connector.decisions import Decision, save_decisions
from shopify_erpnext_connector.poller import mark_dirty
from shopify_erpnext_connector.problems import Problem, ProblemKind, record_check
from shopify_erpnext_connector.push import PushOutcome
from shopify_erpnext_connector.store import open_store, set_dry_run, utc_timestamp

NOW = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)
ALICE_TOKEN = "test-alice-token"
REQUEST_TIMEOUT_SECONDS = 0.5
WRITER_LOCK_TIMEOUT_SECONDS = 0.5
BOB_TOKEN = "test-bob-token"
METRICS_TOKEN = "test-metrics-token"
# Kept apart from its value so the fixture check's header pattern stays meaningful.
AUTH_HEADER = "Authorization"
VARIANT = "gid://shopify/ProductVariant/1001"
OTHER_VARIANT = "gid://shopify/ProductVariant/1002"


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    path = tmp_path / "connector.sqlite3"
    open_store(path).close()
    return path


@pytest.fixture
def store(store_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_store(store_path)
    yield connection
    connection.close()


@pytest.fixture
def worker() -> FakeWorker:
    return FakeWorker()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def server(store_path: Path, worker: FakeWorker, clock: Clock) -> Iterator[ApiServer]:
    config = make_config()
    config = replace(
        config,
        store_path=store_path,
        api=ApiConfig(port=0),
        secrets=replace(
            config.secrets,
            metrics_token=Secret(METRICS_TOKEN),
            operator_tokens={"alice": Secret(ALICE_TOKEN), "bob": Secret(BOB_TOKEN)},
        ),
    )
    server = ApiServer(
        config,
        worker,
        clock=clock,
        request_timeout=REQUEST_TIMEOUT_SECONDS,
        writer_lock_timeout=WRITER_LOCK_TIMEOUT_SECONDS,
    )
    server.start()
    yield server
    server.close()


@pytest.fixture
def client(server: ApiServer) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=f"http://127.0.0.1:{server.port}") as client:
        yield client


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    caplog.set_level(logging.INFO)
    return caplog


def bearer(token: str) -> dict[str, str]:
    return {AUTH_HEADER: f"Bearer {token}"}


def operator(
    token: str = ALICE_TOKEN, environment: str = Environment.STAGING
) -> dict[str, str]:
    return bearer(token) | {ENVIRONMENT_HEADER: environment}


def queue_push(store: sqlite3.Connection, variant_id: str, queued_at: datetime) -> None:
    store.execute(
        "INSERT INTO pending_pushes (variant_id, available, change_from, queued_at)"
        " VALUES (?, 3, 2, ?)",
        (variant_id, utc_timestamp(queued_at)),
    )


def test_request_without_a_token_is_rejected_and_logged(
    client: httpx.Client, logs: pytest.LogCaptureFixture
) -> None:
    response = client.get("/status")

    assert response.status_code == 401
    assert response.json()["environment"] == "staging"
    assert "token" in response.json()["error"]
    rejection = [r.getMessage() for r in logs.records if r.levelno == logging.WARNING]
    assert len(rejection) == 1
    assert "GET" in rejection[0]
    assert "/status" in rejection[0]
    assert "127.0.0.1" in rejection[0]


def test_wrong_token_is_rejected_without_appearing_in_logs(
    client: httpx.Client, logs: pytest.LogCaptureFixture
) -> None:
    wrong = "test-wrong-token-value"

    response = client.get("/status", headers=operator(wrong))

    assert response.status_code == 401
    assert any(r.levelno == logging.WARNING for r in logs.records)
    assert wrong not in logs.text
    for token in (ALICE_TOKEN, BOB_TOKEN, METRICS_TOKEN):
        assert token not in logs.text


def test_query_string_is_left_out_of_the_request_log(
    client: httpx.Client, logs: pytest.LogCaptureFixture
) -> None:
    misplaced = "test-token-in-query"

    response = client.get(f"/status?token={misplaced}", headers=operator())

    assert response.status_code == 200
    assert "/status" in logs.text
    assert misplaced not in logs.text


def test_a_stalled_connection_is_closed(server: ApiServer) -> None:
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as stalled:
        stalled.sendall(b"GET /status HTTP/1.1\r\n")

        assert stalled.recv(1024) == b""


@pytest.mark.parametrize("value", [f"Basic {ALICE_TOKEN}", ALICE_TOKEN, "Bearer"])
def test_authorization_that_is_not_a_bearer_token_is_rejected(
    client: httpx.Client, value: str
) -> None:
    headers = {AUTH_HEADER: value, ENVIRONMENT_HEADER: "staging"}

    assert client.get("/status", headers=headers).status_code == 401


def test_bearer_scheme_is_case_insensitive(client: httpx.Client) -> None:
    headers = {AUTH_HEADER: f"bearer {ALICE_TOKEN}", ENVIRONMENT_HEADER: "staging"}

    assert client.get("/status", headers=headers).status_code == 200


def test_metrics_token_is_rejected_on_operator_endpoints(
    client: httpx.Client,
) -> None:
    response = client.get("/status", headers=operator(METRICS_TOKEN))

    assert response.status_code == 401


def test_operator_token_is_rejected_on_metrics(client: httpx.Client) -> None:
    response = client.get("/metrics", headers=bearer(ALICE_TOKEN))

    assert response.status_code == 401
    assert response.json()["environment"] == "staging"


def test_metrics_without_a_token_is_unauthorized(client: httpx.Client) -> None:
    assert client.get("/metrics").status_code == 401


def test_operator_request_without_an_environment_is_refused(
    client: httpx.Client,
) -> None:
    response = client.get("/status", headers=bearer(ALICE_TOKEN))

    assert response.status_code == 409
    body = response.json()
    assert body["environment"] == "staging"
    assert ENVIRONMENT_HEADER in body["error"]
    assert "staging" in body["error"]


def test_operator_request_for_another_environment_is_refused(
    client: httpx.Client,
) -> None:
    response = client.get(
        "/status", headers=operator(environment=Environment.PRODUCTION)
    )

    assert response.status_code == 409
    assert "staging" in response.json()["error"]
    assert "production" in response.json()["error"]


def test_status_before_the_first_pass(client: httpx.Client) -> None:
    response = client.get("/status", headers=operator())

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {
        "environment": "staging",
        "dry_run": True,
        "last_pass_at": None,
        "last_pass_full": False,
        "last_full_pass_at": None,
        "push_lag_seconds": 0,
        "open_problems": 0,
        "unsent_emails": 0,
    }


def test_status_reports_the_worker_and_the_store(
    client: httpx.Client, store: sqlite3.Connection, worker: FakeWorker
) -> None:
    worker.last_pass_at = NOW - timedelta(seconds=3)
    worker.last_pass_full = True
    worker.last_full_pass_at = NOW - timedelta(hours=1)
    set_dry_run(store, False)
    record_check(
        store,
        ProblemKind.UNBOUND_SKU,
        [
            Problem(ProblemKind.UNBOUND_SKU, VARIANT, {"SKU": "TEST-SKU-001"}),
            Problem(ProblemKind.UNBOUND_SKU, OTHER_VARIANT, {"SKU": "TEST-SKU-002"}),
        ],
        NOW,
    )
    store.execute(
        "UPDATE problems SET email_pending = 0 WHERE subject = ?", (OTHER_VARIANT,)
    )

    body = client.get("/status", headers=operator()).json()

    assert body["dry_run"] is False
    assert body["last_pass_at"] == utc_timestamp(NOW - timedelta(seconds=3))
    assert body["last_pass_full"] is True
    assert body["last_full_pass_at"] == utc_timestamp(NOW - timedelta(hours=1))
    assert body["open_problems"] == 2
    assert body["unsent_emails"] == 1


def test_status_reports_the_push_lag(
    client: httpx.Client, store: sqlite3.Connection
) -> None:
    queue_push(store, VARIANT, NOW - timedelta(seconds=90))

    body = client.get("/status", headers=operator()).json()

    assert body["push_lag_seconds"] == 90


def test_query_string_does_not_change_the_route(client: httpx.Client) -> None:
    response = client.get("/status", params={"verbose": "1"}, headers=operator())

    assert response.status_code == 200


def test_any_operator_token_is_accepted_and_its_name_logged(
    client: httpx.Client, logs: pytest.LogCaptureFixture
) -> None:
    response = client.get("/status", headers=operator(BOB_TOKEN))

    assert response.status_code == 200
    requests = [r.getMessage() for r in logs.records if "/status" in r.getMessage()]
    assert len(requests) == 1
    assert "bob" in requests[0]
    assert "200" in requests[0]
    assert BOB_TOKEN not in logs.text


def test_unknown_path_is_not_found(client: httpx.Client) -> None:
    response = client.get("/nowhere", headers=operator())

    assert response.status_code == 404
    assert response.json()["environment"] == "staging"
    assert "/nowhere" in response.json()["error"]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_wrong_method_is_not_allowed(client: httpx.Client, method: str) -> None:
    response = client.request(method, "/status", headers=operator())

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert response.json()["environment"] == "staging"


def test_store_that_cannot_be_read_is_a_server_error(
    client: httpx.Client, store_path: Path, logs: pytest.LogCaptureFixture
) -> None:
    store_path.unlink()

    response = client.get("/status", headers=operator())

    assert response.status_code == 500
    assert response.json()["environment"] == "staging"
    assert any(r.levelno == logging.ERROR for r in logs.records)
    assert not store_path.exists()


def test_read_handlers_see_one_snapshot_of_the_store(
    client: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    def probe(server: ApiServer, request: api._Request) -> api._Response:
        in_transaction = request.connection.in_transaction
        return server.json(HTTPStatus.OK, {"in_transaction": in_transaction})

    route = api._Route("GET", api._Access.OPERATOR, probe)
    monkeypatch.setitem(api._ROUTES, "/probe", route)

    response = client.get("/probe", headers=operator())

    assert response.json()["in_transaction"] is True


def metrics(client: httpx.Client) -> dict[str, float]:
    """The samples of a metrics scrape, by name and labels."""
    response = client.get("/metrics", headers=bearer(METRICS_TOKEN))
    assert response.status_code == 200
    samples: dict[str, float] = {}
    for line in response.text.splitlines():
        if line and not line.startswith("#"):
            name, value = line.rsplit(" ", 1)
            samples[name] = float(value)
    return samples


def bind(store: sqlite3.Connection, variant_id: str, item_code: str) -> None:
    store.execute(
        "INSERT INTO bindings (variant_id, inventory_item_id, item_code, bound_at)"
        " VALUES (?, ?, ?, ?)",
        (variant_id, variant_id + "-inventory", item_code, utc_timestamp(NOW)),
    )


def test_metrics_before_the_first_pass(client: httpx.Client) -> None:
    response = client.get("/metrics", headers=bearer(METRICS_TOKEN))

    assert response.headers["content-type"] == METRICS_CONTENT_TYPE
    assert metrics(client) == {
        'connector_info{environment="staging"}': 1,
        "connector_last_pass_timestamp_seconds": 0,
        "connector_last_pass_full": 0,
        "connector_last_full_pass_timestamp_seconds": 0,
        "connector_pass_stale": 0,
        "connector_push_lag_seconds": 0,
        **{f'connector_open_problems{{kind="{kind}"}}': 0 for kind in ProblemKind},
        "connector_unsent_emails": 0,
        "connector_dry_run": 1,
    }


def test_every_metric_has_help_and_a_gauge_type(client: httpx.Client) -> None:
    text = client.get("/metrics", headers=bearer(METRICS_TOKEN)).text
    names = {
        line.split("{")[0].split(" ")[0]
        for line in text.splitlines()
        if not line.startswith("#")
    }

    for name in names:
        assert f"# HELP {name} " in text
        assert f"# TYPE {name} gauge" in text


def test_metrics_report_the_last_pass(client: httpx.Client, worker: FakeWorker) -> None:
    worker.last_pass_at = NOW - timedelta(seconds=2)
    worker.last_pass_full = True

    samples = metrics(client)

    assert samples["connector_last_pass_timestamp_seconds"] == (
        (NOW - timedelta(seconds=2)).timestamp()
    )
    assert samples["connector_last_pass_full"] == 1
    assert samples["connector_pass_stale"] == 0


def test_metrics_report_the_last_full_pass(
    client: httpx.Client, worker: FakeWorker
) -> None:
    worker.last_pass_at = NOW - timedelta(seconds=2)
    worker.last_full_pass_at = NOW - timedelta(hours=3)

    samples = metrics(client)

    assert samples["connector_last_full_pass_timestamp_seconds"] == (
        (NOW - timedelta(hours=3)).timestamp()
    )
    assert samples["connector_last_pass_timestamp_seconds"] == (
        (NOW - timedelta(seconds=2)).timestamp()
    )


def test_pass_is_stale_when_none_completed_within_the_threshold_of_start(
    client: httpx.Client, clock: Clock
) -> None:
    clock.now = NOW + PASS_STALE_AFTER - timedelta(seconds=1)
    assert metrics(client)["connector_pass_stale"] == 0

    clock.now = NOW + PASS_STALE_AFTER
    assert metrics(client)["connector_pass_stale"] == 1


def test_pass_is_stale_when_the_last_pass_is_older_than_the_threshold(
    client: httpx.Client, clock: Clock, worker: FakeWorker
) -> None:
    worker.last_pass_at = NOW
    clock.now = NOW + PASS_STALE_AFTER

    assert metrics(client)["connector_pass_stale"] == 1

    worker.last_pass_at = clock.now - timedelta(seconds=5)
    assert metrics(client)["connector_pass_stale"] == 0


def test_push_lag_from_a_pending_push(
    client: httpx.Client, store: sqlite3.Connection, clock: Clock
) -> None:
    queue_push(store, VARIANT, NOW)
    queue_push(store, OTHER_VARIANT, NOW + timedelta(seconds=30))
    clock.now = NOW + timedelta(seconds=45)

    assert metrics(client)["connector_push_lag_seconds"] == 45


def test_push_lag_from_a_dirty_bound_item(
    client: httpx.Client, store: sqlite3.Connection, clock: Clock
) -> None:
    bind(store, VARIANT, "TEST-ITEM-001")
    mark_dirty(store, ["TEST-ITEM-001"], NOW)
    clock.now = NOW + timedelta(seconds=20)

    assert metrics(client)["connector_push_lag_seconds"] == 20


def test_push_lag_ignores_a_dirty_item_that_is_not_bound(
    client: httpx.Client, store: sqlite3.Connection, clock: Clock
) -> None:
    mark_dirty(store, ["TEST-ITEM-001"], NOW)
    clock.now = NOW + timedelta(seconds=20)

    assert metrics(client)["connector_push_lag_seconds"] == 0


def test_metrics_report_problems_by_kind_emails_and_dry_run(
    client: httpx.Client, store: sqlite3.Connection
) -> None:
    record_check(
        store,
        ProblemKind.OVERSELL,
        [Problem(ProblemKind.OVERSELL, VARIANT, {"Item": "TEST-ITEM-001"})],
        NOW,
    )
    set_dry_run(store, False)

    samples = metrics(client)

    assert samples['connector_open_problems{kind="oversell"}'] == 1
    assert samples['connector_open_problems{kind="unbound_sku"}'] == 0
    assert samples["connector_unsent_emails"] == 1
    assert samples["connector_dry_run"] == 0


def test_production_metrics_carry_the_environment_label(
    store_path: Path, worker: FakeWorker
) -> None:
    config = replace(
        make_config(environment=Environment.PRODUCTION),
        store_path=store_path,
        api=ApiConfig(port=0),
    )
    server = ApiServer(config, worker)
    server.start()
    try:
        with httpx.Client(base_url=f"http://127.0.0.1:{server.port}") as client:
            samples = metrics(client)
    finally:
        server.close()

    assert samples['connector_info{environment="production"}'] == 1


def test_audit_records_the_operator_and_action(store: sqlite3.Connection) -> None:
    audit(store, "alice", "dry-run off", "was on", NOW)

    rows = store.execute(
        "SELECT recorded_at, operator, action, detail FROM audit_log"
    ).fetchall()
    assert rows == [(utc_timestamp(NOW), "alice", "dry-run off", "was on")]


def test_problems_lists_open_problems_with_their_email_state(
    client: httpx.Client, store: sqlite3.Connection
) -> None:
    record_check(
        store,
        ProblemKind.UNBOUND_SKU,
        [Problem(ProblemKind.UNBOUND_SKU, VARIANT, {"SKU": "TEST-SKU-001"})],
        NOW,
    )
    record_check(
        store,
        ProblemKind.OVERSELL,
        [Problem(ProblemKind.OVERSELL, OTHER_VARIANT, {"Item": "TEST-ITEM-002"})],
        NOW,
    )
    record_check(store, ProblemKind.OVERSELL, [], NOW + timedelta(seconds=1))

    response = client.get("/problems", headers=operator())

    assert response.status_code == 200
    body = response.json()
    assert body["environment"] == "staging"
    assert body["problems"] == [
        {
            "key": f"unbound_sku:{VARIANT}",
            "kind": "unbound_sku",
            "subject": VARIANT,
            "details": {"SKU": "TEST-SKU-001"},
            "opened_at": utc_timestamp(NOW),
            "resolved_at": None,
            "email": {
                "pending": True,
                "attempts": 0,
                "last_attempt_at": None,
                "communication": None,
            },
        }
    ]


def test_problems_when_none_is_open(client: httpx.Client) -> None:
    assert client.get("/problems", headers=operator()).json()["problems"] == []


def test_problem_shows_one_by_its_key(
    client: httpx.Client, store: sqlite3.Connection
) -> None:
    record_check(
        store,
        ProblemKind.OVERSELL,
        [
            Problem(
                ProblemKind.OVERSELL,
                VARIANT,
                {"Item": "TEST-ITEM-001"},
                context={"Short by": "2"},
            )
        ],
        NOW,
    )

    response = client.get(
        "/problem", params={"key": f"oversell:{VARIANT}"}, headers=operator()
    )

    assert response.status_code == 200
    problem = response.json()["problem"]
    assert problem["key"] == f"oversell:{VARIANT}"
    assert problem["details"] == {"Item": "TEST-ITEM-001", "Short by": "2"}


def test_problem_shows_a_resolved_problem(
    client: httpx.Client, store: sqlite3.Connection
) -> None:
    record_check(
        store,
        ProblemKind.OVERSELL,
        [Problem(ProblemKind.OVERSELL, VARIANT, {"Item": "TEST-ITEM-001"})],
        NOW,
    )
    record_check(store, ProblemKind.OVERSELL, [], NOW + timedelta(seconds=1))

    response = client.get(
        "/problem", params={"key": f"oversell:{VARIANT}"}, headers=operator()
    )

    assert response.status_code == 200
    resolved_at = response.json()["problem"]["resolved_at"]
    assert resolved_at == utc_timestamp(NOW + timedelta(seconds=1))


@pytest.mark.parametrize(
    "key", [f"oversell:{VARIANT}", f"no_such_kind:{VARIANT}", "oversell"]
)
def test_problem_not_found(client: httpx.Client, key: str) -> None:
    response = client.get("/problem", params={"key": key}, headers=operator())

    assert response.status_code == 404
    assert response.json()["environment"] == "staging"
    assert key in response.json()["error"]


def test_problem_without_a_key_is_a_bad_request(client: httpx.Client) -> None:
    response = client.get("/problem", headers=operator())

    assert response.status_code == 400
    assert "key" in response.json()["error"]


def hold_sku_change(
    store: sqlite3.Connection, variant_id: str, bound_item_code: str, sku: str
) -> None:
    store.execute(
        "INSERT INTO sku_changes (variant_id, bound_item_code, detected_at)"
        " VALUES (?, ?, ?)",
        (variant_id, bound_item_code, utc_timestamp(NOW)),
    )
    record_check(
        store,
        ProblemKind.CHANGED_SKU,
        [Problem(ProblemKind.CHANGED_SKU, variant_id, {"SKU": sku})],
        NOW,
    )


def test_bindings_lists_unbound_ambiguous_and_held_variants(
    client: httpx.Client, store: sqlite3.Connection
) -> None:
    third = "gid://shopify/ProductVariant/1003"
    record_check(
        store,
        ProblemKind.UNBOUND_SKU,
        [Problem(ProblemKind.UNBOUND_SKU, VARIANT, {"SKU": "TEST-SKU-001"})],
        NOW,
    )
    record_check(
        store,
        ProblemKind.AMBIGUOUS_SKU,
        [Problem(ProblemKind.AMBIGUOUS_SKU, OTHER_VARIANT, {"SKU": "TEST-SKU-002"})],
        NOW,
    )
    hold_sku_change(store, third, "TEST-ITEM-003", "TEST-SKU-003-NEW")
    record_check(
        store,
        ProblemKind.OVERSELL,
        [Problem(ProblemKind.OVERSELL, VARIANT, {"Item": "TEST-ITEM-001"})],
        NOW,
    )

    response = client.get("/bindings", headers=operator())

    assert response.status_code == 200
    body = response.json()
    assert [p["subject"] for p in body["unbound"]] == [VARIANT]
    assert body["unbound"][0]["details"] == {"SKU": "TEST-SKU-001"}
    assert [p["subject"] for p in body["ambiguous"]] == [OTHER_VARIANT]
    assert body["sku_changes"] == [
        {
            "variant_id": third,
            "bound_item_code": "TEST-ITEM-003",
            "sku": "TEST-SKU-003-NEW",
            "detected_at": utc_timestamp(NOW),
        }
    ]


def test_bindings_when_every_variant_is_bound(client: httpx.Client) -> None:
    body = client.get("/bindings", headers=operator()).json()

    assert body["unbound"] == []
    assert body["ambiguous"] == []
    assert body["sku_changes"] == []


def test_held_sku_change_without_an_open_problem_has_no_sku(
    client: httpx.Client, store: sqlite3.Connection
) -> None:
    hold_sku_change(store, VARIANT, "TEST-ITEM-001", "TEST-SKU-001-NEW")
    record_check(store, ProblemKind.CHANGED_SKU, [], NOW)

    body = client.get("/bindings", headers=operator()).json()

    assert body["sku_changes"][0]["sku"] is None


def decide(store: sqlite3.Connection, variant_id: str, item_code: str) -> None:
    decision = Decision(
        item_code=item_code,
        explanation={"actual_qty": 5.0, "sellable": 3},
        shopify_available=2,
        last_pushed=2,
        action="pushed",
        note=None,
    )
    save_decisions(store, [(variant_id, decision)], NOW)


def record_push(
    store: sqlite3.Connection, variant_id: str, available: int, attempted_at: datetime
) -> None:
    store.execute(
        "INSERT INTO push_history (variant_id, available, change_from,"
        " idempotency_key, attempted_at, outcome) VALUES (?, ?, 1, 'test-key', ?, ?)",
        (variant_id, available, utc_timestamp(attempted_at), PushOutcome.PUSHED),
    )


def get_record(client: httpx.Client, variant_id: str) -> httpx.Response:
    return client.get("/record", params={"variant": variant_id}, headers=operator())


def test_record_explains_a_bound_variant_with_its_push_history(
    client: httpx.Client, store: sqlite3.Connection
) -> None:
    bind(store, VARIANT, "TEST-ITEM-001")
    decide(store, VARIANT, "TEST-ITEM-001")
    record_push(store, VARIANT, 2, NOW)
    record_push(store, VARIANT, 3, NOW + timedelta(seconds=1))
    record_push(store, OTHER_VARIANT, 9, NOW)

    response = get_record(client, VARIANT)

    assert response.status_code == 200
    body = response.json()
    assert body["variant_id"] == VARIANT
    assert body["bound_item_code"] == "TEST-ITEM-001"
    assert body["binding_problem"] is None
    assert body["decision"] == {
        "item_code": "TEST-ITEM-001",
        "explanation": {"actual_qty": 5.0, "sellable": 3},
        "shopify_available": 2,
        "last_pushed": 2,
        "action": "pushed",
        "note": None,
        "decided_at": utc_timestamp(NOW),
    }
    assert body["push_history"] == [
        {
            "attempted_at": utc_timestamp(NOW + timedelta(seconds=1)),
            "available": 3,
            "change_from": 1,
            "outcome": "pushed",
        },
        {
            "attempted_at": utc_timestamp(NOW),
            "available": 2,
            "change_from": 1,
            "outcome": "pushed",
        },
    ]


def test_record_push_history_is_bounded_to_the_newest(
    client: httpx.Client, store: sqlite3.Connection
) -> None:
    bind(store, VARIANT, "TEST-ITEM-001")
    for second in range(RECORD_PUSH_HISTORY + 1):
        record_push(store, VARIANT, second, NOW + timedelta(seconds=second))

    history = get_record(client, VARIANT).json()["push_history"]

    assert len(history) == RECORD_PUSH_HISTORY
    assert history[0]["available"] == RECORD_PUSH_HISTORY
    assert history[-1]["available"] == 1


def test_record_for_an_unbound_variant_says_why(
    client: httpx.Client, store: sqlite3.Connection
) -> None:
    record_check(
        store,
        ProblemKind.UNBOUND_SKU,
        [
            Problem(
                ProblemKind.UNBOUND_SKU,
                VARIANT,
                {"SKU": "TEST-SKU-001", "Reason": "No ERPNext Item has this item code"},
            )
        ],
        NOW,
    )

    response = get_record(client, VARIANT)

    assert response.status_code == 200
    body = response.json()
    assert body["bound_item_code"] is None
    assert body["decision"] is None
    assert body["binding_problem"]["key"] == f"unbound_sku:{VARIANT}"
    assert body["binding_problem"]["details"]["Reason"] == (
        "No ERPNext Item has this item code"
    )


def test_record_for_a_held_variant_keeps_its_last_decision(
    client: httpx.Client, store: sqlite3.Connection
) -> None:
    decide(store, VARIANT, "TEST-ITEM-001")
    hold_sku_change(store, VARIANT, "TEST-ITEM-001", "TEST-SKU-001-NEW")

    body = get_record(client, VARIANT).json()

    assert body["bound_item_code"] is None
    assert body["binding_problem"]["kind"] == "changed_sku"
    assert body["decision"]["item_code"] == "TEST-ITEM-001"
    assert body["decision"]["decided_at"] == utc_timestamp(NOW)


def resolve_unbound(store: sqlite3.Connection, variant_id: str) -> None:
    """An unbound-SKU problem for the variant, opened and then resolved."""
    record_check(
        store,
        ProblemKind.UNBOUND_SKU,
        [Problem(ProblemKind.UNBOUND_SKU, variant_id, {"SKU": "TEST-SKU-001"})],
        NOW,
    )
    record_check(store, ProblemKind.UNBOUND_SKU, [], NOW + timedelta(seconds=1))


def test_record_skips_a_resolved_binding_problem(
    client: httpx.Client, store: sqlite3.Connection
) -> None:
    resolve_unbound(store, VARIANT)
    hold_sku_change(store, VARIANT, "TEST-ITEM-001", "TEST-SKU-001-NEW")

    body = get_record(client, VARIANT).json()

    assert body["binding_problem"]["key"] == f"changed_sku:{VARIANT}"


def test_record_for_a_variant_with_only_a_resolved_problem_is_not_found(
    client: httpx.Client, store: sqlite3.Connection
) -> None:
    resolve_unbound(store, VARIANT)

    assert get_record(client, VARIANT).status_code == 404


def test_record_for_a_held_variant_whose_problem_resolved_shows_the_hold(
    client: httpx.Client, store: sqlite3.Connection
) -> None:
    hold_sku_change(store, VARIANT, "TEST-ITEM-001", "TEST-SKU-001-NEW")
    record_check(store, ProblemKind.CHANGED_SKU, [], NOW)

    response = get_record(client, VARIANT)

    assert response.status_code == 200
    body = response.json()
    assert body["binding_problem"] is None
    assert body["sku_change"] == {
        "bound_item_code": "TEST-ITEM-001",
        "detected_at": utc_timestamp(NOW),
    }


def test_record_ignores_problems_other_than_binding(
    client: httpx.Client, store: sqlite3.Connection
) -> None:
    record_check(
        store,
        ProblemKind.OVERSELL,
        [Problem(ProblemKind.OVERSELL, VARIANT, {"Item": "TEST-ITEM-001"})],
        NOW,
    )

    assert get_record(client, VARIANT).status_code == 404


def test_record_for_an_unknown_variant_is_not_found(client: httpx.Client) -> None:
    response = get_record(client, VARIANT)

    assert response.status_code == 404
    assert response.json()["environment"] == "staging"
    assert VARIANT in response.json()["error"]


def test_report_compares_each_computed_bound_variant(
    client: httpx.Client, store: sqlite3.Connection
) -> None:
    bind(store, VARIANT, "TEST-ITEM-001")
    explanation = {
        "sellable": 3,
        "old_formula": 4,
        "actual_qty": 5.0,
        "reserved_qty": 1.0,
        "reserved_qty_for_production": 0.0,
        "planned_qty": 2.0,
        "unmirrored": 1,
    }
    decision = Decision("TEST-ITEM-001", explanation, 4, None, "dry run", None)
    save_decisions(store, [(VARIANT, decision)], NOW)

    response = client.get("/report", headers=operator())

    assert response.status_code == 200
    assert response.json()["rows"] == [
        {
            "variant_id": VARIANT,
            "item_code": "TEST-ITEM-001",
            "shopify_available": 4,
            "computed": 3,
            "old_formula": 4,
            "actual_qty": 5.0,
            "reserved_qty": 1.0,
            "reserved_qty_for_production": 0.0,
            "planned_qty": 2.0,
            "unmirrored": 1,
        }
    ]


def test_report_before_any_computation_is_empty(client: httpx.Client) -> None:
    assert client.get("/report", headers=operator()).json()["rows"] == []


@pytest.mark.parametrize(
    "path", ["/problems", "/problem", "/bindings", "/record", "/report"]
)
def test_read_endpoints_take_an_operator_token_for_this_environment(
    client: httpx.Client, path: str
) -> None:
    assert client.get(path).status_code == 401
    assert client.get(path, headers=operator(METRICS_TOKEN)).status_code == 401
    production = operator(environment=Environment.PRODUCTION)
    assert client.get(path, headers=production).status_code == 409


def test_record_without_a_variant_is_a_bad_request(client: httpx.Client) -> None:
    response = client.get("/record", headers=operator())

    assert response.status_code == 400
    assert "variant" in response.json()["error"]


# The write path


def audit_rows(store: sqlite3.Connection) -> list[tuple[str, str, object]]:
    rows = store.execute("SELECT operator, action, detail FROM audit_log").fetchall()
    return [
        (operator, action, None if detail is None else json.loads(detail))
        for operator, action, detail in rows
    ]


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch, worker: FakeWorker) -> list[object]:
    """A POST route at /probe that audits its body; returns the bodies it saw.

    It answers 404 after writing when the body asks it to fail, to show that
    a refused mutation leaves nothing behind.
    """
    seen: list[object] = []

    def handle(server: ApiServer, request: api._Request) -> api._Response:
        seen.append(dict(request.body))
        assert worker.writer_lock.locked()
        assert request.operator is not None
        audit(request.connection, request.operator, "probe", json.dumps({}), NOW)
        if request.body.get("fail"):
            return server.error(HTTPStatus.NOT_FOUND, "probe refused")
        if request.body.get("raise"):
            raise RuntimeError("probe failed")
        return server.json(HTTPStatus.OK, {"done": True})

    monkeypatch.setitem(
        api._ROUTES, "/probe", api._Route("POST", api._Access.OPERATOR, handle)
    )
    return seen


def test_a_mutation_runs_under_the_writer_lock_and_commits(
    client: httpx.Client, store: sqlite3.Connection, probe: list[object]
) -> None:
    response = client.post("/probe", json={"x": 1}, headers=operator(BOB_TOKEN))

    assert response.status_code == 200
    assert response.json() == {"environment": "staging", "done": True}
    assert probe == [{"x": 1}]
    assert audit_rows(store) == [("bob", "probe", {})]


def test_a_refused_mutation_writes_nothing(
    client: httpx.Client, store: sqlite3.Connection, probe: list[object]
) -> None:
    response = client.post("/probe", json={"fail": True}, headers=operator())

    assert response.status_code == 404
    assert audit_rows(store) == []


def test_a_failed_mutation_writes_nothing_and_releases_the_lock(
    client: httpx.Client,
    store: sqlite3.Connection,
    worker: FakeWorker,
    probe: list[object],
) -> None:
    response = client.post("/probe", json={"raise": True}, headers=operator())

    assert response.status_code == 500
    assert response.json()["environment"] == "staging"
    assert audit_rows(store) == []
    assert not worker.writer_lock.locked()


def test_a_mutation_while_a_pass_holds_the_lock_is_refused(
    client: httpx.Client,
    store: sqlite3.Connection,
    worker: FakeWorker,
    probe: list[object],
) -> None:
    with worker.writer_lock:
        response = client.post("/probe", json={}, headers=operator())

    assert response.status_code == 503
    body = response.json()
    assert body["environment"] == "staging"
    assert "pass" in body["error"]
    assert "retry" in body["error"]
    assert probe == []
    assert audit_rows(store) == []


def test_a_mutation_waits_for_a_pass_that_ends_within_the_bound(
    client: httpx.Client, worker: FakeWorker, probe: list[object]
) -> None:
    worker.writer_lock.acquire()
    wait = WRITER_LOCK_TIMEOUT_SECONDS / 10
    release = threading.Timer(wait, worker.writer_lock.release)
    release.start()
    try:
        response = client.post("/probe", json={}, headers=operator())
    finally:
        release.join()

    assert response.status_code == 200


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        (b"{not json", "JSON"),
        (b"\xff\xfe", "JSON"),
        (b"[1, 2]", "object"),
        (b"", "object"),
    ],
)
def test_a_body_that_is_not_a_json_object_is_a_bad_request(
    client: httpx.Client, probe: list[object], content: bytes, reason: str
) -> None:
    response = client.post("/probe", content=content, headers=operator())

    assert response.status_code == 400
    assert response.json()["environment"] == "staging"
    assert reason in response.json()["error"]
    assert probe == []


def test_a_body_past_the_limit_is_refused(
    client: httpx.Client, probe: list[object]
) -> None:
    content = b" " * (MAX_BODY_BYTES + 1)

    response = client.post("/probe", content=content, headers=operator())

    assert response.status_code == 413
    assert response.json()["environment"] == "staging"
    assert probe == []


@pytest.mark.parametrize("length", ["many", "-1"])
def test_a_bad_content_length_is_a_bad_request(
    server: ApiServer, probe: list[object], length: str
) -> None:
    request = (
        "POST /probe HTTP/1.1\r\n"
        f"{AUTH_HEADER}: Bearer {ALICE_TOKEN}\r\n"
        f"{ENVIRONMENT_HEADER}: staging\r\n"
        f"Content-Length: {length}\r\n\r\n"
    )
    with socket.create_connection(("127.0.0.1", server.port), timeout=5) as raw:
        raw.sendall(request.encode())
        answer = raw.recv(4096)

    assert answer.startswith(b"HTTP/1.0 400")
    assert probe == []


MUTATIONS = {"/dry-run": {"on": False}}


@pytest.mark.parametrize("path", MUTATIONS)
def test_a_mutation_takes_an_operator_token_for_this_environment(
    client: httpx.Client, store: sqlite3.Connection, path: str
) -> None:
    def status(headers: dict[str, str]) -> int:
        return client.post(path, json=MUTATIONS[path], headers=headers).status_code

    assert status({}) == 401
    assert status(operator(METRICS_TOKEN)) == 401
    assert status(operator(environment=Environment.PRODUCTION)) == 409
    assert status(bearer(ALICE_TOKEN)) == 409
    assert audit_rows(store) == []


def test_a_mutation_endpoint_refuses_get(
    client: httpx.Client, probe: list[object]
) -> None:
    response = client.get("/probe", headers=operator())

    assert response.status_code == 405
    assert response.headers["allow"] == "POST"


def test_turning_dry_run_off_is_audited_with_the_operators_name(
    client: httpx.Client, store: sqlite3.Connection
) -> None:
    response = client.post("/dry-run", json={"on": False}, headers=operator())

    assert response.status_code == 200
    assert response.json() == {
        "environment": "staging",
        "dry_run": False,
        "changed": True,
    }
    assert audit_rows(store) == [("alice", "dry-run", {"old": True, "new": False})]
    assert client.get("/status", headers=operator()).json()["dry_run"] is False


def test_turning_dry_run_back_on_is_audited(
    client: httpx.Client, store: sqlite3.Connection
) -> None:
    set_dry_run(store, False)

    response = client.post("/dry-run", json={"on": True}, headers=operator(BOB_TOKEN))

    assert response.json()["dry_run"] is True
    assert audit_rows(store) == [("bob", "dry-run", {"old": False, "new": True})]


def test_setting_dry_run_to_its_state_changes_and_audits_nothing(
    client: httpx.Client, store: sqlite3.Connection
) -> None:
    response = client.post("/dry-run", json={"on": True}, headers=operator())

    assert response.status_code == 200
    assert response.json() == {
        "environment": "staging",
        "dry_run": True,
        "changed": False,
    }
    assert audit_rows(store) == []


@pytest.mark.parametrize("body", [{}, {"on": "off"}, {"on": 0}, {"on": None}])
def test_dry_run_needs_a_boolean(
    client: httpx.Client, store: sqlite3.Connection, body: dict[str, object]
) -> None:
    response = client.post("/dry-run", json=body, headers=operator())

    assert response.status_code == 400
    assert response.json()["environment"] == "staging"
    assert "on" in response.json()["error"]
    assert audit_rows(store) == []
