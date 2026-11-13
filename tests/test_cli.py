import json
import os
import shlex
import socket
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import entry_points
from pathlib import Path

import httpx
import pytest
from helpers import FakeWorker, make_config

from shopify_erpnext_connector import cli
from shopify_erpnext_connector.api import (
    ENVIRONMENT_HEADER,
    PASS_WAIT_TIMEOUT_SECONDS,
    WRITER_LOCK_TIMEOUT_SECONDS,
    ApiServer,
)
from shopify_erpnext_connector.config import ApiConfig, Environment, Secret
from shopify_erpnext_connector.decisions import Decision, save_decisions
from shopify_erpnext_connector.problems import (
    Problem,
    ProblemKind,
    get_problem,
    record_check,
    record_communication,
    record_delivered,
    record_send_attempt,
    show_command,
)
from shopify_erpnext_connector.push import PushOutcome
from shopify_erpnext_connector.store import (
    dry_run,
    open_store,
    set_dry_run,
    utc_timestamp,
)

CLI_NAME = "shopify-erpnext-connector"
NOW = datetime(2026, 11, 13, 10, 0, tzinfo=UTC)
VARIANT = "gid://shopify/ProductVariant/1001"
OTHER_VARIANT = "gid://shopify/ProductVariant/1002"
NO_ITEM = "No ERPNext Item has this item code"
OPERATOR_TOKEN = "test-operator-token"
# argparse's exit status for a usage error.
USAGE_ERROR = 2
# Kept apart from its value so the fixture check's header pattern stays meaningful.
AUTH_HEADER = "Authorization"
STATUS = {
    "dry_run": False,
    "last_pass_at": "2026-11-13T10:00:00.000000Z",
    "last_pass_full": True,
    "last_full_pass_at": "2026-11-13T09:00:00.000000Z",
    "push_lag_seconds": 0,
    "open_problems": 0,
    "unsent_emails": 0,
}


@dataclass
class Canned:
    """What the canned connector answers, and the requests it was sent."""

    status: int = 200
    body: bytes = b"{}"
    received: list[dict[str, str]] = field(default_factory=list)
    posted: list[tuple[str, object]] = field(default_factory=list)

    def answer(self, status: int, document: dict[str, object]) -> None:
        self.status = status
        self.body = json.dumps(document).encode()


@pytest.fixture
def canned() -> Iterator[tuple[Canned, str]]:
    """A connector stand-in answering every GET and POST with a set response."""
    state = Canned()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            state.posted.append((self.path, json.loads(self.rfile.read(length))))
            self.do_GET()

        def do_GET(self) -> None:
            state.received.append(dict(self.headers.items()))
            self.send_response(state.status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(state.body)))
            self.end_headers()
            self.wfile.write(state.body)

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def configure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    url: str,
    environment: str = "staging",
    token: str = OPERATOR_TOKEN + "\n",
) -> Path:
    """Point the CLI's variables for `environment` at `url` and a token file."""
    token_file = tmp_path / f"{environment}-token"
    token_file.write_text(token)
    prefix = f"CONNECTOR_{environment.upper()}"
    monkeypatch.setenv(f"{prefix}_URL", url)
    monkeypatch.setenv(f"{prefix}_TOKEN_FILE", str(token_file))
    return token_file


@pytest.fixture(autouse=True)
def clean_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in list(os.environ):
        if name.startswith("CONNECTOR_"):
            monkeypatch.delenv(name)


@pytest.fixture
def store_path(tmp_path: Path) -> Path:
    path = tmp_path / "connector.sqlite3"
    open_store(path).close()
    return path


@pytest.fixture
def store(store_path: Path) -> Iterator[sqlite3.Connection]:
    connection = open_store(store_path)
    yield connection
    connection.close()


@pytest.fixture
def api(store_path: Path) -> Iterator[ApiServer]:
    config = make_config()
    config = replace(
        config,
        store_path=store_path,
        api=ApiConfig(port=0),
        secrets=replace(
            config.secrets, operator_tokens={"alice": Secret(OPERATOR_TOKEN)}
        ),
    )
    server = ApiServer(config, FakeWorker())
    server.start()
    yield server
    server.close()


@pytest.fixture
def api_url(api: ApiServer) -> str:
    return f"http://127.0.0.1:{api.port}"


@pytest.fixture
def connected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, api_url: str) -> None:
    """The CLI's staging variables point at the running API."""
    configure(monkeypatch, tmp_path, api_url)


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out, err = capsys.readouterr()
    return code, out, err


def test_the_executable_is_the_cli() -> None:
    (script,) = entry_points(group="console_scripts", name=CLI_NAME)

    assert script.load() is cli.main


def test_serve_runs_the_service(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "config.toml"
    monkeypatch.setenv("CONNECTOR_CONFIG", str(missing))
    monkeypatch.setenv("CONNECTOR_SECRETS_DIR", str(tmp_path))

    assert cli.main(["serve"]) == os.EX_CONFIG
    assert str(missing) in capsys.readouterr().err


def test_serve_refuses_an_env_option(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit:
        cli.main(["--env", "production", "serve"])

    assert exit.value.code == USAGE_ERROR
    assert "configuration" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["serve", "--json"], ["--json", "serve"]])
