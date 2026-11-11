# ERPNext semantics the connector depends on

Observed on 2026-11-11 and 2026-11-12 on a staging ERPNext restored from a production dump: ERPNext v16, Frappe v16, ecommerce-integrations 1.x. Shopify behaviour was observed on a development store at Admin API 2026-10. Site datetimes are in the site's time zone; the containers run in UTC. Statements read from code rather than observed say so.

## Bin fields per transaction

Item `CX-M12-CF5A-TERM` (plain stock item, FIFO) in the stock warehouse `Stores - HL`, unless noted. The Manufacture and Work Order rows use the BOM item `CXP-SP13-CF5-PW200` (FG) and its components `CX-SP13-CF5` and `CX-SP13-PM5-PW200`, one of each per unit. That Work Order had `skip_transfer=1` and every warehouse set to `Stores - HL`; with a transfer to a work-in-progress warehouse the components would move at transfer time instead.

"Visible" is the time from the start of the write call until a separate API reader, polling about every 10 ms, first saw the new Bin row. "Stamp gap" is how long before that moment Bin `modified` had been stamped. Both are upper bounds by up to one poll interval.

Short names in the table: actual is `actual_qty`, reserved is `reserved_qty`, reserved_for_production is `reserved_qty_for_production`, planned is `planned_qty` and projected is `projected_qty`. `reserved_stock` and `stock_value` are the column names.

| Transaction | Bin fields changed | Bin `modified` moved | Stock Ledger Entry | Visible | Stamp gap |
|---|---|---|---|---|---|
| Sales Order submit (qty 2) | reserved +2, projected −2 | yes | none | 0.62 s | 0.05 s |
| Sales Order cancel | reserved −2, projected +2 | yes | none | 1.96 s | 1.85 s |
| Sales Order submit with `reserve_stock=1` (qty 1) | reserved +1, reserved_stock +1, projected −1 | yes | none | 0.23 s | 0.03 s |
| that Sales Order cancelled | reserved −1, reserved_stock −1, projected +1 | yes | none | 0.43 s | 0.20 s |
| Sales Order created by ecommerce-integrations from a Shopify order (qty 2) | reserved +2, projected −2 | yes | none | 3.52 s ¹ | 0.94 s |
| Delivery Note submit (1 of 3 ordered) | actual −1, reserved −1, projected unchanged | yes | −1 | 0.52 s | 0.13 s |
| Sales Order close (2 undelivered) | reserved −2, projected +2 | yes | none | 0.06 s | 0.03 s |
| Material Receipt (+5) | actual +5, projected +5 | yes | +5 | 0.24 s | 0.09 s |
| Purchase Receipt (+3) | actual +3, projected +3 | yes | +3 | 0.38 s | 0.17 s |
| Manufacture Stock Entry (1 of a Work Order for 2) | FG: actual +1, planned −1. Each component: actual −1, reserved_for_production −1. projected unchanged on all three | yes, all three | FG +1, components −1 | 0.30 s | 0.07–0.12 s |
| Stock Reconciliation (32 → 33) | actual +1, projected +1 | yes | one row, `actual_qty` 0, the absolute quantity in `qty_after_transaction` | 0.12 s | 0.06 s |
| Work Order submit (qty 2) | FG: planned +2, projected +2. Each component: reserved_for_production +2, projected −2 | yes, all three | none | 0.11 s | 0.02–0.03 s |
| Backdated Material Receipt (+4), posted before a later Stock Reconciliation | none: the reconciliation fixes the quantity | yes | +4 | 0.20 s | 0.12 s |
| Repost Item Valuation of that entry | none | yes | later entries' `qty_after_transaction` rewritten | 1.78 s | 0.25 s |
| Backdated Material Receipt (+4), posted before a later Material Receipt | actual +4, projected +4 at submit; stock_value not yet updated | yes | +4 | 0.15 s | 0.07 s |
| Repost Item Valuation of that entry | stock_value and valuation_rate only; quantities unchanged | yes | later entries' `qty_after_transaction` rewritten | 1.60 s | 0.21 s |

¹ Run through `bench execute`, including process start-up and the Shopify fetch.

