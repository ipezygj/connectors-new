"""Technical implementation for Hummingbot Gateway V2.1."""

from typing import Any, Callable, Dict, List, Tuple

import numpy as np

from skills.base import SkillBase
from skills.constants import CORRELATION_THRESHOLD, MIN_LOOKBACK, RETURNS_LOOKBACK


class PortfolioCorrelationSkill(SkillBase):
    """Veto skill blocking entries highly correlated with open positions.

    The veto rule is a hard threshold on Pearson correlation: if any open
    position's return series has correlation strictly greater than
    :data:`skills.constants.CORRELATION_THRESHOLD` with the candidate
    asset's series, the candidate is vetoed.

    Returns are supplied via two injected callables so the skill stays
    unit-testable without a live exchange connection:

        * ``portfolio_returns_provider() -> np.ndarray``
              Shape ``(n_open_positions, length)``. An empty array is
              acceptable and indicates no open positions; in that case
              the skill returns no veto.

        * ``asset_returns_provider(symbol: str) -> np.ndarray``
              Shape ``(length,)``. Returns for the candidate asset.

    Both providers must produce series of the same length. The shared
    length must lie in the inclusive range
    ``[MIN_LOOKBACK, RETURNS_LOOKBACK]``. Series shorter than
    :data:`skills.constants.MIN_LOOKBACK` raise :class:`ValueError`
    because Pearson correlation on very short windows is statistically
    unreliable. Series whose length is below
    :data:`skills.constants.RETURNS_LOOKBACK` but at or above
    :data:`skills.constants.MIN_LOOKBACK` are processed at the
    available length, supporting young positions whose history has not
    yet filled the full rolling window.

    Per-position correlations that evaluate to NaN (e.g. zero-variance
    or otherwise degenerate series) trigger a fail-closed veto:
    ``veto = True`` for that pair, propagated to the overall verdict.
    The conservative trading default is to block entries when
    correlation cannot be measured, on the principle that an unmeasured
    risk is treated as the worst-case risk.

    The correlation row across all ``N`` open positions is computed in
    a single vectorized step (mean centring, matrix-vector product, and
    element-wise division), giving an aggregate cost of ``O(N * M)`` for
    lookback ``M``.
    """

    def __init__(
        self,
        portfolio_returns_provider: Callable[[], np.ndarray],
        asset_returns_provider: Callable[[str], np.ndarray],
        threshold: float = CORRELATION_THRESHOLD,
        lookback: int = RETURNS_LOOKBACK,
        min_lookback: int = MIN_LOOKBACK,
    ) -> None:
        """Initialize the correlation veto skill.

        :param portfolio_returns_provider: Callable returning a 2-D
            array of shape ``(n_open_positions, length)`` with the
            historical returns of every open position.
        :param asset_returns_provider: Callable taking a symbol string
            and returning a 1-D array of shape ``(length,)`` with the
            candidate asset's historical returns.
        :param threshold: Correlation cut-off in ``(0.0, 1.0]``. The
            default is :data:`skills.constants.CORRELATION_THRESHOLD`.
        :param lookback: Maximum returns length accepted from each
            provider. The default is
            :data:`skills.constants.RETURNS_LOOKBACK`.
        :param min_lookback: Minimum returns length required from each
            provider. The default is
            :data:`skills.constants.MIN_LOOKBACK`.
        :raises ValueError: If ``threshold`` is outside ``(0.0, 1.0]``,
            ``min_lookback`` is below 2, or ``lookback`` is below
            ``min_lookback``.
        """
        super().__init__(name="portfolio_correlation_veto")
        if not 0.0 < threshold <= 1.0:
            raise ValueError(f"threshold must lie in (0.0, 1.0]; got {threshold!r}.")
        if min_lookback < 2:
            raise ValueError(f"min_lookback must be at least 2; got {min_lookback!r}.")
        if lookback < min_lookback:
            raise ValueError(
                f"lookback must be greater than or equal to min_lookback; "
                f"got lookback={lookback!r}, min_lookback={min_lookback!r}."
            )
        self._portfolio_returns_provider = portfolio_returns_provider
        self._asset_returns_provider = asset_returns_provider
        self._threshold: float = float(threshold)
        self._lookback: int = int(lookback)
        self._min_lookback: int = int(min_lookback)

    @property
    def threshold(self) -> float:
        """Return the active correlation threshold."""
        return self._threshold

    @property
    def lookback(self) -> int:
        """Return the maximum accepted returns length in bars."""
        return self._lookback

    @property
    def min_lookback(self) -> int:
        """Return the minimum required returns length in bars."""
        return self._min_lookback

    def check_veto(self, candidate_asset: str) -> bool:
        """Return ``True`` if the candidate asset must be vetoed.

        Convenience wrapper around :meth:`evaluate` for callers that only
        need the boolean verdict.

        :param candidate_asset: Symbol of the asset under evaluation.
        :return: ``True`` if the candidate exhibits correlation strictly
            greater than the configured threshold against any open
            position, or if any pair correlation is NaN (fail-closed);
            ``False`` otherwise.
        """
        result = self.evaluate({"candidate_asset": candidate_asset})
        return bool(result["veto"])

    def evaluate(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Evaluate the correlation veto rule.

        :param context: Decision context. Must contain the key
            ``"candidate_asset"`` mapping to a non-empty symbol string.
        :return: Dictionary with the following keys:

            * ``"veto"`` (bool): Whether the candidate is rejected.
            * ``"asset"`` (str): Echo of the candidate symbol.
            * ``"max_correlation"`` (Optional[float]): Largest finite
              correlation observed across open positions, or ``None`` if
              no finite correlation could be computed.
            * ``"correlations"`` (List[float]): Per-position correlations
              that were finite, in the row order of the portfolio returns
              matrix with NaN rows omitted.
            * ``"nan_pair_count"`` (int): Number of position pairs whose
              correlation evaluated to NaN. A non-zero value forces the
              fail-closed veto.

        :raises ValueError: If ``context['candidate_asset']`` is missing
            or invalid, or if either provider returns an array whose
            shape or length violates the configured contract.
        """
        candidate_asset = context.get("candidate_asset")
        if not isinstance(candidate_asset, str) or not candidate_asset:
            raise ValueError("context['candidate_asset'] must be a non-empty string.")

        portfolio_returns = self._load_portfolio_returns()
        if portfolio_returns.size == 0:
            self.logger().info(
                "No open positions; skill returns no veto for %s.",
                candidate_asset,
            )
            return {
                "veto": False,
                "asset": candidate_asset,
                "max_correlation": None,
                "correlations": [],
                "nan_pair_count": 0,
            }

        portfolio_length = portfolio_returns.shape[1]
        candidate_returns = self._load_candidate_returns(candidate_asset, portfolio_length)
        correlations, nan_pair_count = self._compute_correlations(candidate_returns, portfolio_returns, candidate_asset)

        # Note: Following explicit bounty spec for positive correlation only.
        high_correlation_veto = any(c > self._threshold for c in correlations)
        veto = high_correlation_veto or nan_pair_count > 0
        max_correlation = max(correlations) if correlations else None

        if veto:
            self.logger().info(
                "VETO: candidate=%s threshold_breach=%s nan_pairs=%d max_corr=%s.",
                candidate_asset,
                high_correlation_veto,
                nan_pair_count,
                f"{max_correlation:.4f}" if max_correlation is not None else "n/a",
            )
        return {
            "veto": veto,
            "asset": candidate_asset,
            "max_correlation": float(max_correlation) if max_correlation is not None else None,
            "correlations": correlations,
            "nan_pair_count": nan_pair_count,
        }

    # -- Internals ------------------------------------------------------------
    def _load_portfolio_returns(self) -> np.ndarray:
        """Fetch and validate the portfolio returns matrix."""
        portfolio_returns = np.asarray(self._portfolio_returns_provider(), dtype=float)
        if portfolio_returns.size == 0:
            return portfolio_returns
        if portfolio_returns.ndim != 2:
            raise ValueError(
                "Portfolio returns must be a 2-D array of shape "
                f"(n_positions, length); got ndim={portfolio_returns.ndim}."
            )
        self._validate_returns_length(portfolio_returns.shape[1], "Portfolio returns")
        return portfolio_returns

    def _load_candidate_returns(self, candidate_asset: str, expected_length: int) -> np.ndarray:
        """Fetch and validate the candidate asset returns vector."""
        candidate_returns = np.asarray(self._asset_returns_provider(candidate_asset), dtype=float)
        if candidate_returns.ndim != 1:
            raise ValueError(f"Candidate returns must be a 1-D array; got ndim={candidate_returns.ndim}.")
        if candidate_returns.shape[0] != expected_length:
            raise ValueError(
                f"Candidate returns length {candidate_returns.shape[0]} does not match "
                f"portfolio returns length {expected_length}."
            )
        return candidate_returns

    def _validate_returns_length(self, length: int, source: str) -> None:
        """Enforce the inclusive ``[min_lookback, lookback]`` length contract."""
        if length < self._min_lookback:
            raise ValueError(
                f"{source} length {length} is below MIN_LOOKBACK ({self._min_lookback}); "
                "Pearson correlation is statistically unreliable on shorter windows."
            )
        if length > self._lookback:
            raise ValueError(
                f"{source} length {length} exceeds RETURNS_LOOKBACK ({self._lookback}); "
                "provider should truncate to the configured window."
            )

    def _compute_correlations(
        self,
        candidate_returns: np.ndarray,
        portfolio_returns: np.ndarray,
        candidate_asset: str,
    ) -> Tuple[List[float], int]:
        """Compute Pearson correlations and count fail-closed NaN pairs.

        Implementation is fully vectorized: the candidate's covariance
        with every position is evaluated by a single matrix-vector
        product, and the correlation row is obtained by an element-wise
        division against the joint standard-deviation product. Total
        cost is ``O(N * M)`` for ``N`` open positions and lookback
        ``M``, dominated by the matmul.

        :return: Tuple ``(correlations, nan_pair_count)``. The
            ``correlations`` list contains every finite per-pair Pearson
            value in row order with NaN rows omitted. The
            ``nan_pair_count`` is the number of pairs that evaluated to
            NaN; any non-zero value triggers the fail-closed veto in
            :meth:`evaluate`.
        """
        n_samples = candidate_returns.shape[0]

        # ``np.errstate`` suppresses the divide-by-zero / invalid-value
        # RuntimeWarnings emitted by zero-variance series. The skill
        # detects the resulting NaNs explicitly below and converts them
        # into a fail-closed veto.
        with np.errstate(invalid="ignore", divide="ignore"):
            candidate_centered = candidate_returns - candidate_returns.mean()
            portfolio_centered = portfolio_returns - portfolio_returns.mean(axis=1, keepdims=True)

            # Covariances: one matmul produces all N pair covariances.
            covariances = (portfolio_centered @ candidate_centered) / n_samples

            # Joint standard-deviation product (population, ddof=0).
            candidate_std = candidate_returns.std()
            portfolio_stds = portfolio_returns.std(axis=1)
            denominator = candidate_std * portfolio_stds

            correlation_row = covariances / denominator

        nan_mask = np.isnan(correlation_row)
        nan_pair_count = int(nan_mask.sum())

        if nan_pair_count > 0:
            for index in np.flatnonzero(nan_mask).tolist():
                self.logger().warning(
                    "Correlation NaN between %s and portfolio row %d "
                    "(zero-variance or invalid series); fail-closed veto for this pair.",
                    candidate_asset,
                    index,
                )

        correlations: List[float] = correlation_row[~nan_mask].astype(float).tolist()
        return correlations, nan_pair_count
