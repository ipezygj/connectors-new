"""Technical implementation for Hummingbot Gateway V2.1."""

from typing import Tuple

from hummingbot.core.data_type.order_book_row import OrderBookRow


def convert_to_order_book_row(row_data: dict) -> OrderBookRow:
    """
    Convert Hyperliquid L2 book entry to HBOT OrderBookRow.
    Input format: {"px": "price", "sz": "size", "n": num_orders}
    """
    return OrderBookRow(float(row_data["px"]), float(row_data["sz"]), 0)


def convert_to_exchange_trading_pair(hb_trading_pair: str) -> str:
    """
    Convert Hummingbot format (BASE-QUOTE) to Hyperliquid format.
    Hyperliquid uses base asset name directly (e.g., "ETH", "BTC").
    """
    base, _ = split_trading_pair(hb_trading_pair)
    return base


def convert_from_exchange_trading_pair(exchange_symbol: str, quote: str = "USD") -> str:
    """
    Convert Hyperliquid symbol to Hummingbot format.
    """
    return f"{exchange_symbol}-{quote}"


def split_trading_pair(trading_pair: str) -> Tuple[str, str]:
    """
    Split a Hummingbot-style trading pair into base and quote assets.
    """
    parts = trading_pair.split("-")
    if len(parts) != 2:
        raise ValueError(f"Invalid trading pair format: {trading_pair}. Expected BASE-QUOTE.")
    return parts[0], parts[1]


def resolve_asset_index(trading_pair: str) -> int:
    """Map trading pair to Hyperliquid asset index."""
    from hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_constants import ASSET_INDEX_MAP

    base = convert_to_exchange_trading_pair(trading_pair)
    if base not in ASSET_INDEX_MAP:
        raise ValueError(f"Unknown asset '{base}' — update ASSET_INDEX_MAP or fetch from /info")
    return ASSET_INDEX_MAP[base]


ERROR_CODES = {
    "INVALID_SIGNATURE": "Request signature verification failed",
    "INSUFFICIENT_MARGIN": "Not enough margin for this order",
    "ORDER_NOT_FOUND": "Order does not exist",
    "RATE_LIMITED": "Rate limit exceeded",
}