`Item.modified` did not move in any of these transactions.

### Multi-line transactions

In a multi-line transaction each Bin is stamped as it is written, and all of them commit together at the end, so the first-written Bin has the largest stamp gap. In these runs every reconciliation row was one Bin. Two to four runs per row:

| Transaction | Bins | Max stamp gap |
|---|---|---|
| Stock Reconciliation | 100 | 1.82–1.98 s |
| Stock Reconciliation, synchronous submit | 196 | 3.63–3.67 s |
| Stock Reconciliation, desk submit (over 100 rows it runs in a worker, per `StockReconciliation.submit`) | 196 | 3.22–3.53 s |
| Sales Order submit, 20 lines | 20 | 0.09–0.15 s |
| Delivery Note submit, 20 lines | 20 | 0.37–0.40 s |
| Delivery Note cancel, 20 lines | 20 | 0.57–1.73 s |
| Sales Order cancel, 20 lines | 20 | 0.14–0.38 s |

The gap is roughly 17 ms per Bin plus 0.2–0.4 s of work after the last Bin is written, with occasional slow runs: one Delivery Note cancel took 1.4 s after its last Bin, and the single-line Sales Order cancel in the first table took 1.85 s. For a reconciliation of every Bin in a warehouse, the bound is the Bin count × 17 ms plus that worst tail: about 6 s for 250 Bins (not run). All figures come from an idle staging host.

### What this means for the poller

- Every transaction tested moved Bin `modified`, including those that write no Stock Ledger Entry: Sales Order submit, cancel and close, Delivery Note cancel and Work Order submit. A Repost Item Valuation moves it again even when no quantity changes. Not tested: Material Issue and Material Transfer, Sales Return and its cancel, Sales Invoice with `update_stock`, Sales Order "Update Items", Work Order stop, close and cancel, Stock Reservation Entries made outside a Sales Order, and an item's first transaction in a warehouse, which inserts the Bin row.
- Bin `modified` is the wall-clock time at which the Bin row is written inside the transaction (`erpnext/stock/doctype/bin/bin.py:update_bin_columns` sets it from `now()`). It is neither the commit time nor the posting date. A backdated entry posted at 17:31:00 got Bin `modified` 17:32:22, its submit time.
- Bin `modified` is a naive timestamp in the site's time zone (Europe/Helsinki on the observed site) with microseconds, for example `2026-11-11 17:29:42.431978`. A watermark has to come from returned `modified` values, never from the connector's own clock. When daylight saving time ends, the repeated hour gives later writes a `modified` earlier than ones already seen, so a `modified > last_seen` poll misses them until a full pass.
- A poll filtered on `modified > last_seen` can miss a row. The single-line Sales Order cancel in the first table stamped `modified` 1.85 s before a reader saw it, so a poller that saw another Bin committed in between would advance past it. The poll window must overlap the previous one by more than the longest stamp gap. That gap grows with the size of the largest Stock Reconciliation an operator submits (3.7 s for 196 Bins), so a fixed one- or two-second overlap is too short.
- A repost writes the Bin first and then reposts GL entries before committing, so its stamp gap grows with the size of the GL repost. On staging, the stamp gap of a one-item repost was 0.21–0.25 s, and the repost call itself took 2.0–2.4 s. Reposts on production are larger and were not measured. Production runs queued reposts from the hourly `repost_entries` job.
- A backdated entry updates `actual_qty` at submit when its quantity still counts: `bin.py:update_qty` re-reads the quantity from the ledger when later entries exist. When a later Stock Reconciliation exists, the reconciliation fixes the quantity, so `actual_qty` stays the same. In both cases the later repost changes only `stock_value`, `valuation_rate` and `modified`.
- `reserved_stock` is part of `reserved_qty`, not added to it: a Sales Order with `reserve_stock=1` raises both by the same amount. Production has stock reservation enabled with auto-reserve off, and none of its Shopify Sales Orders from May to October 2026 set `reserve_stock`.
- `actual − reserved − reserved_for_production` stays the same across a Delivery Note (actual and reserved fall together) and, for the components, across a Manufacture entry (actual and reserved_for_production fall together). For the finished good, a Manufacture entry raises it by the quantity produced: actual rises and planned falls, and planned is not in the formula. `projected_qty` stays the same on all of these.

