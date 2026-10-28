import os
import sys
from pathlib import Path

from shopify_erpnext_connector.config import ConfigError, load_config

CONFIG_PATH_VARIABLE = "CONNECTOR_CONFIG"
SECRETS_DIR_VARIABLE = "CONNECTOR_SECRETS_DIR"
DEFAULT_CONFIG_PATH = "/etc/shopify-erpnext-connector/config.toml"
DEFAULT_SECRETS_DIR = "/run/secrets"


def main() -> int:
    config_path = Path(os.environ.get(CONFIG_PATH_VARIABLE, DEFAULT_CONFIG_PATH))
    secrets_dir = Path(os.environ.get(SECRETS_DIR_VARIABLE, DEFAULT_SECRETS_DIR))
    try:
        config = load_config(config_path, secrets_dir)
    except ConfigError as error:
        print(f"configuration error: {error}", file=sys.stderr)
        return os.EX_CONFIG
    print(
        f"configuration loaded: environment {config.environment}, "
        f"shop {config.shop_domain}, ERPNext {config.erpnext_url}",
        file=sys.stderr,
    )
    return os.EX_OK


if __name__ == "__main__":
    sys.exit(main())
