"""ERPNext REST client: resource lists, single documents and whitelisted methods.

Reads are retried on network errors and 5xx up to a bound. Method calls write,
and ERPNext can commit a write before answering 500, so they are retried only
when the connection was never made. Logs and errors carry method, host, path
and status only: no headers, query strings or bodies. Redirects are never
followed, whatever the injected client's policy: a 3xx raises its status.
"""

import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from http import HTTPStatus
from urllib.parse import quote

import httpx

from shopify_erpnext_connector.config import Config
from shopify_erpnext_connector.pinning import describe, require_pinned
from shopify_erpnext_connector.retry import MAX_ATTEMPTS, backoff_seconds

UNLIMITED = 0  # Frappe's limit_page_length for "every row"; its default is 20

_log = logging.getLogger(__name__)

FilterValue = str | int | float | list[str]
Filter = tuple[str, str, FilterValue]
Document = dict[str, object]

# Raised before the request reached the server, so resending cannot duplicate it.
_NOT_SENT = (httpx.ConnectError, httpx.ConnectTimeout)


class ErpNextError(Exception):
    """An ERPNext request failed or returned something unexpected."""


class ErpNextTransportError(ErpNextError):
    """The request failed on the network, after any retries."""


class ErpNextHTTPError(ErpNextError):
    """ERPNext answered with an error status, after any retries."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class ErpNextPermissionError(ErpNextHTTPError):
    """403: the API user lacks permission."""


class ErpNextNotFoundError(ErpNextHTTPError):
    """404: no such document or method."""


class ErpNextClient:
    def __init__(
        self,
        config: Config,
        http: httpx.Client,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._pinned = httpx.URL(config.erpnext_url)
        self._api_key = config.secrets.erpnext_api_key
        self._api_secret = config.secrets.erpnext_api_secret
        self._http = http
        self._sleep = sleep

    def get_list(
        self,
        doctype: str,
        *,
        fields: Sequence[str],
        filters: Sequence[Filter] = (),
        order_by: str | None = None,
        limit: int = UNLIMITED,
    ) -> list[Document]:
        params = {
            "fields": json.dumps(list(fields)),
            "filters": json.dumps([list(f) for f in filters]),
            "limit_page_length": str(limit),
        }
        if order_by is not None:
            params["order_by"] = order_by
        request = self._build(
            "GET", f"/api/resource/{_segment(doctype)}", params=params
        )
        data = self._send(request, idempotent=True).get("data")
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise _unexpected(request)
        return data

    def get_doc(self, doctype: str, name: str) -> Document:
        request = self._build(
            "GET", f"/api/resource/{_segment(doctype)}/{_segment(name)}"
        )
        data = self._send(request, idempotent=True).get("data")
        if not isinstance(data, dict):
            raise _unexpected(request)
        return data

    def call_method(self, method: str, data: Mapping[str, object]) -> object:
        request = self._build(
            "POST", f"/api/method/{_segment(method)}", json=dict(data)
        )
        return self._send(request, idempotent=False).get("message")

    def _build(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        json: object = None,
    ) -> httpx.Request:
        auth = f"token {self._api_key.reveal()}:{self._api_secret.reveal()}"
        request = self._http.build_request(
            method, path, params=params, json=json, headers={"Authorization": auth}
        )
        require_pinned(request, self._pinned)
        return request

    def _send(
        self, request: httpx.Request, *, idempotent: bool
    ) -> Mapping[str, object]:
        where = describe(request)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            last = attempt == MAX_ATTEMPTS
            try:
                response = self._http.send(request, follow_redirects=False)
            except httpx.TransportError as error:
                retry = not last and (idempotent or isinstance(error, _NOT_SENT))
                _log.warning(
                    "%s failed: %s%s",
                    where,
                    type(error).__name__,
                    ", retrying" if retry else "",
                )
                if not retry:
                    raise ErpNextTransportError(
                        f"{where} failed: {type(error).__name__}"
                    ) from None
            else:
                status = response.status_code
                if status < HTTPStatus.MULTIPLE_CHOICES:
                    _log.debug("%s %d", where, status)
                    return _json_object(response, request)
                retry = (
                    not last
                    and idempotent
                    and status >= HTTPStatus.INTERNAL_SERVER_ERROR
                )
                _log.warning("%s %d%s", where, status, ", retrying" if retry else "")
                if not retry:
                    raise _status_error(where, status)
            self._sleep(backoff_seconds(attempt))
        raise AssertionError("unreachable: the last attempt returns or raises")


def _segment(value: str) -> str:
    return quote(value, safe="")


def _status_error(where: str, status: int) -> ErpNextHTTPError:
    message = f"{where} {status}"
    if status == HTTPStatus.FORBIDDEN:
        return ErpNextPermissionError(message, status)
    if status == HTTPStatus.NOT_FOUND:
        return ErpNextNotFoundError(message, status)
    return ErpNextHTTPError(message, status)


def _json_object(
    response: httpx.Response, request: httpx.Request
) -> Mapping[str, object]:
    try:
        body: object = response.json()
    except ValueError:
        raise _unexpected(request) from None
    if not isinstance(body, dict):
        raise _unexpected(request)
    return body


def _unexpected(request: httpx.Request) -> ErpNextError:
    return ErpNextError(f"{describe(request)}: unexpected response")
