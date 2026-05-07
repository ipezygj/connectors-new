"""Technical implementation for Hummingbot Gateway V2.1."""

"""
L1 State Synchronization Manager — design and module structure.

Purpose
=======
Periodic validator that detects state drift between the local order-book
cache (populated from the WebSocket ``l2Book`` snapshot stream) and the
authoritative REST ``info`` endpoint. On drift, the validator silently
re-syncs the affected symbol's order book without interrupting the
broader connector lifecycle. On repeated upstream failure or
catastrophic drift, it surfaces an actionable status to the connector.

Drift-detection strategy
========================
Hyperliquid's public API does not expose a sequence number on user-facing
order-book channels. The validator therefore relies on two orthogonal
signals available in both the WS and REST ``l2Book`` payloads:

1. **Timestamp staleness** — the ``time`` field (milliseconds) on the
   most recent WS frame is compared against the REST snapshot's
   ``time``. A REST timestamp ahead of the cached WS timestamp by more
   than ``L1_SYNC_STALENESS_THRESHOLD_MS`` indicates the WS stream is
   lagging or has silently disconnected.

2. **Top-of-book divergence** — the cached top bid/ask is compared
   against the REST snapshot's top bid/ask. A divergence above
   ``L1_SYNC_DRIFT_THRESHOLD_BPS`` indicates state drift regardless of
   timestamp freshness.

Either condition triggers a silent re-sync (overwrite local cache with
the REST snapshot). Catastrophic divergence above
``L1_SYNC_DRIFT_HALT_BPS`` escalates to ``DRIFT_CRITICAL`` and invokes
the ``on_drift_critical`` callback for connector-level pause.

This matches the canonical pattern used by Hyperliquid's reference
``order_book_server`` (periodic snapshot fetch + comparison + corrective
action). The full-snapshot diff variant is a candidate Phase 3 extension
for catching middle-of-book changes that do not affect top-of-book.

Fail-closed semantics
=====================
* REST request failure (timeout, 5xx, network error) increments an
  in-memory ``consecutive_failures`` counter. The status reports
  ``SYNC_FAILURE`` for that cycle but does not halt the connector
  until the counter reaches ``L1_SYNC_MAX_CONSECUTIVE_FAILURES``.
* Successful cycles reset the counter to zero.
* The counter is intentionally not persisted across process restarts:
  any restart re-snapshots all symbols on WS reconnect, making a fresh
  counter the correct post-restart state.

Bulk fallback at scale
======================
For symbol counts at or above ``L1_SYNC_BULK_FALLBACK_SYMBOL_COUNT``,
the validator switches its tripwire to a single ``allMids`` REST call
per cycle (one round-trip, no authentication) and only escalates to a
full ``l2Book`` fetch for symbols whose mid-price diverges from the
local top-of-book midpoint. This keeps the per-cycle REST budget under
the public-tier 20 req/sec limit and leaves API capacity for the
connector's primary order-flow traffic.

Performance budget
==================
Target: per-cycle processing time below 100 ms.

* Single-symbol path: one REST round-trip (~50–90 ms typical) plus
  parsing and comparison (<1 ms).
* Multi-symbol path: ``asyncio.gather`` parallelizes the REST calls;
  wall-clock cost is approximately ``max(per_symbol_RTT)``, not
  ``N * per_symbol_RTT``.
* Bulk path (N >= threshold): one ``allMids`` call (~50 ms) plus
  selective ``l2Book`` calls only for suspicious symbols.
* Cycle period of ``L1_SYNC_INTERVAL_SEC`` (default 1.0 s) provides
  10x headroom over the 100 ms processing budget.

Shadow mode
===========
When the ``L1_SYNC_SHADOW_MODE`` environment variable is set to ``1``,
the validator performs the full detection cycle (REST fetches, drift
computation, status reporting) but suppresses re-sync writes and
``DRIFT_CRITICAL`` callback invocations. The mode is intended for
threshold calibration during the first 24–48 h of deployment: collect
real-world drift distributions before trusting the validator with
write authority. Default is OFF.

Logging discipline
==================
At one validation per second across multiple symbols the per-cycle log
volume is otherwise excessive. The validator emits structured log lines
only on status transitions (``OK -> RESYNCED``, ``OK -> SYNC_FAILURE``,
``RESYNCED -> OK``, etc.) and a periodic summary line every
``L1_SYNC_LOG_SUMMARY_INTERVAL_SEC`` seconds containing aggregate
counters.

Observability
=============
``metrics()`` returns a dictionary of monotonic counters
(``total_cycles``, ``resync_count``, ``failure_count``,
``drift_critical_count``, ``bulk_fallback_count``). ``last_results``
returns the most recent per-symbol :class:`ValidationResult`. Both
hooks are zero-cost reads suitable for ops dashboards or supervisory
processes.

Connector integration contract
==============================
The validator depends on a connector implementing the
:class:`ConnectorProtocol` structural interface:

* ``get_l2_snapshot(trading_pair)`` — async, returns the REST
  ``l2Book`` response.
* ``read_top_of_book(symbol)`` — synchronous, returns the cached top
  bid/ask snapshot under the connector's order-book lock; returns
  ``None`` if no cache entry exists.
* ``replace_order_book(symbol, snapshot)`` — async, atomically
  installs a fresh order-book snapshot under the connector's lock.
* ``last_ws_time_ms(symbol)`` — synchronous, returns the timestamp of
  the most recent WS frame applied to the cache; returns ``None`` if
  the connector has not yet wired this tracker, in which case the
  validator falls back to drift-only detection (no staleness check).

The current local connector
(``hyperliquid_perpetual_derivative.HyperliquidPerpetualDerivative``)
does not yet implement ``read_top_of_book``,
``replace_order_book``, or ``last_ws_time_ms``. Wiring those hooks is
a Phase 2 integration task; the validator is decoupled from the
concrete class via :class:`typing.Protocol` so the connector can adopt
the contract without inheritance changes.

Phase split
===========
* **Phase 1 (this module).** Skeleton: signatures, docstrings,
  contract definitions, local constants. All method bodies raise
  :class:`NotImplementedError`. No production-file modifications. No
  test coverage yet because there is nothing to exercise.
* **Phase 2.** Implementation of the skeleton bodies plus a pytest
  suite (target cases listed under ``Test strategy`` below). Move
  constants to ``hyperliquid_perpetual_constants.py``. Wire the four
  protocol methods into the connector. Extend
  ``.github/workflows/consistency-utils.yml`` to cover this directory.
* **Phase 3 (optional).** Full-snapshot diff for middle-of-book
  detection. Persistence of the failure counter if production telemetry
  shows it adds value.

Test strategy (Phase 2)
=======================
Twelve target pytest cases in ``tests/connector/hyperliquid_perpetual/
test_l1_sync_manager.py``:

* ``test_no_drift_no_resync``
* ``test_staleness_triggers_resync``
* ``test_drift_triggers_resync``
* ``test_failure_increments_counter``
* ``test_max_failures_triggers_halt``
* ``test_critical_drift_triggers_halt``
* ``test_shadow_mode_does_not_resync``
* ``test_either_or_symbols_api``
* ``test_graceful_cancellation``
* ``test_protocol_smoke_call_at_init``
* ``test_bulk_fallback_at_threshold``
* ``test_status_transition_logging_only``

A fake connector implementing :class:`ConnectorProtocol` is sufficient
for all twelve; no live exchange connection is required.
"""

