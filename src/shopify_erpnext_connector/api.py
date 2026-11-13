"""The operator API and metrics, served over HTTP from a thread of the connector.

Every request carries a bearer token. Operator endpoints take only the
operator tokens and name the operator by the token that matched; operator
requests must also name the environment they expect in `ENVIRONMENT_HEADER`,
so a token copied into the wrong setup cannot act on this one. Every JSON
response names the connector's environment. `/metrics` takes only the
metrics token and answers in the Prometheus text format, for vmagent.

Each request opens its own store connection: sqlite3 connections are bound
to the thread that made them, and the store's WAL mode lets them read while
the worker writes. Worker state is read through the worker's public
properties only.

Request logs go through `logging` and never include headers, so a token is
never logged.
"""

import hmac
import json
import logging
import socket
import sqlite3
import threading
from collections.abc import Callable, Mapping
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import BaseServer
from typing import Protocol
from urllib.parse import urlsplit

from shopify_erpnext_connector.config import Config
from shopify_erpnext_connector.problems import count_open_problems, count_unsent_emails
from shopify_erpnext_connector.push import unpushed_since
from shopify_erpnext_connector.store import dry_run, utc_timestamp

ENVIRONMENT_HEADER = "X-Connector-Environment"
JSON_CONTENT_TYPE = "application/json"
METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
# No completed pass for this long, counted from start before the first one,
# reports the pass as stale.
PASS_STALE_AFTER = timedelta(minutes=5)

# How often the serving loop checks for `close`, so stopping takes at most this.
SHUTDOWN_POLL_SECONDS = 0.1
# A client that sends nothing for this long is disconnected, so a stalled
# connection cannot hold a handler thread for good.
REQUEST_TIMEOUT_SECONDS = 30.0

_BEARER_SCHEME = "bearer"

_log = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class WorkerState(Protocol):
    @property
    def last_pass_at(self) -> datetime | None: ...

    @property
    def last_pass_full(self) -> bool: ...

    @property
    def last_full_pass_at(self) -> datetime | None: ...


def audit(
    connection: sqlite3.Connection,
    operator: str,
    action: str,
    detail: str | None,
    now: datetime,
) -> None:
    """Record an operator's mutation in the audit log."""
    connection.execute(
        "INSERT INTO audit_log (recorded_at, operator, action, detail)"
        " VALUES (?, ?, ?, ?)",
        (utc_timestamp(now), operator, action, detail),
    )


class _Access(Enum):
    OPERATOR = "operator"
    METRICS = "metrics"


@dataclass(frozen=True)
class _Gauge:
    """A gauge's values by label set, such as `{kind="oversell"}`; "" for none."""

    name: str
    help: str
    samples: Mapping[str, float]

    def render(self) -> str:
        lines = [f"# HELP {self.name} {self.help}", f"# TYPE {self.name} gauge"]
        for labels, value in self.samples.items():
            lines.append(f"{self.name}{labels} {value}")
        return "\n".join(lines)


@dataclass(frozen=True)
class _Response:
    status: HTTPStatus
    body: bytes
    content_type: str = JSON_CONTENT_TYPE
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class _Request:
    connection: sqlite3.Connection
    operator: str | None


@dataclass(frozen=True)
class _Route:
    method: str
    access: _Access
    handle: Callable[["ApiServer", _Request], _Response]


