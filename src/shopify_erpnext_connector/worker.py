"""The worker loop: poll ERPNext, compute sellable stock and push it to Shopify.

Each pass marks the items whose Bin changed as dirty, recomputes the bound
variants of the dirty items and pushes the values that differ from Shopify's.
A push that would raise a level that fell below the last pushed value waits
up to `SALE_SETTLE_HOLD`, with the item kept dirty, since the drop may be a
sale the orders search does not show yet. The hold is kept in memory only.
A full pass, at start, hourly and when dry-run is turned off, also refreshes
the bindings and recomputes every bound variant, so a missed change or an
edit made in Shopify is corrected within the hour.

The whole pass holds the writer lock, so a CLI mutation never interleaves
with a pass's reads and writes. Dry-run is read once at the start of a pass,
so setting it needs no lock. A failed read ends the pass with the dirty
items kept for the next one; the push staleness check and problem emails
still run, since a Shopify outage is what the staleness check reports. A
bound item dirty past the threshold is reported as well as a queued value,
since a pass that fails before pushing queues nothing. All unpushed variants
are one problem for the location, so an outage emails once.

An item whose push fails, or that a failed pass was computing, waits before
it is computed again, twice as long after each consecutive failure, so a
variant that always fails does not make every cycle read all open orders,
nor does a read that fails after a full pass marked every item dirty. A
failed full pass waits the same
way before the next full attempt, since it reads every Item and variant;
passes in between work from the stored bindings. Until a full pass has
succeeded in this process those passes only poll Bins: the stored bindings
may predate SKU changes made while the connector was down. The waits are
kept in memory only: the full pass after a restart retries everything anyway.
"""

import logging
import sqlite3
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from shopify_erpnext_connector.availability import (
    Sellable,
    compute_sellable,
    fetch_bins,
)
from shopify_erpnext_connector.bindings import (
    Binding,
    UnboundKind,
    UnboundVariant,
    load_bindings,
    refresh_bindings,
)
from shopify_erpnext_connector.config import Config
from shopify_erpnext_connector.decisions import Decision, save_decisions
from shopify_erpnext_connector.erpnext import ErpNextClient, ErpNextError
from shopify_erpnext_connector.notify import send_pending
from shopify_erpnext_connector.orders import (
    UNMIRRORED_GRACE,
    OrderMatch,
    fetch_open_orders,
    fetch_sales_order_lines,
    match_orders,
    unmirrored_problems,
)
from shopify_erpnext_connector.poller import (
    SeenBins,
    clear_dirty,
    dirty_items,
    mark_dirty,
    poll_bins,
    record_failed_attempt,
)
from shopify_erpnext_connector.problems import Problem, ProblemKind, record_check
from shopify_erpnext_connector.push import (
    Level,
    PushOutcome,
    PushTarget,
    drop_unbound_pending,
    fetch_levels,
    last_pushed_values,
    push,
    stale_pushes,
)
from shopify_erpnext_connector.shopify import ShopifyClient, ShopifyError
from shopify_erpnext_connector.store import dry_run, purge_push_history

FULL_PASS_INTERVAL = timedelta(hours=1)
# The longest wait before a failed item or full pass is retried.
MAX_RETRY_BACKOFF = timedelta(minutes=15)
NEWEST_ORDERS_SHOWN = 5
# How long a push that would undo a possible sale waits for Shopify's orders
# search, which can lag behind the level, to show the order.
SALE_SETTLE_HOLD = timedelta(seconds=60)
# How often problem emails are sent and their delivery checked: each check
# reads the status of every undelivered email.
EMAIL_CHECK_INTERVAL = timedelta(minutes=1)

DRY_RUN_ACTION = "dry run"
MISSING_ACTION = "inventory item not found"
HELD_ACTION = "held: possible unindexed sale"
_ACTIONS = {
    PushOutcome.PUSHED: "pushed",
    PushOutcome.UNCHANGED: "unchanged",
    PushOutcome.STALE: "stale",
    PushOutcome.FAILED: "failed",
    PushOutcome.NO_LEVEL: "no level",
}
_SETTLED = frozenset({PushOutcome.PUSHED, PushOutcome.UNCHANGED})

