"""Technical implementation for Hummingbot Gateway V2.1."""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

import aiohttp

from hummingbot.connector.derivative.hyperliquid_perpetual import hyperliquid_perpetual_auth as auth
from hummingbot.connector.derivative.hyperliquid_perpetual import hyperliquid_perpetual_constants as constants
from hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_utils import (
    convert_to_exchange_trading_pair,
    resolve_asset_index,
)
from hummingbot.core.data_type.common import OrderType, TradeType

logger = logging.getLogger(__name__)


class HyperliquidPerpetualDerivative:
    """Core connector for Hyperliquid perpetual futures."""

    def __init__(self, private_key: str, testnet: bool = False):
        self._auth = auth.HyperliquidPerpetualAuth(private_key, testnet=testnet)
        self._testnet: bool = testnet
        self._order_lock: asyncio.Lock = asyncio.Lock()
        self._order_book_lock: asyncio.Lock = asyncio.Lock()
        self._order_book: Dict[str, Any] = {}
        self._session: Optional[aiohttp.ClientSession] = None

    # ------------------------------------------------------------------
    # Session
    # ------------------------------------------------------------------

    async def _ensure_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------
    # REST
    # ------------------------------------------------------------------

    @property
    def _rest_url(self) -> str:
        return constants.REST_URL_TESTNET if self._testnet else constants.REST_URL

    async def _post_exchange(self, body: Dict[str, Any]) -> Dict[str, Any]:
        session = await self._ensure_session()
        url = f"{self._rest_url}{constants.EXCHANGE_PATH}"
        async with session.post(url, json=body, timeout=constants.API_CALL_TIMEOUT) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _post_info(self, payload: Dict[str, Any]) -> Any:
        session = await self._ensure_session()
        url = f"{self._rest_url}{constants.INFO_PATH}"
        async with session.post(url, json=payload, timeout=constants.API_CALL_TIMEOUT) as resp:
            resp.raise_for_status()
            return await resp.json()

    # ------------------------------------------------------------------
    # Order placement
    # ------------------------------------------------------------------

    async def place_order(
        self,
        trading_pair: str,
        order_type: OrderType,
        trade_type: TradeType,
        amount: float,
        price: float,
        reduce_only: bool = False,
        client_order_id: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        async with self._order_lock:
            asset_idx = resolve_asset_index(trading_pair)
            is_buy = trade_type == TradeType.BUY

            hl_order_type: Optional[Dict[str, Any]] = None
            if order_type == OrderType.MARKET:
                hl_order_type = {"limit": {"tif": "Ioc"}}

            action = self._auth.build_order_action(
                asset=asset_idx,
                is_buy=is_buy,
                limit_px=str(price),
                sz=str(amount),
                reduce_only=reduce_only,
                order_type=hl_order_type,
                cloid=client_order_id,
            )
            body = self._auth.generate_signed_request(action)

            try:
                return await self._post_exchange(body)
            except Exception as e:
                logger.error("Order failed: %s", e)
                return None

    # ------------------------------------------------------------------
    # Order cancellation
    # ------------------------------------------------------------------

    async def cancel_order(self, trading_pair: str, order_id: int) -> Optional[Dict[str, Any]]:
        async with self._order_lock:
            asset_idx = resolve_asset_index(trading_pair)
            action = self._auth.build_cancel_action(asset=asset_idx, oid=order_id)
            body = self._auth.generate_signed_request(action)

            try:
                return await self._post_exchange(body)
            except Exception as e:
                logger.error("Cancel failed: %s", e)
                return None

    async def cancel_order_by_cloid(self, trading_pair: str, cloid: str) -> Optional[Dict[str, Any]]:
        async with self._order_lock:
            asset_idx = resolve_asset_index(trading_pair)
            action = self._auth.build_cancel_by_cloid(asset=asset_idx, cloid=cloid)
            body = self._auth.generate_signed_request(action)

            try:
                return await self._post_exchange(body)
            except Exception as e:
                logger.error("Cancel by cloid failed: %s", e)
                return None

    # ------------------------------------------------------------------
    # Market data
    # ------------------------------------------------------------------

    async def get_exchange_meta(self) -> Dict[str, Any]:
        return await self._post_info({"type": "meta"})

    async def get_l2_snapshot(self, trading_pair: str) -> Dict[str, Any]:
        coin = convert_to_exchange_trading_pair(trading_pair)
        return await self._post_info({"type": "l2Book", "coin": coin})

    async def get_user_state(self) -> Dict[str, Any]:
        return await self._post_info({"type": "clearinghouseState", "user": self._auth.address})

    async def get_open_orders(self) -> List[Dict[str, Any]]:
        return await self._post_info({"type": "openOrders", "user": self._auth.address})

    async def get_funding_rates(self) -> List[Dict[str, Any]]:
        meta = await self.get_exchange_meta()
        return meta.get("universe", [])

    # ------------------------------------------------------------------
    # WebSocket — order book
    # ------------------------------------------------------------------

    async def _listen_to_order_book_stream(self, symbol: str) -> None:
        ws_url = constants.WSS_URL_TESTNET if self._testnet else constants.WSS_URL
        coin = convert_to_exchange_trading_pair(symbol)
        subscribe_msg = {"method": "subscribe", "subscription": {"type": "l2Book", "coin": coin}}

        while True:
            try:
                import websockets

                async with websockets.connect(ws_url) as ws:
                    await ws.send(json.dumps(subscribe_msg))
                    async for message in ws:
                        data = json.loads(message)
                        await self._process_order_book_message(data)
            except Exception as e:
                logger.error("OB stream error: %s. Reconnecting...", e)
                await asyncio.sleep(5)

    async def _process_order_book_message(self, data: Dict[str, Any]) -> None:
        if "data" not in data:
            return
        payload = data["data"]
        if "levels" not in payload:
            return

        async with self._order_book_lock:
            levels = payload["levels"]
            if len(levels) >= 2:
                self._order_book["bids"] = {float(b["px"]): float(b["sz"]) for b in levels[0]}
                self._order_book["asks"] = {float(a["px"]): float(a["sz"]) for a in levels[1]}

    # ------------------------------------------------------------------
    # WebSocket — user stream
    # ------------------------------------------------------------------

    async def _listen_to_user_stream(self) -> None:
        ws_url = constants.WSS_URL_TESTNET if self._testnet else constants.WSS_URL
        auth_payload = self._auth.generate_ws_auth_payload()

        while True:
            try:
                import websockets

                async with websockets.connect(ws_url) as ws:
                    await ws.send(json.dumps(auth_payload))
                    async for message in ws:
                        data = json.loads(message)
                        await self._process_user_message(data)
            except Exception as e:
                logger.error("User stream error: %s. Reconnecting...", e)
                await asyncio.sleep(5)

    async def _process_user_message(self, data: Dict[str, Any]) -> None:
        if "data" not in data:
            return
        payload = data["data"]
        if isinstance(payload, list):
            for event in payload:
                event_type = event.get("type", "")
                if event_type == "fill":
                    logger.info("Fill: %s %s @ %s", event.get("coin"), event.get("sz"), event.get("px"))
                elif event_type == "order":
                    logger.info("Order update: %s status=%s", event.get("oid"), event.get("status"))
