"""Connector configuration: one non-secret TOML file and a directory of secrets.

Each secret is one file in the secrets directory. Secrets are never read from
environment variables, so a stray variable cannot point an environment at the
wrong credentials.
"""

import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

OPERATOR_TOKEN_PREFIX = "operator_token_"

_SHOP_DOMAIN = re.compile(r"(?:[a-z0-9-]+\.)+myshopify\.com")
_LOCATION_ID = re.compile(r"gid://shopify/Location/[0-9]+")
_OPERATOR_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*")
_ERPNEXT_SCHEMES = ("http", "https")

_Schema = Mapping[str, "type | _Schema"]
_SCHEMA: _Schema = {
    "environment": str,
    "operators": list,
    "shopify": {"shop_domain": str, "location_id": str},
    "erpnext": {"url": str},
}


class ConfigError(Exception):
    """The configuration or a secret file is missing or invalid."""


class Environment(StrEnum):
    STAGING = "staging"
    PRODUCTION = "production"


class Secret:
    """A secret value whose repr and str never show it."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Secret('**********')"

    __str__ = __repr__


@dataclass(frozen=True)
class Secrets:
    erpnext_api_key: Secret
    erpnext_api_secret: Secret
    shopify_access_token: Secret
    metrics_token: Secret
    operator_tokens: Mapping[str, Secret]

    @classmethod
    def read(cls, directory: Path, operators: Sequence[str]) -> "Secrets":
        return cls(
            erpnext_api_key=_read_secret(directory, "erpnext_api_key"),
            erpnext_api_secret=_read_secret(directory, "erpnext_api_secret"),
            shopify_access_token=_read_secret(directory, "shopify_access_token"),
            metrics_token=_read_secret(directory, "metrics_token"),
            operator_tokens={
                name: _read_secret(directory, OPERATOR_TOKEN_PREFIX + name)
                for name in operators
            },
        )


@dataclass(frozen=True)
class Config:
    environment: Environment
    shop_domain: str
    location_id: str
    erpnext_url: str
    secrets: Secrets


def load_config(config_path: Path, secrets_dir: Path) -> Config:
    document = _Document(config_path, _read_toml(config_path))
    document.reject_unknown_fields(document.data, _SCHEMA, prefix="")

    environment_name = document.string("environment")
    try:
        environment = Environment(environment_name)
    except ValueError:
        allowed = ", ".join(e.value for e in Environment)
        raise document.error(
            f"'environment' must be one of {allowed}, not {environment_name!r}"
        ) from None

    shop_domain = document.string("shopify.shop_domain")
    if not _SHOP_DOMAIN.fullmatch(shop_domain):
        raise document.error(
            "'shopify.shop_domain' must be a bare *.myshopify.com host name"
        )

    location_id = document.string("shopify.location_id")
    if not _LOCATION_ID.fullmatch(location_id):
        raise document.error(
            "'shopify.location_id' must be a Shopify location GID "
            "(gid://shopify/Location/<number>)"
        )

    erpnext_url = document.string("erpnext.url")
    parts = urlsplit(erpnext_url)
    if (
        parts.scheme not in _ERPNEXT_SCHEMES
        or not parts.hostname
        or parts.path not in ("", "/")
        or parts.query
        or parts.fragment
    ):
        raise document.error("'erpnext.url' must be an http(s) base URL")

    return Config(
        environment=environment,
        shop_domain=shop_domain,
        location_id=location_id,
        erpnext_url=erpnext_url.rstrip("/"),
        secrets=Secrets.read(secrets_dir, document.operators()),
    )


class _Document:
    def __init__(self, path: Path, data: Mapping[str, object]) -> None:
        self.path = path
        self.data = data

    def error(self, message: str) -> ConfigError:
        return ConfigError(f"{self.path}: {message}")

    def reject_unknown_fields(
        self, table: Mapping[str, object], schema: _Schema, prefix: str
    ) -> None:
        for key, value in table.items():
            dotted = prefix + key
            expected = schema.get(key)
            if expected is None:
                raise self.error(f"unknown field '{dotted}'")
            if isinstance(expected, Mapping):
                if not isinstance(value, Mapping):
                    raise self.error(f"'{dotted}' must be a table")
                self.reject_unknown_fields(value, expected, prefix=dotted + ".")

    def string(self, dotted: str) -> str:
        table: Mapping[str, object] = self.data
        *parents, key = dotted.split(".")
        for parent in parents:
            child = table.get(parent)
            if not isinstance(child, Mapping):
                raise self.error(f"missing required field '{dotted}'")
            table = child
        value = table.get(key)
        if value is None:
            raise self.error(f"missing required field '{dotted}'")
        if not isinstance(value, str) or not value:
            raise self.error(f"'{dotted}' must be a non-empty string")
        return value

    def operators(self) -> list[str]:
        names = self.data.get("operators", [])
        if not isinstance(names, list) or not all(
            isinstance(name, str) and _OPERATOR_NAME.fullmatch(name) for name in names
        ):
            raise self.error(
                "'operators' must be a list of names of lowercase letters, "
                "digits, '_' and '-'"
            )
        return [str(name) for name in names]


def _read_toml(path: Path) -> Mapping[str, object]:
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except OSError as error:
        raise ConfigError(f"cannot read config file {path}: {error.strerror}") from None
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: invalid TOML: {error}") from None


def _read_secret(directory: Path, name: str) -> Secret:
    path = directory / name
    # Errors name the file only: an OS or decode error message can quote content.
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise ConfigError(f"missing secret file {path}") from None
    except OSError as error:
        raise ConfigError(f"cannot read secret file {path}: {error.strerror}") from None
    except UnicodeDecodeError:
        raise ConfigError(f"secret file {path} is not valid UTF-8") from None
    value = text.rstrip("\r\n")
    if not value:
        raise ConfigError(f"secret file {path} is empty")
    return Secret(value)
