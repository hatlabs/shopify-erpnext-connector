import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from helpers import EMAIL_RECIPIENT, EMAIL_SENDER, ERPNEXT_URL, make_config

from shopify_erpnext_connector.config import Config, Environment
from shopify_erpnext_connector.erpnext import MAX_ATTEMPTS, ErpNextClient
from shopify_erpnext_connector.notify import (
    EMAIL_RETRY_INTERVAL,
    MAKE_METHOD,
    MAX_EMAIL_ATTEMPTS,
    render_body,
    send_pending,
)
from shopify_erpnext_connector.problems import (
    Problem,
    ProblemKind,
    count_unsent_emails,
    get_problem,
    record_check,
)
from shopify_erpnext_connector.store import open_store

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
VARIANT = "gid://shopify/ProductVariant/1001"
OTHER_VARIANT = "gid://shopify/ProductVariant/1002"
# run_pass sends from production by default.
SHOW_COMMAND = (
    f"shopify-erpnext-connector --env production problems show unbound_sku:{VARIANT}"
)
ONE_SECOND = timedelta(seconds=1)
COMMUNICATION_PATH = "/api/resource/Communication"


class FakeErpNext:
    """The parts of ERPNext problem emails use: `make` and Communication reads.

    `make_outcome` decides how a `make` call behaves, unless `make_outcomes`
    still holds per-call outcomes, which are used first, in order:
    - "sent": commits the Communication, delivers it and returns its name;
    - "queued": commits it with the given `queued_status` and returns its name;
    - "500": commits it Not Sent and answers 500, as a failed SMTP login does;
    - "unreachable": the connection is refused and nothing is committed.
    """

    def __init__(self) -> None:
        self.make_outcome = "sent"
        self.make_outcomes: list[str] = []
        self.queued_status = "Not Sent"
        self.communications: dict[str, dict[str, object]] = {}
        self.make_calls: list[dict[str, object]] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == f"/api/method/{MAKE_METHOD}":
            return self._make(request)
        if request.url.path.startswith(COMMUNICATION_PATH + "/"):
            name = request.url.path.rsplit("/", 1)[1]
            return httpx.Response(200, json={"data": self.communications[name]})
        return httpx.Response(404, json={})

    def set_status(self, name: str, status: str) -> None:
        self.communications[name]["delivery_status"] = status

    def only_communication(self) -> dict[str, object]:
        (doc,) = self.communications.values()
        return doc

    def _make(self, request: httpx.Request) -> httpx.Response:
        outcome = self.make_outcomes.pop(0) if self.make_outcomes else self.make_outcome
        if outcome == "unreachable":
            raise httpx.ConnectError("connection refused", request=request)
        body = json.loads(request.content)
        self.make_calls.append(body)
        name = f"comm{len(self.communications) + 1:04d}"
        status = {"sent": "Sent", "500": "Not Sent"}.get(outcome, self.queued_status)
        self.communications[name] = {
            "name": name,
            "subject": body["subject"],
            "delivery_status": status,
        }
        if outcome == "500":
            return httpx.Response(500, json={"exc_type": "SMTPAuthenticationError"})
        return httpx.Response(200, json={"message": {"name": name}})


def unbound(subject: str = VARIANT, sku: str = "TEST-SKU-001") -> Problem:
    return Problem(
        kind=ProblemKind.UNBOUND_SKU,
        subject=subject,
        details={"SKU": sku, "Variant": "Test product / Default"},
    )


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "connector.sqlite3"


