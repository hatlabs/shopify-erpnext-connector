import logging

# httpx logs every request's full URL, query string included, at INFO, and
# httpcore traces requests at DEBUG. The clients log their own method, host,
# path and status instead.
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)
