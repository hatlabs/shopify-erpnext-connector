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

The image runs the service with `shopify-erpnext-connector serve`. The same process serves an HTTP API on every interface, on port 8080 unless `[api] port` says otherwise. It speaks plain HTTP, so terminate TLS in front of it. The operator endpoints answer operators: each request carries an operator token as a bearer token and names the environment it expects in the `X-Connector-Environment` header, and a request for another environment is refused. `/status`, `/problems`, `/problem`, `/bindings`, `/record` and `/report` are `GET` reads. `/dry-run`, `/bindings/accept-sku-change`, `/push` and `/rerun` are `POST` mutations with a JSON object body; each records the operator's name in the audit log when it changes something. `/bindings/accept-sku-change` takes the variant GID and the SKU the operator accepts, `{"variant": ..., "sku": ...}`, and answers 409 without changing anything if the variant's recorded SKU is no longer that one. All but `/dry-run` wait up to 90 seconds for a running pass and answer 503 if the pass is still running. `/push` (`{"variant": <GID>}` or `{"all": true}`) and `/rerun` (`{"key": <kind:subject>}`) then ask the worker for a pass and wait up to 120 seconds for it to end. Their answer's `pass` is `completed`, `ended early` (an ERPNext or Shopify error stopped it, or no full pass has succeeded yet, so the work asked for may not have happened) or `not finished`; a pass not finished in time still answers 200. `/dry-run` does not wait: a pass reads the setting at its start, so a change takes effect from the next pass. Two operators with the same token are refused at start, so the audit log names the right one, and so is an operator token equal to the metrics token. `GET /metrics` serves Prometheus gauges and takes only the metrics token.

## Operator CLI

The other `shopify-erpnext-connector` commands are the operator CLI, which reads and changes a connector through its API. Install it with `uv tool install git+https://github.com/hatlabs/shopify-erpnext-connector`.

Each environment is set by two variables, for example in the ops workspace's `.env`. The token is the operator token the connector has as `operator_token_<name>`, kept in a file the variable names; the CLI never takes a token from a variable or an argument.

```bash
CONNECTOR_STAGING_URL=https://connector-staging.example.com
CONNECTOR_STAGING_TOKEN_FILE=/path/to/staging-operator-token
CONNECTOR_PRODUCTION_URL=https://connector.example.com
CONNECTOR_PRODUCTION_TOKEN_FILE=/path/to/production-operator-token
```

`--env staging` or `--env production` selects the environment; without it, `CONNECTOR_ENV` does, and then staging. Every answer names the connector's environment, and the CLI fails when it is not the one selected. An error about a request or an answer starts with the environment asked, as in `production: the connector answered 404: ...`.

| Command | Shows |
|---|---|
| `status` | last pass, push lag, dry-run, open problems and unsent emails |
| `problems` | the open problems |
| `problems show <kind:subject>` | one problem, its details and email state; problem emails name this command with the connector's `--env` |
| `bindings` | unbound and ambiguous variants, and variants held after their SKU changed |
| `record <variant GID>` | a variant's binding, the explanation of its sellable stock and when it was computed, and its recent pushes |
| `report` | the cutover dry-run comparison of computed and current Shopify values |

These commands change the connector:

| Command | Does |
|---|---|
| `dry-run on` or `dry-run off` | turns dry-run on or off from the next pass, even while a pass is running; turning it off asks for a full pass, and the next full pass pushes every bound variant. Setting the state it already has changes nothing and says so |
| `bindings accept-sku-change <variant GID> <sku>` | accepts the new SKU that `bindings` shows for a variant held after its SKU changed, given as `<sku>`. If a pass has since recorded a different SKU, the command is refused as a conflict and changes nothing; run `bindings` again. Otherwise the next full pass binds the variant by that SKU if it still has it, and otherwise keeps it held and reports its newer SKU. An accepted SKU that cannot bind yet, for example because no usable Item has it, keeps the variant held with its acceptance and the reason; it binds once it can, provided the variant still has that SKU. A variant without a held change is reported as not found, and one with no SKU in Shopify, or no changed-SKU problem recorded yet, as a conflict |
| `push <variant GID>` | computes the variant and pushes its value in a pass run now, even if its last push failed recently, then shows the resulting decision and newest push attempt. In dry-run it shows the computed value; nothing is pushed. A variant that is not bound is refused; `record` shows why |
| `push --all` | the same for every bound variant; shows how many items were marked and how many still wait to be pushed after the pass, such as a failed or held push |
| `rerun <kind:subject>` | runs a full pass now and shows whether the problem is still open or cleared |

`push` and `rerun` wait for the pass they ask for, up to two minutes. If it does not end in time, the command says so and exits 0: the request was accepted and the connector still runs the pass, so check `record` or `problems show` later. If the pass ended early, the command says the push may not have happened, or that the problem was not rechecked, and points to the connector's log and `status`.

A command that changes production runs only with `--env production` on the command line; `CONNECTOR_ENV=production` alone is refused before the token is read. If a pass runs for longer than the connector waits, `bindings accept-sku-change`, `push` and `rerun` fail with a 503; run the command again.

`--json` prints the API's JSON answer instead of text.

## License

[AGPL-3.0](LICENSE)
