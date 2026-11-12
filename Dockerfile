FROM python:3.11-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.13 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY README.md LICENSE ./
COPY src/ src/
RUN uv sync --locked --no-dev --no-editable

FROM python:3.11-slim AS runtime
# The store directory exists in the image, owned by the connector, so a new
# named volume mounted there starts with that owner.
RUN useradd --system --no-create-home --uid 10001 connector \
    && install -d -o connector -m 700 /var/lib/shopify-erpnext-connector
COPY --from=build /app/.venv /app/.venv
ENV PATH=/app/.venv/bin:$PATH
USER connector
CMD ["shopify-erpnext-connector", "serve"]
