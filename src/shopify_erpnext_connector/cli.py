"""The `shopify-erpnext-connector` executable.

`serve` runs the connector service; the other commands are the operator CLI,
which reads from a connector's API. Each environment's URL and token file
come from `CONNECTOR_<ENV>_URL` and `CONNECTOR_<ENV>_TOKEN_FILE`; the token
itself is read only from that file, never from a variable or an argument.
`--env` selects the environment, defaulting to `CONNECTOR_ENV` and then to
staging. Every answer must name the selected environment, so a URL pointing
at the wrong connector fails instead of showing its data.
"""

import argparse
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import TypeVar

import httpx

from shopify_erpnext_connector.api import ENVIRONMENT_HEADER
from shopify_erpnext_connector.config import (
    ConfigError,
    Environment,
    Secret,
    read_secret,
)
from shopify_erpnext_connector.problems import CLI_NAME
from shopify_erpnext_connector.service import serve

ENV_VARIABLE = "CONNECTOR_ENV"
DEFAULT_ENVIRONMENT = Environment.STAGING
REQUEST_TIMEOUT_SECONDS = 30.0
EXIT_FAILURE = 1

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
    """The connector a command talks to.

    `explicit` is true when `--env` was given on the command line rather than
    taken from `CONNECTOR_ENV` or the default: a mutation on production
    requires it.
    """

    environment: Environment
    explicit: bool
    url: str
    token_file: Path
    token: Secret


def resolve_target(env_option: str | None, environ: Mapping[str, str]) -> Target:
    """The target for `--env` (None when not given) and the variables."""
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
    url = _variable(environ, url_variable(environment))
    token_file = Path(_variable(environ, token_file_variable(environment)))
    try:
        token = read_secret(token_file)
    except ConfigError as error:
        raise CliError(f"operator token: {error}") from None
    return Target(
        environment, env_option is not None, url.rstrip("/"), token_file, token
    )


def _variable(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name, "")
    if not value:
        raise CliError(f"{name} is not set")
    return value


def fetch(target: Target, path: str, params: Mapping[str, str]) -> JsonObject:
    """GET `path` from the target's API; the answer must name its environment."""
    headers = {
        "Authorization": f"Bearer {target.token.reveal()}",
        ENVIRONMENT_HEADER: target.environment,
    }
    try:
        response = httpx.get(
            target.url + path,
            params=params,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as error:
        raise CliError(
            f"cannot reach the {target.environment} connector at {target.url}: {error}"
        ) from None
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
        raise CliError(
            f"the {target.environment} connector rejected the token in "
            f"{target.token_file}"
        )
    if response.is_error:
        raise CliError(
            f"the connector answered {response.status_code}: {document.get('error')}"
        )
    return {str(key): value for key, value in document.items()}


def _field(document: JsonObject, key: str, kind: type[_T]) -> _T:
    value = document.get(key)
    if not isinstance(value, kind):
        raise CliError(f"unexpected answer from the connector: bad {key!r}")
    return value


def _optional(document: JsonObject, key: str, kind: type[_T]) -> _T | None:
    return None if document.get(key) is None else _field(document, key, kind)


def _number(document: JsonObject, key: str) -> float:
    value = document.get(key)
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise CliError(f"unexpected answer from the connector: bad {key!r}")
    return value


def _on_off(value: bool) -> str:
    return "on" if value else "off"


def _render_status(document: JsonObject) -> str:
    last_pass_at = _optional(document, "last_pass_at", str)
    if last_pass_at is None:
        last_pass = "none yet"
    else:
        scope = "full" if _field(document, "last_pass_full", bool) else "incremental"
        last_pass = f"{last_pass_at} ({scope})"
    return "\n".join(
        [
            f"Environment: {_field(document, 'environment', str)}",
            f"Dry run: {_on_off(_field(document, 'dry_run', bool))}",
            f"Last pass: {last_pass}",
            f"Push lag: {_number(document, 'push_lag_seconds'):g} s",
            f"Open problems: {_field(document, 'open_problems', int)}",
            f"Unsent emails: {_field(document, 'unsent_emails', int)}",
        ]
    )


@dataclass(frozen=True)
class _Read:
    """A read command: the API path, its parameters and the text rendering."""

    path: str
    render: Callable[[JsonObject], str]
    params: Callable[[Mapping[str, object]], dict[str, str]] = lambda _: {}


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
    commands.add_parser("serve", help="run the connector service")
    status = commands.add_parser(
        "status", parents=[common], help="last pass, push lag, dry-run and problems"
    )
    status.set_defaults(read=_Read("/status", _render_status))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    values = vars(parser.parse_args(argv))
    read = values.get("read")
    env_option = values.get("env")
    if not isinstance(read, _Read):
        if env_option is not None:
            parser.error("serve takes its environment from its configuration")
        return serve()
    try:
        target = resolve_target(
            env_option if isinstance(env_option, str) else None, os.environ
        )
        document = fetch(target, read.path, read.params(values))
        output = (
            json.dumps(document, indent=2)
            if values.get("json")
            else read.render(document)
        )
    except CliError as error:
        print(f"{CLI_NAME}: {error}", file=sys.stderr)
        return EXIT_FAILURE
    print(output)
    return os.EX_OK