import asyncio
import enum
import logging
import os
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, List, Optional, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Local constants (Phase 1).
# These will move to ``hyperliquid_perpetual_constants.py`` during Phase 2
# integration; they live here for now so that the skeleton does not modify
# any existing production file.
# ---------------------------------------------------------------------------
L1_SYNC_INTERVAL_SEC: float = 1.0
L1_SYNC_STALENESS_THRESHOLD_MS: int = 1500
L1_SYNC_DRIFT_THRESHOLD_BPS: float = 5.0
L1_SYNC_DRIFT_HALT_BPS: float = 100.0
L1_SYNC_MAX_CONSECUTIVE_FAILURES: int = 3
L1_SYNC_REST_TIMEOUT_SEC: float = 1.0
L1_SYNC_BULK_FALLBACK_SYMBOL_COUNT: int = 10
L1_SYNC_LOG_SUMMARY_INTERVAL_SEC: float = 60.0
L1_SYNC_SHADOW_MODE_ENV: str = "L1_SYNC_SHADOW_MODE"

logger = logging.getLogger(__name__)


class SyncStatus(enum.Enum):
    """Validator outcome for a single symbol cycle."""

    OK = "ok"
    RESYNCED = "resynced"
    SYNC_FAILURE = "sync_failure"
    DRIFT_CRITICAL = "drift_critical"


