"""Technical implementation for Hummingbot Gateway V2.1."""

import json
import time
from typing import Any, Dict, Tuple

from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak
from hummingbot.connector.derivative.hyperliquid_perpetual.hyperliquid_perpetual_constants import (
    EIP712_DOMAIN_NAME,
    EIP712_DOMAIN_VERSION,
    EIP712_MAINNET_CHAIN_ID,
    EIP712_TESTNET_CHAIN_ID,
)

_DOMAIN_TYPE_HASH = keccak(b"EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)")
_AGENT_TYPE_HASH = keccak(b"Agent(address source,bytes32 connectionId)")


class HyperliquidPerpetualAuth:
    """
    Native EIP-712 signer for the Hyperliquid L1 exchange.
    Constructs domain separators and phantom agent structs without SDK dependency.
    """

    def __init__(self, secret_key: str, use_testnet: bool = False) -> None:
        if not secret_key.startswith("0x"):
            secret_key = "0x" + secret_key
        self._account: Account = Account.from_key(secret_key)
        self._use_testnet: bool = use_testnet
        self._domain: Dict[str, Any] = {
            "name": EIP712_DOMAIN_NAME,
            "version": EIP712_DOMAIN_VERSION,
            "chainId": EIP712_TESTNET_CHAIN_ID if use_testnet else EIP712_MAINNET_CHAIN_ID,
            "verifyingContract": "0x0000000000000000000000000000000000000000",
        }
        self._domain_separator: bytes = self._compute_domain_separator()

    @property
    def address(self) -> str:
        return self._account.address

    @property
    def chain_id(self) -> int:
        return self._domain["chainId"]

    # ------------------------------------------------------------------
    # Public signing interface
    # ------------------------------------------------------------------

    def sign_order_action(self, action: Dict[str, Any], nonce: int) -> Dict[str, Any]:
        """
        Signs an order or cancel action and returns the full exchange payload.
        Uses deterministic JSON hashing for the connection ID.
        """
        action_with_nonce = {**action, "nonce": nonce}
        connection_id = self._action_hash(action_with_nonce)
        sig = self._sign_agent(connection_id)
        return {
            "action": action,
            "nonce": nonce,
            "signature": sig,
            "vaultAddress": None,
        }

    def sign_l1_action(self, action: Dict[str, Any], nonce: int) -> Dict[str, Any]:
        """
        Signs a generic L1 action (leverage changes, transfers, withdrawals).
        """
        action_with_nonce = {**action, "nonce": nonce}
        connection_id = self._action_hash(action_with_nonce)
        sig = self._sign_agent(connection_id)
        return {
            "action": action,
            "nonce": nonce,
            "signature": sig,
            "vaultAddress": None,
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

    def _action_hash(self, action: dict) -> bytes:
        """
        Computes the deterministic keccak256 hash of a canonical JSON-serialized action.
        """
        canonical = json.dumps(action, sort_keys=True, separators=(",", ":"))
        return keccak(canonical.encode())

    def _sign_agent(self, connection_id: bytes) -> Dict[str, Any]:
        """
        Signs an Agent(address source, bytes32 connectionId) EIP-712 struct.
        Returns {"r": hex, "s": hex, "v": int}.
        """
        struct_hash = keccak(_AGENT_TYPE_HASH + abi_encode(["address"], [self._account.address]) + connection_id)

        digest = keccak(b"\x19\x01" + self._domain_separator + struct_hash)

        signed = self._account.signHash(digest)

        return {
            "r": hex(signed.r),
            "s": hex(signed.s),
            "v": signed.v,
        }

    @staticmethod
    def timestamp_ms() -> int:
        return int(time.time() * 1000)
