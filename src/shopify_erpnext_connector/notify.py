"""Problem emails, sent and tracked through ERPNext.

Emails go out through ERPNext's whitelisted `make` method, so the connector
holds no SMTP credential. The API user cannot read Email Queue, so delivery is
read from the Communication's `delivery_status` by name. Emails are not filed
against an Item: Frappe would add a "Leave this conversation" link whose click
silently drops the operator from every later email filed against that Item.

A send whose outcome is unknown, or whose Communication ended in Error or
Expired, is retried on a later pass. A failed `make` can have committed its
Communication before answering, so a retry may duplicate the email; that is
accepted. Attempts are bounded per problem version and spaced apart, so a
broken mail account does not repeat the email every pass.
"""

import html
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from shopify_erpnext_connector.config import Config, Environment
from shopify_erpnext_connector.erpnext import (
    ErpNextClient,
    ErpNextError,
    ErpNextTransportError,
)
from shopify_erpnext_connector.problems import (
    CLI_NAME,
    ProblemKind,
    StoredProblem,
    forget_communication,
    problem_key,
    record_communication,
    record_delivered,
    record_send_attempt,
    show_command,
    unsent_emails,
)
from shopify_erpnext_connector.store import utc_timestamp

MAKE_METHOD = "frappe.core.doctype.communication.email.make"
# Read replaces Sent when the recipient opens the email and the mail account
# tracks email status.
DELIVERED = frozenset({"Sent", "Read"})
# Statuses after which ERPNext no longer tries to deliver.
GIVEN_UP = frozenset({"Error", "Expired"})
MAX_EMAIL_ATTEMPTS = 3
EMAIL_RETRY_INTERVAL = timedelta(hours=1)

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class KindText:
    title: str
    subject_label: str
    what_is_wrong: str
    what_to_do: str


KIND_TEXTS: dict[ProblemKind, KindText] = {
    ProblemKind.UNBOUND_SKU: KindText(
        title="Unbound Shopify variant",
        subject_label="Shopify variant",
        what_is_wrong=(
            "The variant's SKU matches no ERPNext Item, so the connector does "
            "not sync its stock and leaves its Shopify quantity as it is."
        ),
        what_to_do=(
            "Set the variant's SKU in Shopify to the item code of its ERPNext "
            "Item, or create the Item."
        ),
    ),
    ProblemKind.AMBIGUOUS_SKU: KindText(
        title="Ambiguous SKU",
        subject_label="Shopify variant",
        what_is_wrong=(
            "The variant's SKU cannot be bound to exactly one stock Item: "
            "another variant has the same SKU, the SKU differs from an item "
            "code only in case or spacing, or the matching Item is a template, "
            "disabled or not a stock item. Its stock is not synced."
        ),
        what_to_do=(
            "Give each variant a unique SKU equal to the item code of one "
            "enabled stock Item."
        ),
    ),
    ProblemKind.CHANGED_SKU: KindText(
        title="Changed SKU on a bound variant",
        subject_label="Shopify variant",
        what_is_wrong=(
            "The variant's SKU changed since it was bound, so the connector "
            "dropped the binding and stopped syncing its stock."
        ),
        what_to_do=(
            "Restore the old SKU, or confirm that the new SKU is the item code "
            "of the right ERPNext Item."
        ),
    ),
    ProblemKind.UNMIRRORED_ORDER: KindText(
        title="Shopify order without a matching Sales Order",
        subject_label="Shopify order",
        what_is_wrong=(
            "Lines of this order have no matching ERPNext Sales Order lines "
            "after the grace period. Their units stay subtracted from the "
            "stock pushed to Shopify until this is fixed."
        ),
        what_to_do=(
            "Check that ecommerce-integrations created the Sales Order and "
            "that its lines name the right Items; create or correct it."
        ),
    ),
    ProblemKind.OVERSELL: KindText(
        title="Orders exceed stock",
        subject_label="Shopify variant",
        what_is_wrong=(
            "Open orders need more units than are in stock or planned in production."
        ),
        what_to_do=(
            "Build or receive the missing units, or tell the affected "
            "customers about the delay."
        ),
    ),
    ProblemKind.STALE_PUSH: KindText(
        title="Stock not pushed to Shopify",
        subject_label="Shopify location",
        what_is_wrong=(
            "The connector has not managed to push the stock of the listed "
            "variants to Shopify in time, so Shopify may show wrong quantities "
            "for them. One email covers all of them; the list and count are "
            "as of when it was sent and the problem stays open until every "
            "variant is pushed."
        ),
        what_to_do=(
            "Check whether Shopify and ERPNext are reachable and what the "
            "connector's log and the variants' push history show."
        ),
    ),
}