@dataclass(frozen=True)
class ValidationResult:
    """Per-symbol cycle outcome.

    :ivar symbol: The trading pair under validation.
    :ivar status: One of :class:`SyncStatus` values.
    :ivar ws_time_ms: Timestamp of the cached WS frame at the time of
        comparison, or ``None`` if the connector does not yet track it.
    :ivar rest_time_ms: Timestamp of the REST snapshot consumed in the
        comparison, or ``None`` if the REST call failed.
    :ivar drift_bps: Top-of-book divergence in basis points, or
        ``None`` if either side was unavailable.
    :ivar latency_ms: Wall-clock duration of the validation cycle for
        this symbol in milliseconds.
    :ivar message: Human-readable summary suitable for diagnostic logs.
    """

    symbol: str
    status: SyncStatus
    ws_time_ms: Optional[int]
    rest_time_ms: Optional[int]
    drift_bps: Optional[float]
    latency_ms: float
    message: str


@runtime_checkable
class ConnectorProtocol(Protocol):
    """Structural contract the validator requires from a connector.

    Implementations need not inherit from this protocol; structural
    compatibility is sufficient. The validator performs an
    ``isinstance`` check at construction time and additionally invokes
    a single read-only method during initialization to surface
    contract violations (missing methods, signature drift) before the
    validation loop is spawned.
    """

    async def get_l2_snapshot(self, trading_pair: str) -> Dict[str, object]:
        """Return the REST ``l2Book`` response for the given pair."""
        ...

    def read_top_of_book(self, symbol: str) -> Optional[Dict[str, float]]:
        """Return the cached top bid/ask under the connector's lock.

        :return: Dictionary with keys ``"bid"``, ``"ask"``,
            ``"time_ms"`` (the cached WS frame time, if tracked); or
            ``None`` if no cache entry exists for the symbol.
        """
        ...

    async def replace_order_book(self, symbol: str, snapshot: Dict[str, object]) -> None:
        """Atomically install a fresh order-book snapshot."""
        ...

    def last_ws_time_ms(self, symbol: str) -> Optional[int]:
        """Return the timestamp of the most recent WS frame applied.

        :return: Timestamp in milliseconds, or ``None`` if the
            connector has not yet wired this tracker.
        """
        ...


