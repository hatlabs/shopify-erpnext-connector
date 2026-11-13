"""The `shopify-erpnext-connector` executable.

`serve` runs the connector service; the other commands are the operator CLI,
which reads and changes a connector through its API. Each environment's URL
and token file come from `CONNECTOR_<ENV>_URL` and `CONNECTOR_<ENV>_TOKEN_FILE`;
the token itself is read only from that file, never from a variable or an
argument. `--env` selects the environment, defaulting to `CONNECTOR_ENV` and
then to staging; a command that changes production requires `--env production`
on the command line. Every answer must name the selected environment, so a URL
pointing at the wrong connector fails instead of showing its data.
"""

import argparse
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import ClassVar, TypeVar

import httpx

from shopify_erpnext_connector.api import (
    ENVIRONMENT_HEADER,
    WRITER_LOCK_TIMEOUT_SECONDS,
)
from shopify_erpnext_connector.config import (
    ConfigError,
    Environment,
    Secret,
    read_secret,
)
from shopify_erpnext_connector.problems import CLI_NAME
from shopify_erpnext_connector.service import serve

ENV_VARIABLE = "CONNECTOR_ENV"
SERVE_COMMAND = "serve"
DEFAULT_ENVIRONMENT = Environment.STAGING
REQUEST_TIMEOUT_SECONDS = 30.0
# A mutation's answer can wait for the connector's pass to release its lock.
MUTATION_TIMEOUT_SECONDS = WRITER_LOCK_TIMEOUT_SECONDS + REQUEST_TIMEOUT_SECONDS
EXIT_FAILURE = 1
# Visible ASCII: what an HTTP header value can carry unchanged.
_BEARER_TOKEN = re.compile(r"[!-~]+")

JsonObject = Mapping[str, object]
_T = TypeVar("_T")


class CliError(Exception):
    """A failure shown to the operator; its message never holds a token."""


def url_variable(environment: Environment) -> str:
    return f"CONNECTOR_{environment.upper()}_URL"


def token_file_variable(environment: Environment) -> str:
    return f"CONNECTOR_{environment.upper()}_TOKEN_FILE"


@dataclass(frozen=True)
class Target:
    """The connector a command talks to."""

    environment: Environment
    url: str
    token_file: Path
    token: Secret


def resolve_target(
    env_option: str | None, environ: Mapping[str, str], *, mutation: bool = False
) -> Target:
    """The target for `--env` (None when not given) and the variables.

    A mutation on production is refused unless `--env` named it, before the
    token is read.
    """
    if env_option is not None:
        environment = Environment(env_option)
    else:
        name = environ.get(ENV_VARIABLE, DEFAULT_ENVIRONMENT)
        try:
            environment = Environment(name)
        except ValueError:
            choices = ", ".join(Environment)
            raise CliError(
                f"{ENV_VARIABLE} is {name!r}, not one of {choices}"
            ) from None
    if mutation and environment is Environment.PRODUCTION and env_option is None:
        raise CliError(
            f"{ENV_VARIABLE} selects production; a command that changes production"
            " needs --env production on the command line"
        )
    url = _variable(environ, url_variable(environment))
    token_file = Path(_variable(environ, token_file_variable(environment)))
    try:
        token = read_secret(token_file)
    except ConfigError as error:
        raise CliError(f"operator token: {error}") from None
    # The transport's error for an illegal header value quotes the value.
    if not _BEARER_TOKEN.fullmatch(token.reveal()):
        raise CliError(
            f"operator token: {token_file} holds characters a bearer token cannot carry"
        )
    return Target(environment, url.rstrip("/"), token_file, token)


