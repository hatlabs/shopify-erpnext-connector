import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from shopify_erpnext_connector.problems import (
    Problem,
    ProblemKind,
    count_open_problems,
    count_unsent_emails,
    get_problem,
    open_problems,
    problem_key,
    record_check,
    record_communication,
    record_delivered,
    record_send_attempt,
    show_command,
    unsent_emails,
)
from shopify_erpnext_connector.store import open_store, utc_timestamp

NOW = datetime(2026, 11, 12, 10, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=5)
VARIANT = "gid://shopify/ProductVariant/1001"
OTHER_VARIANT = "gid://shopify/ProductVariant/1002"


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


def test_new_problem_opens_with_its_email_pending(
    store: sqlite3.Connection,
) -> None:
    record_check(store, ProblemKind.UNBOUND_SKU, [unbound()], NOW)

    stored = get_problem(store, ProblemKind.UNBOUND_SKU, VARIANT)
    assert stored is not None
    assert stored.details == {
        "SKU": "TEST-SKU-001",
        "Variant": "Test product / Default",
    }
    assert stored.opened_at == utc_timestamp(NOW)
    assert stored.resolved_at is None
    assert stored.email_pending
    assert [p.subject for p in unsent_emails(store)] == [VARIANT]


def test_same_problem_on_a_later_pass_changes_nothing(
    store: sqlite3.Connection,
) -> None:
    record_check(store, ProblemKind.UNBOUND_SKU, [unbound()], NOW)
    first = get_problem(store, ProblemKind.UNBOUND_SKU, VARIANT)
    assert first is not None
    record_communication(store, first, "COMM-0001")
    record_delivered(store, first)

    record_check(store, ProblemKind.UNBOUND_SKU, [unbound()], LATER)

    stored = get_problem(store, ProblemKind.UNBOUND_SKU, VARIANT)
    assert stored is not None
    assert not stored.email_pending
    assert stored.opened_at == utc_timestamp(NOW)
    assert stored.communication_name == "COMM-0001"


def test_changed_fingerprint_makes_a_new_email_pending(
    store: sqlite3.Connection,
) -> None:
    record_check(store, ProblemKind.UNBOUND_SKU, [unbound()], NOW)
    first = get_problem(store, ProblemKind.UNBOUND_SKU, VARIANT)
    assert first is not None
    record_communication(store, first, "COMM-0001")
    record_delivered(store, first)

    record_check(store, ProblemKind.UNBOUND_SKU, [unbound(sku="TEST-SKU-002")], LATER)

    stored = get_problem(store, ProblemKind.UNBOUND_SKU, VARIANT)
    assert stored is not None
    assert stored.email_pending
    assert stored.fingerprint != first.fingerprint
    assert stored.details["SKU"] == "TEST-SKU-002"
    assert stored.communication_name is None
    assert stored.opened_at == utc_timestamp(NOW)


def test_detail_order_does_not_change_the_fingerprint() -> None:
    reordered = Problem(
        kind=ProblemKind.UNBOUND_SKU,
        subject=VARIANT,
        details={"Variant": "Test product / Default", "SKU": "TEST-SKU-001"},
    )

    assert reordered.fingerprint == unbound().fingerprint


def with_context(shortfall: str) -> Problem:
    return Problem(
        kind=ProblemKind.OVERSELL,
        subject=VARIANT,
        details={"Item": "TEST-ITEM-001"},
        context={"Short by": shortfall},
    )


def test_context_is_left_out_of_the_fingerprint() -> None:
    assert with_context("1").fingerprint == with_context("2").fingerprint


def test_context_is_stored_after_the_details(store: sqlite3.Connection) -> None:
    record_check(store, ProblemKind.OVERSELL, [with_context("1")], NOW)

    stored = get_problem(store, ProblemKind.OVERSELL, VARIANT)
    assert stored is not None
    assert list(stored.details.items()) == [
        ("Item", "TEST-ITEM-001"),
        ("Short by", "1"),
    ]


def test_changed_context_is_stored_without_a_new_email(
    store: sqlite3.Connection,
) -> None:
    record_check(store, ProblemKind.OVERSELL, [with_context("1")], NOW)
    first = get_problem(store, ProblemKind.OVERSELL, VARIANT)
    assert first is not None
    record_communication(store, first, "COMM-0001")
    record_delivered(store, first)

    record_check(store, ProblemKind.OVERSELL, [with_context("2")], LATER)

    stored = get_problem(store, ProblemKind.OVERSELL, VARIANT)
    assert stored is not None
    assert stored.details["Short by"] == "2"
    assert not stored.email_pending
    assert stored.communication_name == "COMM-0001"
    assert stored.opened_at == utc_timestamp(NOW)


