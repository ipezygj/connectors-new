"""Technical implementation for Hummingbot Gateway V2.1."""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from hummingbot.connector.derivative.hyperliquid_perpetual import hyperliquid_perpetual_auth as auth
from hummingbot.connector.derivative.hyperliquid_perpetual import hyperliquid_perpetual_constants as constants
from hummingbot.connector.derivative.hyperliquid_perpetual import hyperliquid_perpetual_utils as utils
from hummingbot.core.data_type.common import OrderType, TradeType


class HyperliquidPerpetualDerivative:
    """
    Core connector for Hyperliquid perpetual futures.
    Handles L1 action signing, WebSocket subscriptions, and order lifecycle.
    """

    def __init__(self, secret_key: str, use_testnet: bool = False):
        self._auth = auth.HyperliquidPerpetualAuth(secret_key, use_testnet=use_testnet)
        self._order_book_lock: asyncio.Lock = asyncio.Lock()
        self._order_book: Dict[str, Any] = {}
        self._asset_map: Dict[str, int] = {}
        self._sz_decimals: Dict[str, int] = {}
        self._logger = logging.getLogger(__name__)
        self._use_testnet = use_testnet

    @property
    def rest_url(self) -> str:
        return constants.TESTNET_REST_URL if self._use_testnet else constants.REST_URL

    @property
    def wss_url(self) -> str:
        return constants.TESTNET_WSS_URL if self._use_testnet else constants.WSS_URL

    def _next_nonce(self) -> int:
        return self._auth.timestamp_ms()

    async def _fetch_asset_metadata(self) -> None:
        """
        Populates asset_id and size decimal mappings from the info endpoint.
        """
        import aiohttp

        payload = {"type": "meta"}
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.rest_url}{constants.INFO_PATH}",
                json=payload,
                timeout=constants.API_CALL_TIMEOUT,
            ) as resp:
                resp.raise_for_status()
                data = await resp.json()
                universe = data.get("universe", [])
                for idx, asset in enumerate(universe):
                    name = asset.get("name", "")
                    self._asset_map[name] = idx
                    self._sz_decimals[name] = asset.get("szDecimals", 0)

    async def _listen_to_order_book_stream(self, symbol: str) -> None:
        """
        Maintains the WebSocket connection for L2 book updates.
        """
        subscribe_payload = {
            "method": "subscribe",
            "subscription": {"type": constants.WS_ORDER_BOOK_CHANNEL, "coin": symbol},
        }

        while True:
            try:
                import websockets

                async with websockets.connect(self.wss_url) as ws:
                    self._logger.info(f"Connected to WS L2 stream for {symbol}.")
                    await ws.send(json.dumps(subscribe_payload))

                    async for message in ws:
                        data = json.loads(message)
                        await self._process_order_book_message(data)

            except Exception as e:
                self._logger.error(f"WebSocket connection error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _process_order_book_message(self, data: Dict[str, Any]) -> None:
        """
        Parses L2 book snapshots and updates local state under lock.
        """
        if "data" not in data:
            return

        book_data = data["data"]
        coin = book_data.get("coin", "")

        async with self._order_book_lock:
            self._order_book[coin] = {
                "bids": {float(level[0]): float(level[1]) for level in book_data.get("levels", [[]])[0]},
                "asks": {float(level[0]): float(level[1]) for level in book_data.get("levels", [[], []])[1]},
            }

    async def _listen_to_user_stream(self) -> None:
        """
        Private WebSocket stream for order and fill events.
        """
        subscribe_payload = {
            "method": "subscribe",
            "subscription": {"type": constants.WS_USER_EVENTS_CHANNEL, "user": self._auth.address},
        }

        while True:
            try:
                import websockets

                async with websockets.connect(self.wss_url) as ws:
                    self._logger.info("Connected to private user stream.")
                    await ws.send(json.dumps(subscribe_payload))

                    async for message in ws:
                        data = json.loads(message)
                        await self._process_user_stream_message(data)

            except Exception as e:
                self._logger.error(f"User stream connection error: {e}. Reconnecting in 5s...")
                await asyncio.sleep(5)

    async def _process_user_stream_message(self, data: Dict[str, Any]) -> None:
        """
        Routes user stream events.
        """
        if "data" not in data:
            return

        payload = data["data"]
        if "fills" in payload:
            for fill in payload["fills"]:
                await self._handle_fill_update(fill)
        if "orders" in payload:
            for order in payload["orders"]:
                await self._handle_order_update(order)

    async def _handle_order_update(self, payload: Dict[str, Any]) -> None:
        async with self._order_book_lock:
            order_id = payload.get("oid", "")
            status = payload.get("status", "")
            self._logger.info(f"Order {order_id} status: {status}")

    async def _handle_fill_update(self, payload: Dict[str, Any]) -> None:
        async with self._order_book_lock:
            trade_id = payload.get("tid", "")
            filled_qty = payload.get("sz", "0")
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
        Authenticated order placement via signed L1 action.
        """
        if symbol not in self._asset_map:
            await self._fetch_asset_metadata()

        asset_id = self._asset_map.get(symbol)
        if asset_id is None:
            self._logger.error(f"Unknown asset: {symbol}")
            return None

        sz_dec = self._sz_decimals.get(symbol, 0)
        is_buy = trade_type == TradeType.BUY

        tif = {"limit": {"tif": "Gtc"}} if order_type == OrderType.LIMIT else {"limit": {"tif": "Ioc"}}
        action = utils.build_order_action(
            asset_id=asset_id,
            is_buy=is_buy,
            limit_px=utils.float_to_wire(price, 6),
            sz=utils.float_to_wire(amount, sz_dec),
            order_type=tif,
            cloid=client_order_id,
        )

        signed_payload = self._auth.sign_order_action(action, nonce=self._next_nonce())

        try:
            import aiohttp

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.rest_url}{constants.EXCHANGE_PATH}",
                    json=signed_payload,
                    timeout=constants.API_CALL_TIMEOUT,
                ) as resp:
                    resp.raise_for_status()
                    result = await resp.json()
                    parsed = utils.parse_order_response(result)
                    self._logger.info(f"Order placed: {symbol} {trade_type.name} {amount} @ {price}")
                    return parsed
        except Exception as e:
            self._logger.error(f"Order placement failed: {e}")
            return None
