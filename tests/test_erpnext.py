import json
import logging
from collections.abc import Callable

import httpx
import pytest
from helpers import ERPNEXT_API_KEY, ERPNEXT_API_SECRET, ERPNEXT_URL, make_config

from shopify_erpnext_connector.erpnext import (
    ErpNextClient,
    ErpNextError,
    ErpNextHTTPError,
    ErpNextNotFoundError,
    ErpNextPermissionError,
    ErpNextTransportError,
)
from shopify_erpnext_connector.pinning import PinningError
from shopify_erpnext_connector.retry import MAX_ATTEMPTS

# Recorded Bin rows, scrubbed: synthetic item codes and warehouse, quantities only.
BIN_ROWS = [
    {
        "name": "bin0001",
        "item_code": "TEST-ITEM-001",
        "warehouse": "Stores - EX",
        "actual_qty": 33.0,
        "reserved_qty": 2.0,
        "reserved_qty_for_production": 0.0,
        "planned_qty": 0.0,
        "modified": "2026-11-11 17:29:42.431978",
    },
    {
        "name": "bin0002",
        "item_code": "TEST-ITEM-002",
        "warehouse": "Stores - EX",
        "actual_qty": 5.0,
        "reserved_qty": 0.0,
        "reserved_qty_for_production": 1.0,
        "planned_qty": 2.0,
        "modified": "2026-11-11 17:32:22.004512",
    },
]

# Frappe's shape for a permission failure: no data, an exception type and messages.
FORBIDDEN_BODY = {
    "exc_type": "PermissionError",
    "_server_messages": '["{\\"message\\": \\"No permission for Email Queue\\"}"]',
}
SERVER_ERROR_BODY = {"exc_type": "Exception", "exception": "synthetic failure detail"}

Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    def __init__(self, *responses: httpx.Response) -> None:
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.responses.pop(0)


class Sleeps:
    def __init__(self) -> None:
        self.calls: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


def make_client(
    handler: Handler, *, base_url: str = ERPNEXT_URL, sleep: Sleeps | None = None
) -> ErpNextClient:
    http = httpx.Client(base_url=base_url, transport=httpx.MockTransport(handler))
    return ErpNextClient(make_config(), http, sleep=sleep or Sleeps())


def test_get_list_sends_filters_fields_order_and_limit() -> None:
    recorder = Recorder(httpx.Response(200, json={"data": BIN_ROWS}))
    client = make_client(recorder)

    rows = client.get_list(
        "Bin",
        fields=["name", "item_code", "actual_qty", "modified"],
        filters=[
            ("warehouse", "=", "Stores - EX"),
            ("modified", ">", "2026-11-11 17:29:40.000000"),
        ],
        order_by="modified asc",
        limit=500,
    )

    assert rows == BIN_ROWS
    (request,) = recorder.requests
    assert request.method == "GET"
    assert request.url.host == "erpnext.example.com"
    assert request.url.path == "/api/resource/Bin"
    params = request.url.params
    assert json.loads(params["fields"]) == [
        "name",
        "item_code",
        "actual_qty",
        "modified",
    ]
    assert json.loads(params["filters"]) == [
        ["warehouse", "=", "Stores - EX"],
        ["modified", ">", "2026-11-11 17:29:40.000000"],
    ]
    assert params["order_by"] == "modified asc"
    assert params["limit_page_length"] == "500"


def test_get_list_without_limit_asks_for_every_row() -> None:
    recorder = Recorder(httpx.Response(200, json={"data": []}))
    client = make_client(recorder)

    assert client.get_list("Bin", fields=["name"]) == []
    # Frappe pages at 20 rows unless told otherwise; 0 means no limit.
    assert recorder.requests[0].url.params["limit_page_length"] == "0"


def test_requests_carry_token_auth() -> None:
    recorder = Recorder(httpx.Response(200, json={"data": []}))
    make_client(recorder).get_list("Bin", fields=["name"])

    expected = f"token {ERPNEXT_API_KEY}:{ERPNEXT_API_SECRET}"
    assert recorder.requests[0].headers.get("Authorization") == expected


