import os
import signal
import socket
import threading
from pathlib import Path

import httpx
import pytest

from shopify_erpnext_connector.api import ENVIRONMENT_HEADER
from shopify_erpnext_connector.config import DEFAULT_API_PORT
from shopify_erpnext_connector.service import install_stop_handlers, serve
from shopify_erpnext_connector.store import dry_run, open_store
from shopify_erpnext_connector.worker import Worker

EXAMPLE_CONFIG = Path(__file__).parent.parent / "config.example.toml"
SECRET_NAMES = [
    "erpnext_api_key",
    "erpnext_api_secret",
    "shopify_access_token",
    "metrics_token",
    "operator_token_alice",
]
SECRET_VALUE = "placeholder"
# Kept apart from its value so the fixture check's header pattern stays meaningful.
AUTH_HEADER = "Authorization"


def stopped() -> threading.Event:
    stop = threading.Event()
    stop.set()
    return stop


@pytest.fixture
def api_port() -> int:
    """A port free when the test starts; the test's server binds it again."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return port


def write_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    store_path: Path,
    api_port: int,
) -> None:
    for name in SECRET_NAMES:
        (tmp_path / name).write_text(SECRET_VALUE)
    example = EXAMPLE_CONFIG.read_text()
    default_port = f"port = {DEFAULT_API_PORT}"
    assert default_port in example
    config = tmp_path / "config.toml"
    config.write_text(
        f'store_path = "{store_path}"\n'
        + example.replace(default_port, f"port = {api_port}")
    )
    monkeypatch.setenv("CONNECTOR_CONFIG", str(config))
    monkeypatch.setenv("CONNECTOR_SECRETS_DIR", str(tmp_path))


def test_missing_config_exits_with_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "config.toml"
    monkeypatch.setenv("CONNECTOR_CONFIG", str(missing))
    monkeypatch.setenv("CONNECTOR_SECRETS_DIR", str(tmp_path))

    assert serve(stopped()) == os.EX_CONFIG
    assert str(missing) in capsys.readouterr().err


def test_valid_config_opens_the_store_and_reports_the_pinned_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    api_port: int,
) -> None:
    store_path = tmp_path / "connector.sqlite3"
    write_config(tmp_path, monkeypatch, store_path, api_port)

    assert serve(stopped()) == os.EX_OK

    err = capsys.readouterr().err
    assert "staging" in err
    assert "example.myshopify.com" in err
    assert "placeholder" not in err
    connection = open_store(store_path)
    assert dry_run(connection) is True
    connection.close()


def test_store_that_cannot_be_opened_exits_with_an_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    api_port: int,
) -> None:
    store_path = tmp_path / "missing-directory" / "connector.sqlite3"
    write_config(tmp_path, monkeypatch, store_path, api_port)

    assert serve(stopped()) == os.EX_IOERR
    assert str(store_path) in capsys.readouterr().err


def test_api_serves_while_the_worker_runs_and_stops_with_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, api_port: int
) -> None:
    write_config(tmp_path, monkeypatch, tmp_path / "connector.sqlite3", api_port)
    base_url = f"http://127.0.0.1:{api_port}"
    statuses: list[int] = []

    def run(self: Worker, stop: threading.Event) -> None:
        response = httpx.get(
            base_url + "/status",
            headers={
                AUTH_HEADER: f"Bearer {SECRET_VALUE}",
                ENVIRONMENT_HEADER: "staging",
            },
        )
        statuses.append(response.status_code)

    monkeypatch.setattr(Worker, "run", run)

    assert serve(stopped()) == os.EX_OK

    assert statuses == [200]
    with pytest.raises(httpx.ConnectError):
        httpx.get(base_url + "/status")


def test_api_port_in_use_exits_with_an_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    api_port: int,
) -> None:
    write_config(tmp_path, monkeypatch, tmp_path / "connector.sqlite3", api_port)
    with socket.socket() as taken:
        taken.bind(("", api_port))
        taken.listen()

        assert serve(stopped()) == os.EX_OSERR

    assert str(api_port) in capsys.readouterr().err


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT])
def test_stop_signals_set_the_stop_event(signum: signal.Signals) -> None:
    previous = signal.getsignal(signum)
    stop = threading.Event()
    try:
        install_stop_handlers(stop)
        signal.raise_signal(signum)
    finally:
        signal.signal(signum, previous)

    assert stop.is_set()