@pytest.fixture
def store(db_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_store(db_path)
    yield connection
    connection.close()


@pytest.fixture
def erpnext() -> FakeErpNext:
    return FakeErpNext()


def run_pass(
    store: sqlite3.Connection,
    erpnext: FakeErpNext,
    found: list[Problem],
    *,
    kind: ProblemKind = ProblemKind.UNBOUND_SKU,
    config: Config | None = None,
    now: datetime = NOW,
) -> None:
    config = config or make_config(
        environment=Environment.PRODUCTION, email_enabled=True
    )
    record_check(store, kind, found, now)
    http = httpx.Client(base_url=ERPNEXT_URL, transport=httpx.MockTransport(erpnext))
    client = ErpNextClient(config, http, sleep=lambda _: None)
    send_pending(store, client, config, now)


def email_attempts(store: sqlite3.Connection) -> int:
    stored = get_problem(store, ProblemKind.UNBOUND_SKU, VARIANT)
    assert stored is not None
    return stored.email_attempts


def is_pending(store: sqlite3.Connection, subject: str = VARIANT) -> bool:
    stored = get_problem(store, ProblemKind.UNBOUND_SKU, subject)
    assert stored is not None
    return stored.email_pending


def test_new_problem_sends_one_email_with_the_cli_command(
    store: sqlite3.Connection, erpnext: FakeErpNext
) -> None:
    run_pass(store, erpnext, [unbound()])

    (call,) = erpnext.make_calls
    assert call["recipients"] == EMAIL_RECIPIENT
    assert call["sender"] == EMAIL_SENDER
    assert call["send_email"] == 1
    assert call["communication_medium"] == "Email"
    assert call["sent_or_received"] == "Sent"
    assert SHOW_COMMAND in str(call["content"])
    assert "TEST-SKU-001" in str(call["content"])
    assert call["subject"] == f"[production] Unbound Shopify variant: {VARIANT}"
    assert not is_pending(store)


def test_email_is_not_filed_against_a_document(
    store: sqlite3.Connection, erpnext: FakeErpNext
) -> None:
    run_pass(store, erpnext, [unbound()])

    (call,) = erpnext.make_calls
    assert "doctype" not in call
    assert "name" not in call


def test_same_problem_on_the_next_pass_sends_nothing(
    store: sqlite3.Connection, erpnext: FakeErpNext
) -> None:
    run_pass(store, erpnext, [unbound()])
    requests_after_first = len(erpnext.requests)

    run_pass(store, erpnext, [unbound()])

    assert len(erpnext.make_calls) == 1
    assert len(erpnext.requests) == requests_after_first


def test_changed_fingerprint_sends_one_update(
    store: sqlite3.Connection, erpnext: FakeErpNext
) -> None:
    run_pass(store, erpnext, [unbound()])

    run_pass(store, erpnext, [unbound(sku="TEST-SKU-002")])
    run_pass(store, erpnext, [unbound(sku="TEST-SKU-002")])

    assert len(erpnext.make_calls) == 2
    assert "TEST-SKU-002" in str(erpnext.make_calls[1]["content"])


def test_resolved_problem_that_reopens_sends_again(
    store: sqlite3.Connection, erpnext: FakeErpNext
) -> None:
    run_pass(store, erpnext, [unbound()])
    run_pass(store, erpnext, [])
    assert len(erpnext.make_calls) == 1

    run_pass(store, erpnext, [unbound()])

    assert len(erpnext.make_calls) == 2


def test_restart_does_not_re_email(db_path: Path, erpnext: FakeErpNext) -> None:
    first = open_store(db_path)
    run_pass(first, erpnext, [unbound()])
    first.close()

    second = open_store(db_path)
    run_pass(second, erpnext, [unbound()])
    second.close()

    assert len(erpnext.make_calls) == 1


def test_unreachable_erpnext_keeps_the_email_pending_until_a_later_pass(
    store: sqlite3.Connection, erpnext: FakeErpNext
) -> None:
    erpnext.make_outcome = "unreachable"
    run_pass(store, erpnext, [unbound()])
    assert erpnext.make_calls == []
    assert is_pending(store)

    erpnext.make_outcome = "sent"
    run_pass(store, erpnext, [unbound()], now=NOW + EMAIL_RETRY_INTERVAL)

    assert len(erpnext.make_calls) == 1
    assert not is_pending(store)


def test_unreachable_erpnext_stops_the_pass(
    store: sqlite3.Connection, erpnext: FakeErpNext
) -> None:
    erpnext.make_outcome = "unreachable"

    run_pass(store, erpnext, [unbound(), unbound(OTHER_VARIANT)])

    make_attempts = [
        r for r in erpnext.requests if r.url.path == f"/api/method/{MAKE_METHOD}"
    ]
    # One problem's attempts (the client retries refused connections), not two.
    assert len(make_attempts) == MAX_ATTEMPTS


def test_failed_send_moves_on_to_the_next_problem(
    store: sqlite3.Connection, erpnext: FakeErpNext
) -> None:
    erpnext.make_outcomes = ["500", "sent"]

    run_pass(store, erpnext, [unbound(), unbound(OTHER_VARIANT)])

    assert [call["subject"] for call in erpnext.make_calls] == [
        f"[production] Unbound Shopify variant: {VARIANT}",
        f"[production] Unbound Shopify variant: {OTHER_VARIANT}",
    ]
    assert erpnext.communications["comm0002"]["delivery_status"] == "Sent"
    assert not is_pending(store, OTHER_VARIANT)
    assert is_pending(store)
    assert email_attempts(store) == 1


def test_delivery_not_sent_keeps_the_email_pending_and_counted(
    store: sqlite3.Connection, erpnext: FakeErpNext
) -> None:
    erpnext.make_outcome = "queued"
    run_pass(store, erpnext, [unbound()])

    assert is_pending(store)
    assert count_unsent_emails(store) == 1

    erpnext.set_status("comm0001", "Sent")
    run_pass(store, erpnext, [unbound()])

    assert len(erpnext.make_calls) == 1
    assert not is_pending(store)
    assert count_unsent_emails(store) == 0


def test_email_read_before_its_status_was_seen_counts_as_delivered(
    store: sqlite3.Connection, erpnext: FakeErpNext
) -> None:
    erpnext.make_outcome = "queued"
    run_pass(store, erpnext, [unbound()])

    # With Track Email Status on, opening the email turns Sent into Read.
    erpnext.set_status("comm0001", "Read")
    run_pass(store, erpnext, [unbound()])

    assert len(erpnext.make_calls) == 1
    assert not is_pending(store)


@pytest.mark.parametrize("given_up", ["Error", "Expired"])
def test_delivery_that_failed_for_good_is_sent_again_as_a_new_attempt(
    store: sqlite3.Connection, erpnext: FakeErpNext, given_up: str
) -> None:
    erpnext.make_outcome = "queued"
    erpnext.queued_status = given_up
    run_pass(store, erpnext, [unbound()])
    assert is_pending(store)

    erpnext.make_outcome = "sent"
    run_pass(store, erpnext, [unbound()], now=NOW + EMAIL_RETRY_INTERVAL)

    assert len(erpnext.make_calls) == 2
    assert email_attempts(store) == 2
    assert not is_pending(store)


def test_server_error_is_retried_only_after_the_retry_interval(
    store: sqlite3.Connection, erpnext: FakeErpNext
) -> None:
    erpnext.make_outcome = "500"
    run_pass(store, erpnext, [unbound()])
    assert is_pending(store)

    erpnext.make_outcome = "sent"
    run_pass(store, erpnext, [unbound()], now=NOW + EMAIL_RETRY_INTERVAL - ONE_SECOND)
    assert len(erpnext.make_calls) == 1

    run_pass(store, erpnext, [unbound()], now=NOW + EMAIL_RETRY_INTERVAL)

    assert len(erpnext.make_calls) == 2
    assert not is_pending(store)


def test_no_email_after_the_attempts_run_out_and_it_counts_as_unsent(
    store: sqlite3.Connection, erpnext: FakeErpNext
) -> None:
    erpnext.make_outcome = "500"

    for attempt in range(MAX_EMAIL_ATTEMPTS + 2):
        run_pass(store, erpnext, [unbound()], now=NOW + attempt * EMAIL_RETRY_INTERVAL)

    assert len(erpnext.make_calls) == MAX_EMAIL_ATTEMPTS
    assert is_pending(store)
    assert count_unsent_emails(store) == 1


def test_changed_fingerprint_resets_the_attempts(
    store: sqlite3.Connection, erpnext: FakeErpNext
) -> None:
    erpnext.make_outcome = "500"
    for attempt in range(MAX_EMAIL_ATTEMPTS):
        run_pass(store, erpnext, [unbound()], now=NOW + attempt * EMAIL_RETRY_INTERVAL)
    after_exhausted = NOW + MAX_EMAIL_ATTEMPTS * EMAIL_RETRY_INTERVAL

    erpnext.make_outcome = "sent"
    run_pass(store, erpnext, [unbound(sku="TEST-SKU-002")], now=after_exhausted)

    assert len(erpnext.make_calls) == MAX_EMAIL_ATTEMPTS + 1
    assert not is_pending(store)


def test_attempts_survive_a_restart(db_path: Path, erpnext: FakeErpNext) -> None:
    erpnext.make_outcome = "500"
    first = open_store(db_path)
    run_pass(first, erpnext, [unbound()])
    first.close()

    erpnext.make_outcome = "sent"
    second = open_store(db_path)
    run_pass(second, erpnext, [unbound()], now=NOW + ONE_SECOND)
    assert len(erpnext.make_calls) == 1

    run_pass(second, erpnext, [unbound()], now=NOW + EMAIL_RETRY_INTERVAL)
    assert len(erpnext.make_calls) == 2
    assert email_attempts(second) == 2
    second.close()


def test_staging_email_command_asks_staging(
    store: sqlite3.Connection, erpnext: FakeErpNext
) -> None:
    staging = make_config(environment=Environment.STAGING, email_enabled=True)

    run_pass(store, erpnext, [unbound()], config=staging)

    (call,) = erpnext.make_calls
    assert SHOW_COMMAND.replace("production", "staging") in str(call["content"])


def test_sending_disabled_records_the_problem_without_calling_erpnext(
    store: sqlite3.Connection, erpnext: FakeErpNext
) -> None:
    run_pass(
        store,
        erpnext,
        [unbound()],
        config=make_config(environment=Environment.PRODUCTION),
    )

    assert erpnext.requests == []
    assert is_pending(store)
    assert count_unsent_emails(store) == 1


def test_resolved_problem_is_not_sent(
    store: sqlite3.Connection, erpnext: FakeErpNext
) -> None:
    disabled = make_config(environment=Environment.PRODUCTION)
    run_pass(store, erpnext, [unbound()], config=disabled)

    run_pass(store, erpnext, [])

    assert erpnext.requests == []


def test_shopify_values_are_escaped_in_the_email(
    store: sqlite3.Connection, erpnext: FakeErpNext
) -> None:
    run_pass(store, erpnext, [unbound(sku="<script>alert(1)</script>")])

    content = str(erpnext.make_calls[0]["content"])
    assert "<script>" not in content
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in content


def test_subject_line_escapes_the_problem_subject(
    store: sqlite3.Connection, erpnext: FakeErpNext
) -> None:
    run_pass(store, erpnext, [unbound(subject="<b>1001</b>")])

    subject = str(erpnext.make_calls[0]["subject"])
    assert "<b>" not in subject
    assert "&lt;b&gt;1001&lt;/b&gt;" in subject


def test_oversell_email_names_the_order_numbers_and_the_item(
    store: sqlite3.Connection,
) -> None:
    oversell = Problem(
        kind=ProblemKind.OVERSELL,
        subject=VARIANT,
        details={
            "Item": "TEST-ITEM-001",
            "Short by": "2",
            "Newest Shopify orders": "#1001, #1002",
        },
    )
    record_check(store, ProblemKind.OVERSELL, [oversell], NOW)
    stored = get_problem(store, ProblemKind.OVERSELL, VARIANT)
    assert stored is not None

    body = render_body(stored, Environment.PRODUCTION)

    assert "#1001, #1002" in body
    assert "TEST-ITEM-001" in body


def test_email_names_what_is_wrong_and_the_action(
    store: sqlite3.Connection, erpnext: FakeErpNext
) -> None:
    run_pass(store, erpnext, [unbound()])

    content = str(erpnext.make_calls[0]["content"])
    assert "What is wrong" in content
    assert "What to do" in content


def test_outcome_for_an_outdated_version_does_not_clear_the_update(
    store: sqlite3.Connection, erpnext: FakeErpNext
) -> None:
    erpnext.make_outcome = "queued"
    run_pass(store, erpnext, [unbound()])

    record_check(store, ProblemKind.UNBOUND_SKU, [unbound(sku="TEST-SKU-002")], NOW)
    erpnext.set_status("comm0001", "Sent")
    erpnext.make_outcome = "sent"
    run_pass(store, erpnext, [unbound(sku="TEST-SKU-002")])

    assert len(erpnext.make_calls) == 2
    assert not is_pending(store)


def test_queued_email_is_rechecked_each_pass_without_resending(
    store: sqlite3.Connection, erpnext: FakeErpNext
) -> None:
    erpnext.make_outcome = "queued"
    erpnext.queued_status = "Sending"

    for attempt in range(MAX_EMAIL_ATTEMPTS + 1):
        run_pass(store, erpnext, [unbound()], now=NOW + attempt * EMAIL_RETRY_INTERVAL)

    assert len(erpnext.make_calls) == 1
    assert email_attempts(store) == 1
    assert count_unsent_emails(store) == 1

    erpnext.set_status("comm0001", "Sent")
    run_pass(
        store,
        erpnext,
        [unbound()],
        now=NOW + (MAX_EMAIL_ATTEMPTS + 1) * EMAIL_RETRY_INTERVAL,
    )

    assert not is_pending(store)
