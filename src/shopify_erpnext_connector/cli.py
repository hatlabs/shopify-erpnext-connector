"""The `shopify-erpnext-connector` executable.

`serve` runs the connector service; the other commands are the operator CLI.
"""

import argparse
from collections.abc import Sequence

from shopify_erpnext_connector.problems import CLI_NAME
from shopify_erpnext_connector.service import serve


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=CLI_NAME)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve", help="run the connector service")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _parser().parse_args(argv)
    return serve()
