from pathlib import Path

import pytest

from shopify_erpnext_connector.config import (
    ConfigError,
    Environment,
    Secret,
    load_config,
)

SHOP_DOMAIN = "example.myshopify.com"
LOCATION_ID = "gid://shopify/Location/1234567890"
ERPNEXT_URL = "https://erpnext.example.com"

CONFIG_TOML = f"""\
environment = "staging"
operators = ["alice"]

[shopify]
shop_domain = "{SHOP_DOMAIN}"
location_id = "{LOCATION_ID}"

[erpnext]
url = "{ERPNEXT_URL}"
"""

SECRET_VALUES = {
    "erpnext_api_key": "test-erpnext-key",
    "erpnext_api_secret": "test-erpnext-secret",
    "shopify_access_token": "test-shopify-token",
    "metrics_token": "test-metrics-token",
    "operator_token_alice": "test-operator-token",
}


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(CONFIG_TOML)
    return path


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "secrets"
    directory.mkdir()
    for name, value in SECRET_VALUES.items():
        (directory / name).write_text(value)
    return directory


def test_config_and_secrets_pin_the_environment(
    config_path: Path, secrets_dir: Path
) -> None:
    config = load_config(config_path, secrets_dir)

    assert config.environment is Environment.STAGING
    assert config.shop_domain == SHOP_DOMAIN
    assert config.location_id == LOCATION_ID
    assert config.erpnext_url == ERPNEXT_URL
    assert config.secrets.erpnext_api_key.reveal() == "test-erpnext-key"
    assert config.secrets.erpnext_api_secret.reveal() == "test-erpnext-secret"
    assert config.secrets.shopify_access_token.reveal() == "test-shopify-token"
    assert config.secrets.metrics_token.reveal() == "test-metrics-token"
    assert {
        name: token.reveal() for name, token in config.secrets.operator_tokens.items()
    } == {"alice": "test-operator-token"}


def test_production_environment_is_accepted(
    config_path: Path, secrets_dir: Path
) -> None:
    config_path.write_text(
        CONFIG_TOML.replace('environment = "staging"', 'environment = "production"')
    )

    assert load_config(config_path, secrets_dir).environment is Environment.PRODUCTION


def test_missing_secret_file_names_the_file_without_any_value(
    config_path: Path, secrets_dir: Path
) -> None:
    (secrets_dir / "shopify_access_token").unlink()

    with pytest.raises(ConfigError) as excinfo:
        load_config(config_path, secrets_dir)

    message = str(excinfo.value)
    assert str(secrets_dir / "shopify_access_token") in message
    for value in SECRET_VALUES.values():
        assert value not in message


def test_missing_operator_token_file_names_the_file(
    config_path: Path, secrets_dir: Path
) -> None:
    (secrets_dir / "operator_token_alice").unlink()

    with pytest.raises(ConfigError, match="operator_token_alice"):
        load_config(config_path, secrets_dir)


