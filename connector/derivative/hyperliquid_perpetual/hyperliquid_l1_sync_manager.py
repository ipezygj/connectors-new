"""Technical implementation for Hummingbot Gateway V2.1."""

"""
L1 State Synchronization Manager — design and module structure.

Purpose
=======
Periodic validator that detects state drift between the local order-book
cache (populated from the WebSocket ``l2Book`` snapshot stream) and the
authoritative REST ``info`` endpoint. On drift, the validator silently
re-syncs the affected symbol's order book without interrupting the
broader connector lifecycle. On catastrophic drift, it surfaces an
actionable status to the connector via the
``on_drift_critical`` callback.

Drift-detection strategy
========================
Hyperliquid's public API does not expose a sequence number on
user-facing order-book channels. The validator therefore relies on two
orthogonal signals:

1. **Timestamp staleness** — the ``time`` field (milliseconds) on the
   most recent WS frame is compared against the REST snapshot's
   ``time``. A REST timestamp ahead of the cached WS timestamp by more
   than ``L1_SYNC_STALENESS_THRESHOLD_MS`` indicates the WS stream is
   lagging or has silently disconnected.

2. **Top-of-book divergence** — the cached top bid/ask is compared
   against the REST snapshot's top bid/ask. A divergence above
   ``L1_SYNC_DRIFT_THRESHOLD_BPS`` indicates state drift regardless of
   timestamp freshness.

Either condition triggers a silent re-sync (overwrite the local cache
with the REST snapshot). Catastrophic divergence above
``L1_SYNC_DRIFT_HALT_BPS`` escalates to ``DRIFT_CRITICAL`` and invokes
the ``on_drift_critical`` callback for connector-level pause.

Fail-closed semantics
=====================
* REST request failure (timeout, exception) increments an in-memory
  ``consecutive_failures`` counter. The status reports
  ``SYNC_FAILURE`` for that cycle but does not halt the connector;
  per the task brief, sync failures alone are not sufficient to halt.
* Successful cycles reset the counter to zero.
* The counter is intentionally not persisted across process restarts:
  any restart re-snapshots all symbols on WS reconnect, making a fresh
  counter the correct post-restart state.

Bulk fallback at scale
======================
For symbol counts at or above ``L1_SYNC_BULK_FALLBACK_SYMBOL_COUNT``,
the validator switches to a single ``allMids`` REST call per cycle as
a tripwire and only escalates to a full ``l2Book`` fetch for symbols
whose mid-price diverges from the local top-of-book midpoint by more
than half of ``L1_SYNC_DRIFT_THRESHOLD_BPS``. Keeps the per-cycle REST
budget below the public-tier rate limit and reserves API capacity for
the connector's primary order-flow traffic.

Performance budget
==================
Target: per-cycle processing time below 100 ms.

* Single-symbol path: one REST round-trip (~50–90 ms typical) plus
  parsing and comparison (<1 ms).
* Multi-symbol path: ``asyncio.gather`` parallelizes the REST calls;
  wall-clock cost is approximately ``max(per_symbol_RTT)``.
* Bulk path (N >= threshold): one ``allMids`` call (~50 ms) plus
  selective ``l2Book`` calls only for suspicious symbols.
* Cycle period of ``L1_SYNC_INTERVAL_SEC`` (default 1.0 s) provides
  10x headroom over the 100 ms processing budget.

Shadow mode
===========
When the ``L1_SYNC_SHADOW_MODE`` environment variable is set to ``1``,
the validator performs the full detection cycle but suppresses re-sync
writes and ``DRIFT_CRITICAL`` callback invocations. The mode is
intended for threshold calibration during the first 24–48 h of
deployment. Default is OFF.

Logging discipline
==================
Per-cycle logging is transition-only (``OK -> RESYNCED``,
``OK -> SYNC_FAILURE``, etc.) plus a periodic summary line every
``L1_SYNC_LOG_SUMMARY_INTERVAL_SEC`` seconds containing aggregate
counters.

Observability
=============
``metrics()`` returns a dictionary of monotonic counters
(``total_cycles``, ``resync_count``, ``failure_count``,
``drift_critical_count``, ``bulk_fallback_count``,
``consecutive_failures``). ``last_results`` returns the most recent
per-symbol :class:`ValidationResult`. Both hooks are zero-cost reads.

Connector integration contract
==============================
The validator depends on a connector implementing the
:class:`ConnectorProtocol` structural interface:

* ``async get_l2_snapshot(trading_pair)`` — REST ``l2Book`` response.
* ``async read_top_of_book(symbol)`` — cached top bid/ask under the
  connector's lock; returns ``None`` if no cache entry exists.
* ``async replace_order_book(symbol, snapshot)`` — atomic install of
  a fresh order-book snapshot.
* ``last_ws_time_ms(symbol)`` — sync read of the timestamp of the most
  recent WS frame; returns ``None`` if not yet tracked.
* ``async get_all_mids()`` — REST ``allMids`` response (mapping of
  coin to mid-price string), used by the bulk tripwire path.

Phase split
===========
* **Phase 1 (PR #6 baseline).** Skeleton: signatures, docstrings,
  contract definitions, ``NotImplementedError`` bodies.
* **Phase 2 (this update).** Method bodies, pytest suite, constants
  migrated to ``hyperliquid_perpetual_constants.py``, connector wired
  with the five protocol methods, CI extended.
* **Phase 3 (optional).** Full-snapshot diff for middle-of-book
  detection if telemetry shows top-of-book monitoring misses
  meaningful drift.
"""

