import logging
import os
import signal
import sqlite3
import sys
import threading
from pathlib import Path

import httpx

from shopify_erpnext_connector.api import ApiServer
from shopify_erpnext_connector.config import ConfigError, load_config
from shopify_erpnext_connector.erpnext import ErpNextClient
from shopify_erpnext_connector.shopify import ShopifyClient
from shopify_erpnext_connector.store import StoreError, open_store
from shopify_erpnext_connector.worker import Worker

CONFIG_PATH_VARIABLE = "CONNECTOR_CONFIG"
SECRETS_DIR_VARIABLE = "CONNECTOR_SECRETS_DIR"
DEFAULT_CONFIG_PATH = "/etc/shopify-erpnext-connector/config.toml"
DEFAULT_SECRETS_DIR = "/run/secrets"

CONNECT_TIMEOUT_SECONDS = 10.0
# ERPNext's email `make` sends over SMTP, with an OAuth token fetch, inside
# the request.
READ_TIMEOUT_SECONDS = 60.0


def install_stop_handlers(stop: threading.Event) -> None:
    """Set `stop` on SIGTERM or SIGINT; the worker ends after its current pass."""
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda _signum, _frame: stop.set())


def serve(stop: threading.Event | None = None) -> int:
    """Run the worker and the API until SIGTERM or SIGINT, or until `stop` is set."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
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
    try:
        connection = open_store(config.store_path)
    except (StoreError, sqlite3.Error) as error:
        print(f"cannot open the store {config.store_path}: {error}", file=sys.stderr)
        return os.EX_IOERR

    if stop is None:
        stop = threading.Event()
        install_stop_handlers(stop)

    timeout = httpx.Timeout(READ_TIMEOUT_SECONDS, connect=CONNECT_TIMEOUT_SECONDS)
    try:
        with (
            httpx.Client(
                base_url=f"https://{config.shop_domain}", timeout=timeout
            ) as shopify_http,
            httpx.Client(base_url=config.erpnext_url, timeout=timeout) as erpnext_http,
        ):
            worker = Worker(
                config,
                connection,
                ShopifyClient(config, shopify_http),
                ErpNextClient(config, erpnext_http),
            )
            try:
                api = ApiServer(config, worker)
            except OSError as error:
                print(
                    f"cannot serve the API on port {config.api.port}: {error}",
                    file=sys.stderr,
                )
                return os.EX_OSERR
            api.start()
            try:
                worker.run(stop)
            finally:
                api.close()
    finally:
        connection.close()
    return os.EX_OK
