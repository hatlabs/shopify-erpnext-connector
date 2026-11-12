"""Shopify Admin GraphQL client, pinned to one shop, API version and location.

Throttling is cost-aware: each response's throttle status decides whether the
next call waits, and a THROTTLED response waits and retries. Queries marked
idempotent are also retried on network errors and 5xx up to a bound. Mutations
are not: they raise typed errors, and the caller decides whether to resend the
same batch (see `InventoryBatch`). Logs and errors carry
method, host, path and status only: no headers or bodies. Redirects are never
followed, whatever the injected client's policy: a 3xx raises its status.
"""

import logging
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus

import httpx

from shopify_erpnext_connector.config import Config
from shopify_erpnext_connector.pinning import PinningError, describe, require_pinned
from shopify_erpnext_connector.retry import MAX_ATTEMPTS, backoff_seconds

API_VERSION = "2026-10"
GRAPHQL_PATH = f"/admin/api/{API_VERSION}/graphql.json"
BATCH_LIMIT = 250
MAX_THROTTLE_RETRIES = 5
# Used when a THROTTLED response carries no throttle status to compute a wait.
THROTTLE_FALLBACK_SECONDS = 1.0

THROTTLED = "THROTTLED"
CHANGE_FROM_QUANTITY_STALE = "CHANGE_FROM_QUANTITY_STALE"

_log = logging.getLogger(__name__)

_SET_QUANTITIES = """\
mutation SetAvailable(
  $input: InventorySetQuantitiesInput!
  $idempotencyKey: String!
) {
  inventorySetQuantities(input: $input) @idempotent(key: $idempotencyKey) {
    inventoryAdjustmentGroup { id }
    userErrors { code field message }
  }
}
"""
# The user error `field` path of an entry: ["input", "quantities", "<index>", ...].
_QUANTITIES_FIELD = ("input", "quantities")


@dataclass(frozen=True)
class QuantityChange:
    """Set `available` of one inventory item at one location.

    `change_from_quantity` is the quantity last read; Shopify rejects the
    change as stale if the level moved since. None sets it unconditionally.
    """

    inventory_item_id: str
    location_id: str
    quantity: int
    change_from_quantity: int | None


@dataclass(frozen=True)
class InventoryBatch:
    """One `inventorySetQuantities` mutation.

    The key is held in memory only, never persisted. Resend a batch with its
    key only to retry the same request after a transport error, in the same
    process; any changed request needs a new batch and a new key. After a
    restart, rebuild the batch with a new key: an entry carrying
    `change_from_quantity` makes a replay of the lost batch fail as stale
    rather than apply twice.
    """

    changes: tuple[QuantityChange, ...]
    idempotency_key: str


def new_idempotency_key() -> str:
    return str(uuid.uuid4())


def batch_changes(changes: Sequence[QuantityChange]) -> list[InventoryBatch]:
    """Split changes into batches of at most `BATCH_LIMIT`, each with a new key."""
    return [
        InventoryBatch(tuple(changes[i : i + BATCH_LIMIT]), new_idempotency_key())
        for i in range(0, len(changes), BATCH_LIMIT)
    ]


@dataclass(frozen=True)
class UserError:
    code: str | None
    field: tuple[str, ...]
    message: str


class ShopifyError(Exception):
    """A Shopify request failed or returned something unexpected."""


class ShopifyTransportError(ShopifyError):
    """The request failed on the network; it may or may not have been applied."""


class ShopifyHTTPError(ShopifyError):
    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class ShopifyGraphQLError(ShopifyError):
    """Top-level GraphQL errors, such as ACCESS_DENIED or persistent THROTTLED."""

    def __init__(self, message: str, codes: tuple[str, ...]) -> None:
        super().__init__(message)
        self.codes = codes


class ShopifyUserError(ShopifyError):
    """A mutation's userErrors. Shopify applied none of the batch's entries."""

    def __init__(
        self,
        message: str,
        user_errors: tuple[UserError, ...],
        failed_changes: tuple[QuantityChange, ...],
    ) -> None:
        super().__init__(message)
        self.user_errors = user_errors
        self.codes = tuple(e.code or "" for e in user_errors)
        self.failed_changes = failed_changes


