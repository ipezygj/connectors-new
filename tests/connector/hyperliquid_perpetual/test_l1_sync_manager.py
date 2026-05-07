"""Technical implementation for Hummingbot Gateway V2.1."""

import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

import pytest

from connector.derivative.hyperliquid_perpetual.hyperliquid_l1_sync_manager import (
    L1HeartbeatValidator,
    SyncStatus,
    ValidationResult,
)

# ---------------------------------------------------------------------------
# Fakes and helpers
# ---------------------------------------------------------------------------


class FakeConnector:
    """In-memory fake satisfying ``ConnectorProtocol`` for unit tests."""

    def __init__(self) -> None:
        self.tops: Dict[str, Dict[str, float]] = {}
        self.ws_times: Dict[str, int] = {}
        self.snapshots: Dict[str, Dict[str, Any]] = {}
        self.mids: Dict[str, str] = {}
        self.replace_calls: List[tuple] = []
        self.l2_call_count: int = 0
        self.allmids_call_count: int = 0
        self.smoke_call_count: int = 0
        self.l2_should_raise: Optional[BaseException] = None
        self.allmids_should_raise: Optional[BaseException] = None

    async def get_l2_snapshot(self, symbol: str) -> Dict[str, Any]:
        self.l2_call_count += 1
        if self.l2_should_raise is not None:
            raise self.l2_should_raise
        return self.snapshots[symbol]

    async def read_top_of_book(self, symbol: str) -> Optional[Dict[str, float]]:
        return self.tops.get(symbol)

    async def replace_order_book(self, symbol: str, snapshot: Dict[str, Any]) -> None:
        self.replace_calls.append((symbol, snapshot))

    def last_ws_time_ms(self, symbol: str) -> Optional[int]:
        if symbol == "__protocol_smoke__":
            self.smoke_call_count += 1
            return None
        return self.ws_times.get(symbol)

    async def get_all_mids(self) -> Dict[str, str]:
        self.allmids_call_count += 1
        if self.allmids_should_raise is not None:
            raise self.allmids_should_raise
        return self.mids


async def _no_op_callback(_: ValidationResult) -> None:
    return None


def _make_l2(time_ms: int, bid: float, ask: float, coin: str = "BTC") -> Dict[str, Any]:
    """Build a minimal l2Book-style payload."""
    return {
        "coin": coin,
        "time": time_ms,
        "levels": [
            [{"px": str(bid), "sz": "1.0", "n": 1}],
            [{"px": str(ask), "sz": "1.0", "n": 1}],
        ],
    }


def _build_validator(connector: FakeConnector, **kwargs: Any) -> L1HeartbeatValidator:
    """Construct a validator with sensible test defaults."""
    defaults: Dict[str, Any] = {
        "on_drift_critical": _no_op_callback,
        "symbols": ["BTC"],
    }
    defaults.update(kwargs)
    return L1HeartbeatValidator(connector=connector, **defaults)


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------


def test_no_drift_no_resync() -> None:
    """Matched TOB and fresh timestamp produce status OK with no resync."""
    fake = FakeConnector()
    fake.tops["BTC"] = {"bid": 50_000.0, "ask": 50_001.0, "time_ms": 1_000.0}
    fake.ws_times["BTC"] = 1_000
    fake.snapshots["BTC"] = _make_l2(1_100, 50_000.0, 50_001.0)

    validator = _build_validator(fake)
    result = asyncio.run(validator._validate_symbol("BTC"))

    assert result.status == SyncStatus.OK
    assert result.drift_bps is not None and result.drift_bps < 1e-6
    assert fake.replace_calls == []


def test_staleness_triggers_resync() -> None:
    """REST timestamp far ahead of cached WS timestamp forces a resync."""
    fake = FakeConnector()
    fake.tops["BTC"] = {"bid": 50_000.0, "ask": 50_001.0, "time_ms": 1_000.0}
    fake.ws_times["BTC"] = 1_000
    # REST snapshot timestamp >> ws_time + staleness threshold (default 1500 ms).
    fake.snapshots["BTC"] = _make_l2(5_000, 50_000.0, 50_001.0)

    validator = _build_validator(fake)
    result = asyncio.run(validator._validate_symbol("BTC"))

    assert result.status == SyncStatus.RESYNCED
    assert len(fake.replace_calls) == 1
    assert fake.replace_calls[0][0] == "BTC"


