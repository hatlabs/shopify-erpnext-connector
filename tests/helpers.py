"""Shared test configuration: placeholder hosts, addresses and secrets."""

import threading
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from shopify_erpnext_connector.config import (
    DEFAULT_API_PORT,
    DEFAULT_STORE_PATH,
    ApiConfig,
    Config,
    EmailConfig,
    Environment,
    Secret,
    Secrets,
    WorkerConfig,
)
from shopify_erpnext_connector.worker import PassResult

SHOP_DOMAIN = "example.myshopify.com"
LOCATION_ID = "gid://shopify/Location/1234567890"
ERPNEXT_URL = "https://erpnext.example.com"
WAREHOUSE = "Stores - TEST"
POLL_INTERVAL = timedelta(seconds=5)
PUSH_STALE_AFTER = timedelta(minutes=30)
ERPNEXT_API_KEY = "test-erpnext-key"
ERPNEXT_API_SECRET = "test-erpnext-secret"
SHOPIFY_ACCESS_TOKEN = "test-shopify-token"
EMAIL_RECIPIENT = "operator@example.com"
EMAIL_SENDER = "notifications@example.com"


def make_config(
    *,
    environment: Environment = Environment.STAGING,
    email_enabled: bool = False,
) -> Config:
    return Config(
        environment=environment,
        shop_domain=SHOP_DOMAIN,
        location_id=LOCATION_ID,
        erpnext_url=ERPNEXT_URL,
        warehouse=WAREHOUSE,
        email=EmailConfig(
            enabled=email_enabled,
            recipient=EMAIL_RECIPIENT,
            sender=EMAIL_SENDER,
        ),
        worker=WorkerConfig(
            poll_interval=POLL_INTERVAL, push_stale_after=PUSH_STALE_AFTER
        ),
        api=ApiConfig(port=DEFAULT_API_PORT),
        store_path=Path(DEFAULT_STORE_PATH),
        secrets=Secrets(
            erpnext_api_key=Secret(ERPNEXT_API_KEY),
            erpnext_api_secret=Secret(ERPNEXT_API_SECRET),
            shopify_access_token=Secret(SHOPIFY_ACCESS_TOKEN),
            metrics_token=Secret("test-metrics-token"),
            operator_tokens={},
        ),
    )


@dataclass(frozen=True)
class PassRequest:
    full: bool
    items: tuple[str, ...]


@dataclass
class FakeWorker:
    """The worker state the API reads, set by hand."""

    last_pass_at: datetime | None = None
    last_pass_full: bool = False
    last_full_pass_at: datetime | None = None
    writer_lock: threading.Lock = field(default_factory=threading.Lock)
    requests: list[PassRequest] = field(default_factory=list)
    # How a waited-for pass goes, and what it does to the store meanwhile.
    pass_result: PassResult = PassResult.COMPLETED
    during_pass: Callable[[], None] | None = None
    waits: list[tuple[int, float]] = field(default_factory=list)

    def request_pass(self, *, full: bool = False, items: Collection[str] = ()) -> int:
        assert self.writer_lock.locked()
        self.requests.append(PassRequest(full, tuple(items)))
        return len(self.requests)

    def wait_for_pass(self, ticket: int, timeout: float) -> PassResult:
        # The pass needs the lock the request was made under.
        assert not self.writer_lock.locked()
        self.waits.append((ticket, timeout))
        if self.during_pass is not None:
            self.during_pass()
        return self.pass_result
