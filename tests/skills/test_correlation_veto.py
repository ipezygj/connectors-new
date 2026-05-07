"""Technical implementation for Hummingbot Gateway V2.1."""

from unittest.mock import MagicMock

import numpy as np
import pytest

from skills.correlation_veto_skill import PortfolioCorrelationSkill


def _build_skill(portfolio_returns: np.ndarray, candidate_returns: np.ndarray):
    """Construct a skill instance with ``unittest.mock`` providers.

    :param portfolio_returns: Array returned by the portfolio provider.
    :param candidate_returns: Array returned by the asset provider for
        any symbol.
    :return: Tuple ``(skill, portfolio_mock, asset_mock)``.
    """
    portfolio_mock = MagicMock(return_value=portfolio_returns)
    asset_mock = MagicMock(return_value=candidate_returns)
    skill = PortfolioCorrelationSkill(
        portfolio_returns_provider=portfolio_mock,
        asset_returns_provider=asset_mock,
    )
    return skill, portfolio_mock, asset_mock


def test_high_correlation_veto() -> None:
    """Perfect correlation (rho = 1.0) against an open position vetoes."""
    rng = np.random.default_rng(0)
    base = rng.standard_normal(288)
    portfolio = np.array([base])
    skill, portfolio_mock, asset_mock = _build_skill(portfolio, base)

    assert skill.check_veto("BTC") is True
    portfolio_mock.assert_called_once_with()
    asset_mock.assert_called_once_with("BTC")


def test_low_correlation_pass() -> None:
    """Orthogonal sine and cosine over a full period give rho ~ 0 -> no veto."""
    t = np.arange(288)
    sin_signal = np.sin(2.0 * np.pi * t / 288.0)
    cos_signal = np.cos(2.0 * np.pi * t / 288.0)
    portfolio = np.array([sin_signal])
    skill, _, _ = _build_skill(portfolio, cos_signal)

    assert skill.check_veto("ETH") is False


def test_insufficient_data() -> None:
    """Returns shorter than ``MIN_LOOKBACK`` raise :class:`ValueError`."""
    too_short = np.linspace(0.0, 1.0, 40)
    portfolio = np.array([too_short])
    skill, _, _ = _build_skill(portfolio, too_short)

    with pytest.raises(ValueError, match="MIN_LOOKBACK"):
        skill.check_veto("SOL")


def test_young_asset_processing() -> None:
    """Length 100 sits inside the accepted window and is processed normally."""
    t = np.arange(100)
    sin_signal = np.sin(2.0 * np.pi * t / 100.0)
    cos_signal = np.cos(2.0 * np.pi * t / 100.0)
    portfolio = np.array([sin_signal])
    skill, _, _ = _build_skill(portfolio, cos_signal)

    # Length 100 is between MIN_LOOKBACK (50) and RETURNS_LOOKBACK (288).
    # The orthogonal pair gives correlation ~0, well below threshold.
    assert skill.check_veto("ARB") is False


def test_nan_data_fail_closed() -> None:
    """Zero-variance series produce NaN correlation, forcing a fail-closed veto."""
    flat = np.zeros(288)
    candidate = np.linspace(0.0, 1.0, 288)
    portfolio = np.array([flat])
    skill, _, _ = _build_skill(portfolio, candidate)

    assert skill.check_veto("OP") is True


def test_empty_portfolio() -> None:
    """No open positions short-circuits to ``veto = False`` without fetching candidate."""
    portfolio = np.empty((0, 288))
    candidate = np.linspace(0.0, 1.0, 288)
    skill, portfolio_mock, asset_mock = _build_skill(portfolio, candidate)

    assert skill.check_veto("AVAX") is False
    portfolio_mock.assert_called_once_with()
    asset_mock.assert_not_called()