def test_get_doc_quotes_the_doctype_and_name() -> None:
    doc = {"name": "TEST/ITEM 1", "item_code": "TEST/ITEM 1", "disabled": 0}
    recorder = Recorder(httpx.Response(200, json={"data": doc}))

    assert make_client(recorder).get_doc("Sales Order", "TEST/ITEM 1") == doc
    assert (
        recorder.requests[0].url.raw_path
        == b"/api/resource/Sales%20Order/TEST%2FITEM%201"
    )


def test_get_doc_missing_raises_not_found() -> None:
    recorder = Recorder(httpx.Response(404, json={"exc_type": "DoesNotExistError"}))

    with pytest.raises(ErpNextNotFoundError) as excinfo:
        make_client(recorder).get_doc("Communication", "missing")
    assert excinfo.value.status == 404


def test_call_method_posts_json_and_returns_message() -> None:
    recorder = Recorder(httpx.Response(200, json={"message": {"name": "COMM-0001"}}))

    result = make_client(recorder).call_method(
        "frappe.core.doctype.communication.email.make",
        {"subject": "Test problem", "send_email": 1},
    )

    assert result == {"name": "COMM-0001"}
    (request,) = recorder.requests
    assert request.method == "POST"
    assert (
        request.url.path == "/api/method/frappe.core.doctype.communication.email.make"
    )
    assert json.loads(request.content) == {"subject": "Test problem", "send_email": 1}


def test_server_error_is_retried_up_to_the_bound() -> None:
    responses = [httpx.Response(502, json=SERVER_ERROR_BODY)] * MAX_ATTEMPTS
    recorder = Recorder(*responses)
    sleeps = Sleeps()

    with pytest.raises(ErpNextHTTPError) as excinfo:
        make_client(recorder, sleep=sleeps).get_list("Bin", fields=["name"])

    assert excinfo.value.status == 502
    assert len(recorder.requests) == MAX_ATTEMPTS
    assert len(sleeps.calls) == MAX_ATTEMPTS - 1


def test_server_error_then_success_returns_the_data() -> None:
    recorder = Recorder(
        httpx.Response(503, json=SERVER_ERROR_BODY),
        httpx.Response(200, json={"data": BIN_ROWS}),
    )

    assert make_client(recorder).get_list("Bin", fields=["name"]) == BIN_ROWS
    assert len(recorder.requests) == 2


def test_network_error_is_retried_up_to_the_bound() -> None:
    attempts: list[httpx.Request] = []

    def refuse(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ErpNextTransportError):
        make_client(refuse).get_list("Bin", fields=["name"])
    assert len(attempts) == MAX_ATTEMPTS


def test_read_is_retried_after_a_read_timeout() -> None:
    responses: list[httpx.Request] = []

    def time_out_once(request: httpx.Request) -> httpx.Response:
        responses.append(request)
        if len(responses) == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(200, json={"data": BIN_ROWS})

    assert make_client(time_out_once).get_list("Bin", fields=["name"]) == BIN_ROWS
    assert len(responses) == 2


def test_forbidden_is_not_retried() -> None:
    recorder = Recorder(httpx.Response(403, json=FORBIDDEN_BODY))
    sleeps = Sleeps()

    with pytest.raises(ErpNextPermissionError) as excinfo:
        make_client(recorder, sleep=sleeps).get_list("Email Queue", fields=["name"])

    assert excinfo.value.status == 403
    assert len(recorder.requests) == 1
    assert sleeps.calls == []


def test_method_call_is_not_retried_after_a_server_error() -> None:
    # make commits its Communication before failing with 500; a resend duplicates it.
    recorder = Recorder(httpx.Response(500, json=SERVER_ERROR_BODY))

    with pytest.raises(ErpNextHTTPError):
        make_client(recorder).call_method("example.method", {})
    assert len(recorder.requests) == 1