import asyncio
import enum
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Protocol, Set, runtime_checkable

from hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_constants import (
    L1_SYNC_BULK_FALLBACK_SYMBOL_COUNT,
    L1_SYNC_DRIFT_HALT_BPS,
    L1_SYNC_DRIFT_THRESHOLD_BPS,
    L1_SYNC_INTERVAL_SEC,
    L1_SYNC_LOG_SUMMARY_INTERVAL_SEC,
    L1_SYNC_MAX_CONSECUTIVE_FAILURES,
    L1_SYNC_REST_TIMEOUT_SEC,
    L1_SYNC_SHADOW_MODE_ENV,
    L1_SYNC_STALENESS_THRESHOLD_MS,
)

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
    a single read-only sync method during initialization to surface
    contract violations before the validation loop is spawned.
    """

    async def get_l2_snapshot(self, trading_pair: str) -> Dict[str, Any]:
        """Return the REST ``l2Book`` response for the given pair."""
        ...

    async def read_top_of_book(self, symbol: str) -> Optional[Dict[str, float]]:
        """Return the cached top bid/ask under the connector's lock.

        :return: Dictionary with keys ``"bid"``, ``"ask"``,
            ``"time_ms"``; or ``None`` if no cache entry exists.
        """
        ...

    async def replace_order_book(self, symbol: str, snapshot: Dict[str, Any]) -> None:
        """Atomically install a fresh order-book snapshot."""
        ...

    def last_ws_time_ms(self, symbol: str) -> Optional[int]:
        """Return the timestamp of the most recent WS frame applied."""
        ...

    async def get_all_mids(self) -> Dict[str, str]:
        """Return mapping of coin symbol to mid price string."""
        ...


class L1HeartbeatValidator:
    """Periodic L1 state validator with silent re-sync.

    Construction is **either-or** with respect to the symbol source:
    pass exactly one of ``symbols`` (static list) or
    ``symbols_provider`` (callable returning the current list each
    cycle). Lifecycle methods (:meth:`start`, :meth:`stop`) are
    idempotent.
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
        """Initialize the validator. See module-level design notes for details."""
        if (symbols is None) == (symbols_provider is None):
            raise ValueError("Exactly one of `symbols` or `symbols_provider` must be supplied.")
        if interval_sec <= 0:
            raise ValueError(f"interval_sec must be positive; got {interval_sec!r}.")
        if staleness_threshold_ms <= 0:
            raise ValueError(f"staleness_threshold_ms must be positive; got {staleness_threshold_ms!r}.")
        if drift_threshold_bps <= 0:
            raise ValueError(f"drift_threshold_bps must be positive; got {drift_threshold_bps!r}.")
        if drift_halt_bps <= drift_threshold_bps:
            raise ValueError(
                f"drift_halt_bps ({drift_halt_bps!r}) must exceed " f"drift_threshold_bps ({drift_threshold_bps!r})."
            )
        if max_consecutive_failures < 1:
            raise ValueError(f"max_consecutive_failures must be >= 1; got {max_consecutive_failures!r}.")
        if bulk_fallback_threshold < 2:
            raise ValueError(f"bulk_fallback_threshold must be >= 2; got {bulk_fallback_threshold!r}.")
        if rest_timeout_sec <= 0:
            raise ValueError(f"rest_timeout_sec must be positive; got {rest_timeout_sec!r}.")

        if not isinstance(connector, ConnectorProtocol):
            raise TypeError(f"connector must implement ConnectorProtocol; got {type(connector).__name__}.")

        self._connector = connector
        self._on_drift_critical = on_drift_critical
        self._symbols = list(symbols) if symbols is not None else None
        self._symbols_provider = symbols_provider
        self._interval_sec = float(interval_sec)
        self._staleness_threshold_ms = int(staleness_threshold_ms)
        self._drift_threshold_bps = float(drift_threshold_bps)
        self._drift_halt_bps = float(drift_halt_bps)
        self._max_consecutive_failures = int(max_consecutive_failures)
        self._bulk_fallback_threshold = int(bulk_fallback_threshold)
        self._rest_timeout_sec = float(rest_timeout_sec)

        self._task: Optional[asyncio.Task] = None
        self._last_results: Dict[str, ValidationResult] = {}
        self._counters: Dict[str, int] = {
            "total_cycles": 0,
            "resync_count": 0,
            "failure_count": 0,
            "drift_critical_count": 0,
            "bulk_fallback_count": 0,
            "consecutive_failures": 0,
        }
        self._shadow_mode: bool = os.environ.get(L1_SYNC_SHADOW_MODE_ENV, "0") == "1"
        self._last_summary_monotonic: float = 0.0
        self._pending_callbacks: Set[asyncio.Task] = set()

        self._smoke_protocol_call()

    # -- Lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Spawn the validation loop as an asyncio task. Idempotent."""
        if self.is_running:
            logger.warning("L1HeartbeatValidator.start() called while already running; ignoring.")
            return
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Cancel the validation loop and clean up. Idempotent."""
        if self._task is None or self._task.done():
            self._task = None
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    # -- Introspection ------------------------------------------------------

    @property
    def is_running(self) -> bool:
        """Return ``True`` if the validation loop task is active."""
        return self._task is not None and not self._task.done()

    @property
    def shadow_mode(self) -> bool:
        """Return ``True`` if shadow-mode env-var is set."""
        return self._shadow_mode

    @property
    def last_results(self) -> Dict[str, ValidationResult]:
        """Return the most recent per-symbol validation outcomes (snapshot copy)."""
        return dict(self._last_results)

    def metrics(self) -> Dict[str, int]:
        """Return aggregate monotonic counters (snapshot copy)."""
        return dict(self._counters)

    # -- Internals ----------------------------------------------------------

    async def _run_loop(self) -> None:
        """Validation loop; runs until cancelled."""
        try:
            while True:
                cycle_start = time.monotonic()
                symbols = self._resolve_symbols()

                if not symbols:
                    await asyncio.sleep(self._interval_sec)
                    continue

                if len(symbols) >= self._bulk_fallback_threshold:
                    self._counters["bulk_fallback_count"] += 1
                    symbols_to_validate = await self._bulk_tripwire(symbols)
                else:
                    symbols_to_validate = list(symbols)

                if symbols_to_validate:
                    results = await asyncio.gather(
                        *(self._validate_symbol(s) for s in symbols_to_validate),
                        return_exceptions=False,
                    )
                    self._update_results_and_metrics(results)

                self._counters["total_cycles"] += 1
                self._maybe_log_summary()

                elapsed = time.monotonic() - cycle_start
                sleep_remaining = max(0.0, self._interval_sec - elapsed)
                await asyncio.sleep(sleep_remaining)
        except asyncio.CancelledError:
            logger.info("L1HeartbeatValidator loop cancelled.")
            raise

    async def _validate_symbol(self, symbol: str) -> ValidationResult:
        """Single-symbol fetch + compare + maybe-resync."""
        cycle_start = time.monotonic()

        local_top = await self._connector.read_top_of_book(symbol)
        ws_time_ms = self._connector.last_ws_time_ms(symbol)

        try:
            rest_snapshot = await asyncio.wait_for(
                self._connector.get_l2_snapshot(symbol),
                timeout=self._rest_timeout_sec,
            )
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
            latency_ms = (time.monotonic() - cycle_start) * 1000
            return ValidationResult(
                symbol=symbol,
                status=SyncStatus.SYNC_FAILURE,
                ws_time_ms=ws_time_ms,
                rest_time_ms=None,
                drift_bps=None,
                latency_ms=latency_ms,
                message=f"REST fetch failed: {type(exc).__name__}: {exc}",
            )

        rest_time_ms = rest_snapshot.get("time")
        rest_top = self._extract_rest_top(rest_snapshot)
        drift_bps = self._compute_drift_bps(local_top, rest_top)

        stale = False
        if ws_time_ms is not None and rest_time_ms is not None:
            stale = (rest_time_ms - ws_time_ms) > self._staleness_threshold_ms

        if drift_bps is not None and drift_bps > self._drift_halt_bps:
            latency_ms = (time.monotonic() - cycle_start) * 1000
            result = ValidationResult(
                symbol=symbol,
                status=SyncStatus.DRIFT_CRITICAL,
                ws_time_ms=ws_time_ms,
                rest_time_ms=rest_time_ms,
                drift_bps=drift_bps,
                latency_ms=latency_ms,
                message=(f"Drift {drift_bps:.2f} bps exceeds halt threshold " f"{self._drift_halt_bps:.2f} bps."),
            )
            if not self._shadow_mode:
                self._spawn_callback(self._on_drift_critical(result))
            return result

        needs_resync = (drift_bps is not None and drift_bps > self._drift_threshold_bps) or stale

        if needs_resync:
            if not self._shadow_mode:
                await self._resync_symbol(symbol, rest_snapshot)
            latency_ms = (time.monotonic() - cycle_start) * 1000
            return ValidationResult(
                symbol=symbol,
                status=SyncStatus.RESYNCED,
                ws_time_ms=ws_time_ms,
                rest_time_ms=rest_time_ms,
                drift_bps=drift_bps,
                latency_ms=latency_ms,
                message=(f"Resynced (shadow={self._shadow_mode}, " f"drift_bps={drift_bps}, stale={stale})."),
            )

        latency_ms = (time.monotonic() - cycle_start) * 1000
        return ValidationResult(
            symbol=symbol,
            status=SyncStatus.OK,
            ws_time_ms=ws_time_ms,
            rest_time_ms=rest_time_ms,
            drift_bps=drift_bps,
            latency_ms=latency_ms,
            message="OK",
        )

    async def _bulk_tripwire(self, symbols: List[str]) -> List[str]:
        """One ``allMids`` REST call; return symbols flagged for full check."""
        try:
            mids = await asyncio.wait_for(
                self._connector.get_all_mids(),
                timeout=self._rest_timeout_sec,
            )
        except (asyncio.TimeoutError, Exception) as exc:  # noqa: BLE001
            logger.warning(
                "allMids tripwire failed (%s: %s); falling back to full validation.",
                type(exc).__name__,
                exc,
            )
            return list(symbols)

        flagged: List[str] = []
        half_threshold = self._drift_threshold_bps / 2.0
        for symbol in symbols:
            rest_mid_str = mids.get(symbol)
            if rest_mid_str is None:
                flagged.append(symbol)
                continue
            try:
                rest_mid = float(rest_mid_str)
            except (TypeError, ValueError):
                flagged.append(symbol)
                continue
            local_top = await self._connector.read_top_of_book(symbol)
            if local_top is None:
                flagged.append(symbol)
                continue
            local_mid = (local_top["bid"] + local_top["ask"]) / 2.0
            if local_mid <= 0 or rest_mid <= 0:
                flagged.append(symbol)
                continue
            drift_bps = abs(local_mid - rest_mid) / rest_mid * 10000.0
            if drift_bps > half_threshold:
                flagged.append(symbol)
        return flagged

    async def _resync_symbol(self, symbol: str, rest_snapshot: Dict[str, Any]) -> None:
        """Timestamp-guarded write of REST snapshot to connector cache."""
        if self._shadow_mode:
            return
        current_ws_time = self._connector.last_ws_time_ms(symbol)
        rest_time = rest_snapshot.get("time")
        if current_ws_time is not None and rest_time is not None and rest_time <= current_ws_time:
            return
        await self._connector.replace_order_book(symbol, rest_snapshot)

    def _compute_drift_bps(
        self,
        local_top: Optional[Dict[str, float]],
        rest_top: Optional[Dict[str, float]],
    ) -> Optional[float]:
        """Top-of-book divergence in basis points."""
        if local_top is None or rest_top is None:
            return None
        try:
            local_mid = (local_top["bid"] + local_top["ask"]) / 2.0
            rest_mid = (rest_top["bid"] + rest_top["ask"]) / 2.0
        except (KeyError, TypeError):
            return None
        if local_mid <= 0 or rest_mid <= 0:
            return None
        return abs(local_mid - rest_mid) / rest_mid * 10000.0

    def _resolve_symbols(self) -> List[str]:
        """Either-or resolution of static list vs callable provider."""
        if self._symbols is not None:
            return list(self._symbols)
        return list(self._symbols_provider())

    def _smoke_protocol_call(self) -> None:
        """Construction-time contract check on the connector."""
        try:
            self._connector.last_ws_time_ms("__protocol_smoke__")
        except Exception as exc:
            raise TypeError(
                f"Connector failed protocol smoke call on last_ws_time_ms: " f"{type(exc).__name__}: {exc}"
            ) from exc

    # -- Helpers ------------------------------------------------------------

    @staticmethod
    def _extract_rest_top(rest_snapshot: Dict[str, Any]) -> Optional[Dict[str, float]]:
        """Extract the top bid/ask from a REST l2Book payload."""
        levels = rest_snapshot.get("levels")
        if not isinstance(levels, list) or len(levels) < 2:
            return None
        bids, asks = levels[0], levels[1]
        if not bids or not asks:
            return None
        try:
            return {"bid": float(bids[0]["px"]), "ask": float(asks[0]["px"])}
        except (KeyError, TypeError, ValueError):
            return None

    def _update_results_and_metrics(self, results: List[ValidationResult]) -> None:
        """Apply per-symbol results to last_results and update counters."""
        any_failure = False
        for result in results:
            previous = self._last_results.get(result.symbol)
            if previous is None or previous.status != result.status:
                logger.info(
                    "L1 sync %s: %s -> %s | %s",
                    result.symbol,
                    previous.status.value if previous else "INITIAL",
                    result.status.value,
                    result.message,
                )

            if result.status == SyncStatus.RESYNCED:
                self._counters["resync_count"] += 1
            elif result.status == SyncStatus.SYNC_FAILURE:
                self._counters["failure_count"] += 1
                any_failure = True
            elif result.status == SyncStatus.DRIFT_CRITICAL:
                self._counters["drift_critical_count"] += 1

            self._last_results[result.symbol] = result

        if any_failure:
            self._counters["consecutive_failures"] += 1
        else:
            self._counters["consecutive_failures"] = 0

    def _maybe_log_summary(self) -> None:
        """Emit the periodic aggregate counters line."""
        now = time.monotonic()
        if now - self._last_summary_monotonic < L1_SYNC_LOG_SUMMARY_INTERVAL_SEC:
            return
        self._last_summary_monotonic = now
        logger.info(
            "L1 sync summary | cycles=%d resyncs=%d failures=%d critical=%d bulk=%d cf=%d",
            self._counters["total_cycles"],
            self._counters["resync_count"],
            self._counters["failure_count"],
            self._counters["drift_critical_count"],
            self._counters["bulk_fallback_count"],
            self._counters["consecutive_failures"],
        )

    def _spawn_callback(self, coro: Awaitable[None]) -> None:
        """Launch ``on_drift_critical`` without awaiting; track lifetime."""
        task = asyncio.ensure_future(coro)
        self._pending_callbacks.add(task)
        task.add_done_callback(self._pending_callbacks.discard)