def test_serve_refuses_a_json_option(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    with pytest.raises(SystemExit) as exit:
        cli.main(argv)

    assert exit.value.code == USAGE_ERROR
    assert "--json" in capsys.readouterr().err


def test_a_command_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit:
        cli.main([])

    assert exit.value.code == USAGE_ERROR
    assert "serve" in capsys.readouterr().err


def test_status_against_the_connector(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    api_url: str,
) -> None:
    configure(monkeypatch, tmp_path, api_url)

    code, out, err = run(capsys, "status")

    assert code == 0, err
    assert "staging" in out
    assert "Dry run: on" in out
    assert "Last pass: none yet" in out
    assert "Last full pass: none yet" in out
    assert OPERATOR_TOKEN not in out + err


def test_status_after_a_completed_full_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    canned: tuple[Canned, str],
) -> None:
    state, url = canned
    state.answer(
        200,
        {"environment": "staging"}
        | STATUS
        | {"push_lag_seconds": 42.5, "open_problems": 3, "unsent_emails": 1},
    )
    configure(monkeypatch, tmp_path, url)

    code, out, err = run(capsys, "status")

    assert code == 0, err
    lines = out.splitlines()
    assert f"Last pass: {STATUS['last_pass_at']} (full)" in lines
    assert f"Last full pass: {STATUS['last_full_pass_at']}" in lines
    assert "Push lag: 42.5 s" in lines
    assert "Open problems: 3" in lines
    assert "Unsent emails: 1" in lines


def test_status_as_json(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    api_url: str,
) -> None:
    configure(monkeypatch, tmp_path, api_url)

    code, out, _ = run(capsys, "status", "--json")

    assert code == 0
    assert json.loads(out)["environment"] == "staging"
    assert json.loads(out)["dry_run"] is True


def test_requests_carry_the_token_and_the_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    canned: tuple[Canned, str],
) -> None:
    state, url = canned
    state.answer(200, {"environment": "staging"} | STATUS)
    configure(monkeypatch, tmp_path, url)

    code, _, _ = run(capsys, "status")

    assert code == 0
    (headers,) = state.received
    assert headers[AUTH_HEADER] == f"Bearer {OPERATOR_TOKEN}"
    assert headers[ENVIRONMENT_HEADER] == "staging"


def test_environment_defaults_to_staging(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure(monkeypatch, tmp_path, "http://connector.example.com")

    target = cli.resolve_target(None, os.environ)

    assert target.environment is Environment.STAGING
    assert target.url == "http://connector.example.com"
    assert target.token.reveal() == OPERATOR_TOKEN


def test_environment_from_the_environment_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure(monkeypatch, tmp_path, "https://connector.example.com", "production")
    monkeypatch.setenv("CONNECTOR_ENV", "production")

    target = cli.resolve_target(None, os.environ)

    assert target.environment is Environment.PRODUCTION


def test_env_option_overrides_the_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure(monkeypatch, tmp_path, "https://connector.example.com", "production")
    monkeypatch.setenv("CONNECTOR_ENV", "staging")

    target = cli.resolve_target("production", os.environ)

    assert target.environment is Environment.PRODUCTION


@pytest.mark.parametrize("position", ["before", "after"])
def test_env_option_goes_before_or_after_the_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    canned: tuple[Canned, str],
    position: str,
) -> None:
    state, url = canned
    state.answer(200, {"environment": "production"} | STATUS)
    configure(monkeypatch, tmp_path, url, "production")
    argv = (
        ["--env", "production", "status"]
        if position == "before"
        else ["status", "--env", "production"]
    )

    code, _, err = run(capsys, *argv)

    assert code == 0, err
    assert state.received[0][ENVIRONMENT_HEADER] == "production"


def test_unknown_environment_variable_value_is_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("CONNECTOR_ENV", "testing")

    code, _, err = run(capsys, "status")

    assert code != 0
    assert "CONNECTOR_ENV" in err
    assert "testing" in err


def test_missing_url_variable_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(monkeypatch, tmp_path, "https://connector.example.com")
    monkeypatch.delenv("CONNECTOR_STAGING_URL")

    code, _, err = run(capsys, "status")

    assert code != 0
    assert "CONNECTOR_STAGING_URL" in err


def test_missing_token_file_variable_is_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(monkeypatch, tmp_path, "https://connector.example.com")
    monkeypatch.delenv("CONNECTOR_STAGING_TOKEN_FILE")

    code, _, err = run(capsys, "status")

    assert code != 0
    assert "CONNECTOR_STAGING_TOKEN_FILE" in err


def test_missing_token_file_is_an_error_naming_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token_file = configure(monkeypatch, tmp_path, "https://connector.example.com")
    token_file.unlink()

    code, _, err = run(capsys, "status")

    assert code != 0
    assert str(token_file) in err


def test_empty_token_file_is_an_error_naming_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    token_file = configure(
        monkeypatch, tmp_path, "https://connector.example.com", token="\n"
    )

    code, _, err = run(capsys, "status")

    assert code != 0
    assert str(token_file) in err
    assert "empty" in err


def test_token_that_cannot_be_sent_is_an_error_naming_only_its_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    canned: tuple[Canned, str],
) -> None:
    _, url = canned
    token_file = configure(monkeypatch, tmp_path, url, token="test-op\nerator\n")

    code, _, err = run(capsys, "status")

    assert code != 0
    assert str(token_file) in err
    assert "test-op" not in err.replace(str(token_file), "")


