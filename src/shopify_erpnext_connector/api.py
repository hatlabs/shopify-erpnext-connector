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

Mutations are POST requests with a JSON object body. Each runs in one store
transaction with its audit row; an answer of 400 or above rolls the
transaction back, so a refused mutation leaves nothing behind. A mutation of
what a pass works on first takes the worker's writer lock, waiting at most
`writer_lock_timeout` for a running pass. Dry-run does not: a pass reads it
once at its start, so a change takes effect from the next pass and can be
made while ERPNext or Shopify is slow.

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
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import BaseServer
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit

from shopify_erpnext_connector.bindings import (
    accept_sku_change,
    bound_item_code,
    load_sku_change,
    load_sku_changes,
)
from shopify_erpnext_connector.config import Config
from shopify_erpnext_connector.decisions import (
    comparison_report,
    load_dated_decision,
)
from shopify_erpnext_connector.problems import (
    ProblemKind,
    StoredProblem,
    count_open_problems,
    count_unsent_emails,
    get_problem,
    open_problems,
    parse_problem_key,
    problem_key,
)
from shopify_erpnext_connector.push import recent_pushes, unpushed_since
from shopify_erpnext_connector.retry import READ_TIMEOUT_SECONDS
from shopify_erpnext_connector.store import dry_run, set_dry_run, utc_timestamp
from shopify_erpnext_connector.worker import (
    BINDING_PROBLEM_KINDS,
    NO_SKU,
    SKU_DETAIL,
)

ENVIRONMENT_HEADER = "X-Connector-Environment"
JSON_CONTENT_TYPE = "application/json"
METRICS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
# No completed pass for this long, counted from start before the first one,
# reports the pass as stale.
PASS_STALE_AFTER = timedelta(minutes=5)

# The newest push attempts `record` shows.
RECORD_PUSH_HISTORY = 10

# How often the serving loop checks for `close`, so stopping takes at most this.
SHUTDOWN_POLL_SECONDS = 0.1
# A client that sends nothing for this long is disconnected, so a stalled
# connection cannot hold a handler thread for good.
REQUEST_TIMEOUT_SECONDS = 30.0
# How long a mutation waits for a running pass to release the writer lock: a
# pass holds it across upstream calls, so at least as long as one call can
# take, with room for the connect and the pass's own work.
WRITER_LOCK_MARGIN_SECONDS = 30.0
WRITER_LOCK_TIMEOUT_SECONDS = READ_TIMEOUT_SECONDS + WRITER_LOCK_MARGIN_SECONDS
# Request bodies hold a few identifiers; anything larger is refused unread.
MAX_BODY_BYTES = 64 * 1024

# Audit log action names; stable, so the log can be searched by them.
DRY_RUN_ACTION = "dry-run"
ACCEPT_SKU_CHANGE_ACTION = "accept-sku-change"

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

    @property
    def writer_lock(self) -> threading.Lock: ...

    def request_full_pass(self) -> None: ...


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
    query: Mapping[str, str]
    body: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class _Route:
    method: str
    access: _Access
    handle: Callable[["ApiServer", _Request], _Response]
    # Whether a mutation waits for the worker's writer lock.
    writer_lock: bool = True


