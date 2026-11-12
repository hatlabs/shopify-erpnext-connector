import os
import signal
import threading
from pathlib import Path

import pytest

from shopify_erpnext_connector.__main__ import install_stop_handlers, main
from shopify_erpnext_connector.store import dry_run, open_store

EXAMPLE_CONFIG = Path(__file__).parent.parent / "config.example.toml"
SECRET_NAMES = [
    "erpnext_api_key",
    "erpnext_api_secret",
    "shopify_access_token",
    "metrics_token",
    "operator_token_alice",
]


def stopped() -> threading.Event:
    stop = threading.Event()
    stop.set()
    return stop


def write_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, store_path: Path
) -> None:
    for name in SECRET_NAMES:
        (tmp_path / name).write_text("placeholder")
    config = tmp_path / "config.toml"
    config.write_text(f'store_path = "{store_path}"\n' + EXAMPLE_CONFIG.read_text())
    monkeypatch.setenv("CONNECTOR_CONFIG", str(config))
    monkeypatch.setenv("CONNECTOR_SECRETS_DIR", str(tmp_path))


def test_missing_config_exits_with_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "config.toml"
    monkeypatch.setenv("CONNECTOR_CONFIG", str(missing))
    monkeypatch.setenv("CONNECTOR_SECRETS_DIR", str(tmp_path))

    assert main(stopped()) == os.EX_CONFIG
    assert str(missing) in capsys.readouterr().err


def test_valid_config_opens_the_store_and_reports_the_pinned_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store_path = tmp_path / "connector.sqlite3"
    write_config(tmp_path, monkeypatch, store_path)

    assert main(stopped()) == os.EX_OK

    err = capsys.readouterr().err
    assert "staging" in err
    assert "example.myshopify.com" in err
    assert "placeholder" not in err
    connection = open_store(store_path)
    assert dry_run(connection) is True
    connection.close()


def test_store_that_cannot_be_opened_exits_with_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store_path = tmp_path / "missing-directory" / "connector.sqlite3"
    write_config(tmp_path, monkeypatch, store_path)

    assert main(stopped()) == os.EX_IOERR
    assert str(store_path) in capsys.readouterr().err


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
