"""Technical implementation for Hummingbot Gateway V2.1."""

from typing import Dict

# ---------------------------------------------------------------------------
# Exchange metadata
# ---------------------------------------------------------------------------
EXCHANGE_NAME: str = "backpack_perpetual"
BROKER_ID: str = "hummingbot"
MAX_ORDER_ID_LEN: int = 32

# ---------------------------------------------------------------------------
# Base URLs
# ---------------------------------------------------------------------------
REST_URL: str = "https://api.backpack.exchange"
WSS_URL: str = "wss://ws.backpack.exchange"

# ---------------------------------------------------------------------------
# REST API endpoints
# ---------------------------------------------------------------------------
TICKER_PRICE_CHANGE_PATH: str = "/api/v1/ticker"
EXCHANGE_INFO_PATH: str = "/api/v1/markets"
SNAPSHOT_PATH: str = "/api/v1/depth"
SERVER_TIME_PATH: str = "/api/v1/time"
PING_PATH: str = "/api/v1/ping"

# Account & trading
ACCOUNT_INFO_PATH: str = "/api/v1/account"
BALANCES_PATH: str = "/api/v1/capital"
CREATE_ORDER_PATH: str = "/api/v1/order"
CANCEL_ORDER_PATH: str = "/api/v1/order"
ORDER_STATUS_PATH: str = "/api/v1/order"
OPEN_ORDERS_PATH: str = "/api/v1/orders"
TRADE_HISTORY_PATH: str = "/api/v1/trades"

# Perpetual-specific
POSITION_PATH: str = "/api/v1/position"
FUNDING_RATE_PATH: str = "/api/v1/fundingRate"
MARK_PRICE_PATH: str = "/api/v1/markPrice"
LEVERAGE_PATH: str = "/api/v1/leverage"

# ---------------------------------------------------------------------------
# WebSocket channels
# ---------------------------------------------------------------------------
WS_HEARTBEAT_INTERVAL: float = 20.0
WS_ORDER_BOOK_CHANNEL: str = "depth"
WS_TRADES_CHANNEL: str = "trades"
WS_USER_ORDERS_CHANNEL: str = "orders"
WS_USER_TRADES_CHANNEL: str = "fills"
WS_POSITIONS_CHANNEL: str = "positions"

# ---------------------------------------------------------------------------
# Rate limits (requests per second)
# ---------------------------------------------------------------------------
RATE_LIMITS: Dict[str, int] = {
    "default": 10,
    "orders": 5,
    "ws": 3,
}

# ---------------------------------------------------------------------------
# Order & position defaults
# ---------------------------------------------------------------------------
DEFAULT_LEVERAGE: int = 1
MAX_LEVERAGE: int = 20
SUPPORTED_ORDER_TYPES: list = ["LIMIT", "MARKET"]
SUPPORTED_POSITION_MODES: list = ["ONE_WAY", "HEDGE"]

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------
ORDER_STATUS_POLL_INTERVAL: float = 10.0
FUNDING_RATE_POLL_INTERVAL: float = 60.0
HEARTBEAT_TIMEOUT: float = 30.0
API_CALL_TIMEOUT: float = 10.0
