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
    "location_id",
    ["1234567890", "gid://shopify/Product/1", "gid://shopify/Location/abc"],
)
def test_location_id_must_be_a_location_gid(
    config_path: Path, secrets_dir: Path, location_id: str
) -> None:
    config_path.write_text(CONFIG_TOML.replace(LOCATION_ID, location_id))

    with pytest.raises(ConfigError, match="shopify.location_id"):
        load_config(config_path, secrets_dir)


@pytest.mark.parametrize(
    "url",
    [
        "https://",
        "https://erpnext.example.com/app",
        "https://erpnext.example.com/?a=1",
        "https://erpnext.example.com/#x",
        "http://[::1",
        "https://erpnext.example.com:http",
        "https://erpnext.example.com:99999",
    ],
)
def test_erpnext_url_must_be_a_base_url(
    config_path: Path, secrets_dir: Path, url: str
) -> None:
    config_path.write_text(CONFIG_TOML.replace(ERPNEXT_URL, url))

    with pytest.raises(ConfigError, match="erpnext.url"):
        load_config(config_path, secrets_dir)


def test_erpnext_url_trailing_slash_is_dropped(
    config_path: Path, secrets_dir: Path
) -> None:
    config_path.write_text(CONFIG_TOML.replace(ERPNEXT_URL, ERPNEXT_URL + "/"))

    assert load_config(config_path, secrets_dir).erpnext_url == ERPNEXT_URL


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


@pytest.mark.parametrize(
    ("original", "replacement"),
    [
        (f'location_id = "{LOCATION_ID}"', "location_id = 1234567890"),
        ('environment = "staging"', 'environment = ""'),
    ],
)
def test_field_that_is_not_a_non_empty_string_is_rejected(
    config_path: Path, secrets_dir: Path, original: str, replacement: str
) -> None:
    config_path.write_text(CONFIG_TOML.replace(original, replacement))

    with pytest.raises(ConfigError, match="must be a non-empty string"):
        load_config(config_path, secrets_dir)


def test_table_given_as_a_string_is_rejected(
    config_path: Path, secrets_dir: Path
) -> None:
    shopify_table = (
        f'[shopify]\nshop_domain = "{SHOP_DOMAIN}"\nlocation_id = "{LOCATION_ID}"\n'
    )
    config_path.write_text('shopify = "x"\n' + CONFIG_TOML.replace(shopify_table, ""))

    with pytest.raises(ConfigError, match="'shopify' must be a table"):
        load_config(config_path, secrets_dir)


def test_operators_given_as_a_string_is_rejected(
    config_path: Path, secrets_dir: Path
) -> None:
    config_path.write_text(CONFIG_TOML.replace('["alice"]', '"alice"'))

    with pytest.raises(ConfigError, match="operators"):
        load_config(config_path, secrets_dir)


def test_config_without_operators_has_no_operator_tokens(
    config_path: Path, secrets_dir: Path
) -> None:
    config_path.write_text(CONFIG_TOML.replace('operators = ["alice"]\n', ""))

    assert load_config(config_path, secrets_dir).secrets.operator_tokens == {}


def test_secret_that_is_not_utf8_names_the_file_without_its_content(
    config_path: Path, secrets_dir: Path
) -> None:
    (secrets_dir / "erpnext_api_key").write_bytes(b"\xff\xfeleaky-secret-value")

    with pytest.raises(ConfigError) as excinfo:
        load_config(config_path, secrets_dir)

    message = str(excinfo.value)
    assert str(secrets_dir / "erpnext_api_key") in message
    assert "leaky-secret-value" not in message


def test_secret_path_that_is_a_directory_cannot_be_read(
    config_path: Path, secrets_dir: Path
) -> None:
    (secrets_dir / "metrics_token").unlink()
    (secrets_dir / "metrics_token").mkdir()

    with pytest.raises(ConfigError, match="cannot read secret file"):
        load_config(config_path, secrets_dir)


def test_missing_config_file_names_the_path(tmp_path: Path, secrets_dir: Path) -> None:
    missing = tmp_path / "absent.toml"

    with pytest.raises(ConfigError, match=str(missing)):
        load_config(missing, secrets_dir)


def test_invalid_toml_names_the_path(config_path: Path, secrets_dir: Path) -> None:
    config_path.write_text("environment = \n")

    with pytest.raises(ConfigError, match=str(config_path)):
        load_config(config_path, secrets_dir)


def test_config_that_is_not_utf8_names_the_path(
    config_path: Path, secrets_dir: Path
) -> None:
    config_path.write_bytes(b'# K\xe4ytt\xf6\nenvironment = "staging"\n')

    with pytest.raises(ConfigError, match=f"{config_path} is not valid UTF-8"):
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