_POLICY_TEXTS = {
    "DENY": "Stops selling at zero",
    "CONTINUE": "Keeps selling at zero: a build is needed",
}

# The binding problems' detail naming the variant's current SKU.
SKU_DETAIL = "SKU"
# The SKU detail of a variant with no SKU.
NO_SKU = "(none)"

_BINDING_PROBLEM_KINDS = {
    UnboundKind.EMPTY_SKU: ProblemKind.UNBOUND_SKU,
    UnboundKind.NO_ITEM: ProblemKind.UNBOUND_SKU,
    UnboundKind.NEAR_MATCH: ProblemKind.UNBOUND_SKU,
    UnboundKind.DUPLICATE_SKU: ProblemKind.AMBIGUOUS_SKU,
    UnboundKind.TEMPLATE_ITEM: ProblemKind.AMBIGUOUS_SKU,
    UnboundKind.ITEM_DISABLED: ProblemKind.AMBIGUOUS_SKU,
    UnboundKind.NON_STOCK_ITEM: ProblemKind.AMBIGUOUS_SKU,
    UnboundKind.SKU_CHANGED: ProblemKind.CHANGED_SKU,
}
# Every problem kind that says why a variant is not bound. `record` reports the
# first open one in this order.
BINDING_PROBLEM_KINDS = tuple(dict.fromkeys(_BINDING_PROBLEM_KINDS.values()))
_BINDING_REASONS = {
    UnboundKind.EMPTY_SKU: "The variant has no SKU",
    UnboundKind.NO_ITEM: "No ERPNext Item has this item code",
    UnboundKind.NEAR_MATCH: "Item codes differ from the SKU only in case or spacing",
    UnboundKind.DUPLICATE_SKU: "Other variants have the same SKU",
    UnboundKind.TEMPLATE_ITEM: "The Item is a template with variants",
    UnboundKind.ITEM_DISABLED: "The Item is disabled",
    UnboundKind.NON_STOCK_ITEM: "The Item is not a stock item",
    UnboundKind.SKU_CHANGED: "The SKU changed since the variant was bound",
}

_log = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class _Backoff:
    failures: int
    retry_at: datetime


@dataclass(frozen=True)
class _Computed:
    binding: Binding
    sellable: Sellable
    level: Level
    last_pushed: int | None

    def target(self) -> PushTarget:
        return PushTarget(
            self.binding.variant_id,
            self.binding.inventory_item_id,
            self.sellable.sellable,
            self.level.available,
        )

    def decision(self, action: str) -> Decision:
        available = self.level.available
        note = None
        if self.last_pushed is not None and available not in (None, self.last_pushed):
            note = (
                f"Shopify had {available} where the connector last pushed "
                f"{self.last_pushed}: changed outside the connector, such as by "
                "a sale, a restock or a hand edit"
            )
        return Decision(
            item_code=self.binding.item_code,
            explanation=self.sellable.explanation(),
            shopify_available=available,
            last_pushed=self.last_pushed,
            action=action,
            note=note,
        )


