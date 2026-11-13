# shopify-erpnext-connector

Shopify ↔ ERPNext connector: inventory, orders, items.

**Status: work in progress.** Nothing here is ready for use yet.

## Scope

Phase A syncs inventory from ERPNext to Shopify. The connector binds Shopify variants to ERPNext Items by SKU, pushes each bound variant's finished stock that is not reserved for orders or production to Shopify, and reports problems to the operator by email sent through ERPNext. Orders, invoices and product data are out of scope for phase A.

The connector supports one stock location and one currency. Settings such as the warehouse, company and accounts are configuration.

## Development

Requires [uv](https://docs.astral.sh/uv/).

```bash
./run sync       # create .venv with the dev extra
./run ci-check   # lint, typecheck, tests and the fixture check
./run help       # list all commands
```

## Configuration

The connector reads one non-secret TOML file and a directory holding one file per secret; secrets are never read from environment variables. See [config.example.toml](config.example.toml) and [compose.example.yaml](compose.example.yaml).

The image runs as uid 10001, and Compose mounts secret files with their host owner and mode, so make them readable by that uid: `chown 10001 secrets/* && chmod 400 secrets/*`. The connector keeps its state in a SQLite store under `/var/lib/shopify-erpnext-connector`, a volume in the example. A new store starts in dry-run: the connector computes and records what it would push but writes nothing to Shopify until an operator turns dry-run off.

The image runs the service with `shopify-erpnext-connector serve`. The same process serves an HTTP API on every interface, on port 8080 unless `[api] port` says otherwise. It speaks plain HTTP, so terminate TLS in front of it. The operator endpoints (`/status`, `/problems`, `/problem`, `/bindings`, `/record` and `/report`, all `GET`) answer operators: each request carries an operator token as a bearer token and names the environment it expects in the `X-Connector-Environment` header, and a request for another environment is refused. `GET /metrics` serves Prometheus gauges and takes only the metrics token.

## Operator CLI

The other `shopify-erpnext-connector` commands are the operator CLI, which reads from a connector's API. Install it with `uv tool install git+https://github.com/hatlabs/shopify-erpnext-connector`.

Each environment is set by two variables, for example in the ops workspace's `.env`. The token is the operator token the connector has as `operator_token_<name>`, kept in a file the variable names; the CLI never takes a token from a variable or an argument.

```bash
CONNECTOR_STAGING_URL=https://connector-staging.example.com
CONNECTOR_STAGING_TOKEN_FILE=/path/to/staging-operator-token
CONNECTOR_PRODUCTION_URL=https://connector.example.com
CONNECTOR_PRODUCTION_TOKEN_FILE=/path/to/production-operator-token
```

`--env staging` or `--env production` selects the environment; without it, `CONNECTOR_ENV` does, and then staging. Every answer names the connector's environment, and the CLI fails when it is not the one selected.

| Command | Shows |
|---|---|
| `status` | last pass, push lag, dry-run, open problems and unsent emails |
| `problems` | the open problems |
| `problems show <kind:subject>` | one problem, its details and email state; problem emails name this command with the connector's `--env` |
| `bindings` | unbound and ambiguous variants, and variants held after their SKU changed |
| `record <variant GID>` | a variant's binding, the explanation of its sellable stock and its recent pushes |
| `report` | the cutover dry-run comparison of computed and current Shopify values |

`--json` prints the API's JSON answer instead of text.

## License

[AGPL-3.0](LICENSE)
