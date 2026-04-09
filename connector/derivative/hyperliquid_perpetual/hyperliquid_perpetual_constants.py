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

# ---------------------------------------------------------------------------
# REST API endpoints
# ---------------------------------------------------------------------------
INFO_PATH: str = "/info"
EXCHANGE_PATH: str = "/exchange"

# ---------------------------------------------------------------------------
# WebSocket channels
# ---------------------------------------------------------------------------
WS_HEARTBEAT_INTERVAL: float = 20.0
WS_ORDER_BOOK_CHANNEL: str = "l2Book"
WS_TRADES_CHANNEL: str = "trades"
WS_USER_EVENTS_CHANNEL: str = "userEvents"
WS_USER_FILLS_CHANNEL: str = "userFills"

# ---------------------------------------------------------------------------
# Rate limits
# ---------------------------------------------------------------------------
RATE_LIMITS: Dict[str, int] = {
    "default": 20,
    "orders": 10,
    "ws": 5,
}

# ---------------------------------------------------------------------------
# Order & position defaults
# ---------------------------------------------------------------------------
DEFAULT_LEVERAGE: int = 1
MAX_LEVERAGE: int = 50
SUPPORTED_ORDER_TYPES: list = ["LIMIT", "MARKET"]

# ---------------------------------------------------------------------------
# EIP-712 Domain Configuration
# ---------------------------------------------------------------------------
EIP712_DOMAIN_MAINNET: Dict[str, object] = {
    "name": "Exchange",
    "version": "1",
    "chainId": 1337,
    "verifyingContract": "0x0000000000000000000000000000000000000000",
}

EIP712_DOMAIN_TESTNET: Dict[str, object] = {
    "name": "Exchange",
    "version": "1",
    "chainId": 421614,
    "verifyingContract": "0x0000000000000000000000000000000000000000",
}

# ---------------------------------------------------------------------------
# Testnet URLs
# ---------------------------------------------------------------------------
REST_URL_TESTNET: str = "https://api.hyperliquid-testnet.xyz"
WSS_URL_TESTNET: str = "wss://api.hyperliquid-testnet.xyz/ws"
EXCHANGE_PATH_TESTNET: str = "/exchange"

# ---------------------------------------------------------------------------
# Asset index mapping (static subset — fetch dynamically via /info for full list)
# ---------------------------------------------------------------------------
ASSET_INDEX_MAP: Dict[str, int] = {
    "BTC": 0,
    "ETH": 1,
    "SOL": 2,
    "AVAX": 3,
    "ARB": 4,
    "DOGE": 5,
    "OP": 6,
    "MATIC": 7,
    "SUI": 8,
    "APT": 9,
}

# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------
ORDER_STATUS_POLL_INTERVAL: float = 10.0
FUNDING_RATE_POLL_INTERVAL: float = 60.0
HEARTBEAT_TIMEOUT: float = 30.0
API_CALL_TIMEOUT: float = 10.0