def test_problem_absent_on_a_later_pass_is_resolved_without_email(
    store: sqlite3.Connection,
) -> None:
    record_check(store, ProblemKind.UNBOUND_SKU, [unbound()], NOW)

    record_check(store, ProblemKind.UNBOUND_SKU, [], LATER)

    stored = get_problem(store, ProblemKind.UNBOUND_SKU, VARIANT)
    assert stored is not None
    assert stored.resolved_at == utc_timestamp(LATER)
    assert not stored.email_pending
    assert open_problems(store) == []
    assert count_unsent_emails(store) == 0


def test_resolved_problem_that_reopens_is_emailed_again(
    store: sqlite3.Connection,
) -> None:
    record_check(store, ProblemKind.UNBOUND_SKU, [unbound()], NOW)
    first = get_problem(store, ProblemKind.UNBOUND_SKU, VARIANT)
    assert first is not None
    record_send_attempt(store, first, NOW)
    record_communication(store, first, "COMM-0001")
    record_delivered(store, first)
    record_check(store, ProblemKind.UNBOUND_SKU, [], LATER)

    reopened_at = LATER + timedelta(minutes=5)
    record_check(store, ProblemKind.UNBOUND_SKU, [unbound()], reopened_at)

    stored = get_problem(store, ProblemKind.UNBOUND_SKU, VARIANT)
    assert stored is not None
    assert stored.resolved_at is None
    assert stored.opened_at == utc_timestamp(reopened_at)
    assert stored.email_pending
    assert stored.communication_name is None
    assert stored.email_attempts == 0


def test_check_resolves_only_its_own_kind(store: sqlite3.Connection) -> None:
    record_check(store, ProblemKind.UNBOUND_SKU, [unbound()], NOW)

    record_check(store, ProblemKind.CHANGED_SKU, [], LATER)

    assert [p.subject for p in open_problems(store)] == [VARIANT]


def test_partial_check_resolves_only_the_subjects_it_checked(
    store: sqlite3.Connection,
) -> None:
    record_check(
        store, ProblemKind.UNBOUND_SKU, [unbound(), unbound(OTHER_VARIANT)], NOW
    )

    record_check(store, ProblemKind.UNBOUND_SKU, [], LATER, checked=[VARIANT])

    assert [p.subject for p in open_problems(store)] == [OTHER_VARIANT]


def test_check_listing_a_subject_twice_is_rejected(
    store: sqlite3.Connection,
) -> None:
    with pytest.raises(ValueError, match="twice"):
        record_check(
            store,
            ProblemKind.UNBOUND_SKU,
            [unbound(sku="TEST-SKU-001"), unbound(sku="TEST-SKU-002")],
            NOW,
        )

    assert open_problems(store) == []


def test_check_listing_a_problem_of_another_kind_is_rejected(
    store: sqlite3.Connection,
) -> None:
    with pytest.raises(ValueError, match="kind"):
        record_check(store, ProblemKind.OVERSELL, [unbound()], NOW)

    assert open_problems(store) == []


def test_problems_survive_a_restart(db_path: Path) -> None:
    first = open_store(db_path)
    record_check(first, ProblemKind.UNBOUND_SKU, [unbound()], NOW)
    stored = get_problem(first, ProblemKind.UNBOUND_SKU, VARIANT)
    assert stored is not None
    record_communication(first, stored, "COMM-0001")
    record_delivered(first, stored)
    first.close()

    second = open_store(db_path)
    record_check(second, ProblemKind.UNBOUND_SKU, [unbound()], LATER)

    assert unsent_emails(second) == []
    second.close()


def test_delivery_of_an_outdated_version_is_not_recorded(
    store: sqlite3.Connection,
) -> None:
    record_check(store, ProblemKind.UNBOUND_SKU, [unbound()], NOW)
    outdated = get_problem(store, ProblemKind.UNBOUND_SKU, VARIANT)
    assert outdated is not None
    record_check(store, ProblemKind.UNBOUND_SKU, [unbound(sku="TEST-SKU-002")], LATER)

    record_communication(store, outdated, "COMM-0001")
    record_delivered(store, outdated)

    stored = get_problem(store, ProblemKind.UNBOUND_SKU, VARIANT)
    assert stored is not None
    assert stored.email_pending
    assert stored.communication_name is None


