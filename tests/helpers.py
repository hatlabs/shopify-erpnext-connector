"""Shared test configuration: placeholder hosts, addresses and secrets."""

from shopify_erpnext_connector.config import (
    Config,
    EmailConfig,
    Environment,
    Secret,
    Secrets,
)

SHOP_DOMAIN = "example.myshopify.com"
LOCATION_ID = "gid://shopify/Location/1234567890"
ERPNEXT_URL = "https://erpnext.example.com"
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
        email=EmailConfig(
            enabled=email_enabled,
            recipient=EMAIL_RECIPIENT,
            sender=EMAIL_SENDER,
        ),
        secrets=Secrets(
            erpnext_api_key=Secret(ERPNEXT_API_KEY),
            erpnext_api_secret=Secret(ERPNEXT_API_SECRET),
            shopify_access_token=Secret(SHOPIFY_ACCESS_TOKEN),
            metrics_token=Secret("test-metrics-token"),
            operator_tokens={},
        ),
    )
