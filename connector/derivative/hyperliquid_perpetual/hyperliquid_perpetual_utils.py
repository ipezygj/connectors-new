"""Technical implementation for Hummingbot Gateway V2.1."""

from typing import Any, Dict, Optional


def float_to_wire(value: float, sz_decimals: int) -> str:
    """
    Converts a float to the wire format expected by the Hyperliquid API.
    Truncates to sz_decimals precision without rounding.
    """
    factor = 10**sz_decimals
    truncated = int(value * factor) / factor
    return f"{truncated:.{sz_decimals}f}"


def parse_order_response(response: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extracts order status fields from the exchange response.
    """
    status = response.get("status", "")
    data = response.get("response", {}).get("data", {})
    if status == "ok" and "statuses" in data:
        statuses = data["statuses"]
        if statuses and "resting" in statuses[0]:
            return {"order_id": statuses[0]["resting"]["oid"], "status": "open"}
        if statuses and "filled" in statuses[0]:
            return {"order_id": statuses[0]["filled"]["oid"], "status": "filled"}
    return {"order_id": None, "status": "error", "raw": response}


def build_order_action(
    asset_id: int,
    is_buy: bool,
    limit_px: str,
    sz: str,
    reduce_only: bool = False,
    order_type: Optional[Dict[str, Any]] = None,
    cloid: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Constructs a complete order action in Hyperliquid wire format.
    """
    if order_type is None:
        order_type = {"limit": {"tif": "Gtc"}}

    order_wire: Dict[str, Any] = {
        "a": asset_id,
        "b": is_buy,
        "p": limit_px,
        "s": sz,
        "r": reduce_only,
        "t": order_type,
    }
    if cloid is not None:
        order_wire["c"] = cloid

    return {
        "type": "order",
        "orders": [order_wire],
        "grouping": "na",
    }


def build_cancel_action(asset_id: int, oid: int) -> Dict[str, Any]:
    """
    Constructs a cancel action for a single order by exchange order ID.
    """
    return {
        "type": "cancel",
        "cancels": [{"a": asset_id, "o": oid}],
    }


def build_cancel_by_cloid(asset_id: int, cloid: str) -> Dict[str, Any]:
    """
    Constructs a cancel action using a client order ID.
    """
    return {
        "type": "cancelByCloid",
        "cancels": [{"asset": asset_id, "cloid": cloid}],
    }


ERROR_CODES: Dict[str, str] = {
    "INVALID_SIGNATURE": "Signature verification failed",
    "INSUFFICIENT_MARGIN": "Insufficient margin for order",
    "ORDER_NOT_FOUND": "Order does not exist",
    "RATE_LIMIT": "Rate limit exceeded",
}