def test_method_call_is_not_retried_after_a_read_timeout() -> None:
    attempts: list[httpx.Request] = []

    def time_out(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(ErpNextTransportError):
        make_client(time_out).call_method("example.method", {})
    assert len(attempts) == 1


def test_method_call_is_retried_when_the_connection_failed() -> None:
    attempts: list[httpx.Request] = []

    def refuse_once(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, json={"message": "ok"})

    assert make_client(refuse_once).call_method("example.method", {}) == "ok"
    assert len(attempts) == 2


def test_failed_request_with_retries_logs_no_token_header_or_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG)
    responses = [httpx.Response(500, json=SERVER_ERROR_BODY)] * MAX_ATTEMPTS
    recorder = Recorder(*responses)

    with pytest.raises(ErpNextHTTPError) as excinfo:
        make_client(recorder).get_list(
            "Bin", fields=["name"], filters=[("item_code", "=", "TEST-ITEM-001")]
        )

    logged = caplog.text + "".join(str(r.args) for r in caplog.records)
    for forbidden in (
        ERPNEXT_API_KEY,
        ERPNEXT_API_SECRET,
        "token ",
        "synthetic failure detail",
        "TEST-ITEM-001",
    ):
        assert forbidden.lower() not in logged.lower()
        assert forbidden.lower() not in str(excinfo.value).lower()
    assert "GET erpnext.example.com /api/resource/Bin 500" in caplog.text


def test_transport_error_logs_no_url_query(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    with pytest.raises(ErpNextTransportError) as excinfo:
        make_client(refuse).get_list(
            "Bin", fields=["name"], filters=[("item_code", "=", "TEST-ITEM-001")]
        )

    assert "TEST-ITEM-001" not in caplog.text
    assert "TEST-ITEM-001" not in str(excinfo.value)
    assert "ConnectError" in caplog.text


@pytest.mark.parametrize(
    "base_url",
    [
        "https://other.example.com",
        "http://erpnext.example.com",
        "https://erpnext.example.com:8443",
    ],
)
def test_request_to_unpinned_target_raises_before_network_io(base_url: str) -> None:
    recorder = Recorder()

    with pytest.raises(PinningError):
        make_client(recorder, base_url=base_url).get_list("Bin", fields=["name"])
    assert recorder.requests == []


def test_unexpected_response_shape_raises() -> None:
    recorder = Recorder(httpx.Response(200, json={"data": {"not": "a list"}}))

    with pytest.raises(ErpNextError):
        make_client(recorder).get_list("Bin", fields=["name"])


def test_non_json_response_raises() -> None:
    recorder = Recorder(httpx.Response(200, text="<html>maintenance</html>"))

    with pytest.raises(ErpNextError):
        make_client(recorder).get_doc("Item", "TEST-ITEM-001")


REDIRECT_TARGET = "https://other.example.com/api/resource/Bin"


def redirecting_client(status: int, requests: list[httpx.Request]) -> ErpNextClient:
    def redirect(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host != "erpnext.example.com":
            return httpx.Response(200, json={"data": [], "message": "ok"})
        return httpx.Response(status, headers={"Location": REDIRECT_TARGET})

    # A client configured to follow redirects must not carry the token elsewhere.
    http = httpx.Client(
        base_url=ERPNEXT_URL,
        follow_redirects=True,
        transport=httpx.MockTransport(redirect),
    )
    return ErpNextClient(make_config(), http, sleep=Sleeps())


@pytest.mark.parametrize("status", [301, 302, 307])
def test_read_redirect_raises_without_following(status: int) -> None:
    requests: list[httpx.Request] = []

    with pytest.raises(ErpNextHTTPError) as excinfo:
        redirecting_client(status, requests).get_list("Bin", fields=["name"])

    assert excinfo.value.status == status
    assert [r.url.host for r in requests] == ["erpnext.example.com"]


@pytest.mark.parametrize("status", [301, 302, 307])
def test_method_call_redirect_raises_without_following(status: int) -> None:
    requests: list[httpx.Request] = []

    with pytest.raises(ErpNextHTTPError) as excinfo:
        redirecting_client(status, requests).call_method("example.method", {})

    assert excinfo.value.status == status
    assert [r.url.host for r in requests] == ["erpnext.example.com"]
