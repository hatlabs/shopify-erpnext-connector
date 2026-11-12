"""Refuse any request whose target differs from the environment's pinned one.

The clients send through an injected `httpx.Client`, so the target on the wire
comes from that client's base URL. Comparing each built request against the
configured pin makes a miswired client fail closed before any network I/O.
"""

import httpx


class PinningError(Exception):
    """A request or mutation targets something other than the pinned one."""


def require_pinned(request: httpx.Request, pinned: httpx.URL) -> None:
    target = (request.url.scheme, request.url.host, request.url.port)
    if target != (pinned.scheme, pinned.host, pinned.port):
        raise PinningError(
            f"refusing {request.method} to {request.url.scheme}://"
            f"{request.url.netloc.decode('ascii')}: "
            f"this environment is pinned to {pinned.scheme}://"
            f"{pinned.netloc.decode('ascii')}"
        )


def describe(request: httpx.Request) -> str:
    """Method, host and path: all a log line or error message may show."""
    return f"{request.method} {request.url.host} {request.url.path}"
