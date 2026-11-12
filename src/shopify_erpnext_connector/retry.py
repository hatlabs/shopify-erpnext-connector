"""The bounded retry both clients give reads on network errors and 5xx."""

MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 1.0


def backoff_seconds(attempt: int) -> float:
    """The wait after failed attempt `attempt` (counted from 1), doubling each time."""
    return RETRY_DELAY_SECONDS * 2 ** (attempt - 1)
