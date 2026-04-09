"""Technical implementation for Hummingbot Gateway V2.1."""

import json
import time
from typing import Any, Dict, Optional, Tuple

from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak

from hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_constants import (
    EIP712_DOMAIN_MAINNET,
    EIP712_DOMAIN_TESTNET,
)

# ---------------------------------------------------------------------------
# EIP-712 type hashes (pre-computed constants)
# ---------------------------------------------------------------------------
_DOMAIN_TYPE_HASH: bytes = keccak(b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
_AGENT_TYPE_HASH: bytes = keccak(b"Agent(address source,bytes32 connectionId)")


class HyperliquidPerpetualAuth:
    """EIP-712 L1 signing for Hyperliquid order and cancel actions."""

    def __init__(
        self,
        private_key: str,
        testnet: bool = False,
    ) -> None:
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        self._account = Account.from_key(private_key)
        self._domain: Dict[str, Any] = EIP712_DOMAIN_TESTNET if testnet else EIP712_DOMAIN_MAINNET
        self._domain_separator: bytes = self._compute_domain_separator()

    @property
    def address(self) -> str:
        return self._account.address

    # ------------------------------------------------------------------
    # Public signing interface
    # ------------------------------------------------------------------

    def sign_order(self, order_action: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
        """Sign order action. Returns (signature, nonce_ms)."""
        nonce = self._timestamp_ms()
        action_with_nonce = {**order_action, "nonce": nonce}
        connection_id = self._action_hash(action_with_nonce)
        sig = self._sign_agent(connection_id)
        return sig, nonce

    def sign_cancel(self, cancel_action: Dict[str, Any]) -> Tuple[Dict[str, Any], int]:
        """Sign cancel action. Returns (signature, nonce_ms)."""
        nonce = self._timestamp_ms()
        action_with_nonce = {**cancel_action, "nonce": nonce}
        connection_id = self._action_hash(action_with_nonce)
        sig = self._sign_agent(connection_id)
        return sig, nonce

    # ------------------------------------------------------------------
    # Action builders
    # ------------------------------------------------------------------

    @staticmethod
    def build_order_action(
        asset: int,
        is_buy: bool,
        limit_px: str,
        sz: str,
        reduce_only: bool = False,
        order_type: Optional[Dict[str, Any]] = None,
        cloid: Optional[str] = None,
    ) -> Dict[str, Any]:
        if order_type is None:
            order_type = {"limit": {"tif": "Gtc"}}

        order_wire: Dict[str, Any] = {
            "a": asset,
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

    @staticmethod
    def build_cancel_action(asset: int, oid: int) -> Dict[str, Any]:
        return {
            "type": "cancel",
            "cancels": [{"a": asset, "o": oid}],
        }

    @staticmethod
    def build_cancel_by_cloid(asset: int, cloid: str) -> Dict[str, Any]:
        return {
            "type": "cancelByCloid",
            "cancels": [{"asset": asset, "cloid": cloid}],
        }

    # ------------------------------------------------------------------
    # Authenticated request body construction
    # ------------------------------------------------------------------

    def generate_signed_request(
        self,
        action: Dict[str, Any],
        vault_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build authenticated POST body for /exchange."""
        if action.get("type") in ("cancel", "cancelByCloid"):
            sig, nonce = self.sign_cancel(action)
        else:
            sig, nonce = self.sign_order(action)

        body: Dict[str, Any] = {
            "action": action,
            "nonce": nonce,
            "signature": sig,
        }
        if vault_address is not None:
            body["vaultAddress"] = vault_address

        return body

    def generate_ws_auth_payload(self) -> Dict[str, Any]:
        nonce = self._timestamp_ms()
        action = {"type": "subscribe", "channel": "user", "nonce": nonce}
        connection_id = self._action_hash(action)
        sig = self._sign_agent(connection_id)

        return {
            "method": "subscribe",
            "subscription": {"type": "userEvents", "user": self.address},
            "signature": sig,
            "nonce": nonce,
        }

    # ------------------------------------------------------------------
    # EIP-712 signing internals
    # ------------------------------------------------------------------

    def _compute_domain_separator(self) -> bytes:
        return keccak(
            _DOMAIN_TYPE_HASH
            + keccak(self._domain["name"].encode())
            + keccak(self._domain["version"].encode())
            + abi_encode(["uint256"], [self._domain["chainId"]])
            + abi_encode(["address"], [self._domain["verifyingContract"]])
        )

    def _action_hash(self, action: Dict[str, Any]) -> bytes:
        canonical = json.dumps(action, sort_keys=True, separators=(",", ":"))
        return keccak(canonical.encode())

    def _sign_agent(self, connection_id: bytes) -> Dict[str, Any]:
        struct_hash = keccak(_AGENT_TYPE_HASH + abi_encode(["address"], [self._account.address]) + connection_id)

        digest = keccak(b"\x19\x01" + self._domain_separator + struct_hash)

        signed = self._account.signHash(digest)

        return {
            "r": hex(signed.r),
            "s": hex(signed.s),
            "v": signed.v,
        }

    @staticmethod
    def _timestamp_ms() -> int:
        return int(time.time() * 1000)
