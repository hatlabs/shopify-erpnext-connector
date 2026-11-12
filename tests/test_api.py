import logging
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from helpers import make_config

from shopify_erpnext_connector.api import ENVIRONMENT_HEADER, ApiServer, audit
from shopify_erpnext_connector.config import ApiConfig, Environment, Secret
from shopify_erpnext_connector.problems import Problem, ProblemKind, record_check
from shopify_erpnext_connector.store import open_store, set_dry_run, utc_timestamp

NOW = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)
ALICE_TOKEN = "test-alice-token"
BOB_TOKEN = "test-bob-token"
METRICS_TOKEN = "test-metrics-token"
# Kept apart from its value so the fixture check's header pattern stays meaningful.
AUTH_HEADER = "Authorization"
VARIANT = "gid://shopify/ProductVariant/1001"
OTHER_VARIANT = "gid://shopify/ProductVariant/1002"


@dataclass
class FakeWorker:
    last_pass_at: datetime | None = None
    last_pass_full: bool = False


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
    server = ApiServer(config, worker, clock=clock)
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
        "push_lag_seconds": 0,
        "open_problems": 0,
        "unsent_emails": 0,
    }


def test_status_reports_the_worker_and_the_store(
    client: httpx.Client, store: sqlite3.Connection, worker: FakeWorker
) -> None:
    worker.last_pass_at = NOW - timedelta(seconds=3)
    worker.last_pass_full = True
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


def test_audit_records_the_operator_and_action(store: sqlite3.Connection) -> None:
    audit(store, "alice", "dry-run off", "was on", NOW)

    rows = store.execute(
        "SELECT recorded_at, operator, action, detail FROM audit_log"
    ).fetchall()
    assert rows == [(utc_timestamp(NOW), "alice", "dry-run off", "was on")]