class ChangeFromQuantityStaleError(ShopifyUserError):
    """Every failed entry's level moved since it was read."""


@dataclass(frozen=True)
class _Throttle:
    requested: float
    available: float
    restore_rate: float

    def wait_seconds(self) -> float:
        return max(0.0, self.requested - self.available) / self.restore_rate


class ShopifyClient:
    def __init__(
        self,
        config: Config,
        http: httpx.Client,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._pinned = httpx.URL(f"https://{config.shop_domain}")
        self._location_id = config.location_id
        self._token = config.secrets.shopify_access_token
        self._http = http
        self._sleep = sleep
        self._throttle: _Throttle | None = None

    def execute(
        self,
        query: str,
        variables: Mapping[str, object] | None = None,
        *,
        idempotent: bool = False,
    ) -> dict[str, object]:
        """Run a query or mutation and return its `data`.

        Pass `idempotent=True` for a query to retry it on network errors and
        5xx. Never for a mutation: it may have been applied before failing.
        """
        request = self._http.build_request(
            "POST",
            GRAPHQL_PATH,
            json={"query": query, "variables": dict(variables or {})},
            headers={"X-Shopify-Access-Token": self._token.reveal()},
        )
        require_pinned(request, self._pinned)
        where = describe(request)

        for attempt in range(MAX_THROTTLE_RETRIES + 1):
            self._wait_for_budget()
            body = self._send(request, where, idempotent=idempotent)
            self._throttle = _throttle_status(body)
            codes = _error_codes(body)
            if codes is None:
                data = body.get("data")
                if not isinstance(data, dict):
                    raise ShopifyError(f"{where}: unexpected response")
                return data
            if codes != (THROTTLED,) or attempt == MAX_THROTTLE_RETRIES:
                raise ShopifyGraphQLError(f"{where}: {', '.join(codes)}", codes)
            delay = (
                self._throttle.wait_seconds()
                if self._throttle
                else THROTTLE_FALLBACK_SECONDS
            )
            _log.warning("%s throttled, retrying in %.1f s", where, delay)
            self._throttle = None
            self._sleep(delay)
        raise AssertionError("unreachable: the last attempt returns or raises")

    def set_quantities(self, batch: InventoryBatch) -> None:
        if not 0 < len(batch.changes) <= BATCH_LIMIT:
            raise ValueError(f"a batch holds 1 to {BATCH_LIMIT} changes")
        if any(c.location_id != self._location_id for c in batch.changes):
            raise PinningError(
                f"refusing an inventory mutation outside the pinned location "
                f"{self._location_id}"
            )
        data = self.execute(
            _SET_QUANTITIES,
            {
                "input": {
                    "name": "available",
                    "reason": "correction",
                    "quantities": [
                        {
                            "inventoryItemId": c.inventory_item_id,
                            "locationId": c.location_id,
                            "quantity": c.quantity,
                            "changeFromQuantity": c.change_from_quantity,
                        }
                        for c in batch.changes
                    ],
                },
                "idempotencyKey": batch.idempotency_key,
            },
        )
        result = data.get("inventorySetQuantities")
        if not isinstance(result, dict):
            raise ShopifyError("inventorySetQuantities: unexpected response")
        user_errors = _user_errors(result.get("userErrors"))
        if user_errors:
            raise _user_error(user_errors, batch.changes)

    def _wait_for_budget(self) -> None:
        if self._throttle is None:
            return
        delay = self._throttle.wait_seconds()
        self._throttle = None
        if delay > 0:
            _log.info("Shopify query budget low, waiting %.1f s", delay)
            self._sleep(delay)

    def _send(
        self, request: httpx.Request, where: str, *, idempotent: bool
    ) -> Mapping[str, object]:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            retry = idempotent and attempt < MAX_ATTEMPTS
            try:
                response = self._http.send(request, follow_redirects=False)
            except httpx.TransportError as error:
                name = type(error).__name__
                _log.warning(
                    "%s failed: %s%s", where, name, ", retrying" if retry else ""
                )
                if not retry:
                    raise ShopifyTransportError(f"{where} failed: {name}") from None
            else:
                status = response.status_code
                if status == HTTPStatus.OK:
                    _log.debug("%s %d", where, status)
                    return _json_object(response, where)
                retry = retry and status >= HTTPStatus.INTERNAL_SERVER_ERROR
                _log.warning("%s %d%s", where, status, ", retrying" if retry else "")
                if not retry:
                    raise ShopifyHTTPError(f"{where} {status}", status)
            self._sleep(backoff_seconds(attempt))
        raise AssertionError("unreachable: the last attempt returns or raises")


def _json_object(response: httpx.Response, where: str) -> Mapping[str, object]:
    try:
        body: object = response.json()
    except ValueError:
        raise ShopifyError(f"{where}: unexpected response") from None
    if not isinstance(body, dict):
        raise ShopifyError(f"{where}: unexpected response")
    return body


def _error_codes(body: Mapping[str, object]) -> tuple[str, ...] | None:
    errors = body.get("errors")
    if not errors:
        return None
    if not isinstance(errors, list):
        return ("",)
    codes: list[str] = []
    for error in errors:
        extensions = error.get("extensions") if isinstance(error, dict) else None
        code = extensions.get("code") if isinstance(extensions, dict) else None
        codes.append(code if isinstance(code, str) else "")
    return tuple(codes)


def _throttle_status(body: Mapping[str, object]) -> _Throttle | None:
    extensions = body.get("extensions")
    cost = extensions.get("cost") if isinstance(extensions, dict) else None
    if not isinstance(cost, dict):
        return None
    status = cost.get("throttleStatus")
    requested = cost.get("requestedQueryCost")
    if not isinstance(status, dict) or not isinstance(requested, int | float):
        return None
    available = status.get("currentlyAvailable")
    restore_rate = status.get("restoreRate")
    if not isinstance(available, int | float) or not isinstance(
        restore_rate, int | float
    ):
        return None
    if restore_rate <= 0:
        return None
    return _Throttle(float(requested), float(available), float(restore_rate))


def _user_errors(value: object) -> tuple[UserError, ...]:
    if not isinstance(value, list):
        raise ShopifyError("inventorySetQuantities: unexpected userErrors")
    errors: list[UserError] = []
    for item in value:
        if not isinstance(item, dict):
            raise ShopifyError("inventorySetQuantities: unexpected userErrors")
        code = item.get("code")
        field = item.get("field")
        message = item.get("message")
        errors.append(
            UserError(
                code=code if isinstance(code, str) else None,
                field=tuple(str(f) for f in field) if isinstance(field, list) else (),
                message=message if isinstance(message, str) else "",
            )
        )
    return tuple(errors)


def _user_error(
    user_errors: tuple[UserError, ...], changes: tuple[QuantityChange, ...]
) -> ShopifyUserError:
    indices = sorted(
        {i for e in user_errors if (i := _entry_index(e, changes)) is not None}
    )
    failed = tuple(changes[i] for i in indices)
    codes = ", ".join(e.code or "?" for e in user_errors)
    message = f"inventorySetQuantities: {codes} at entries {indices}"
    if all(e.code == CHANGE_FROM_QUANTITY_STALE for e in user_errors):
        return ChangeFromQuantityStaleError(message, user_errors, failed)
    return ShopifyUserError(message, user_errors, failed)


def _entry_index(error: UserError, changes: tuple[QuantityChange, ...]) -> int | None:
    prefix_length = len(_QUANTITIES_FIELD)
    if error.field[:prefix_length] != _QUANTITIES_FIELD:
        return None
    if len(error.field) <= prefix_length or not error.field[prefix_length].isdigit():
        return None
    index = int(error.field[prefix_length])
    return index if index < len(changes) else None