def _variable(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    if not value:
        raise CliError(f"{name} is not set")
    return value


def request(
    target: Target,
    path: str,
    *,
    params: Mapping[str, str] | None = None,
    body: JsonObject | None = None,
) -> JsonObject:
    """GET `path` from the target's API, or POST `body` to it if given.

    The answer must name the target's environment.
    """
    headers = {
        "Authorization": f"Bearer {target.token.reveal()}",
        ENVIRONMENT_HEADER: target.environment,
    }
    try:
        if body is None:
            response = httpx.get(
                target.url + path,
                params=params,
                headers=headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
        else:
            response = httpx.post(
                target.url + path,
                json=body,
                headers=headers,
                timeout=MUTATION_TIMEOUT_SECONDS,
            )
    # InvalidURL is not an HTTPError.
    except httpx.InvalidURL as error:
        raise CliError(
            f"{url_variable(target.environment)} is not a valid URL: {error}"
        ) from None
    except httpx.HTTPError as error:
        raise CliError(f"cannot reach the connector at {target.url}: {error}") from None
    try:
        document: object = response.json()
    except ValueError:
        document = None
    if not isinstance(document, dict):
        raise CliError(
            f"the connector at {target.url} answered {response.status_code}"
            " without a JSON document"
        )
    serving = document.get("environment")
    if serving != target.environment:
        raise CliError(
            f"the connector at {target.url} serves {serving}, not "
            f"{target.environment}; check {url_variable(target.environment)}"
        )
    if response.status_code == HTTPStatus.UNAUTHORIZED:
        raise CliError(f"the connector rejected the token in {target.token_file}")
    if not response.is_success:
        raise CliError(
            f"the connector answered {response.status_code}: {document.get('error')}"
        )
    return {str(key): value for key, value in document.items()}


def _malformed(key: str) -> CliError:
    return CliError(f"unexpected answer from the connector: bad {key!r}")


def _field(document: JsonObject, key: str, kind: type[_T]) -> _T:
    value = document.get(key)
    if not isinstance(value, kind):
        raise _malformed(key)
    return value


def _optional(document: JsonObject, key: str, kind: type[_T]) -> _T | None:
    return None if document.get(key) is None else _field(document, key, kind)


def _number(document: JsonObject, key: str) -> float:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise _malformed(key)
    return value


def _as_object(value: object, key: str) -> JsonObject:
    if not isinstance(value, dict):
        raise _malformed(key)
    return {str(k): v for k, v in value.items()}


def _object(document: JsonObject, key: str) -> JsonObject:
    return _as_object(document.get(key), key)


def _optional_object(document: JsonObject, key: str) -> JsonObject | None:
    return None if document.get(key) is None else _object(document, key)


def _objects(document: JsonObject, key: str) -> list[JsonObject]:
    return [_as_object(value, key) for value in _field(document, key, list)]


def _details(problem: JsonObject) -> dict[str, str]:
    return {name: str(value) for name, value in _object(problem, "details").items()}


def _summary(details: Mapping[str, str]) -> str:
    return "; ".join(f"{name}: {value}" for name, value in details.items())


def _quantity(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


_ON = "on"
_OFF = "off"


def _on_off(value: bool) -> str:
    return _ON if value else _OFF


def _environment_line(document: JsonObject) -> str:
    return f"Environment: {_field(document, 'environment', str)}"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [max(len(row[i]) for row in [headers, *rows]) for i in range(len(headers))]
    return [
        "  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip()
        for row in [headers, *rows]
    ]


def _email_state(problem: JsonObject) -> str:
    email = _object(problem, "email")
    if email.get("pending"):
        last = email.get("last_attempt_at") or "never"
        state = f"pending, {email.get('attempts')} attempts, last {last}"
    else:
        state = "not pending"
    communication = email.get("communication")
    if communication is not None:
        state += f", ERPNext Communication {communication}"
    return state


def _render_status(document: JsonObject) -> str:
    last_pass_at = _optional(document, "last_pass_at", str)
    if last_pass_at is None:
        last_pass = "none yet"
    else:
        scope = "full" if _field(document, "last_pass_full", bool) else "incremental"
        last_pass = f"{last_pass_at} ({scope})"
    last_full_pass = _optional(document, "last_full_pass_at", str) or "none yet"
    return "\n".join(
        [
            _environment_line(document),
            f"Dry run: {_on_off(_field(document, 'dry_run', bool))}",
            f"Last pass: {last_pass}",
            f"Last full pass: {last_full_pass}",
            f"Push lag: {_number(document, 'push_lag_seconds'):g} s",
            f"Open problems: {_field(document, 'open_problems', int)}",
            f"Unsent emails: {_field(document, 'unsent_emails', int)}",
        ]
    )


def _render_problems(document: JsonObject) -> str:
    problems = _objects(document, "problems")
    lines = [_environment_line(document)]
    if not problems:
        return "\n".join([*lines, "No open problems."])
    lines.append(f"Open problems: {len(problems)}")
    for problem in problems:
        line = f"  {_field(problem, 'key', str)}  opened {problem.get('opened_at')}"
        if _object(problem, "email").get("pending"):
            line += "  email pending"
        lines.append(line)
    return "\n".join(lines)


def _render_problem(document: JsonObject) -> str:
    problem = _object(document, "problem")
    resolved_at = problem.get("resolved_at") or "no, still open"
    details = [f"  {name}: {value}" for name, value in _details(problem).items()]
    return "\n".join(
        [
            _environment_line(document),
            f"Problem: {_field(problem, 'key', str)}",
            f"Opened: {problem.get('opened_at')}",
            f"Resolved: {resolved_at}",
            *details,
            f"Email: {_email_state(problem)}",
        ]
    )


_BINDING_SECTIONS = (
    ("Unbound variants", "unbound"),
    ("Ambiguous variants", "ambiguous"),
)


def _render_bindings(document: JsonObject) -> str:
    lines = [_environment_line(document)]
    for title, key in _BINDING_SECTIONS:
        problems = _objects(document, key)
        lines.append(f"{title}: {len(problems) or 'none'}")
        lines.extend(
            f"  {problem.get('subject')}  {_summary(_details(problem))}"
            for problem in problems
        )
    changes = _objects(document, "sku_changes")
    lines.append(f"Held SKU changes: {len(changes) or 'none'}")
    lines.extend(_held_line(change) for change in changes)
    return "\n".join(lines)


def _held_line(change: JsonObject) -> str:
    line = (
        f"  {change.get('variant_id')}  was bound to {change.get('bound_item_code')},"
        f" SKU now {change.get('sku') or 'unknown'},"
        f" held since {change.get('detected_at')}"
    )
    accepted = _optional(change, "accepted_sku", str)
    if accepted is not None:
        line += f", accepted {accepted} for the next full pass"
    return line


def _binding_line(document: JsonObject) -> str:
    item_code = _optional(document, "bound_item_code", str)
    if item_code is not None:
        return f"Bound to: {item_code}"
    problem = _optional_object(document, "binding_problem")
    if problem is not None:
        return (
            f"Not bound: {_summary(_details(problem))} (problem {problem.get('key')})"
        )
    change = _optional_object(document, "sku_change")
    if change is None:
        return "Not bound"
    return (
        "Not bound: SKU changed since it was bound to"
        f" {change.get('bound_item_code')} (detected {change.get('detected_at')})"
    )


def _decision_lines(document: JsonObject) -> list[str]:
    decision = _optional_object(document, "decision")
    if decision is None:
        return ["Last decision: none"]
    explanation = _object(decision, "explanation")
    lines = [
        f"Last decision: item {decision.get('item_code')},"
        f" action {decision.get('action')}",
        f"  Decided at: {_field(decision, 'decided_at', str)}",
        *(f"  {name}: {_quantity(value)}" for name, value in explanation.items()),
        f"  Shopify available: {_quantity(decision.get('shopify_available'))}",
        f"  Last pushed: {_quantity(decision.get('last_pushed'))}",
    ]
    if decision.get("note"):
        lines.append(f"  Note: {decision.get('note')}")
    return lines


def _history_lines(document: JsonObject) -> list[str]:
    history = _objects(document, "push_history")
    if not history:
        return ["Push history: none"]
    return [
        "Push history, newest first:",
        *(
            f"  {push.get('attempted_at')}  {push.get('available')}"
            f" (from {_quantity(push.get('change_from'))})  {push.get('outcome')}"
            for push in history
        ),
    ]


def _render_record(document: JsonObject) -> str:
    return "\n".join(
        [
            _environment_line(document),
            f"Variant: {document.get('variant_id')}",
            _binding_line(document),
            *_decision_lines(document),
            *_history_lines(document),
        ]
    )


_REPORT_COLUMNS = (
    ("Variant", "variant_id"),
    ("Item", "item_code"),
    ("Shopify", "shopify_available"),
    ("Computed", "computed"),
    ("Old", "old_formula"),
    ("Actual", "actual_qty"),
    ("Reserved", "reserved_qty"),
    ("Production", "reserved_qty_for_production"),
    ("Planned", "planned_qty"),
    ("Unmirrored", "unmirrored"),
)


def _render_report(document: JsonObject) -> str:
    rows = _objects(document, "rows")
    lines = [_environment_line(document)]
    if not rows:
        return "\n".join([*lines, "No bound variant has been computed yet."])
    headers = [title for title, _ in _REPORT_COLUMNS]
    cells = [[_quantity(row.get(key)) for _, key in _REPORT_COLUMNS] for row in rows]
    return "\n".join([*lines, *_table(headers, cells)])


def _render_dry_run(document: JsonObject) -> str:
    on = _field(document, "dry_run", bool)
    if not _field(document, "changed", bool):
        state = f"Dry run: already {_on_off(on)}; nothing changed"
        return "\n".join([_environment_line(document), state])
    return "\n".join(
        [
            _environment_line(document),
            f"Dry run: {_on_off(on)}, was {_on_off(not on)}",
            "This takes effect from the next pass.",
        ]
    )


def _render_accepted_sku_change(document: JsonObject) -> str:
    return "\n".join(
        [
            _environment_line(document),
            f"Accepted SKU {_field(document, 'accepted_sku', str)}"
            f" for {_field(document, 'variant_id', str)},"
            f" which was bound to {_field(document, 'bound_item_code', str)}.",
            "The next full pass binds it by that SKU if the variant still has it.",
        ]
    )


@dataclass(frozen=True)
class _Read:
    """A read command: the API path, its parameters and the text rendering."""

    mutation: ClassVar[bool] = False
    path: str
    render: Callable[[JsonObject], str]
    params: Callable[[Mapping[str, object]], dict[str, str]] = lambda _: {}

    def send(self, target: Target, values: Mapping[str, object]) -> JsonObject:
        return request(target, self.path, params=self.params(values))


@dataclass(frozen=True)
class _Write:
    """A mutation: the API path, its JSON body and the text rendering."""

    mutation: ClassVar[bool] = True
    path: str
    render: Callable[[JsonObject], str]
    body: Callable[[Mapping[str, object]], JsonObject]

    def send(self, target: Target, values: Mapping[str, object]) -> JsonObject:
        return request(target, self.path, body=self.body(values))


def _parser() -> argparse.ArgumentParser:
    # SUPPRESS leaves an option unset unless given, so it can go before or
    # after a command, and a given --env can be told from a default one.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--env",
        choices=list(Environment),
        default=argparse.SUPPRESS,
        help=f"the connector's environment (default: ${ENV_VARIABLE}, else "
        f"{DEFAULT_ENVIRONMENT})",
    )
    common.add_argument(
        "--json",
        action="store_true",
        default=argparse.SUPPRESS,
        help="print the API's JSON answer",
    )
    parser = argparse.ArgumentParser(prog=CLI_NAME, parents=[common])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(SERVE_COMMAND, help="run the connector service")
    status = commands.add_parser(
        "status", parents=[common], help="last pass, push lag, dry-run and problems"
    )
    status.set_defaults(call=_Read("/status", _render_status))
    problems = commands.add_parser(
        "problems", parents=[common], help="list open problems, or show one"
    )
    problems.set_defaults(call=_Read("/problems", _render_problems))
    show = problems.add_subparsers(dest="action").add_parser(
        "show", parents=[common], help="show one problem, open or resolved"
    )
    show.add_argument("key", help="the problem's kind:subject, as its email names it")
    show.set_defaults(
        call=_Read("/problem", _render_problem, lambda v: {"key": str(v["key"])})
    )
    bindings = commands.add_parser(
        "bindings",
        parents=[common],
        help="unbound and ambiguous variants, and variants held after a SKU change",
    )
    bindings.set_defaults(call=_Read("/bindings", _render_bindings))
    accept = bindings.add_subparsers(dest="action").add_parser(
        "accept-sku-change",
        parents=[common],
        help="let a variant held after its SKU changed bind by the SKU"
        " `bindings` shows",
    )
    accept.add_argument("variant", help="the variant's GID")
    accept.set_defaults(
        call=_Write(
            "/bindings/accept-sku-change",
            _render_accepted_sku_change,
            lambda v: {"variant": str(v["variant"])},
        )
    )
    record = commands.add_parser(
        "record",
        parents=[common],
        help="a variant's sellable stock explanation and push history",
    )
    record.add_argument("variant", help="the variant's GID")
    record.set_defaults(
        call=_Read("/record", _render_record, lambda v: {"variant": str(v["variant"])})
    )
    commands.add_parser(
        "report", parents=[common], help="the cutover dry-run comparison"
    ).set_defaults(call=_Read("/report", _render_report))
    dry_run = commands.add_parser(
        "dry-run", parents=[common], help="turn dry-run on or off from the next pass"
    )
    dry_run.add_argument("state", choices=[_ON, _OFF])
    dry_run.set_defaults(
        call=_Write("/dry-run", _render_dry_run, lambda v: {"on": v["state"] == _ON})
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    values = vars(parser.parse_args(argv))
    env_option = values.get("env")
    if values["command"] == SERVE_COMMAND:
        if env_option is not None:
            parser.error("serve takes its environment from its configuration")
        if values.get("json"):
            parser.error("serve has no --json output")
        return serve()
    call: _Read | _Write = values["call"]
    try:
        target = resolve_target(
            env_option if isinstance(env_option, str) else None,
            os.environ,
            mutation=call.mutation,
        )
    except CliError as error:
        return _fail(str(error))
    try:
        document = call.send(target, values)
        output = (
            json.dumps(document, indent=2)
            if values.get("json")
            else call.render(document)
        )
    except CliError as error:
        # Named so that an answer such as "no problem" cannot be read as
        # coming from the environment the operator meant to ask.
        return _fail(f"{target.environment}: {error}")
    print(output)
    return os.EX_OK


def _fail(message: str) -> int:
    print(f"{CLI_NAME}: {message}", file=sys.stderr)
    return EXIT_FAILURE
