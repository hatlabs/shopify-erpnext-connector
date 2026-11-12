"""Problem lifecycle: open, update, resolve and reopen, persisted in the store.

A problem is keyed by kind and subject (a Shopify variant GID, an ERPNext item
code or a Shopify order ID) and fingerprinted by its details. Each pass of a
check reports what it found with `record_check`: a new or reopened problem,
or one whose fingerprint changed, gets an email pending; a problem the check
no longer finds is resolved, with no email. A problem's context, such as a
shortfall that moves with every order, is stored and shown but not
fingerprinted, so a change in it alone is recorded without a new email. The
email state lives in the same row, so a restart neither loses a pending email
nor sends one twice.
"""

import hashlib
import json
import shlex
import sqlite3
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from shopify_erpnext_connector.store import utc_timestamp

CLI_NAME = "shopify-erpnext-connector"


class ProblemKind(StrEnum):
    UNBOUND_SKU = "unbound_sku"
    AMBIGUOUS_SKU = "ambiguous_sku"
    CHANGED_SKU = "changed_sku"
    UNMIRRORED_ORDER = "unmirrored_order"
    OVERSELL = "oversell"
    STALE_PUSH = "stale_push"


@dataclass(frozen=True)
class Problem:
    """What a check found. `details` and then `context` are shown, in order.

    Only `details` are fingerprinted: they say what makes it a new problem.
    Both hold identifiers and quantities only: never customer names or
    addresses.
    """

    kind: ProblemKind
    subject: str
    details: Mapping[str, str]
    context: Mapping[str, str] = field(default_factory=dict)
    fingerprint: str = field(init=False)

    def __post_init__(self) -> None:
        content = json.dumps(dict(self.details), sort_keys=True)
        digest = hashlib.sha256(content.encode()).hexdigest()
        object.__setattr__(self, "fingerprint", digest)


@dataclass(frozen=True)
class StoredProblem:
    kind: ProblemKind
    subject: str
    details: Mapping[str, str]
    fingerprint: str
    opened_at: str
    resolved_at: str | None
    email_pending: bool
    communication_name: str | None
    email_attempts: int
    last_email_attempt_at: str | None


def problem_key(kind: ProblemKind, subject: str) -> str:
    return f"{kind}:{subject}"


def show_command(kind: ProblemKind, subject: str) -> str:
    return f"{CLI_NAME} problems show {shlex.quote(problem_key(kind, subject))}"


def record_check(
    connection: sqlite3.Connection,
    kind: ProblemKind,
    found: Sequence[Problem],
    now: datetime,
    *,
    checked: Collection[str] | None = None,
) -> None:
    """Record one pass of the check for `kind`.

    `checked` names the subjects the pass examined; open problems among them
    that were not found are resolved. None means the pass examined every
    subject of the kind. A problem of another kind, or a subject listed twice,
    raises ValueError: either would make the stored problem flip between
    versions and be emailed on every pass.
    """
    timestamp = utc_timestamp(now)
    found_subjects = {problem.subject for problem in found}
    if any(problem.kind != kind for problem in found):
        raise ValueError(f"record_check({kind}): found a problem of another kind")
    if len(found_subjects) != len(found):
        raise ValueError(f"record_check({kind}): a subject is listed twice")
    connection.execute("BEGIN")
    try:
        for problem in found:
            _record_found(connection, problem, timestamp)
        for stored in _open_of_kind(connection, kind):
            if stored.subject in found_subjects:
                continue
            if checked is not None and stored.subject not in checked:
                continue
            connection.execute(
                "UPDATE problems SET resolved_at = ?, email_pending = 0 "
                "WHERE kind = ? AND subject = ?",
                (timestamp, kind, stored.subject),
            )
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


def get_problem(
    connection: sqlite3.Connection, kind: ProblemKind, subject: str
) -> StoredProblem | None:
    row = connection.execute(
        f"SELECT {_COLUMNS} FROM problems WHERE kind = ? AND subject = ?",
        (kind, subject),
    ).fetchone()
    return None if row is None else _stored(row)


def open_problems(connection: sqlite3.Connection) -> list[StoredProblem]:
    rows = connection.execute(
        f"SELECT {_COLUMNS} FROM problems WHERE resolved_at IS NULL "
        "ORDER BY opened_at, kind, subject"
    ).fetchall()
    return [_stored(row) for row in rows]


def unsent_emails(connection: sqlite3.Connection) -> list[StoredProblem]:
    """Open problems whose current email has not been confirmed Sent."""
    return [problem for problem in open_problems(connection) if problem.email_pending]