## Sale to Sales Order

- Production's webhook path enqueues `sync_sales_order` on the short queue. In production data from May to October 2026, 204 Shopify orders that have a matching request log took 4.1–20.7 s from Shopify `created_at` to Sales Order `creation`, with a mean of 7.2 s. The period has 289 Sales Orders with a `shopify_order_id`, and 86 of them have no matching request log, so the two counts overlap by one; neither the overlap nor the missing logs were investigated, and the delay of orders without a log is unknown.
- Read through the API from the one Sales Order synced from the development store: `shopify_order_id` holds the numeric Shopify order ID as a string (for example `"1234567890123"`), not the GraphQL GID (`gid://shopify/Order/...`), and `shopify_order_number` holds the order name with its `#`. Each line has `item_code`, `qty` in the selling UOM, `stock_qty`, `conversion_factor` and `delivered_qty`.
- A line reserves only its undelivered quantity: the table shows a Delivery Note lowering `reserved_qty` by what it delivers, and closing the Sales Order releasing the rest. A comparison with Bin reservations therefore counts `stock_qty − delivered_qty × conversion_factor` on submitted Sales Orders that are not Closed, in stock UOM. Product bundles, whose packed items reserve the components, were not observed.
- A paid order also gets a Sales Invoice in the same transaction. The invoice has `update_stock` 0, so it has no Bin effect.
- Without webhooks, ecommerce-integrations' `sync_old_orders` is an hourly one-shot backfill over an armed window that clears its flag when done. Its `old_orders_from` and `old_orders_to` are converted with `astimezone()`, so they are read in the server process's time zone, not the site's.

## What ecommerce-integrations' inventory push writes

From the code of ecommerce-integrations 1.x:

- It pushes `cint(actual_qty) − cint(reserved_qty)` from the Bin of each mapped warehouse (`shopify/inventory.py:upload_inventory_data_to_shopify`). It ignores `reserved_qty_for_production`, has no floor at 0, so it can push a negative number, and truncates fractions.
- `controllers/inventory.py:get_inventory_levels` selects every Shopify `Ecommerce Item` row whose Bin in a mapped warehouse has `modified` later than the row's `inventory_synced_on`. It does not filter out template items, empty variant IDs or disabled items. An item with no Bin in the mapped warehouse is never pushed.
- The warehouse-to-location mapping is the Shopify Setting's `shopify_warehouse_mapping` table. Production maps one warehouse to one location.
- It runs at most every `inventory_sync_frequency` minutes (30 on production) and walks the rows in batches of 50. For each row it makes two REST calls at API 2024-01: it looks up the variant for its inventory item, then sets the level absolutely, without compare-and-set. It commits after each row. A deleted variant or location marks the row synced and moves on. Any other error leaves the row unsynced, so it is retried on every run.
- On production (read 2026-11-11) the push is on and still runs, and its newest logs are Failed. There are 318 Shopify Ecommerce Item rows for 308 item codes. 29 rows have an empty variant ID, and all 29 are template items.

This is the baseline for the cutover dry-run.

## Shopify `inventorySetQuantities`

On the development store, with quantity name `available`. Every call carried two valid entries; the table shows what was at index 1.

| Entry at index 1 | Entries in call | Response | Valid entries applied |
|---|---|---|---|
| unknown `inventoryItemId` | 3 | HTTP 200, `inventoryAdjustmentGroup: null`, user error `INVALID_INVENTORY_ITEM` with `field` index `"1"` | no |
| unknown `locationId` | 3 | HTTP 200, `null`, `INVALID_LOCATION`, index `"1"` | no |
| the second valid item, with a stale `changeFromQuantity` | 2 | HTTP 200, `null`, `CHANGE_FROM_QUANTITY_STALE`, index `"1"` | no, not even the first entry, whose `changeFromQuantity` was right |
| inventory item with tracking off | 3 | HTTP 200, adjustment group, no user errors | yes, and the untracked item's level was set too |
| the second valid item (control) | 2 | HTTP 200, adjustment group, no user errors | yes |

