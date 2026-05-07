"""Technical implementation for Hummingbot Gateway V2.1."""

# -- Portfolio Correlation Veto -----------------------------------------------
# Pearson correlation strictly greater than this value vetoes a new entry.
CORRELATION_THRESHOLD: float = 0.85

# Number of historical return bars consumed by the correlation calculation.
# At a 5-minute bar interval this corresponds to a 24-hour rolling window.
RETURNS_LOOKBACK: int = 288

# Minimum acceptable returns length. Series shorter than this raise
# ``ValueError`` because Pearson correlation on very short windows is
# statistically unreliable. Values between MIN_LOOKBACK and
# RETURNS_LOOKBACK are processed using whatever length the provider
# supplies (supports young positions whose history has not yet filled
# the full rolling window).
MIN_LOOKBACK: int = 50
