"""How long both clients wait for an upstream call, and the bounded retry
they give reads on network errors and 5xx."""

CONNECT_TIMEOUT_SECONDS = 10.0
# ERPNext's email `make` sends over SMTP, with an OAuth token fetch, inside
# the request.
READ_TIMEOUT_SECONDS = 60.0
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1.0


def backoff_seconds(attempt: int) -> float:
    """The wait after failed attempt `attempt` (counted from 1), doubling each time."""
    return RETRY_DELAY_SECONDS * 2 ** (attempt - 1)
