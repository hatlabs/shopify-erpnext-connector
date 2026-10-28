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
- Secret values are `Secret` objects whose `repr` and `str` are redacted. Call `reveal()` only where the value is sent.

## Conventions

- Python 3.11, `src/` layout, strict typing with ty.
- This repo is public. Never write real hostnames, domains, IP addresses, internal URLs or account names into it, including tests and examples. Use placeholders such as `example.myshopify.com` and `erpnext.example.com`.
- Test fixtures are synthetic or scrubbed: placeholder hosts, no auth headers, no customer or address fields, only item codes and quantities. `./run check-fixtures` enforces part of this in CI.
