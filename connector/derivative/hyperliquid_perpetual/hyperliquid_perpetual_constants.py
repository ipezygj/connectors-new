"""Technical implementation for Hummingbot Gateway V2.1."""

from typing import Dict

# ---------------------------------------------------------------------------
# Exchange metadata
# ---------------------------------------------------------------------------
EXCHANGE_NAME: str = "hyperliquid_perpetual"
BROKER_ID: str = "hummingbot"
MAX_ORDER_ID_LEN: int = 128

# ---------------------------------------------------------------------------
# Base URLs
# ---------------------------------------------------------------------------
REST_URL: str = "https://api.hyperliquid.xyz"
WSS_URL: str = "wss://api.hyperliquid.xyz/ws"
TESTNET_REST_URL: str = "https://api.hyperliquid-testnet.xyz"
TESTNET_WSS_URL: str = "wss://api.hyperliquid-testnet.xyz/ws"

# ---------------------------------------------------------------------------
# REST API endpoints
# ---------------------------------------------------------------------------
INFO_PATH: str = "/info"
EXCHANGE_PATH: str = "/exchange"

# ---------------------------------------------------------------------------
# EIP-712 domain parameters
# ---------------------------------------------------------------------------
EIP712_DOMAIN_NAME: str = "Exchange"
EIP712_DOMAIN_VERSION: str = "1"
EIP712_MAINNET_CHAIN_ID: int = 1337
EIP712_TESTNET_CHAIN_ID: int = 421614

# ---------------------------------------------------------------------------
# WebSocket channels
# ---------------------------------------------------------------------------
WS_HEARTBEAT_INTERVAL: float = 20.0
WS_ORDER_BOOK_CHANNEL: str = "l2Book"
WS_TRADES_CHANNEL: str = "trades"
WS_USER_EVENTS_CHANNEL: str = "userEvents"
WS_USER_FILLS_CHANNEL: str = "userFills"

# ---------------------------------------------------------------------------
# Rate limits (requests per second)
# ---------------------------------------------------------------------------
RATE_LIMITS: Dict[str, int] = {
    "default": 20,
    "orders": 10,
    "info": 20,
    "ws": 5,
}

# ---------------------------------------------------------------------------
# Order & position defaults
# ---------------------------------------------------------------------------
DEFAULT_LEVERAGE: int = 1
MAX_LEVERAGE: int = 50
SUPPORTED_ORDER_TYPES: list = ["LIMIT", "MARKET"]
SUPPORTED_POSITION_MODES: list = ["ONE_WAY"]

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------
ORDER_STATUS_POLL_INTERVAL: float = 10.0
FUNDING_RATE_POLL_INTERVAL: float = 3600.0
HEARTBEAT_TIMEOUT: float = 30.0
API_CALL_TIMEOUT: float = 10.0
