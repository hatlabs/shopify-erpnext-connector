import os
from pathlib import Path

import pytest

from shopify_erpnext_connector.__main__ import main

EXAMPLE_CONFIG = Path(__file__).parent.parent / "config.example.toml"


def test_missing_config_exits_with_config_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "config.toml"
    monkeypatch.setenv("CONNECTOR_CONFIG", str(missing))
    monkeypatch.setenv("CONNECTOR_SECRETS_DIR", str(tmp_path))

    assert main() == os.EX_CONFIG
    assert str(missing) in capsys.readouterr().err


def test_valid_config_reports_the_pinned_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    for name in [
        "erpnext_api_key",
        "erpnext_api_secret",
        "shopify_access_token",
        "metrics_token",
        "operator_token_alice",
    ]:
        (tmp_path / name).write_text("placeholder")
    monkeypatch.setenv("CONNECTOR_CONFIG", str(EXAMPLE_CONFIG))
    monkeypatch.setenv("CONNECTOR_SECRETS_DIR", str(tmp_path))

    assert main() == os.EX_OK
    err = capsys.readouterr().err
    assert "staging" in err
    assert "example.myshopify.com" in err
    assert "placeholder" not in err