def test_send_attempts_are_counted_with_the_time_of_the_last(
    store: sqlite3.Connection,
) -> None:
    record_check(store, ProblemKind.UNBOUND_SKU, [unbound()], NOW)
    stored = get_problem(store, ProblemKind.UNBOUND_SKU, VARIANT)
    assert stored is not None
    assert stored.email_attempts == 0
    assert stored.last_email_attempt_at is None

    record_send_attempt(store, stored, NOW)
    record_send_attempt(store, stored, LATER)

    attempted = get_problem(store, ProblemKind.UNBOUND_SKU, VARIANT)
    assert attempted is not None
    assert attempted.email_attempts == 2
    assert attempted.last_email_attempt_at == utc_timestamp(LATER)
    assert attempted.email_pending


def test_changed_fingerprint_resets_the_send_attempts(
    store: sqlite3.Connection,
) -> None:
    record_check(store, ProblemKind.UNBOUND_SKU, [unbound()], NOW)
    stored = get_problem(store, ProblemKind.UNBOUND_SKU, VARIANT)
    assert stored is not None
    record_send_attempt(store, stored, NOW)

    record_check(store, ProblemKind.UNBOUND_SKU, [unbound(sku="TEST-SKU-002")], LATER)

    changed = get_problem(store, ProblemKind.UNBOUND_SKU, VARIANT)
    assert changed is not None
    assert changed.email_attempts == 0
    assert changed.last_email_attempt_at is None


def test_send_attempt_for_an_outdated_version_is_not_counted(
    store: sqlite3.Connection,
) -> None:
    record_check(store, ProblemKind.UNBOUND_SKU, [unbound()], NOW)
    outdated = get_problem(store, ProblemKind.UNBOUND_SKU, VARIANT)
    assert outdated is not None
    record_check(store, ProblemKind.UNBOUND_SKU, [unbound(sku="TEST-SKU-002")], LATER)

    record_send_attempt(store, outdated, LATER)

    stored = get_problem(store, ProblemKind.UNBOUND_SKU, VARIANT)
    assert stored is not None
    assert stored.email_attempts == 0


def test_send_attempts_survive_a_restart(db_path: Path) -> None:
    first = open_store(db_path)
    record_check(first, ProblemKind.UNBOUND_SKU, [unbound()], NOW)
    stored = get_problem(first, ProblemKind.UNBOUND_SKU, VARIANT)
    assert stored is not None
    record_send_attempt(first, stored, NOW)
    first.close()

    second = open_store(db_path)
    record_check(second, ProblemKind.UNBOUND_SKU, [unbound()], LATER)
    kept = get_problem(second, ProblemKind.UNBOUND_SKU, VARIANT)
    second.close()

    assert kept is not None
    assert kept.email_attempts == 1
    assert kept.last_email_attempt_at == utc_timestamp(NOW)


def test_unsent_count_covers_open_problems_awaiting_delivery(
    store: sqlite3.Connection,
) -> None:
    record_check(
        store, ProblemKind.UNBOUND_SKU, [unbound(), unbound(OTHER_VARIANT)], NOW
    )
    stored = get_problem(store, ProblemKind.UNBOUND_SKU, VARIANT)
    assert stored is not None
    record_communication(store, stored, "COMM-0001")

    # Handed to ERPNext but not yet Sent still counts.
    assert count_unsent_emails(store) == 2

    record_delivered(store, stored)
    assert count_unsent_emails(store) == 1


def test_problem_key_and_show_command_name_the_problem() -> None:
    assert problem_key(ProblemKind.UNBOUND_SKU, VARIANT) == f"unbound_sku:{VARIANT}"
    assert show_command(ProblemKind.UNMIRRORED_ORDER, "5550001") == (
        "shopify-erpnext-connector problems show unmirrored_order:5550001"
    )


def test_show_command_quotes_a_subject_the_shell_would_split() -> None:
    assert show_command(ProblemKind.AMBIGUOUS_SKU, "SKU WITH SPACE") == (
        "shopify-erpnext-connector problems show 'ambiguous_sku:SKU WITH SPACE'"
    )


def test_open_problems_are_counted_by_kind_with_every_kind_present(
    store: sqlite3.Connection,
) -> None:
    record_check(
        store, ProblemKind.UNBOUND_SKU, [unbound(), unbound(OTHER_VARIANT)], NOW
    )
    record_check(
        store,
        ProblemKind.OVERSELL,
        [Problem(ProblemKind.OVERSELL, VARIANT, {"Item": "TEST-ITEM"})],
        NOW,
    )
    record_check(store, ProblemKind.OVERSELL, [], LATER)

    counts = count_open_problems(store)

    assert counts == {kind: 0 for kind in ProblemKind} | {ProblemKind.UNBOUND_SKU: 2}