class ApiServer:
    """The API on `config.api.port` of every interface, run by `start`."""

    def __init__(
        self,
        config: Config,
        worker: WorkerState,
        *,
        clock: Callable[[], datetime] = _utc_now,
        request_timeout: float = REQUEST_TIMEOUT_SECONDS,
    ) -> None:
        self.request_timeout = request_timeout
        self._config = config
        self._worker = worker
        self._clock = clock
        self._started_at = clock()
        self._http = ThreadingHTTPServer(("", config.api.port), partial(_Handler, self))
        self._thread = threading.Thread(
            target=self._http.serve_forever,
            args=(SHUTDOWN_POLL_SECONDS,),
            name="api",
            daemon=True,
        )

    @property
    def port(self) -> int:
        return self._http.server_port

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        if self._thread.is_alive():
            self._http.shutdown()
            self._thread.join()
        self._http.server_close()

    def json(self, status: HTTPStatus, body: Mapping[str, object]) -> _Response:
        document = {"environment": self._config.environment} | dict(body)
        return _Response(status, json.dumps(document).encode())

    def error(self, status: HTTPStatus, message: str) -> _Response:
        return self.json(status, {"error": message})

    def operator(self, authorization: str | None) -> str | None:
        """The name of the operator whose token was presented, if any."""
        presented = _bearer_token(authorization)
        if presented is None:
            return None
        matched = None
        # Every token is compared, so the time taken does not tell which matched.
        for name, token in self._config.secrets.operator_tokens.items():
            if hmac.compare_digest(presented, token.reveal().encode()):
                matched = name
        return matched

    def is_metrics_token(self, authorization: str | None) -> bool:
        presented = _bearer_token(authorization)
        return presented is not None and hmac.compare_digest(
            presented, self._config.secrets.metrics_token.reveal().encode()
        )

    def wrong_environment(self, requested: str | None) -> str | None:
        """Why a request for `requested` is refused, or None if it is not."""
        serving = self._config.environment
        if requested is None:
            return (
                f"the request names no environment in {ENVIRONMENT_HEADER}; "
                f"this connector serves {serving}"
            )
        if requested != serving:
            return f"this connector serves {serving}, not {requested}"
        return None

    def connect(self) -> sqlite3.Connection:
        # mode=rw: a missing store is an error, not a new empty database.
        uri = self._config.store_path.resolve().as_uri() + "?mode=rw"
        return sqlite3.connect(uri, uri=True, isolation_level=None)

    def status(self, request: _Request) -> _Response:
        connection = request.connection
        last_pass_at = self._worker.last_pass_at
        last_full_pass_at = self._worker.last_full_pass_at
        return self.json(
            HTTPStatus.OK,
            {
                "dry_run": dry_run(connection),
                "last_pass_at": (
                    None if last_pass_at is None else utc_timestamp(last_pass_at)
                ),
                "last_pass_full": self._worker.last_pass_full,
                "last_full_pass_at": (
                    None
                    if last_full_pass_at is None
                    else utc_timestamp(last_full_pass_at)
                ),
                "push_lag_seconds": self._push_lag(connection),
                "open_problems": sum(count_open_problems(connection).values()),
                "unsent_emails": count_unsent_emails(connection),
            },
        )

    def metrics(self, request: _Request) -> _Response:
        connection = request.connection
        now = self._clock()
        last_pass_at = self._worker.last_pass_at
        last_full_pass_at = self._worker.last_full_pass_at
        stale_since = self._started_at if last_pass_at is None else last_pass_at
        gauges = [
            _Gauge(
                "connector_info",
                "Always 1; the environment label names the connector's environment.",
                {f'{{environment="{self._config.environment}"}}': 1},
            ),
            _Gauge(
                "connector_last_pass_timestamp_seconds",
                "Start of the last completed pass, in Unix time; 0 before the first.",
                {"": 0 if last_pass_at is None else last_pass_at.timestamp()},
            ),
            _Gauge(
                "connector_last_pass_full",
                "1 if the last completed pass was a full pass, else 0.",
                {"": int(self._worker.last_pass_full)},
            ),
            _Gauge(
                "connector_last_full_pass_timestamp_seconds",
                "Start of the last completed full pass, in Unix time; "
                "0 before the first.",
                {"": 0 if last_full_pass_at is None else last_full_pass_at.timestamp()},
            ),
            _Gauge(
                "connector_pass_stale",
                "1 if no pass completed in the last "
                f"{PASS_STALE_AFTER.total_seconds():g} seconds, counted from "
                "start before the first pass, else 0.",
                {"": int(now - stale_since >= PASS_STALE_AFTER)},
            ),
            _Gauge(
                "connector_push_lag_seconds",
                "Age of the oldest value waiting to be pushed to Shopify; "
                "0 if none waits.",
                {"": self._push_lag(connection)},
            ),
            _Gauge(
                "connector_open_problems",
                "Open problems by kind.",
                {
                    f'{{kind="{kind}"}}': count
                    for kind, count in count_open_problems(connection).items()
                },
            ),
            _Gauge(
                "connector_unsent_emails",
                "Open problems whose email has not been confirmed sent.",
                {"": count_unsent_emails(connection)},
            ),
            _Gauge(
                "connector_dry_run",
                "1 if the connector pushes nothing to Shopify, else 0.",
                {"": int(dry_run(connection))},
            ),
        ]
        text = "\n".join(gauge.render() for gauge in gauges) + "\n"
        return _Response(HTTPStatus.OK, text.encode(), METRICS_CONTENT_TYPE)

    def _push_lag(self, connection: sqlite3.Connection) -> float:
        """Seconds the oldest value not yet pushed has waited; 0 if none waits."""
        since = unpushed_since(connection)
        if not since:
            return 0
        oldest = datetime.fromisoformat(min(since.values()))
        return max(0.0, (self._clock() - oldest).total_seconds())