def send_pending(
    connection: sqlite3.Connection,
    erpnext: ErpNextClient,
    config: Config,
    now: datetime,
) -> None:
    """Send or confirm the email of every open problem not yet Sent.

    With sending disabled nothing is sent and the emails stay pending.
    """
    if not config.email.enabled:
        return
    for problem in unsent_emails(connection):
        key = problem_key(problem.kind, problem.subject)
        try:
            _advance(connection, erpnext, config, problem, now)
        except ErpNextTransportError:
            _log.warning("ERPNext unreachable; problem emails wait for a later pass")
            return
        except ErpNextError as error:
            _log.warning("problem email for %s not sent: %s", key, error)


def render_subject(problem: StoredProblem, environment: Environment) -> str:
    text = KIND_TEXTS[problem.kind]
    return f"[{environment}] {text.title}: {html.escape(problem.subject)}"


def render_body(problem: StoredProblem, environment: Environment) -> str:
    text = KIND_TEXTS[problem.kind]
    rows = [(text.subject_label, problem.subject), *problem.details.items()]
    table = "\n".join(
        f'<tr><th align="left">{html.escape(label)}</th>'
        f"<td>{html.escape(value)}</td></tr>"
        for label, value in rows
    )
    command = html.escape(show_command(problem.kind, problem.subject, environment))
    return (
        f"<p>{CLI_NAME} ({environment}) found a problem: "
        f"<strong>{text.title}</strong>.</p>\n"
        f"<table>\n{table}\n</table>\n"
        f"<p><strong>What is wrong:</strong> {text.what_is_wrong}</p>\n"
        f"<p><strong>What to do:</strong> {text.what_to_do}</p>\n"
        f"<p>Details: <code>{command}</code></p>\n"
        "<p>The problem closes by itself once the connector's check passes; "
        "no email is sent when it closes.</p>"
    )


def _advance(
    connection: sqlite3.Connection,
    erpnext: ErpNextClient,
    config: Config,
    problem: StoredProblem,
    now: datetime,
) -> None:
    name = problem.communication_name
    if name is None:
        if not _may_send(problem, now):
            return
        name = _send(connection, erpnext, config, problem, now)
    status = erpnext.get_doc("Communication", name).get("delivery_status")
    if status in DELIVERED:
        record_delivered(connection, problem)
    elif status in GIVEN_UP:
        forget_communication(connection, problem)


def _may_send(problem: StoredProblem, now: datetime) -> bool:
    if problem.email_attempts >= MAX_EMAIL_ATTEMPTS:
        return False
    last = problem.last_email_attempt_at
    # Stored timestamps are fixed-width UTC, so they order as strings.
    return last is None or last <= utc_timestamp(now - EMAIL_RETRY_INTERVAL)


def _send(
    connection: sqlite3.Connection,
    erpnext: ErpNextClient,
    config: Config,
    problem: StoredProblem,
    now: datetime,
) -> str:
    # Counted before the call, so a send that fails or never returns uses up
    # an attempt too.
    record_send_attempt(connection, problem, now)
    data: dict[str, object] = {
        "recipients": config.email.recipient,
        "sender": config.email.sender,
        "subject": render_subject(problem, config.environment),
        "content": render_body(problem, config.environment),
        "communication_medium": "Email",
        "sent_or_received": "Sent",
        "send_email": 1,
        # Send within the request, as verified on staging, so the status is
        # usually final by the time `make` answers.
        "now": 1,
    }
    message = erpnext.call_method(MAKE_METHOD, data)
    name = message.get("name") if isinstance(message, dict) else None
    if not isinstance(name, str) or not name:
        raise ErpNextError(f"{MAKE_METHOD}: no Communication name in the response")
    record_communication(connection, problem, name)
    return name
