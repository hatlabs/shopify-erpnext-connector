"""Read Shopify's available levels and push sellable values with compare-and-set."""

from collections.abc import Sequence
from dataclasses import dataclass

from shopify_erpnext_connector.shopify import ShopifyClient, ShopifyError

# The most IDs `nodes` accepts in one query.
LEVELS_PAGE_SIZE = 250
AVAILABLE = "available"

_LEVELS_QUERY = """\
query InventoryLevels($ids: [ID!]!, $locationId: ID!) {
  nodes(ids: $ids) {
    ... on InventoryItem {
      id
      inventoryLevel(locationId: $locationId) {
        quantities(names: ["available"]) { name quantity }
      }
      variant { inventoryPolicy }
    }
  }
}
"""


@dataclass(frozen=True)
class Level:
    """An inventory item's level at the location and its variant's policy.

    `available` is None when the item is not stocked at the location.
    `policy` is the variant's `inventoryPolicy`, such as DENY or CONTINUE.
    """

    available: int | None
    policy: str


def fetch_levels(
    shopify: ShopifyClient, inventory_item_ids: Sequence[str], location_id: str
) -> dict[str, Level]:
    """Read the level of each inventory item at the location, by item ID.

    An item Shopify does not know, such as one whose variant was deleted, is
    left out, so the other items' levels are still read.
    """
    ids = list(dict.fromkeys(inventory_item_ids))
    levels: dict[str, Level] = {}
    for start in range(0, len(ids), LEVELS_PAGE_SIZE):
        chunk = ids[start : start + LEVELS_PAGE_SIZE]
        data = shopify.execute(
            _LEVELS_QUERY, {"ids": chunk, "locationId": location_id}, idempotent=True
        )
        nodes = data.get("nodes")
        if not isinstance(nodes, list) or len(nodes) != len(chunk):
            raise _unexpected_levels()
        for requested, node in zip(chunk, nodes, strict=True):
            if node is None:
                continue
            item_id, level = _parse_level(node)
            if item_id != requested:
                raise _unexpected_levels()
            levels[item_id] = level
    return levels


def _parse_level(node: object) -> tuple[str, Level]:
    if not isinstance(node, dict):
        raise _unexpected_levels()
    item_id = node.get("id")
    variant = node.get("variant")
    policy = variant.get("inventoryPolicy") if isinstance(variant, dict) else None
    if not isinstance(item_id, str) or not isinstance(policy, str):
        raise _unexpected_levels()
    inventory_level = node.get("inventoryLevel")
    if inventory_level is None:
        return item_id, Level(None, policy)
    if not isinstance(inventory_level, dict):
        raise _unexpected_levels()
    return item_id, Level(_available(inventory_level.get("quantities")), policy)


def _available(quantities: object) -> int:
    if not isinstance(quantities, list):
        raise _unexpected_levels()
    for entry in quantities:
        if not isinstance(entry, dict):
            raise _unexpected_levels()
        if entry.get("name") == AVAILABLE:
            quantity = entry.get("quantity")
            # bool is an int subclass; a boolean is not a quantity.
            if not isinstance(quantity, int) or isinstance(quantity, bool):
                raise _unexpected_levels()
            return quantity
    raise _unexpected_levels()


def _unexpected_levels() -> ShopifyError:
    return ShopifyError("inventory levels: unexpected response")
