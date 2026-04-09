"""Technical implementation for Hummingbot Gateway V2.1."""

import json
import time
from typing import Any, Dict, Optional, Tuple

import msgpack
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_account.signers.local import LocalAccount
from eth_utils import keccak

from hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_constants import (
    EIP712_DOMAIN_MAINNET,
    EIP712_DOMAIN_TESTNET,
)

# ---------------------------------------------------------------------------
# EIP-712 type hashes
# ---------------------------------------------------------------------------
_DOMAIN_TYPE_HASH: bytes = keccak(b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
_AGENT_TYPE_HASH: bytes = keccak(b"Agent(address source,bytes32 connectionId)")

# Null address used when signing without vault delegation
_NULL_ADDRESS: str = "0x0000000000000000000000000000000000000000"


def _canonical_order_wire(w: Dict[str, Any]) -> bytes:
    """HL L1 canonical field ordering for order wire: a, b, p, s, r, t, [c]."""
    fields = [w["a"], w["b"], w["p"], w["s"], w["r"], w["t"]]
    if "c" in w:
        fields.append(w["c"])
    return msgpack.packb(fields, use_bin_type=True)


def _canonical_cancel_wire(c: Dict[str, Any]) -> bytes:
    """HL L1 canonical field ordering for cancel wire: a, o."""
    return msgpack.packb([c["a"], c["o"]], use_bin_type=True)


def _canonical_cancel_cloid_wire(c: Dict[str, Any]) -> bytes:
    """HL L1 canonical field ordering for cancel-by-cloid wire: asset, cloid."""
    return msgpack.packb([c["asset"], c["cloid"]], use_bin_type=True)


class HyperliquidPerpetualAuth:
    """EIP-712 L1 signing with agent delegation and vault support."""

    def __init__(
        self,
        private_key: str,
        testnet: bool = False,
        vault_address: Optional[str] = None,
    ) -> None:
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        self._account: LocalAccount = Account.from_key(private_key)
        self._vault_address: Optional[str] = vault_address
        self._testnet: bool = testnet
        self._domain: Dict[str, Any] = EIP712_DOMAIN_TESTNET if testnet else EIP712_DOMAIN_MAINNET
        self._domain_separator: bytes = self._compute_domain_separator()

        # Agent delegation state
        self._agent_account: Optional[LocalAccount] = None
        self._agent_domain_separator: Optional[bytes] = None

    @property
    def address(self) -> str:
        return self._account.address

    @property
    def active_address(self) -> str:
        """Wallet address used for signing — agent if delegated, otherwise primary."""
        if self._agent_account is not None:
            return self._agent_account.address
        return self._account.address

    # ------------------------------------------------------------------
    # Agent delegation
    # ------------------------------------------------------------------

    def connect_agent(self, agent_private_key: str) -> Dict[str, Any]:
        """
        Register a phantom agent wallet for delegated signing.
        Returns the signed connection payload to POST to /exchange.
        """
        if not agent_private_key.startswith("0x"):
            agent_private_key = "0x" + agent_private_key
        self._agent_account = Account.from_key(agent_private_key)

        # Agent uses its own domain separator (same domain params)
        self._agent_domain_separator = self._compute_domain_separator()

        # Build the agent connection approval signed by the primary wallet
        connection_id = self._compute_agent_connection_id(self._agent_account.address)
        sig = self._sign_with_account(self._account, self._domain_separator, connection_id)
        nonce = self._timestamp_ms()

        return {
            "action": {
                "type": "connect",
                "chain": "Hyperliquid",
                "agent": {"source": self._account.address, "connectionId": connection_id.hex()},
                "agentAddress": self._agent_account.address,
            },
            "nonce": nonce,
            "signature": sig,
        }

    def _compute_agent_connection_id(self, agent_address: str) -> bytes:
        return keccak(abi_encode(["address", "address"], [self._account.address, agent_address]))

    # ------------------------------------------------------------------
    # Signing interface
    # ------------------------------------------------------------------

    def sign_order(
        self,
        order_action: Dict[str, Any],
        vault_address: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], int]:
        """Sign order action. Returns (signature, nonce_ms)."""
        nonce = self._timestamp_ms()
        connection_id = self._compute_action_hash(order_action, nonce, vault_address)
        sig = self._sign_active(connection_id)
        return sig, nonce

    def sign_cancel(
        self,
        cancel_action: Dict[str, Any],
        vault_address: Optional[str] = None,
    ) -> Tuple[Dict[str, Any], int]:
        """Sign cancel action. Returns (signature, nonce_ms)."""
        nonce = self._timestamp_ms()
        connection_id = self._compute_action_hash(cancel_action, nonce, vault_address)
        sig = self._sign_active(connection_id)
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
    # Request body construction
    # ------------------------------------------------------------------

    def generate_signed_request(
        self,
        action: Dict[str, Any],
        vault_address: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Build authenticated POST body for /exchange."""
        effective_vault = vault_address or self._vault_address

        if action.get("type") in ("cancel", "cancelByCloid"):
            sig, nonce = self.sign_cancel(action, vault_address=effective_vault)
        else:
            sig, nonce = self.sign_order(action, vault_address=effective_vault)

        body: Dict[str, Any] = {
            "action": action,
            "nonce": nonce,
            "signature": sig,
        }
        if effective_vault is not None:
            body["vaultAddress"] = effective_vault

        return body

    def generate_ws_auth_payload(self) -> Dict[str, Any]:
        nonce = self._timestamp_ms()
        connection_id = self._compute_action_hash({"type": "subscribe", "channel": "user"}, nonce, vault_address=None)
        sig = self._sign_active(connection_id)

        return {
            "method": "subscribe",
            "subscription": {"type": "userEvents", "user": self.address},
            "signature": sig,
            "nonce": nonce,
        }

    # ------------------------------------------------------------------
    # EIP-712 internals
    # ------------------------------------------------------------------

    def _compute_domain_separator(self) -> bytes:
        return keccak(
            _DOMAIN_TYPE_HASH
            + keccak(self._domain["name"].encode())
            + keccak(self._domain["version"].encode())
            + abi_encode(["uint256"], [self._domain["chainId"]])
            + abi_encode(["address"], [self._domain["verifyingContract"]])
        )

    def _compute_action_hash(
        self,
        action: Dict[str, Any],
        nonce: int,
        vault_address: Optional[str] = None,
    ) -> bytes:
        """
        Canonical action hashing per HL L1 spec.
        Uses msgpack-ordered field serialization for order/cancel wires,
        then keccak256 over the assembled payload.
        """
        action_type = action.get("type", "")
        parts: list = []

        if action_type == "order":
            for w in action["orders"]:
                parts.append(_canonical_order_wire(w))
            parts.append(msgpack.packb(action.get("grouping", "na"), use_bin_type=True))
        elif action_type == "cancel":
            for c in action["cancels"]:
                parts.append(_canonical_cancel_wire(c))
        elif action_type == "cancelByCloid":
            for c in action["cancels"]:
                parts.append(_canonical_cancel_cloid_wire(c))
        else:
            # Fallback for non-trading actions (subscribe, etc.)
            parts.append(json.dumps(action, sort_keys=True, separators=(",", ":")).encode())

        nonce_bytes = msgpack.packb(nonce, use_bin_type=True)
        vault_bytes = b""
        if vault_address is not None:
            vault_bytes = msgpack.packb(vault_address, use_bin_type=True)

        return keccak(b"".join(parts) + nonce_bytes + vault_bytes)

    def _sign_active(self, connection_id: bytes) -> Dict[str, Any]:
        """Sign using the active account (agent if delegated, else primary)."""
        if self._agent_account is not None:
            domain_sep = self._agent_domain_separator or self._domain_separator
            return self._sign_with_account(self._agent_account, domain_sep, connection_id)
        return self._sign_with_account(self._account, self._domain_separator, connection_id)

    @staticmethod
    def _sign_with_account(account: LocalAccount, domain_separator: bytes, connection_id: bytes) -> Dict[str, Any]:
        struct_hash = keccak(_AGENT_TYPE_HASH + abi_encode(["address"], [account.address]) + connection_id)
        digest = keccak(b"\x19\x01" + domain_separator + struct_hash)
        signed = account.unsafe_sign_hash(digest)

        return {
            "r": hex(signed.r),
            "s": hex(signed.s),
            "v": signed.v,
        }

    @staticmethod
    def _timestamp_ms() -> int:
        return int(time.time() * 1000)