def test_drift_triggers_resync() -> None:
    """Top-of-book drift above threshold (5 bps default) forces a resync."""
    fake = FakeConnector()
    fake.tops["BTC"] = {"bid": 50_000.0, "ask": 50_001.0, "time_ms": 1_000.0}
    fake.ws_times["BTC"] = 1_000
    # ~10 bps drift (REST mid ~50050.5 vs local ~50000.5).
    fake.snapshots["BTC"] = _make_l2(1_100, 50_050.0, 50_051.0)

    validator = _build_validator(fake)
    result = asyncio.run(validator._validate_symbol("BTC"))

    assert result.status == SyncStatus.RESYNCED
    assert result.drift_bps is not None and result.drift_bps > 5.0
    assert len(fake.replace_calls) == 1


def test_failure_increments_counter() -> None:
    """REST exception yields SYNC_FAILURE status and increments counter."""
    fake = FakeConnector()
    fake.tops["BTC"] = {"bid": 50_000.0, "ask": 50_001.0, "time_ms": 1_000.0}
    fake.ws_times["BTC"] = 1_000
    fake.l2_should_raise = ConnectionError("simulated network failure")

    validator = _build_validator(fake)
    result = asyncio.run(validator._validate_symbol("BTC"))
    validator._update_results_and_metrics([result])

    assert result.status == SyncStatus.SYNC_FAILURE
    assert validator.metrics()["failure_count"] == 1
    assert validator.metrics()["consecutive_failures"] == 1
    assert fake.replace_calls == []


def test_max_failures_does_not_halt() -> None:
    """Per task brief: sync failures alone never invoke the halt callback."""
    fake = FakeConnector()
    fake.tops["BTC"] = {"bid": 50_000.0, "ask": 50_001.0, "time_ms": 1_000.0}
    fake.ws_times["BTC"] = 1_000
    fake.l2_should_raise = TimeoutError("simulated timeout")

    callback_calls: List[ValidationResult] = []

    async def callback(result: ValidationResult) -> None:
        callback_calls.append(result)

    validator = L1HeartbeatValidator(
        connector=fake,
        on_drift_critical=callback,
        symbols=["BTC"],
        max_consecutive_failures=2,
    )

    async def run_n_cycles() -> None:
        for _ in range(5):
            r = await validator._validate_symbol("BTC")
            validator._update_results_and_metrics([r])

    asyncio.run(run_n_cycles())

    assert validator.metrics()["failure_count"] == 5
    assert validator.metrics()["consecutive_failures"] == 5
    assert callback_calls == []  # halt callback never fires on sync failures


def test_critical_drift_triggers_halt() -> None:
    """Drift above halt threshold yields DRIFT_CRITICAL and fires the callback."""
    fake = FakeConnector()
    fake.tops["BTC"] = {"bid": 50_000.0, "ask": 50_001.0, "time_ms": 1_000.0}
    fake.ws_times["BTC"] = 1_000
    # ~1000 bps drift, far above halt threshold (default 100 bps).
    fake.snapshots["BTC"] = _make_l2(1_100, 55_000.0, 55_001.0)

    callback_calls: List[ValidationResult] = []

    async def callback(result: ValidationResult) -> None:
        callback_calls.append(result)

    validator = L1HeartbeatValidator(
        connector=fake,
        on_drift_critical=callback,
        symbols=["BTC"],
    )

    async def runner() -> None:
        result = await validator._validate_symbol("BTC")
        # Allow the spawned callback task to run.
        await asyncio.sleep(0)
        return result

    result = asyncio.run(runner())

    assert result.status == SyncStatus.DRIFT_CRITICAL
    assert result.drift_bps is not None and result.drift_bps > 100.0
    assert len(callback_calls) == 1
    assert callback_calls[0].status == SyncStatus.DRIFT_CRITICAL