class L1HeartbeatValidator:
    """Periodic L1 state validator with silent re-sync.

    Construction is **either-or** with respect to the symbol source:
    pass exactly one of ``symbols`` (static list) or
    ``symbols_provider`` (callable returning the current list each
    cycle). Static is appropriate for tests and short-lived processes;
    the callable is appropriate when the connector's tracked symbol
    set changes at runtime (new positions opened, old positions
    closed).

    Lifecycle methods (:meth:`start`, :meth:`stop`) are idempotent and
    safe to call from any async context.
    """

    def __init__(
        self,
        connector: ConnectorProtocol,
        on_drift_critical: Callable[[ValidationResult], Awaitable[None]],
        symbols: Optional[List[str]] = None,
        symbols_provider: Optional[Callable[[], List[str]]] = None,
        interval_sec: float = L1_SYNC_INTERVAL_SEC,
        staleness_threshold_ms: int = L1_SYNC_STALENESS_THRESHOLD_MS,
        drift_threshold_bps: float = L1_SYNC_DRIFT_THRESHOLD_BPS,
        drift_halt_bps: float = L1_SYNC_DRIFT_HALT_BPS,
        max_consecutive_failures: int = L1_SYNC_MAX_CONSECUTIVE_FAILURES,
        bulk_fallback_threshold: int = L1_SYNC_BULK_FALLBACK_SYMBOL_COUNT,
        rest_timeout_sec: float = L1_SYNC_REST_TIMEOUT_SEC,
    ) -> None:
        """Initialize the validator.

        :param connector: Object implementing :class:`ConnectorProtocol`.
            The constructor verifies structural compatibility via
            ``isinstance`` and a smoke read of
            :meth:`ConnectorProtocol.last_ws_time_ms` to surface
            contract violations at boot.
        :param on_drift_critical: Async callback invoked when a cycle
            yields :data:`SyncStatus.DRIFT_CRITICAL`. Suppressed when
            shadow mode is active.
        :param symbols: Static list of trading pairs to validate.
            Mutually exclusive with ``symbols_provider``.
        :param symbols_provider: Callable returning the current list
            of trading pairs each cycle. Mutually exclusive with
            ``symbols``.
        :param interval_sec: Sleep between validation cycles.
        :param staleness_threshold_ms: Maximum tolerated lag between
            cached WS time and the REST snapshot's ``time`` field
            before a re-sync is forced.
        :param drift_threshold_bps: Top-of-book divergence above
            which a re-sync is forced.
        :param drift_halt_bps: Top-of-book divergence above which the
            ``DRIFT_CRITICAL`` callback fires.
        :param max_consecutive_failures: Number of consecutive
            ``SYNC_FAILURE`` cycles tolerated before the connector
            is asked to halt.
        :param bulk_fallback_threshold: Symbol count at or above
            which the validator switches to the ``allMids`` tripwire.
        :param rest_timeout_sec: Aiohttp timeout applied to each REST
            call.
        :raises ValueError: If both or neither of ``symbols`` and
            ``symbols_provider`` are supplied, or if any numeric
            parameter is non-positive.
        :raises TypeError: If ``connector`` does not satisfy
            :class:`ConnectorProtocol`.
        """
        raise NotImplementedError("Skeleton — implementation in Phase 2.")

    # -- Lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Spawn the validation loop as an asyncio task. Idempotent.

        Calling :meth:`start` while the loop is already running is a
        no-op; a single warning is emitted to surface the duplicate
        start attempt.
        """
        raise NotImplementedError("Skeleton — implementation in Phase 2.")

    async def stop(self) -> None:
        """Cancel the validation loop and clean up. Idempotent.

        Calling :meth:`stop` before :meth:`start` is a no-op.
        Cancellation propagates :class:`asyncio.CancelledError` to any
        in-flight REST call; ``aiohttp`` sessions are closed in the
        ``finally`` block of :meth:`_run_loop`.
        """
        raise NotImplementedError("Skeleton — implementation in Phase 2.")

    # -- Introspection ------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """Return ``True`` if the validation loop task is active."""
        raise NotImplementedError("Skeleton — implementation in Phase 2.")

    @property
    def shadow_mode(self) -> bool:
        """Return ``True`` if the shadow-mode environment variable is set.

        Shadow mode is determined once at instance construction by
        reading ``os.environ[L1_SYNC_SHADOW_MODE_ENV]`` and comparing
        the value to the literal ``"1"``.
        """
        raise NotImplementedError("Skeleton — implementation in Phase 2.")

    @property
    def last_results(self) -> Dict[str, ValidationResult]:
        """Return the most recent per-symbol validation outcomes.

        The returned dictionary is a snapshot copy; callers may mutate
        it without affecting the validator's internal state.
        """
        raise NotImplementedError("Skeleton — implementation in Phase 2.")

    def metrics(self) -> Dict[str, int]:
        """Return aggregate monotonic counters for ops introspection.

        :return: Dictionary with keys ``"total_cycles"``,
            ``"resync_count"``, ``"failure_count"``,
            ``"drift_critical_count"``, ``"bulk_fallback_count"``.
        """
        raise NotImplementedError("Skeleton — implementation in Phase 2.")

    # -- Internals ----------------------------------------------------------

    async def _run_loop(self) -> None:
        """Validation loop; runs until cancelled.

        Each iteration:

        1. Resolves the active symbol set via :meth:`_resolve_symbols`.
        2. If the symbol count meets ``bulk_fallback_threshold``,
           invokes :meth:`_bulk_tripwire` and reduces the
           full-validation set to flagged symbols only.
        3. Runs :meth:`_validate_symbol` for each remaining symbol via
           ``asyncio.gather``.
        4. Updates ``last_results`` and aggregate metrics, emits
           transition-only logs and the periodic summary line.
        5. Sleeps ``interval_sec`` before the next iteration.

        Wrapped in ``try/finally`` to guarantee ``aiohttp`` session
        cleanup on cancellation.
        """
        raise NotImplementedError("Skeleton — implementation in Phase 2.")

    async def _validate_symbol(self, symbol: str) -> ValidationResult:
        """Execute a single-symbol fetch + compare + maybe-resync.

        Uses the lock-snapshot-pattern: the cached top-of-book is read
        under the connector's order-book lock and copied into a local
        immutable, after which the lock is released for the REST call.
        Re-sync writes (when triggered) re-acquire the lock and use a
        timestamp guard: only overwrite if
        ``rest_snapshot.time > current_local_top.time_ms``.
        """
        raise NotImplementedError("Skeleton — implementation in Phase 2.")

    async def _bulk_tripwire(self, symbols: List[str]) -> List[str]:
        """One ``allMids`` REST call; return symbols flagged for full check.

        :param symbols: All currently-tracked symbols.
        :return: Subset of ``symbols`` whose ``allMids`` mid-price
            diverges from the cached top-of-book midpoint by more than
            half of ``drift_threshold_bps``.
        """
        raise NotImplementedError("Skeleton — implementation in Phase 2.")

    async def _resync_symbol(self, symbol: str, rest_snapshot: Dict[str, object]) -> None:
        """Timestamp-guarded write of a REST snapshot to the connector cache.

        Suppressed when :attr:`shadow_mode` is true.
        """
        raise NotImplementedError("Skeleton — implementation in Phase 2.")

    def _compute_drift_bps(
        self,
        local_top: Optional[Dict[str, float]],
        rest_top: Dict[str, float],
    ) -> Optional[float]:
        """Top-of-book divergence in basis points.

        Defined as ``10000 * abs(local_mid - rest_mid) / rest_mid``,
        where each side's mid is ``(bid + ask) / 2``. Returns ``None``
        if ``local_top`` is missing or any mid is non-positive.
        """
        raise NotImplementedError("Skeleton — implementation in Phase 2.")

    def _resolve_symbols(self) -> List[str]:
        """Return the current active symbol list.

        Either returns the static list passed at construction or
        invokes the registered ``symbols_provider``. The result is not
        cached between cycles so the provider can change at runtime.
        """
        raise NotImplementedError("Skeleton — implementation in Phase 2.")

    async def _smoke_protocol_call(self) -> None:
        """Construction-time contract check on the connector.

        Calls ``self._connector.last_ws_time_ms`` with a synthetic
        symbol to verify the method exists and accepts a string. Any
        exception raised is wrapped in :class:`TypeError` with a
        contract-violation message before propagation.
        """
        raise NotImplementedError("Skeleton — implementation in Phase 2.")