_ROUTES: Mapping[str, _Route] = {
    "/status": _Route("GET", _Access.OPERATOR, ApiServer.status),
    "/metrics": _Route("GET", _Access.METRICS, ApiServer.metrics),
}


def _bearer_token(authorization: str | None) -> bytes | None:
    if authorization is None:
        return None
    scheme, _, token = authorization.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != _BEARER_SCHEME or not token:
        return None
    return token.encode()


class _Handler(BaseHTTPRequestHandler):
    def __init__(
        self,
        api: ApiServer,
        request: socket.socket,
        client_address: tuple[str, int],
        server: BaseServer,
    ) -> None:
        self._api = api
        self._operator: str | None = None
        super().__init__(request, client_address, server)

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self._api.request_timeout)

    def do_GET(self) -> None:
        self._send(self._respond())

    do_POST = do_PUT = do_PATCH = do_DELETE = do_GET

    def _respond(self) -> _Response:
        api = self._api
        path = urlsplit(self.path).path
        route = _ROUTES.get(path)
        if route is None:
            return api.error(HTTPStatus.NOT_FOUND, f"no such endpoint: {path}")
        if self.command != route.method:
            response = api.error(
                HTTPStatus.METHOD_NOT_ALLOWED,
                f"{path} takes {route.method}, not {self.command}",
            )
            return _Response(
                response.status, response.body, headers={"Allow": route.method}
            )
        authorization = self.headers.get("Authorization")
        if route.access is _Access.METRICS:
            allowed = api.is_metrics_token(authorization)
        else:
            self._operator = api.operator(authorization)
            allowed = self._operator is not None
        if not allowed:
            _log.warning(
                "rejected %s %s from %s: no valid %s token",
                self.command,
                path,
                self.client_address[0],
                route.access.value,
            )
            return api.error(
                HTTPStatus.UNAUTHORIZED,
                f"a valid {route.access.value} bearer token is required",
            )
        if route.access is _Access.OPERATOR:
            refusal = api.wrong_environment(self.headers.get(ENVIRONMENT_HEADER))
            if refusal is not None:
                return api.error(HTTPStatus.CONFLICT, refusal)
        try:
            with closing(api.connect()) as connection:
                return route.handle(api, _Request(connection, self._operator))
        except Exception:
            # The operator gets a JSON answer; the details go to the log only.
            _log.exception("%s %s failed", self.command, path)
            return api.error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "the request failed; the connector's log has the details",
            )

    def _send(self, response: _Response) -> None:
        self.send_response(response.status)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Content-Length", str(len(response.body)))
        for name, value in response.headers.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(response.body)

    def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
        _log.info(
            # The query string is left out: a client may put a token there.
            "%s %s %s %s %s",
            self.client_address[0],
            self._operator or "-",
            self.command,
            urlsplit(self.path).path,
            code,
        )

    def log_message(self, format: str, *args: object) -> None:
        _log.info("%s %s", self.client_address[0], format % args)