- One bad entry fails the whole batch. The error comes back as a user error with HTTP 200, and `field` is `["input", "quantities", "<index>", "<field>"]`, which names the bad entry. A retry can drop or fix that entry and resend the rest.
- Shopify accepts a level for an untracked inventory item, so it does not reject untracked variants for the connector.
- `quantityAfterChange` is `null` in the response, so reading the level back is the only way to confirm the result.
- The token tested had `read_inventory` and `write_inventory` but not `read_locations`, and listing locations returned `ACCESS_DENIED`. A startup check that the configured location exists needs `read_locations`. Reading an inventory level at that location through `read_inventory` alone may work instead; that was not tried.

## Email through `communication.email.make`

`frappe.core.doctype.communication.email.make` checks permission before it writes anything:

- with a reference document: `email` permission on that document's doctype;
- without one: `create` permission on Communication.

It then inserts the Communication with permissions ignored and, when `send_email=1`, queues the mail on the default outgoing account.

Run as a test API user with `send_email=1` and `now=1`, first with email muted:

| Roles | Reference | Result |
|---|---|---|
| Stock User | none | 403, no access to Communication |
| Stock User | an Item | 200, Communication and Email Queue row |
| Stock User | a Sales Order | 403, no `email` permission on Sales Order |
| Stock User + Inbox User | none | 200, Communication and Email Queue row |

Then with email unmuted and a real outgoing account: Gmail over SMTP with OAuth, on an account with "always use account's email address as sender" set. Both calls were made as a Stock User referencing an Item.

- The first call failed to log in to SMTP. It returned HTTP 500 with no Communication name, yet the Communication and an Email Queue row in Not Sent had already been committed. A caller that retries after a 500 creates a second Communication and queue row. The orphaned row stays Not Sent; by the code, Frappe's scheduled queue flush retries Not Sent rows, so both emails could go out once sending works again (not observed: the scheduler is stopped on staging). The Communication's `delivery_status` after the failure was not read.
- The second call was delivered. The queue row and `Communication.delivery_status` both reached Sent within the request; with `now=1` the SMTP send, including any OAuth token refresh, runs after the commit inside the same HTTP request.
- The send path did not check the user's roles again: Stock User was enough to send.
- The From address was the outgoing account's address, because of the account setting above. On an account without that setting, `make` uses the API user's own address unless `sender` is passed; delivery from such an account was not tried.
- Stock User cannot read Email Queue (403). It can read its own Communication by name, though not through the list. `delivery_status` was seen as Sent; by the code it also takes Sending, Error and Expired as the queue row changes. The connector can track an email by keeping the Communication name that `make` returns.

With email muted, `make` still succeeds against a dummy "Notifications" account and leaves the queue row Not Sent.

## Roles for the connector

With Stock User only, the connector's API user got data from Bin (including a `modified` filter), Item, Sales Order (including `Sales Order Item` fields through the parent list), Work Order, Stock Ledger Entry, Stock Reservation Entry and Warehouse. It got 403 on Ecommerce Item, Email Queue and Repost Item Valuation.

Stock User can send an email that references a doctype it has `email` permission on: Item was tried, and Bin has the permission in the DocPerms but was not tried. Sales Order was refused. Some phase A problems have no such document: a Shopify variant whose SKU matches no Item, and problems about a Shopify order. The connector's role set is therefore Stock User plus Inbox User, which adds create on Communication so an email can go out without a reference; that path was exercised only with email muted. Inbox User also lets the user read, by name, any Communication that has no reference document.

## Not observed

- Commit delay of large reposts on production, the stamp gap when the hourly job runs queued reposts, and stamp gaps under production load.
- An `inventorySetQuantities` call with more than one bad entry: whether the user errors name every bad entry or only the first.
- What ecommerce-integrations does to the Sales Order and Bin when a Shopify order is cancelled or edited before fulfilment.
- Whether DST end actually produces a missed row: the repeated hour is inferred from naive site-local timestamps, not observed.
- `delivery_status` after a failed send, the values Error and Expired, and whether the scheduled flush resends an orphaned Not Sent row.
- Delivery from an outgoing account that does not force its own address as sender, or with an explicit `sender`.
