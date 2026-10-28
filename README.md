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

## License

[AGPL-3.0](LICENSE)