def count_unsent_emails(connection: sqlite3.Connection) -> int:
    """The unsent-email metric: open problems whose email is not yet Sent.

    An email whose send attempts ran out stays pending, so it counts here
    until the problem resolves or changes.
    """
    row = connection.execute(
        "SELECT count(*) FROM problems WHERE resolved_at IS NULL AND email_pending = 1"
    ).fetchone()
    return row[0]


# The email-state updates below match the fingerprint, so an outcome for a
# version the check has since replaced is dropped.


def record_send_attempt(
    connection: sqlite3.Connection, problem: StoredProblem, now: datetime
) -> None:
    _update_email_state(
        connection,
        problem,
        "email_attempts = email_attempts + 1, last_email_attempt_at = ?",
        (utc_timestamp(now),),
    )


def record_communication(
    connection: sqlite3.Connection, problem: StoredProblem, name: str
) -> None:
    _update_email_state(connection, problem, "communication_name = ?", (name,))


def forget_communication(
    connection: sqlite3.Connection, problem: StoredProblem
) -> None:
    _update_email_state(connection, problem, "communication_name = NULL", ())


def record_delivered(connection: sqlite3.Connection, problem: StoredProblem) -> None:
    _update_email_state(connection, problem, "email_pending = 0", ())


_COLUMNS = (
    "kind, subject, details, fingerprint, opened_at, resolved_at, "
    "email_pending, communication_name, email_attempts, last_email_attempt_at"
)


def _record_found(
    connection: sqlite3.Connection, problem: Problem, timestamp: str
) -> None:
    stored = get_problem(connection, problem.kind, problem.subject)
    shown = json.dumps({**problem.details, **problem.context})
    if (
        stored is not None
        and stored.resolved_at is None
        and stored.fingerprint == problem.fingerprint
    ):
        connection.execute(
            "UPDATE problems SET details = ? WHERE kind = ? AND subject = ?",
            (shown, problem.kind, problem.subject),
        )
        return
    reopening = stored is None or stored.resolved_at is not None
    connection.execute(
        "INSERT INTO problems (kind, subject, details, fingerprint, "
        "opened_at, resolved_at, email_pending, communication_name, "
        "email_attempts, last_email_attempt_at) "
        "VALUES (?, ?, ?, ?, ?, NULL, 1, NULL, 0, NULL) "
        "ON CONFLICT (kind, subject) DO UPDATE SET details = excluded.details, "
        "fingerprint = excluded.fingerprint, "
        "opened_at = CASE WHEN ? THEN excluded.opened_at ELSE opened_at END, "
        "resolved_at = NULL, email_pending = 1, communication_name = NULL, "
        "email_attempts = 0, last_email_attempt_at = NULL",
        (
            problem.kind,
            problem.subject,
            shown,
            problem.fingerprint,
            timestamp,
            reopening,
        ),
    )


def _open_of_kind(
    connection: sqlite3.Connection, kind: ProblemKind
) -> list[StoredProblem]:
    rows = connection.execute(
        f"SELECT {_COLUMNS} FROM problems WHERE kind = ? AND resolved_at IS NULL",
        (kind,),
    ).fetchall()
    return [_stored(row) for row in rows]


def _update_email_state(
    connection: sqlite3.Connection,
    problem: StoredProblem,
    assignments: str,
    values: tuple[str, ...],
) -> None:
    connection.execute(
        f"UPDATE problems SET {assignments} "
        "WHERE kind = ? AND subject = ? AND fingerprint = ? AND resolved_at IS NULL",
        (*values, problem.kind, problem.subject, problem.fingerprint),
    )


def _stored(row: tuple[object, ...]) -> StoredProblem:
    (
        kind,
        subject,
        details,
        fingerprint,
        opened_at,
        resolved_at,
        email_pending,
        communication_name,
        email_attempts,
        last_email_attempt_at,
    ) = row
    decoded = json.loads(str(details))
    return StoredProblem(
        kind=ProblemKind(kind),
        subject=str(subject),
        details={str(k): str(v) for k, v in decoded.items()},
        fingerprint=str(fingerprint),
        opened_at=str(opened_at),
        resolved_at=_optional(resolved_at),
        email_pending=bool(email_pending),
        communication_name=_optional(communication_name),
        email_attempts=int(str(email_attempts)),
        last_email_attempt_at=_optional(last_email_attempt_at),
    )


def _optional(value: object) -> str | None:
    return None if value is None else str(value)