def test_shadow_mode_does_not_resync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shadow mode suppresses replace_order_book calls and halt callbacks."""
    monkeypatch.setenv("L1_SYNC_SHADOW_MODE", "1")

    fake = FakeConnector()
    fake.tops["BTC"] = {"bid": 50_000.0, "ask": 50_001.0, "time_ms": 1_000.0}
    fake.ws_times["BTC"] = 1_000
    # Drift exceeding both threshold and halt threshold.
    fake.snapshots["BTC"] = _make_l2(1_100, 55_000.0, 55_001.0)

    callback_calls: List[ValidationResult] = []

    async def callback(result: ValidationResult) -> None:
        callback_calls.append(result)

    validator = L1HeartbeatValidator(
        connector=fake,
        on_drift_critical=callback,
        symbols=["BTC"],
    )

    assert validator.shadow_mode is True

    async def runner() -> None:
        r = await validator._validate_symbol("BTC")
        await asyncio.sleep(0)
        return r

    result = asyncio.run(runner())

    # Status still reflects detected drift, but actions suppressed.
    assert result.status == SyncStatus.DRIFT_CRITICAL
    assert fake.replace_calls == []
    assert callback_calls == []


def test_either_or_symbols_api() -> None:
    """Constructor rejects both/neither static list and provider callable."""
    fake = FakeConnector()

    with pytest.raises(ValueError, match="Exactly one of"):
        L1HeartbeatValidator(connector=fake, on_drift_critical=_no_op_callback)

    with pytest.raises(ValueError, match="Exactly one of"):
        L1HeartbeatValidator(
            connector=fake,
            on_drift_critical=_no_op_callback,
            symbols=["BTC"],
            symbols_provider=lambda: ["ETH"],
        )

    # Either alone is accepted.
    L1HeartbeatValidator(connector=fake, on_drift_critical=_no_op_callback, symbols=["BTC"])
    L1HeartbeatValidator(
        connector=fake,
        on_drift_critical=_no_op_callback,
        symbols_provider=lambda: ["ETH"],
    )


def test_graceful_cancellation() -> None:
    """start() then stop() cleanly cancels the loop task."""
    fake = FakeConnector()
    fake.tops["BTC"] = {"bid": 100.0, "ask": 100.1, "time_ms": 1.0}
    fake.ws_times["BTC"] = 1
    fake.snapshots["BTC"] = _make_l2(1, 100.0, 100.1)

    validator = _build_validator(fake, interval_sec=10.0)

    async def lifecycle() -> None:
        assert not validator.is_running
        await validator.start()
        assert validator.is_running
        # Yield once so the loop spawns and runs at least one cycle.
        await asyncio.sleep(0.05)
        await validator.stop()
        assert not validator.is_running
        # Idempotent stop.
        await validator.stop()

    asyncio.run(lifecycle())
    assert fake.l2_call_count >= 1


def test_protocol_smoke_call_at_init() -> None:
    """Connector whose last_ws_time_ms raises fails init with TypeError."""

    class BrokenConnector:
        async def get_l2_snapshot(self, symbol: str) -> Dict[str, Any]:
            return {}

        async def read_top_of_book(self, symbol: str) -> Optional[Dict[str, float]]:
            return None

        async def replace_order_book(self, symbol: str, snapshot: Dict[str, Any]) -> None:
            return None

        def last_ws_time_ms(self, symbol: str) -> Optional[int]:
            raise RuntimeError("contract violation simulated")

        async def get_all_mids(self) -> Dict[str, str]:
            return {}

    with pytest.raises(TypeError, match="protocol smoke call"):
        L1HeartbeatValidator(
            connector=BrokenConnector(),
            on_drift_critical=_no_op_callback,
            symbols=["BTC"],
        )


def test_bulk_fallback_at_threshold() -> None:
    """Bulk tripwire fires at threshold and flags only divergent symbols."""
    fake = FakeConnector()
    symbols = [f"COIN{i}" for i in range(10)]

    # Most symbols agree (mids match local TOB); one diverges by 50%.
    for sym in symbols:
        fake.tops[sym] = {"bid": 100.0, "ask": 100.10, "time_ms": 1.0}
        fake.ws_times[sym] = 1
        fake.snapshots[sym] = _make_l2(1, 100.0, 100.10, coin=sym)
    fake.mids = {sym: "100.05" for sym in symbols}
    fake.mids["COIN5"] = "150.0"  # divergent mid

    validator = _build_validator(fake, symbols=symbols)
    flagged = asyncio.run(validator._bulk_tripwire(symbols))

    assert "COIN5" in flagged
    assert "COIN0" not in flagged
    assert fake.allmids_call_count == 1


def test_status_transition_logging_only(caplog: pytest.LogCaptureFixture) -> None:
    """Steady-state OK status logs once on first transition, not on repeats."""
    fake = FakeConnector()
    fake.tops["BTC"] = {"bid": 100.0, "ask": 100.1, "time_ms": 1_000.0}
    fake.ws_times["BTC"] = 1_000
    fake.snapshots["BTC"] = _make_l2(1_100, 100.0, 100.1)

    validator = _build_validator(fake)

    caplog.set_level(logging.INFO, logger="connector.derivative.hyperliquid_perpetual.hyperliquid_l1_sync_manager")

    async def runner() -> None:
        for _ in range(3):
            r = await validator._validate_symbol("BTC")
            validator._update_results_and_metrics([r])

    asyncio.run(runner())

    transition_lines = [rec for rec in caplog.records if rec.message.startswith("L1 sync BTC:")]
    assert len(transition_lines) == 1
    assert "INITIAL" in transition_lines[0].message
    assert "ok" in transition_lines[0].message