def test_redirect_is_not_a_successful_answer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    canned: tuple[Canned, str],
) -> None:
    state, url = canned
    state.answer(302, {"environment": "staging"} | STATUS)
    configure(monkeypatch, tmp_path, url)

    code, out, err = run(capsys, "status", "--json")

    assert code != 0
    assert out == ""
    assert "302" in err


def test_response_for_another_environment_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    canned: tuple[Canned, str],
) -> None:
    state, url = canned
    state.answer(200, {"environment": "production"} | STATUS)
    configure(monkeypatch, tmp_path, url)

    code, out, err = run(capsys, "status")

    assert code != 0
    assert out == ""
    assert "production" in err
    assert "staging" in err


def test_wrong_token_is_reported_without_the_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    api_url: str,
) -> None:
    wrong = "test-wrong-token-value"
    token_file = configure(monkeypatch, tmp_path, api_url, token=wrong)

    code, out, err = run(capsys, "status")

    assert code != 0
    assert str(token_file) in err
    assert "rejected" in err
    assert wrong not in out + err


def test_url_of_another_environments_connector_is_refused(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    api_url: str,
) -> None:
    configure(monkeypatch, tmp_path, api_url, "production")

    code, out, err = run(capsys, "--env", "production", "status")

    assert code != 0
    assert out == ""
    assert "serves staging, not production" in err
    assert "CONNECTOR_PRODUCTION_URL" in err


@pytest.mark.parametrize("status", [404, 409])
def test_refusal_is_reported_with_the_connectors_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    canned: tuple[Canned, str],
    status: int,
) -> None:
    state, url = canned
    state.answer(status, {"environment": "staging", "error": "test-reason"})
    configure(monkeypatch, tmp_path, url)

    code, out, err = run(capsys, "status")

    assert code != 0
    assert out == ""
    assert "test-reason" in err


def test_server_error_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    canned: tuple[Canned, str],
) -> None:
    state, url = canned
    state.answer(500, {"environment": "staging", "error": "the request failed"})
    configure(monkeypatch, tmp_path, url)

    code, _, err = run(capsys, "status")

    assert code != 0
    assert "500" in err
    assert "the request failed" in err


def test_answer_that_is_not_json_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    canned: tuple[Canned, str],
) -> None:
    state, url = canned
    state.status = 502
    state.body = b"<html>Bad Gateway</html>"
    configure(monkeypatch, tmp_path, url)

    code, _, err = run(capsys, "status")

    assert code != 0
    assert "502" in err


def test_answer_missing_a_required_field_is_reported(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    canned: tuple[Canned, str],
) -> None:
    state, url = canned
    state.answer(200, {"environment": "staging"})
    configure(monkeypatch, tmp_path, url)

    code, out, err = run(capsys, "status")

    assert code != 0
    assert out == ""
    assert "bad 'dry_run'" in err


@pytest.mark.parametrize(
    ("status", "document"),
    [
        (404, {"environment": "production", "error": "no problem"}),
        (500, {"environment": "production", "error": "the request failed"}),
        (401, {"environment": "production", "error": "unauthorized"}),
        (200, {"environment": "staging"} | STATUS),
        (200, {"environment": "production"}),
        (502, None),
    ],
)
def test_errors_about_an_answer_name_the_environment_asked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    canned: tuple[Canned, str],
    status: int,
    document: dict[str, object] | None,
) -> None:
    state, url = canned
    if document is None:
        state.status = status
        state.body = b"<html>Bad Gateway</html>"
    else:
        state.answer(status, document)
    configure(monkeypatch, tmp_path, url, "production")
    monkeypatch.setenv("CONNECTOR_ENV", "production")

    code, out, err = run(capsys, "status")

    assert code == cli.EXIT_FAILURE
    assert out == ""
    assert err.startswith(f"{CLI_NAME}: production: ")