class Worker:
    """Runs passes against one store, shop and ERPNext site.

    `writer_lock` is held for the whole of each pass. Anything else that
    writes to Shopify, ERPNext or the bindings takes it first.
    """

    def __init__(
        self,
        config: Config,
        connection: sqlite3.Connection,
        shopify: ShopifyClient,
        erpnext: ErpNextClient,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self.writer_lock = threading.Lock()
        self._config = config
        self._connection = connection
        self._shopify = shopify
        self._erpnext = erpnext
        self._clock = clock
        self._seen = SeenBins()
        self._last_pass_at: datetime | None = None
        self._last_pass_full = False
        self._last_full_pass_at: datetime | None = None
        self._dry_run_before: bool | None = None
        self._full_pass_requested = False
        self._backoff: dict[str, _Backoff] = {}
        self._full_pass_backoff: _Backoff | None = None
        self._computing: list[str] = []
        self._held_since: dict[str, datetime] = {}
        self._last_email_check_at: datetime | None = None

    @property
    def last_pass_at(self) -> datetime | None:
        """When the last pass that completed started; None before the first."""
        return self._last_pass_at

    @property
    def last_pass_full(self) -> bool:
        return self._last_pass_full

    @property
    def last_full_pass_at(self) -> datetime | None:
        """When the last full pass that completed started; None before the first."""
        return self._last_full_pass_at

    def request_full_pass(self) -> None:
        """Make the next pass a full one; call with `writer_lock` held."""
        self._full_pass_requested = True

    def run(self, stop: threading.Event) -> None:
        interval = self._config.worker.poll_interval.total_seconds()
        while not stop.is_set():
            self.run_pass(self._clock())
            stop.wait(interval)

    def run_pass(self, now: datetime) -> None:
        connection = self._connection
        with self.writer_lock:
            dry = dry_run(connection)
            if self._dry_run_before and not dry:
                self._full_pass_requested = True
            self._dry_run_before = dry
            full = (
                self._full_pass_requested
                or self._last_full_pass_at is None
                or now - self._last_full_pass_at >= FULL_PASS_INTERVAL
            ) and (
                self._full_pass_backoff is None
                or self._full_pass_backoff.retry_at <= now
            )
            try:
                self._sync(now, full=full, dry=dry)
            except (ShopifyError, ErpNextError) as error:
                _log.warning("pass ended early; dirty items wait: %s", error)
                still_dirty = dirty_items(connection)
                self._back_off([c for c in self._computing if c in still_dirty], now)
                if full:
                    self._full_pass_backoff = self._next_backoff(
                        self._full_pass_backoff, now
                    )
            else:
                if full:
                    self._last_full_pass_at = now
                    self._full_pass_requested = False
                    self._full_pass_backoff = None
                if self._last_full_pass_at is not None:
                    self._last_pass_at = now
                    self._last_pass_full = full
            if not dry:
                found = stale_pushes(
                    connection,
                    now,
                    self._config.worker.push_stale_after,
                    location_id=self._config.location_id,
                )
                record_check(
                    connection,
                    ProblemKind.STALE_PUSH,
                    [] if found is None else [found],
                    now,
                )
            last = self._last_email_check_at
            if last is None or now - last >= EMAIL_CHECK_INTERVAL:
                send_pending(connection, self._erpnext, self._config, now)
                self._last_email_check_at = now

    def _sync(self, now: datetime, *, full: bool, dry: bool) -> None:
        connection = self._connection
        self._computing = []
        poll_bins(connection, self._erpnext, self._config.warehouse, self._seen, now)
        if not full and self._last_full_pass_at is None:
            return
        if full:
            result = refresh_bindings(connection, self._shopify, self._erpnext, now)
            self._record_binding_problems(result.unbound, now)
            purge_push_history(connection, now)
        bindings = load_bindings(connection)
        by_item = {b.item_code: b for b in bindings}
        if full:
            # An unbound variant's value is never pushed, so it is not a
            # stale push either.
            drop_unbound_pending(connection)
            mark_dirty(connection, list(by_item), now)

        dirty = dirty_items(connection)
        # An unbound variant's Shopify quantity is left alone.
        unbound = [code for code in dirty if code not in by_item]
        clear_dirty(connection, unbound)
        self._forget_backoff(unbound)
        targets = [
            by_item[code]
            for code in dirty
            if code in by_item and (full or self._due(code, now))
        ]
        if not targets:
            return

        self._computing = [b.item_code for b in targets]
        match, computed = self._compute(targets, bindings, now)
        record_check(
            connection,
            ProblemKind.UNMIRRORED_ORDER,
            unmirrored_problems(match, now, UNMIRRORED_GRACE),
            now,
        )
        record_check(
            connection,
            ProblemKind.OVERSELL,
            [_oversell(c, match) for c in computed if c.sellable.shortfall > 0],
            now,
            checked=None if full else {c.binding.variant_id for c in computed},
        )
        if dry:
            self._save_decisions([(c, DRY_RUN_ACTION) for c in computed], now)
            computed_items = [c.binding.item_code for c in computed]
            clear_dirty(connection, computed_items)
            self._forget_backoff(computed_items)
            return
        self._push(computed, bindings, now)

    def _due(self, item_code: str, now: datetime) -> bool:
        backoff = self._backoff.get(item_code)
        return backoff is None or backoff.retry_at <= now

    def _forget_backoff(self, item_codes: Sequence[str]) -> None:
        for code in item_codes:
            self._backoff.pop(code, None)

    def _back_off(self, item_codes: Sequence[str], now: datetime) -> None:
        for code in item_codes:
            self._backoff[code] = self._next_backoff(self._backoff.get(code), now)

    def _next_backoff(self, previous: _Backoff | None, now: datetime) -> _Backoff:
        failures = 1 if previous is None else previous.failures + 1
        wait = min(
            self._config.worker.poll_interval * 2 ** (failures - 1),
            MAX_RETRY_BACKOFF,
        )
        return _Backoff(failures, now + wait)

    def _compute(
        self, targets: Sequence[Binding], bindings: Sequence[Binding], now: datetime
    ) -> tuple[OrderMatch, list[_Computed]]:
        """Compute the targets; skip those whose inventory item Shopify lost."""
        # Levels first: a sale after this read moves the level, so the
        # compare-and-set push fails as stale and is recomputed, instead of
        # putting the sold unit back on sale. Orders before Bins: a Sales
        # Order made between those reads subtracts its units twice for one
        # pass instead of giving them back.
        levels = fetch_levels(
            self._shopify,
            [b.inventory_item_id for b in targets],
            self._config.location_id,
        )
        orders = fetch_open_orders(self._shopify)
        so_lines = fetch_sales_order_lines(
            self._erpnext,
            [order.order_id for order in orders],
            self._config.warehouse,
        )
        match = match_orders(
            orders, so_lines, {b.variant_id: b.item_code for b in bindings}
        )
        bins = fetch_bins(
            self._erpnext, [b.item_code for b in targets], self._config.warehouse
        )
        unmirrored = match.unmirrored_by_variant
        last_pushed = last_pushed_values(self._connection)
        computed: list[_Computed] = []
        missing: list[tuple[Binding, Decision]] = []
        for b in targets:
            sellable = compute_sellable(
                bins[b.item_code], unmirrored.get(b.variant_id, 0)
            )
            level = levels.get(b.inventory_item_id)
            if level is None:
                decision = Decision(
                    item_code=b.item_code,
                    explanation=sellable.explanation(),
                    shopify_available=None,
                    last_pushed=last_pushed.get(b.variant_id),
                    action=MISSING_ACTION,
                    note=None,
                )
                missing.append((b, decision))
            else:
                computed.append(
                    _Computed(b, sellable, level, last_pushed.get(b.variant_id))
                )
        self._skip_missing(missing, now)
        return match, computed

    def _skip_missing(
        self, missing: Sequence[tuple[Binding, Decision]], now: datetime
    ) -> None:
        if not missing:
            return
        save_decisions(self._connection, [(b.variant_id, d) for b, d in missing], now)
        codes = [b.item_code for b, _ in missing]
        record_failed_attempt(self._connection, codes)
        self._back_off(codes, now)
        # Most likely the variant was deleted; a full pass unbinds or rebinds it.
        self._full_pass_requested = True

    def _push(
        self,
        computed: Sequence[_Computed],
        bindings: Sequence[Binding],
        now: datetime,
    ) -> None:
        stale = self._push_round(computed, now, retry_stale=True)
        if stale:
            _, recomputed = self._compute(stale, bindings, now)
            self._push_round(recomputed, now, retry_stale=False)

    def _push_round(
        self, computed: Sequence[_Computed], now: datetime, *, retry_stale: bool
    ) -> list[Binding]:
        """Push and record the outcomes; return the stale ones to retry, if asked.

        A stale variant not retried, or a held one, stays dirty for the next
        pass.
        """
        held = [c for c in computed if self._hold(c, now)]
        self._save_decisions([(c, HELD_ACTION) for c in held], now)
        computed = [c for c in computed if c not in held]
        result = push(
            self._connection,
            self._shopify,
            self._config.location_id,
            [c.target() for c in computed],
            now,
        )
        outcomes = result.outcomes
        retry = [
            c
            for c in computed
            if retry_stale and outcomes[c.binding.variant_id] is PushOutcome.STALE
        ]
        done = [c for c in computed if c not in retry]
        decisions: list[tuple[_Computed, str]] = []
        for c in done:
            outcome = outcomes[c.binding.variant_id]
            action = _ACTIONS[outcome]
            if outcome is PushOutcome.FAILED:
                action = f"{action}: {result.codes[c.binding.variant_id]}"
            decisions.append((c, action))
        self._save_decisions(decisions, now)
        settled = [
            c.binding.item_code
            for c in done
            if outcomes[c.binding.variant_id] in _SETTLED
        ]
        failed = [
            c.binding.item_code for c in done if c.binding.item_code not in settled
        ]
        clear_dirty(self._connection, settled)
        self._forget_backoff(settled)
        for c in done:
            if c.binding.item_code in settled:
                self._held_since.pop(c.binding.variant_id, None)
        record_failed_attempt(self._connection, failed)
        self._back_off(failed, now)
        return [c.binding for c in retry]

    def _hold(self, computed: _Computed, now: datetime) -> bool:
        """Whether to hold a push that would raise a level a sale may have lowered.

        A level below the last pushed value can hold a sale whose order the
        orders search does not return yet; pushing would put the sold unit
        back on sale. After the hold the push goes ahead, so a hand edit is
        still overwritten.
        """
        variant = computed.binding.variant_id
        level = computed.level.available
        last = computed.last_pushed
        if (
            level is None
            or last is None
            or level >= last
            or computed.sellable.sellable <= level
        ):
            self._held_since.pop(variant, None)
            return False
        since = self._held_since.setdefault(variant, now)
        return now - since < SALE_SETTLE_HOLD

    def _record_binding_problems(
        self, unbound: Sequence[UnboundVariant], now: datetime
    ) -> None:
        found: dict[ProblemKind, list[Problem]] = {
            kind: [] for kind in _BINDING_PROBLEM_KINDS.values()
        }
        for variant in unbound:
            kind = _BINDING_PROBLEM_KINDS[variant.kind]
            found[kind].append(
                Problem(kind, variant.variant_id, _binding_details(variant))
            )
        for kind, problems in found.items():
            record_check(self._connection, kind, problems, now)

    def _save_decisions(
        self, decisions: Sequence[tuple[_Computed, str]], now: datetime
    ) -> None:
        save_decisions(
            self._connection,
            [(c.binding.variant_id, c.decision(action)) for c, action in decisions],
            now,
        )


def _oversell(computed: _Computed, match: OrderMatch) -> Problem:
    variant = computed.binding.variant_id
    policy = computed.level.policy
    orders = match.newest_orders_with(variant)[:NEWEST_ORDERS_SHOWN]
    # The shortfall and orders move with every order and fulfilment: context,
    # so a standing oversell is not emailed again for each.
    return Problem(
        ProblemKind.OVERSELL,
        variant,
        {
            "Item": computed.binding.item_code,
            "Shopify inventory policy": _POLICY_TEXTS.get(policy, policy),
        },
        context={
            "Short by": str(computed.sellable.shortfall),
            "Newest Shopify orders": ", ".join(orders) or "none open",
        },
    )


def _binding_details(variant: UnboundVariant) -> dict[str, str]:
    details = {
        SKU_DETAIL: variant.sku or NO_SKU,
        "Reason": _BINDING_REASONS[variant.kind],
    }
    if variant.kind is not UnboundKind.SKU_CHANGED:
        return details | _related_details(variant)
    details["Bound item"] = variant.item_codes[0]
    blocker = variant.accepted_unbound
    if blocker is not None:
        reason = _BINDING_REASONS[blocker.kind]
        details["Accepted SKU"] = f"Does not bind yet: {reason}"
        details |= _related_details(blocker)
    return details


def _related_details(variant: UnboundVariant) -> dict[str, str]:
    """The Items and variants that keep `variant` from binding."""
    details: dict[str, str] = {}
    if variant.kind is UnboundKind.NEAR_MATCH:
        details["Similar item codes"] = ", ".join(variant.item_codes)
    elif variant.item_codes:
        details["Item"] = variant.item_codes[0]
    if variant.other_variant_ids:
        details["Other variants"] = ", ".join(variant.other_variant_ids)
    return details
