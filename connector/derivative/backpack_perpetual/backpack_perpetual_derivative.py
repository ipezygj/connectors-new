"""Technical implementation for Hummingbot Gateway V2.1."""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from hummingbot.connector.derivative.backpack_perpetual import (
    backpack_perpetual_auth as auth,
    backpack_perpetual_constants as constants,
)
from hummingbot.core.data_type.common import OrderType, TradeType


class BackpackPerpetualDerivative:
    """Backpack perpetual futures connector."""

    def __init__(self, api_key: str, api_secret: str):
        self._auth = auth.BackpackPerpetualAuth(api_key, api_secret)
        self._order_book_lock: asyncio.Lock = asyncio.Lock()  # Async state protection
        self._order_book: Dict[str, Any] = {}
        self._logger = logging.getLogger(__name__)

    async def _listen_to_order_book_stream(self, symbol: str) -> None:
        """
        Maintains the WebSocket connection for public order book updates.
        Handles automatic reconnection on drops.
        """
        ws_url = constants.WSS_URL
        subscribe_payload = {
            "method": "SUBSCRIBE",
            "params": [f"depth.{symbol}"],
            "id": 1,
        }

        while True:
            try:
                import websockets

                async with websockets.connect(ws_url) as ws:
                    self._logger.info(f"Connected to WS stream for {symbol}.")
                    await ws.send(json.dumps(subscribe_payload))

                    async for message in ws:
                        data = json.loads(message)
                        await self._process_order_book_message(data)

            except Exception as e:
                self._logger.error(f"WebSocket connection error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _process_order_book_message(self, data: Dict[str, Any]) -> None:
        """
        Parses incoming WS data and safely mutates the local order book state.
        Uses asyncio.Lock to prevent race conditions during read/write cycles.
        """
        if "data" not in data or "e" not in data["data"]:
            return

        payload = data["data"]

        async with self._order_book_lock:
            if "b" in payload:
                for bid in payload["b"]:
                    price, amount = float(bid[0]), float(bid[1])
                    if amount == 0:
                        self._order_book.get("bids", {}).pop(price, None)
                    else:
                        self._order_book.setdefault("bids", {})[price] = amount

            if "a" in payload:
                for ask in payload["a"]:
                    price, amount = float(ask[0]), float(ask[1])
                    if amount == 0:
                        self._order_book.get("asks", {}).pop(price, None)
                    else:
                        self._order_book.setdefault("asks", {})[price] = amount

    async def _listen_to_user_stream(self) -> None:
        """
        Private WS stream for tracking fills and account changes.
        Authenticates via signed subscription payload.
        """
        ws_url = constants.WSS_URL
        auth_payload = self._auth.generate_ws_auth_payload()

        while True:
            try:
                import websockets

                async with websockets.connect(ws_url) as ws:
                    self._logger.info("Connected to private user stream.")

                    # Authenticate the connection
                    await ws.send(json.dumps(auth_payload))

                    # Subscribe to account updates after auth
                    subscribe_payload = {
                        "method": "SUBSCRIBE",
                        "params": ["account.orderUpdate", "account.fill"],
                        "id": 2,
                    }
                    await ws.send(json.dumps(subscribe_payload))

                    async for message in ws:
                        data = json.loads(message)
                        await self._process_user_stream_message(data)

            except Exception as e:
                self._logger.error(f"User stream connection error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _process_user_stream_message(self, data: Dict[str, Any]) -> None:
        """
        Routes private stream events to the appropriate handler.
        """
        if "data" not in data:
            return

        payload = data["data"]
        event_type = payload.get("e", "")

        if event_type == "orderUpdate":
            await self._handle_order_update(payload)
        elif event_type == "fill":
            await self._handle_fill_update(payload)

    async def _handle_order_update(self, payload: Dict[str, Any]) -> None:
        """
        Processes order status changes from the private stream.
        """
        async with self._order_book_lock:
            order_id = payload.get("orderId", "")
            status = payload.get("orderStatus", "")
            self._logger.info(f"Order {order_id} status: {status}")

    async def _handle_fill_update(self, payload: Dict[str, Any]) -> None:
        """
        Processes trade fill events from the private stream.
        """
        async with self._order_book_lock:
            trade_id = payload.get("tradeId", "")
            filled_qty = payload.get("quantity", "0")
            self._logger.info(f"Fill {trade_id} qty: {filled_qty}")

    async def place_order(
        self,
        symbol: str,
        order_type: OrderType,
        trade_type: TradeType,
        amount: float,
        price: float,
        client_order_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """
        Authenticated order placement via REST.
        """
        endpoint = f"{constants.REST_URL}{constants.CREATE_ORDER_PATH}"
        instruction = "orderExecute"

        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": "Ask" if trade_type == TradeType.SELL else "Bid",
            "orderType": "Limit" if order_type == OrderType.LIMIT else "Market",
            "quantity": str(amount),
            "price": str(price),
        }
        if client_order_id is not None:
            params["clientId"] = str(client_order_id)

        headers = self._auth.generate_auth_headers(instruction, params)

        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    endpoint, headers=headers, json=params, timeout=constants.API_CALL_TIMEOUT
                ) as resp:
                    resp.raise_for_status()
                    result = await resp.json()
                    self._logger.info(f"Order placed: {symbol} {trade_type.name} {amount} @ {price}")
                    return result
        except Exception as e:
            self._logger.error(f"Order placement failed: {e}")
            return None