def test_unreachable_connector_is_reported(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    configure(monkeypatch, tmp_path, url)

    code, _, err = run(capsys, "status")

    assert code != 0
    assert err.startswith(f"{CLI_NAME}: staging: ")
    assert url in err


def test_malformed_url_is_reported_naming_its_variable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure(monkeypatch, tmp_path, "http://host:abc")

    code, out, err = run(capsys, "status")

    assert code == cli.EXIT_FAILURE
    assert out == ""
    assert err.startswith(f"{CLI_NAME}: staging: ")
    assert "CONNECTOR_STAGING_URL" in err
    assert len(err.splitlines()) == 1


def unbound(store: sqlite3.Connection, variant_id: str, sku: str) -> None:
    record_check(
        store,
        ProblemKind.UNBOUND_SKU,
        [Problem(ProblemKind.UNBOUND_SKU, variant_id, {"SKU": sku, "Reason": NO_ITEM})],
        NOW,
    )


def oversold(store: sqlite3.Connection, variant_id: str) -> None:
    record_check(
        store,
        ProblemKind.OVERSELL,
        [
            Problem(
                ProblemKind.OVERSELL,
                variant_id,
                {"Item": "TEST-ITEM-001"},
                context={"Short by": "2"},
            )
        ],
        NOW,
    )


def bind_and_decide(store: sqlite3.Connection, variant_id: str) -> None:
    store.execute(
        "INSERT INTO bindings (variant_id, inventory_item_id, item_code, bound_at)"
        " VALUES (?, ?, 'TEST-ITEM-001', ?)",
        (variant_id, variant_id + "-inventory", utc_timestamp(NOW)),
    )
    explanation = {
        "actual_qty": 5.0,
        "reserved_qty": 1.0,
        "reserved_qty_for_production": 0.0,
        "planned_qty": 2.0,
        "unmirrored": 1,
        "sellable": 3,
        "old_formula": 4,
    }
    decision = Decision("TEST-ITEM-001", explanation, 2, 2, "pushed", None)
    save_decisions(store, [(variant_id, decision)], NOW)
    store.execute(
        "INSERT INTO push_history (variant_id, available, change_from,"
        " idempotency_key, attempted_at, outcome) VALUES (?, 3, 2, 'test-key', ?, ?)",
        (variant_id, utc_timestamp(NOW), PushOutcome.PUSHED),
    )


@pytest.mark.usefixtures("connected")
def test_problems_lists_the_open_problems(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    unbound(store, VARIANT, "TEST-SKU-001")
    oversold(store, OTHER_VARIANT)

    code, out, err = run(capsys, "problems")

    assert code == 0, err
    assert "staging" in out
    assert f"unbound_sku:{VARIANT}" in out
    assert f"oversell:{OTHER_VARIANT}" in out
    assert "email pending" in out


@pytest.mark.usefixtures("connected")
def test_problems_when_none_is_open(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "problems")

    assert code == 0
    assert "No open problems" in out


@pytest.mark.usefixtures("connected")
def test_the_command_in_a_problem_email_shows_the_problem(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    oversold(store, VARIANT)
    command = show_command(ProblemKind.OVERSELL, VARIANT, Environment.STAGING)
    program, *argv = shlex.split(command)

    code, out, err = run(capsys, *argv)

    assert program == CLI_NAME
    assert code == 0, err
    assert f"oversell:{VARIANT}" in out
    assert "Item: TEST-ITEM-001" in out
    assert "Short by: 2" in out
    assert f"Opened: {utc_timestamp(NOW)}" in out
    assert "Email: pending" in out


def test_the_command_in_a_production_email_asks_production(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    store_path: Path,
    store: sqlite3.Connection,
    api_url: str,
) -> None:
    config = make_config(environment=Environment.PRODUCTION)
    production = ApiServer(
        replace(
            config,
            store_path=store_path,
            api=ApiConfig(port=0),
            secrets=replace(
                config.secrets, operator_tokens={"alice": Secret(OPERATOR_TOKEN)}
            ),
        ),
        FakeWorker(),
    )
    production.start()
    try:
        # The operator's shell selects no environment, so it would ask staging.
        configure(monkeypatch, tmp_path, api_url)
        configure(
            monkeypatch,
            tmp_path,
            f"http://127.0.0.1:{production.port}",
            Environment.PRODUCTION,
        )
        oversold(store, VARIANT)
        command = show_command(ProblemKind.OVERSELL, VARIANT, Environment.PRODUCTION)

        code, out, err = run(capsys, *shlex.split(command)[1:])
    finally:
        production.close()

    assert code == 0, err
    assert "Environment: production" in out
    assert f"Problem: oversell:{VARIANT}" in out


@pytest.mark.usefixtures("connected")
def test_problems_show_of_a_resolved_problem_whose_email_was_sent(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    oversold(store, VARIANT)
    problem = get_problem(store, ProblemKind.OVERSELL, VARIANT)
    assert problem is not None
    record_send_attempt(store, problem, NOW)
    record_communication(store, problem, "TEST-COMM-0001")
    record_delivered(store, problem)
    resolved_at = NOW + timedelta(seconds=1)
    record_check(store, ProblemKind.OVERSELL, [], resolved_at)

    code, out, err = run(capsys, "problems", "show", f"oversell:{VARIANT}")

    assert code == 0, err
    lines = out.splitlines()
    assert f"Resolved: {utc_timestamp(resolved_at)}" in lines
    assert "Email: not pending, ERPNext Communication TEST-COMM-0001" in lines


@pytest.mark.usefixtures("connected")
def test_problems_show_of_an_unknown_problem_is_not_found(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = run(capsys, "problems", "show", f"oversell:{VARIANT}")

    assert code != 0
    assert out == ""
    assert f"staging: the connector answered 404: no problem oversell:{VARIANT}" in err


@pytest.mark.usefixtures("connected")
def test_problems_as_json(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    oversold(store, VARIANT)

    code, out, _ = run(capsys, "problems", "--json")

    assert code == 0
    assert json.loads(out)["problems"][0]["key"] == f"oversell:{VARIANT}"


@pytest.mark.usefixtures("connected")
def test_bindings_shows_unbound_ambiguous_and_held_variants(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    held = "gid://shopify/ProductVariant/1003"
    unbound(store, VARIANT, "TEST-SKU-001")
    record_check(
        store,
        ProblemKind.AMBIGUOUS_SKU,
        [Problem(ProblemKind.AMBIGUOUS_SKU, OTHER_VARIANT, {"SKU": "TEST-SKU-002"})],
        NOW,
    )
    store.execute(
        "INSERT INTO sku_changes (variant_id, bound_item_code, detected_at)"
        " VALUES (?, 'TEST-ITEM-003', ?)",
        (held, utc_timestamp(NOW)),
    )
    record_check(
        store,
        ProblemKind.CHANGED_SKU,
        [Problem(ProblemKind.CHANGED_SKU, held, {"SKU": "TEST-SKU-003-NEW"})],
        NOW,
    )

    code, out, err = run(capsys, "bindings")

    assert code == 0, err
    unbound_part, rest = out.split("Ambiguous")
    ambiguous_part, held_part = rest.split("Held")
    assert VARIANT in unbound_part
    assert NO_ITEM in unbound_part
    assert OTHER_VARIANT in ambiguous_part
    assert held in held_part
    assert "TEST-ITEM-003" in held_part
    assert "TEST-SKU-003-NEW" in held_part


@pytest.mark.usefixtures("connected")
def test_bindings_when_every_variant_is_bound(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, _ = run(capsys, "bindings")

    assert code == 0
    assert VARIANT not in out
    assert "Unbound variants: none" in out


@pytest.mark.usefixtures("connected")
def test_record_explains_a_bound_variant(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    bind_and_decide(store, VARIANT)

    code, out, err = run(capsys, "record", VARIANT)

    assert code == 0, err
    assert "Bound to: TEST-ITEM-001" in out
    assert f"  Decided at: {utc_timestamp(NOW)}" in out.splitlines()
    assert "sellable: 3" in out
    assert "reserved_qty: 1" in out
    assert "Shopify available: 2" in out
    assert f"{utc_timestamp(NOW)}  3 (from 2)  pushed" in out


@pytest.mark.usefixtures("connected")
def test_record_for_an_unbound_variant_says_it_is_unbound_and_why(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    unbound(store, VARIANT, "TEST-SKU-001")

    code, out, err = run(capsys, "record", VARIANT)

    assert code == 0, err
    assert "Not bound" in out
    assert NO_ITEM in out
    assert f"unbound_sku:{VARIANT}" in out


@pytest.mark.usefixtures("connected")
def test_record_for_a_held_variant_without_a_problem_names_the_hold(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    store.execute(
        "INSERT INTO sku_changes (variant_id, bound_item_code, detected_at)"
        " VALUES (?, 'TEST-ITEM-001', ?)",
        (VARIANT, utc_timestamp(NOW)),
    )

    code, out, err = run(capsys, "record", VARIANT)

    assert code == 0, err
    assert (
        "Not bound: SKU changed since it was bound to TEST-ITEM-001"
        f" (detected {utc_timestamp(NOW)})"
    ) in out.splitlines()


@pytest.mark.usefixtures("connected")
def test_record_for_an_unknown_variant_is_not_found(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out, err = run(capsys, "record", VARIANT)

    assert code != 0
    assert out == ""
    assert f"no record of variant {VARIANT}" in err


@pytest.mark.usefixtures("connected")
def test_report_compares_the_computed_values(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    bind_and_decide(store, VARIANT)

    code, out, err = run(capsys, "report")

    assert code == 0, err
    header, row = out.splitlines()[-2:]
    assert header.split()[:4] == ["Variant", "Item", "Shopify", "Computed"]
    quantities = ["2", "3", "4", "5", "1", "0", "2", "1"]
    assert row.split() == [VARIANT, "TEST-ITEM-001", *quantities]


@pytest.mark.usefixtures("connected")
def test_report_before_any_computation(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "report")

    assert code == 0
    assert "No bound variant has been computed yet" in out


# Mutations


def audit_rows(store: sqlite3.Connection) -> list[tuple[str, str]]:
    return store.execute("SELECT operator, action FROM audit_log").fetchall()


def hold(store: sqlite3.Connection, variant_id: str, item_code: str, sku: str) -> None:
    store.execute(
        "INSERT INTO sku_changes (variant_id, bound_item_code, detected_at)"
        " VALUES (?, ?, ?)",
        (variant_id, item_code, utc_timestamp(NOW)),
    )
    record_check(
        store,
        ProblemKind.CHANGED_SKU,
        [Problem(ProblemKind.CHANGED_SKU, variant_id, {"SKU": sku})],
        NOW,
    )


@pytest.mark.usefixtures("connected")
def test_dry_run_off_turns_it_off(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, "dry-run", "off")

    assert code == 0, err
    assert out.splitlines() == [
        "Environment: staging",
        "Dry run: off, was on",
        "This takes effect from the next pass.",
    ]
    assert dry_run(store) is False
    assert audit_rows(store) == [("alice", "dry-run")]


@pytest.mark.usefixtures("connected")
def test_dry_run_on_when_already_on_says_so(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, "dry-run", "on")

    assert code == 0, err
    assert "Dry run: already on; nothing changed" in out.splitlines()
    assert audit_rows(store) == []


@pytest.mark.usefixtures("connected")
def test_dry_run_as_json(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, _ = run(capsys, "dry-run", "off", "--json")

    assert code == 0
    assert json.loads(out) == {
        "environment": "staging",
        "dry_run": False,
        "changed": True,
    }


def test_dry_run_takes_on_or_off(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit:
        cli.main(["dry-run", "maybe"])

    assert exit.value.code == USAGE_ERROR


@pytest.mark.usefixtures("connected")
def test_accept_sku_change_names_the_accepted_sku(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    hold(store, VARIANT, "TEST-ITEM-001", "TEST-SKU-001-NEW")

    code, out, err = run(
        capsys, "bindings", "accept-sku-change", VARIANT, "TEST-SKU-001-NEW"
    )

    assert code == 0, err
    assert out.splitlines() == [
        "Environment: staging",
        f"Accepted SKU TEST-SKU-001-NEW for {VARIANT},"
        " which was bound to TEST-ITEM-001.",
        "The next full pass binds it by that SKU if the variant still has it.",
    ]
    assert audit_rows(store) == [("alice", "accept-sku-change")]
    code, out, err = run(capsys, "bindings")
    assert code == 0, err
    assert "accepted TEST-SKU-001-NEW" in out


@pytest.mark.usefixtures("connected")
def test_accept_sku_change_as_json(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    hold(store, VARIANT, "TEST-ITEM-001", "TEST-SKU-001-NEW")

    code, out, _ = run(
        capsys, "bindings", "accept-sku-change", VARIANT, "TEST-SKU-001-NEW", "--json"
    )

    assert code == 0
    assert json.loads(out)["bound_item_code"] == "TEST-ITEM-001"


@pytest.mark.usefixtures("connected")
def test_accept_sku_change_without_a_hold_is_reported(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(
        capsys, "bindings", "accept-sku-change", VARIANT, "TEST-SKU-001-NEW"
    )

    assert code == cli.EXIT_FAILURE
    assert out == ""
    assert err.startswith(f"{CLI_NAME}: staging: ")
    assert "404" in err
    assert f"{VARIANT} has no held SKU change" in err
    assert audit_rows(store) == []


@pytest.mark.usefixtures("connected")
def test_accept_sku_change_refuses_a_sku_bindings_no_longer_shows(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    hold(store, VARIANT, "TEST-ITEM-001", "TEST-SKU-001-NEWER")

    code, out, err = run(
        capsys, "bindings", "accept-sku-change", VARIANT, "TEST-SKU-001-NEW"
    )

    assert code == cli.EXIT_FAILURE
    assert out == ""
    assert "409" in err
    assert "TEST-SKU-001-NEWER" in err
    assert audit_rows(store) == []


def test_accept_sku_change_posts_the_variant_and_the_sku(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    canned: tuple[Canned, str],
) -> None:
    state, url = canned
    state.answer(
        200,
        {
            "environment": "staging",
            "variant_id": VARIANT,
            "bound_item_code": "TEST-ITEM-001",
            "accepted_sku": "TEST-SKU-001-NEW",
        },
    )
    configure(monkeypatch, tmp_path, url)

    code, _, err = run(
        capsys, "bindings", "accept-sku-change", VARIANT, "TEST-SKU-001-NEW"
    )

    assert code == 0, err
    assert state.posted == [
        (
            "/bindings/accept-sku-change",
            {"variant": VARIANT, "sku": "TEST-SKU-001-NEW"},
        )
    ]


def test_a_mutation_posts_its_body_with_the_token_and_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    canned: tuple[Canned, str],
) -> None:
    state, url = canned
    state.answer(200, {"environment": "staging", "dry_run": False, "changed": True})
    configure(monkeypatch, tmp_path, url)

    code, _, err = run(capsys, "dry-run", "off")

    assert code == 0, err
    assert state.posted == [("/dry-run", {"on": False})]
    (headers,) = state.received
    assert headers[AUTH_HEADER] == f"Bearer {OPERATOR_TOKEN}"
    assert headers[ENVIRONMENT_HEADER] == "staging"


@pytest.mark.parametrize(
    "argv",
    [
        ["dry-run", "off"],
        ["bindings", "accept-sku-change", VARIANT, "TEST-SKU-001-NEW"],
        ["push", VARIANT],
        ["push", "--all"],
        ["rerun", f"unbound_sku:{VARIANT}"],
    ],
)
def test_a_mutation_on_production_needs_env_on_the_command_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    canned: tuple[Canned, str],
    argv: list[str],
) -> None:
    state, url = canned
    token_file = configure(monkeypatch, tmp_path, url, "production")
    # The refusal comes before the token is read, so a missing file is not
    # what stops the command.
    token_file.unlink()
    monkeypatch.setenv("CONNECTOR_ENV", "production")

    code, out, err = run(capsys, *argv)

    assert code == cli.EXIT_FAILURE
    assert out == ""
    assert "--env production" in err
    assert str(token_file) not in err
    assert state.received == []


def test_a_mutation_on_production_runs_with_env_on_the_command_line(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    canned: tuple[Canned, str],
) -> None:
    state, url = canned
    state.answer(200, {"environment": "production", "dry_run": False, "changed": True})
    configure(monkeypatch, tmp_path, url, "production")

    code, out, err = run(capsys, "--env", "production", "dry-run", "off")

    assert code == 0, err
    assert "Environment: production" in out
    assert state.posted == [("/dry-run", {"on": False})]


def test_reads_on_production_need_no_env_option(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    canned: tuple[Canned, str],
) -> None:
    state, url = canned
    state.answer(200, {"environment": "production"} | STATUS)
    configure(monkeypatch, tmp_path, url, "production")
    monkeypatch.setenv("CONNECTOR_ENV", "production")

    code, _, err = run(capsys, "status")

    assert code == 0, err


def test_a_mutation_refused_while_a_pass_runs_says_to_retry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    canned: tuple[Canned, str],
) -> None:
    state, url = canned
    state.answer(
        503,
        {"environment": "staging", "error": "a pass is running; retry the command"},
    )
    configure(monkeypatch, tmp_path, url)

    code, out, err = run(capsys, "dry-run", "off")

    assert code == cli.EXIT_FAILURE
    assert out == ""
    assert "503" in err
    assert "retry" in err


def test_a_mutation_outwaits_the_connectors_wait_for_a_pass(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    timeouts: list[float] = []

    def post(
        url: str,
        *,
        json: Mapping[str, object],
        headers: Mapping[str, str],
        timeout: float,
    ) -> httpx.Response:
        timeouts.append(timeout)
        answer = {"environment": "staging", "dry_run": False, "changed": True}
        return httpx.Response(200, json=answer, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", post)
    configure(monkeypatch, tmp_path, "http://connector.example.com")

    code, _, err = run(capsys, "dry-run", "off")

    assert code == 0, err
    (timeout,) = timeouts
    assert timeout > WRITER_LOCK_TIMEOUT_SECONDS


def test_a_command_that_waits_for_a_pass_outwaits_the_connector() -> None:
    assert cli.WAITING_MUTATION_TIMEOUT_SECONDS > (
        WRITER_LOCK_TIMEOUT_SECONDS + PASS_WAIT_TIMEOUT_SECONDS
    )


@pytest.mark.parametrize(
    ("argv", "answer"),
    [
        (["push", "--all"], {"item_count": 1, "still_dirty": 0}),
        (["rerun", f"unbound_sku:{VARIANT}"], {"key": f"unbound_sku:{VARIANT}"}),
    ],
)
def test_push_and_rerun_wait_as_long_as_the_connector(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
    answer: dict[str, object],
) -> None:
    configure(monkeypatch, tmp_path, "http://connector.example.com")
    timeouts: list[object] = []

    def post(url: str, **kwargs: object) -> httpx.Response:
        timeouts.append(kwargs["timeout"])
        document = {"environment": "staging", "pass": "not finished"} | answer
        return httpx.Response(200, json=document)

    monkeypatch.setattr(httpx, "post", post)

    code, _, err = run(capsys, *argv)

    assert code == 0, err
    assert timeouts == [cli.WAITING_MUTATION_TIMEOUT_SECONDS]


@pytest.mark.usefixtures("connected")
def test_push_reports_the_decision_and_newest_push(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    set_dry_run(store, False)
    bind_and_decide(store, VARIANT)

    code, out, err = run(capsys, "push", VARIANT)

    assert code == 0, err
    lines = out.splitlines()
    assert lines[:3] == [
        "Environment: staging",
        f"Variant: {VARIANT} (item TEST-ITEM-001)",
        "Last decision: item TEST-ITEM-001, action pushed",
    ]
    assert f"  Decided at: {utc_timestamp(NOW)}" in lines
    assert lines[-1] == f"Newest push: {utc_timestamp(NOW)}  3 (from 2)  pushed"
    assert "Dry run" not in out
    assert audit_rows(store) == [("alice", "push")]


@pytest.mark.usefixtures("connected")
def test_push_in_dry_run_says_nothing_was_pushed(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    bind_and_decide(store, VARIANT)

    code, out, err = run(capsys, "push", VARIANT)

    assert code == 0, err
    assert "Dry run is on: computed, nothing pushed." in out.splitlines()


@pytest.mark.usefixtures("connected")
def test_push_all_reports_the_items_marked_and_left(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    set_dry_run(store, False)
    bind_and_decide(store, VARIANT)

    code, out, err = run(capsys, "push", "--all")

    assert code == 0, err
    assert out.splitlines() == [
        "Environment: staging",
        "Marked 1 bound item for a push.",
        "After the pass, 1 still waits to be pushed.",
    ]
    assert audit_rows(store) == [("alice", "push")]


@pytest.mark.parametrize(
    ("left", "line"),
    [
        (0, "After the pass, none waits to be pushed."),
        (2, "After the pass, 2 still wait to be pushed."),
    ],
)
def test_push_all_reports_how_many_still_wait(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    canned: tuple[Canned, str],
    left: int,
    line: str,
) -> None:
    state, url = canned
    state.answer(
        200,
        {
            "environment": "staging",
            "all": True,
            "item_count": 3,
            "still_dirty": left,
            "dry_run": False,
            "pass": "completed",
        },
    )
    configure(monkeypatch, tmp_path, url)

    code, out, err = run(capsys, "push", "--all")

    assert code == 0, err
    assert out.splitlines() == [
        "Environment: staging",
        "Marked 3 bound items for a push.",
        line,
    ]


@pytest.mark.usefixtures("connected")
def test_push_as_json(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    bind_and_decide(store, VARIANT)

    code, out, _ = run(capsys, "push", VARIANT, "--json")

    assert code == 0
    document = json.loads(out)
    assert document["item_code"] == "TEST-ITEM-001"
    assert document["pass"] == "completed"


@pytest.mark.usefixtures("connected")
def test_push_of_an_unknown_variant_is_reported(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, "push", VARIANT)

    assert code == cli.EXIT_FAILURE
    assert out == ""
    assert "404" in err
    assert audit_rows(store) == []


@pytest.mark.parametrize("argv", [["push"], ["push", VARIANT, "--all"]])
def test_push_takes_a_variant_or_all(
    capsys: pytest.CaptureFixture[str], argv: list[str]
) -> None:
    with pytest.raises(SystemExit) as exit:
        cli.main(argv)

    assert exit.value.code == USAGE_ERROR


@pytest.mark.parametrize(
    ("argv", "answer", "advice"),
    [
        (
            ["push", VARIANT],
            {"variant_id": VARIANT, "item_code": "TEST-ITEM-001"},
            f"Check `record {VARIANT}` later.",
        ),
        (
            ["push", "--all"],
            {"all": True, "item_count": 2, "still_dirty": 2},
            "The items stay marked for the next pass.",
        ),
        (
            ["rerun", f"unbound_sku:{VARIANT}"],
            {"key": f"unbound_sku:{VARIANT}"},
            f"Check `problems show unbound_sku:{VARIANT}` later.",
        ),
    ],
)
def test_a_pass_that_did_not_end_in_time_is_reported_as_pending(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    canned: tuple[Canned, str],
    argv: list[str],
    answer: dict[str, object],
    advice: str,
) -> None:
    state, url = canned
    document = {"environment": "staging", "dry_run": False, "pass": "not finished"}
    state.answer(200, document | answer)
    configure(monkeypatch, tmp_path, url)

    code, out, err = run(capsys, *argv)

    assert code == 0, err
    lines = out.splitlines()
    assert "did not end within the connector's wait" in lines[-2]
    assert lines[-1] == advice


@pytest.mark.parametrize(
    ("argv", "answer", "outcome"),
    [
        (
            ["push", VARIANT],
            {"variant_id": VARIANT, "item_code": "TEST-ITEM-001"},
            "The pass ended early; the push may not have happened.",
        ),
        (
            ["push", "--all"],
            {"all": True, "item_count": 2, "still_dirty": 2},
            "The pass ended early; the pushes may not have happened.",
        ),
        (
            ["rerun", f"unbound_sku:{VARIANT}"],
            {"key": f"unbound_sku:{VARIANT}", "problem": {"resolved_at": None}},
            "The full pass ended early; the problem was not rechecked.",
        ),
    ],
)
def test_a_pass_that_ended_early_is_reported_as_such(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    canned: tuple[Canned, str],
    argv: list[str],
    answer: dict[str, object],
    outcome: str,
) -> None:
    state, url = canned
    document = {"environment": "staging", "dry_run": False, "pass": "ended early"}
    state.answer(200, document | answer)
    configure(monkeypatch, tmp_path, url)

    code, out, err = run(capsys, *argv)

    assert code == 0, err
    assert out.splitlines()[-2:] == [
        outcome,
        "The connector's log and `status` show why.",
    ]
    assert "still open" not in out


@pytest.mark.usefixtures("connected")
def test_rerun_reports_a_cleared_problem(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    set_dry_run(store, False)
    unbound(store, VARIANT, "TEST-SKU-001")
    later = NOW + timedelta(minutes=5)
    record_check(store, ProblemKind.UNBOUND_SKU, [], later)

    code, out, err = run(capsys, "rerun", f"unbound_sku:{VARIANT}")

    assert code == 0, err
    assert out.splitlines() == [
        "Environment: staging",
        f"Problem: unbound_sku:{VARIANT}",
        f"After a full pass: cleared at {utc_timestamp(later)}",
    ]
    assert audit_rows(store) == [("alice", "rerun")]


@pytest.mark.usefixtures("connected")
def test_rerun_reports_a_problem_still_open(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    set_dry_run(store, False)
    unbound(store, VARIANT, "TEST-SKU-001")

    code, out, err = run(capsys, "rerun", f"unbound_sku:{VARIANT}")

    assert code == 0, err
    assert out.splitlines()[-1] == "After a full pass: still open"


@pytest.mark.usefixtures("connected")
def test_rerun_in_dry_run_says_nothing_was_pushed(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    unbound(store, VARIANT, "TEST-SKU-001")

    code, out, err = run(capsys, "rerun", f"unbound_sku:{VARIANT}")

    assert code == 0, err
    assert out.splitlines()[-2:] == [
        "After a full pass: still open",
        "Dry run is on: rechecked, nothing pushed.",
    ]


@pytest.mark.usefixtures("connected")
def test_rerun_as_json(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    unbound(store, VARIANT, "TEST-SKU-001")

    code, out, _ = run(capsys, "rerun", f"unbound_sku:{VARIANT}", "--json")

    assert code == 0
    assert json.loads(out)["problem"]["resolved_at"] is None


@pytest.mark.usefixtures("connected")
def test_rerun_of_an_unknown_problem_is_reported(
    store: sqlite3.Connection, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(capsys, "rerun", f"unbound_sku:{VARIANT}")

    assert code == cli.EXIT_FAILURE
    assert out == ""
    assert "404" in err
    assert audit_rows(store) == []