class ApiServer:
    """The API on `config.api.port` of every interface, run by `start`."""

    def __init__(
        self,
        config: Config,
        worker: WorkerState,
        *,
        clock: Callable[[], datetime] = _utc_now,
        request_timeout: float = REQUEST_TIMEOUT_SECONDS,
        writer_lock_timeout: float = WRITER_LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self.request_timeout = request_timeout
        self.writer_lock_timeout = writer_lock_timeout
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

    def mutate(self, route: _Route, request: _Request) -> _Response:
        """Run a mutation in one transaction, under the writer lock if it takes it."""
        if not route.writer_lock:
            return self._transact(route, request)
        lock = self._worker.writer_lock
        if not lock.acquire(timeout=self.writer_lock_timeout):
            return self.error(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "a pass is running; retry the command in a moment",
            )
        try:
            return self._transact(route, request)
        finally:
            lock.release()

    def _transact(self, route: _Route, request: _Request) -> _Response:
        connection = request.connection
        # IMMEDIATE: what the handler reads cannot change before it writes.
        connection.execute("BEGIN IMMEDIATE")
        try:
            response = route.handle(self, request)
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        refused = response.status >= HTTPStatus.BAD_REQUEST
        connection.execute("ROLLBACK" if refused else "COMMIT")
        return response

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

    def problems(self, request: _Request) -> _Response:
        problems = open_problems(request.connection)
        return self.json(
            HTTPStatus.OK, {"problems": [_problem_json(p) for p in problems]}
        )

    def problem(self, request: _Request) -> _Response:
        key = request.query.get("key")
        if key is None:
            return self.error(HTTPStatus.BAD_REQUEST, "the key parameter is required")
        parsed = parse_problem_key(key)
        stored = None if parsed is None else get_problem(request.connection, *parsed)
        if stored is None:
            return self.error(HTTPStatus.NOT_FOUND, f"no problem {key}")
        return self.json(HTTPStatus.OK, {"problem": _problem_json(stored)})

    def bindings(self, request: _Request) -> _Response:
        problems = open_problems(request.connection)

        def of_kind(kind: ProblemKind) -> list[dict[str, object]]:
            return [_problem_json(p) for p in problems if p.kind is kind]

        new_skus = {
            p.subject: p.details.get(SKU_DETAIL)
            for p in problems
            if p.kind is ProblemKind.CHANGED_SKU
        }
        return self.json(
            HTTPStatus.OK,
            {
                "unbound": of_kind(ProblemKind.UNBOUND_SKU),
                "ambiguous": of_kind(ProblemKind.AMBIGUOUS_SKU),
                "sku_changes": [
                    {
                        "variant_id": change.variant_id,
                        "bound_item_code": change.bound_item_code,
                        "sku": new_skus.get(change.variant_id),
                        "accepted_sku": change.accepted_sku,
                        "detected_at": change.detected_at,
                    }
                    for change in load_sku_changes(request.connection)
                ],
            },
        )

    def record(self, request: _Request) -> _Response:
        variant_id = request.query.get("variant")
        if variant_id is None:
            return self.error(
                HTTPStatus.BAD_REQUEST, "the variant parameter is required"
            )
        connection = request.connection
        item_code = bound_item_code(connection, variant_id)
        binding_problem = next(
            (
                problem
                for kind in BINDING_PROBLEM_KINDS
                if (problem := get_problem(connection, kind, variant_id)) is not None
                and problem.resolved_at is None
            ),
            None,
        )
        decision = load_dated_decision(connection, variant_id)
        history = recent_pushes(connection, variant_id, RECORD_PUSH_HISTORY)
        sku_change = load_sku_change(connection, variant_id)
        known = item_code or binding_problem or decision or history or sku_change
        if not known:
            return self.error(
                HTTPStatus.NOT_FOUND, f"no record of variant {variant_id}"
            )
        return self.json(
            HTTPStatus.OK,
            {
                "variant_id": variant_id,
                "bound_item_code": item_code,
                "binding_problem": (
                    None if binding_problem is None else _problem_json(binding_problem)
                ),
                "decision": (
                    None
                    if decision is None
                    else asdict(decision.decision) | {"decided_at": decision.decided_at}
                ),
                "sku_change": (
                    None
                    if sku_change is None
                    else {
                        "bound_item_code": sku_change.bound_item_code,
                        "detected_at": sku_change.detected_at,
                    }
                ),
                "push_history": [asdict(attempt) for attempt in history],
            },
        )

    def report(self, request: _Request) -> _Response:
        rows = comparison_report(request.connection)
        return self.json(HTTPStatus.OK, {"rows": [asdict(row) for row in rows]})

    def set_dry_run(self, request: _Request) -> _Response:
        on = request.body.get("on")
        if not isinstance(on, bool):
            return self.error(
                HTTPStatus.BAD_REQUEST, "the body needs 'on' as true or false"
            )
        connection = request.connection
        was = dry_run(connection)
        if on != was:
            set_dry_run(connection, on)
            self.audit_mutation(request, DRY_RUN_ACTION, {"old": was, "new": on})
        return self.json(HTTPStatus.OK, {"dry_run": on, "changed": on != was})

    def accept_sku_change(self, request: _Request) -> _Response:
        variant_id = request.body.get("variant")
        if not isinstance(variant_id, str) or not variant_id:
            return self.error(
                HTTPStatus.BAD_REQUEST, "the body needs 'variant' as a variant GID"
            )
        connection = request.connection
        change = load_sku_change(connection, variant_id)
        if change is None:
            return self.error(
                HTTPStatus.NOT_FOUND, f"variant {variant_id} has no held SKU change"
            )
        # The SKU `bindings` shows: the one the operator saw and accepts.
        problem = get_problem(connection, ProblemKind.CHANGED_SKU, variant_id)
        sku = (
            None
            if problem is None or problem.resolved_at is not None
            else problem.details.get(SKU_DETAIL)
        )
        if sku is None or sku == NO_SKU:
            return self.error(
                HTTPStatus.CONFLICT,
                f"variant {variant_id} has no new SKU recorded to accept;"
                " retry after the next full pass",
            )
        accept_sku_change(connection, variant_id, sku)
        detail = {
            "variant_id": variant_id,
            "bound_item_code": change.bound_item_code,
            "accepted_sku": sku,
        }
        self.audit_mutation(request, ACCEPT_SKU_CHANGE_ACTION, detail)
        self._worker.request_full_pass()
        return self.json(HTTPStatus.OK, detail)

    def audit_mutation(
        self, request: _Request, action: str, detail: Mapping[str, object]
    ) -> None:
        # Operator routes reach a handler only with an operator's token.
        assert request.operator is not None
        audit(
            request.connection,
            request.operator,
            action,
            json.dumps(detail),
            self._clock(),
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
    "/problems": _Route("GET", _Access.OPERATOR, ApiServer.problems),
    "/problem": _Route("GET", _Access.OPERATOR, ApiServer.problem),
    "/bindings": _Route("GET", _Access.OPERATOR, ApiServer.bindings),
    "/record": _Route("GET", _Access.OPERATOR, ApiServer.record),
    "/report": _Route("GET", _Access.OPERATOR, ApiServer.report),
    "/dry-run": _Route(
        "POST", _Access.OPERATOR, ApiServer.set_dry_run, writer_lock=False
    ),
    "/bindings/accept-sku-change": _Route(
        "POST", _Access.OPERATOR, ApiServer.accept_sku_change
    ),
}


def _problem_json(problem: StoredProblem) -> dict[str, object]:
    return {
        "key": problem_key(problem.kind, problem.subject),
        "kind": problem.kind,
        "subject": problem.subject,
        "details": dict(problem.details),
        "opened_at": problem.opened_at,
        "resolved_at": problem.resolved_at,
        "email": {
            "pending": problem.email_pending,
            "attempts": problem.email_attempts,
            "last_attempt_at": problem.last_email_attempt_at,
            "communication": problem.communication_name,
        },
    }


def _json_object(content: bytes) -> Mapping[str, object] | str:
    """The body as a JSON object, or why it is not one."""
    try:
        document: object = json.loads(content) if content else None
    except ValueError:
        return "the body is not valid JSON"
    if not isinstance(document, dict):
        return "the body must be a JSON object"
    return {str(key): value for key, value in document.items()}


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
        # Read before answering anything: closing a connection with unread
        # data resets it, and the client could lose the answer.
        content = self._read_body()
        if isinstance(content, _Response):
            return content
        url = urlsplit(self.path)
        path = url.path
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
        body: Mapping[str, object] = {}
        if route.method != "GET":
            parsed = _json_object(content)
            if isinstance(parsed, str):
                return api.error(HTTPStatus.BAD_REQUEST, parsed)
            body = parsed
        try:
            with closing(api.connect()) as connection:
                query = dict(parse_qsl(url.query))
                request = _Request(connection, self._operator, query, body)
                if route.method != "GET":
                    return api.mutate(route, request)
                # One read transaction, so every statement of the handler sees
                # the same WAL snapshot even while the worker commits a pass.
                connection.execute("BEGIN")
                try:
                    return route.handle(api, request)
                finally:
                    connection.rollback()
        except Exception:
            # The operator gets a JSON answer; the details go to the log only.
            _log.exception("%s %s failed", self.command, path)
            return api.error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "the request failed; the connector's log has the details",
            )

    def _read_body(self) -> bytes | _Response:
        api = self._api
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length < 0:
            return api.error(HTTPStatus.BAD_REQUEST, "bad Content-Length")
        if length > MAX_BODY_BYTES:
            return api.error(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                f"the body is over {MAX_BODY_BYTES} bytes",
            )
        return self.rfile.read(length)

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