def test_secret_in_environment_variable_is_ignored(
    config_path: Path, secrets_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHOPIFY_ACCESS_TOKEN", "token-from-env")
    monkeypatch.setenv("shopify_access_token", "token-from-env")

    config = load_config(config_path, secrets_dir)
    assert config.secrets.shopify_access_token.reveal() == "test-shopify-token"

    (secrets_dir / "shopify_access_token").unlink()
    with pytest.raises(ConfigError, match="shopify_access_token"):
        load_config(config_path, secrets_dir)


@pytest.mark.parametrize("ending", ["\n", "\r\n"])
def test_trailing_newline_is_stripped_from_secret(
    config_path: Path, secrets_dir: Path, ending: str
) -> None:
    (secrets_dir / "erpnext_api_key").write_text(f"key with spaces{ending}")

    config = load_config(config_path, secrets_dir)

    assert config.secrets.erpnext_api_key.reveal() == "key with spaces"


def test_empty_secret_file_is_rejected(config_path: Path, secrets_dir: Path) -> None:
    (secrets_dir / "metrics_token").write_text("\n")

    with pytest.raises(ConfigError, match="metrics_token"):
        load_config(config_path, secrets_dir)


@pytest.mark.parametrize(
    ("removed_line", "field"),
    [
        ('environment = "staging"\n', "environment"),
        (f'shop_domain = "{SHOP_DOMAIN}"\n', "shopify.shop_domain"),
        (f'location_id = "{LOCATION_ID}"\n', "shopify.location_id"),
        (f'url = "{ERPNEXT_URL}"\n', "erpnext.url"),
    ],
)
def test_missing_mandatory_field_fails_naming_it(
    config_path: Path, secrets_dir: Path, removed_line: str, field: str
) -> None:
    config_path.write_text(CONFIG_TOML.replace(removed_line, ""))

    with pytest.raises(ConfigError, match=f"'{field}'"):
        load_config(config_path, secrets_dir)


def test_unknown_environment_name_is_rejected(
    config_path: Path, secrets_dir: Path
) -> None:
    config_path.write_text(
        CONFIG_TOML.replace('environment = "staging"', 'environment = "prod"')
    )

    with pytest.raises(ConfigError, match="environment"):
        load_config(config_path, secrets_dir)


def test_unknown_field_is_rejected(config_path: Path, secrets_dir: Path) -> None:
    config_path.write_text(CONFIG_TOML + 'shop_domian = "typo.example.com"\n')

    with pytest.raises(ConfigError, match="shop_domian"):
        load_config(config_path, secrets_dir)


@pytest.mark.parametrize(
    "shop_domain",
    ["https://example.myshopify.com", "example.com", "example.myshopify.com/admin"],
)
def test_shop_domain_must_be_a_bare_myshopify_host(
    config_path: Path, secrets_dir: Path, shop_domain: str
) -> None:
    config_path.write_text(CONFIG_TOML.replace(SHOP_DOMAIN, shop_domain))

    with pytest.raises(ConfigError, match="shopify.shop_domain"):
        load_config(config_path, secrets_dir)


@pytest.mark.parametrize("url", ["erpnext.example.com", "ftp://erpnext.example.com"])
def test_erpnext_url_must_be_http_or_https(
    config_path: Path, secrets_dir: Path, url: str
) -> None:
    config_path.write_text(CONFIG_TOML.replace(ERPNEXT_URL, url))

    with pytest.raises(ConfigError, match="erpnext.url"):
        load_config(config_path, secrets_dir)


@pytest.mark.parametrize(
    "url",
    [
        "https://admin-u5er:s3cr3t-pw@erpnext.example.com",
        "https://admin-u5er@erpnext.example.com",
        "https://:s3cr3t-pw@erpnext.example.com",
    ],
)
def test_erpnext_url_with_credentials_is_rejected_without_echoing_them(
    config_path: Path, secrets_dir: Path, url: str
) -> None:
    config_path.write_text(CONFIG_TOML.replace(ERPNEXT_URL, url))

    with pytest.raises(ConfigError, match="erpnext.url") as excinfo:
        load_config(config_path, secrets_dir)

    assert "s3cr3t-pw" not in str(excinfo.value)
    assert "admin-u5er" not in str(excinfo.value)


def test_operator_name_cannot_escape_the_secrets_directory(
    config_path: Path, secrets_dir: Path
) -> None:
    config_path.write_text(CONFIG_TOML.replace('["alice"]', '["../alice"]'))

    with pytest.raises(ConfigError, match="operators"):
        load_config(config_path, secrets_dir)


def test_missing_config_file_names_the_path(tmp_path: Path, secrets_dir: Path) -> None:
    missing = tmp_path / "absent.toml"

    with pytest.raises(ConfigError, match=str(missing)):
        load_config(missing, secrets_dir)


def test_invalid_toml_names_the_path(config_path: Path, secrets_dir: Path) -> None:
    config_path.write_text("environment = \n")

    with pytest.raises(ConfigError, match=str(config_path)):
        load_config(config_path, secrets_dir)


def test_secret_repr_and_str_never_show_the_value() -> None:
    secret = Secret("hunter2")

    assert "hunter2" not in repr(secret)
    assert "hunter2" not in str(secret)
    assert "hunter2" not in f"{secret}"


def test_config_repr_never_shows_secret_values(
    config_path: Path, secrets_dir: Path
) -> None:
    config_repr = repr(load_config(config_path, secrets_dir))

    for value in SECRET_VALUES.values():
        assert value not in config_repr


def test_example_config_loads(tmp_path: Path) -> None:
    example = Path(__file__).parent.parent / "config.example.toml"
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    for name in [
        "erpnext_api_key",
        "erpnext_api_secret",
        "shopify_access_token",
        "metrics_token",
        "operator_token_alice",
    ]:
        (secrets / name).write_text("placeholder")

    assert load_config(example, secrets).shop_domain == "example.myshopify.com"
