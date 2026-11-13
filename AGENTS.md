# AGENTS.md

## Project Overview

`shopify-erpnext-connector` binds Shopify variants to ERPNext Items by SKU and keeps Shopify's available stock in line with ERPNext. Phase A covers inventory sync only: a worker loop polls ERPNext, computes sellable stock, pushes it to Shopify and emails the operator about problems through ERPNext. A small HTTP API in the same process serves the operator CLI and the health and metrics endpoint.

The plan lives in the repo's GitHub issues: the parent plan issue and one issue per implementation unit.

## Development Commands

- `./run sync` — create the virtual environment and install dependencies, including the `dev` extra
- `./run lint` — ruff check and format check
- `./run format` — auto-format with ruff
- `./run typecheck` — ty
- `./run test` — pytest (extra arguments go to pytest)
- `./run check-fixtures` — reject real shop hosts, `Authorization` header values and deny-listed strings
- `./run build-image` — build the container image locally
- `./run ci-check` — everything CI runs

## Configuration

- One non-secret TOML file (`CONNECTOR_CONFIG`, default `/etc/shopify-erpnext-connector/config.toml`) and a secrets directory with one file per secret (`CONNECTOR_SECRETS_DIR`, default `/run/secrets`). See `config.example.toml` and `compose.example.yaml`.
- The environment name, Shopify shop domain, Shopify location ID and ERPNext URL are mandatory: they pin what an environment may talk to.
- Secrets are read only from files. Never add an environment-variable fallback for a secret. The set of secret files is defined in one place, `Secrets` in `config.py`.
- Secret values are `Secret` objects whose `repr` and `str` are redacted. Call `reveal()` only where the value is sent or compared.
- The HTTP API (`api.py`) never logs headers or tokens. Operator endpoints take only operator tokens and the `X-Connector-Environment` header; `/metrics` takes only the metrics token. Each request opens its own store connection, and the API reads worker state and asks for passes only through `Worker`'s public properties and `request_pass`/`wait_for_pass`. A mutation that waits for a pass (`/push`, `/rerun`) waits after releasing the writer lock, since the pass needs it.
- Mutations are `POST` routes with identifiers in a JSON body. Each runs in one store transaction with its audit row. A mutation that changes pass inputs or bindings, or asks for a pass, also runs under `Worker.writer_lock` with a bounded wait. `/dry-run` writes only the settings row, which a pass reads once at its start, so it skips the lock and takes effect from the next pass; a mutation that changes something writes exactly one audit row (operator name, stable action name, JSON detail). The CLI refuses a mutation against production unless `--env production` is on the command line.

## Conventions

- Python 3.11, `src/` layout, strict typing with ty.
- This repo is public. Never write real hostnames, domains, IP addresses, internal URLs or account names into it, including tests and examples. Use placeholders such as `example.myshopify.com` and `erpnext.example.com`.
- Test fixtures are synthetic or scrubbed: placeholder hosts, no auth headers, no customer or address fields, only item codes and quantities. `./run check-fixtures` enforces part of this in CI.
- Tests of the API's authentication send `Authorization` headers carrying placeholder tokens (`test-...`). They name the header through an `AUTH_HEADER` constant, which `./run check-fixtures` does not match; never put a real token in one.
