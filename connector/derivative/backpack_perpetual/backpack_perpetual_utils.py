"""Technical implementation for Hummingbot Gateway V2.1."""

from hummingbot.core.data_type.order_book_row import OrderBookRow


def convert_to_order_book_row(row_data: list) -> OrderBookRow:
    """Convert [price, amount] to OrderBookRow."""
    return OrderBookRow(float(row_data[0]), float(row_data[1]), 0)


# Backpack specific error mapping
ERROR_CODES = {
    "1000": "Invalid Signature",
    "1001": "Unauthorized",
    "2000": "Insufficient Balance",
    "3000": "Order Not Found",
}
