import json
import os
import socket
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import entry_points
from pathlib import Path

import pytest
from helpers import make_config

from shopify_erpnext_connector import cli
from shopify_erpnext_connector.api import ENVIRONMENT_HEADER, ApiServer
from shopify_erpnext_connector.config import ApiConfig, Environment, Secret
from shopify_erpnext_connector.store import open_store

CLI_NAME = "shopify-erpnext-connector"
OPERATOR_TOKEN = "test-operator-token"
# Kept apart from its value so the fixture check's header pattern stays meaningful.
AUTH_HEADER = "Authorization"
STATUS = {
    "dry_run": False,
    "last_pass_at": "2026-11-13T10:00:00.000000Z",
    "last_pass_full": True,
    "push_lag_seconds": 0,
    "open_problems": 0,
    "unsent_emails": 0,
}


@dataclass
class FakeWorker:
    last_pass_at: None = None
    last_pass_full: bool = False
    last_full_pass_at: None = None


@dataclass
class Canned:
    """What the canned connector answers, and the headers it was sent."""

    status: int = 200
    body: bytes = b"{}"
    received: list[dict[str, str]] = field(default_factory=list)

    def answer(self, status: int, document: dict[str, object]) -> None:
        self.status = status
        self.body = json.dumps(document).encode()


@pytest.fixture
def canned() -> Iterator[tuple[Canned, str]]:
    """A connector stand-in answering every GET with a set response."""
    state = Canned()

    class Handler(BaseHTTPRequestHandler):
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
def api(tmp_path: Path) -> Iterator[ApiServer]:
    store_path = tmp_path / "connector.sqlite3"
    open_store(store_path).close()
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

    assert exit.value.code != 0
    assert "configuration" in capsys.readouterr().err


def test_a_command_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit:
        cli.main([])

    assert exit.value.code != 0
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
    assert OPERATOR_TOKEN not in out + err


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
    assert target.explicit is False
    assert target.url == "http://connector.example.com"
    assert target.token.reveal() == OPERATOR_TOKEN


def test_environment_from_the_environment_variable_is_not_explicit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure(monkeypatch, tmp_path, "https://connector.example.com", "production")
    monkeypatch.setenv("CONNECTOR_ENV", "production")

    target = cli.resolve_target(None, os.environ)

    assert target.environment is Environment.PRODUCTION
    assert target.explicit is False


def test_env_option_overrides_the_variable_and_is_explicit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    configure(monkeypatch, tmp_path, "https://connector.example.com", "production")
    monkeypatch.setenv("CONNECTOR_ENV", "staging")

    target = cli.resolve_target("production", os.environ)

    assert target.environment is Environment.PRODUCTION
    assert target.explicit is True


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
    assert url in err
