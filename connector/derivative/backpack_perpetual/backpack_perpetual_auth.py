"""Technical implementation for Hummingbot Gateway V2.1."""

import base64
import hashlib
import hmac
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from hummingbot.connector.derivative.backpack_perpetual.backpack_perpetual_constants import (
    API_CALL_TIMEOUT,
    REST_URL,
)


class BackpackPerpetualAuth:
    """
    Handles authentication for the Backpack Exchange API.
    Supports both HMAC-SHA256 and Ed25519 signature schemes.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        signature_scheme: str = "ed25519",
    ) -> None:
        self._api_key: str = api_key
        self._api_secret: str = api_secret
        self._signature_scheme: str = signature_scheme

    @property
    def api_key(self) -> str:
        return self._api_key

    def _get_timestamp(self) -> int:
        return int(time.time() * 1000)

    def _sign_ed25519(self, payload: str) -> str:
        secret_bytes = base64.b64decode(self._api_secret)
        private_key = Ed25519PrivateKey.from_private_bytes(secret_bytes[:32])
        signature = private_key.sign(payload.encode("utf-8"))
        return base64.b64encode(signature).decode("utf-8")

    def _sign_hmac(self, payload: str) -> str:
        return base64.b64encode(
            hmac.new(
                self._api_secret.encode("utf-8"),
                payload.encode("utf-8"),
                hashlib.sha256,
            ).digest()
        ).decode("utf-8")

    def _build_signature_payload(
        self,
        timestamp: int,
        instruction: str,
        params: Optional[Dict[str, Any]] = None,
        window: int = 5000,
    ) -> str:
        ordered_params = ""
        if params:
            sorted_items = sorted(params.items())
            ordered_params = urlencode(sorted_items)
        return f"instruction={instruction}&{ordered_params}&timestamp={timestamp}&window={window}"

    def generate_auth_headers(
        self,
        instruction: str,
        params: Optional[Dict[str, Any]] = None,
        window: int = 5000,
    ) -> Dict[str, str]:
        timestamp = self._get_timestamp()
        payload = self._build_signature_payload(
            timestamp=timestamp,
            instruction=instruction,
            params=params,
            window=window,
        )

        if self._signature_scheme == "ed25519":
            signature = self._sign_ed25519(payload)
        else:
            signature = self._sign_hmac(payload)

        return {
            "X-API-Key": self._api_key,
            "X-Signature": signature,
            "X-Timestamp": str(timestamp),
            "X-Window": str(window),
            "Content-Type": "application/json; charset=utf-8",
        }

    def generate_ws_auth_payload(self) -> Dict[str, Any]:
        timestamp = self._get_timestamp()
        window = 5000
        payload = f"instruction=subscribe&timestamp={timestamp}&window={window}"

        if self._signature_scheme == "ed25519":
            signature = self._sign_ed25519(payload)
        else:
            signature = self._sign_hmac(payload)

        return {
            "method": "SUBSCRIBE",
            "params": ["account"],
            "signature": [self._api_key, signature, str(timestamp), str(window)],
        }
