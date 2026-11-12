import os
from importlib.metadata import entry_points
from pathlib import Path

import pytest

from shopify_erpnext_connector import cli

CLI_NAME = "shopify-erpnext-connector"


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


def test_a_command_is_required(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit:
        cli.main([])

    assert exit.value.code != 0
    assert "serve" in capsys.readouterr().err
