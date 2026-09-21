from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

from notification_hub.config import ConfigurationError

type PrivateKey = ed25519.Ed25519PrivateKey | rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey


def load_private_key(path: Path) -> PrivateKey:
    try:
        data = path.read_bytes()
        key = serialization.load_pem_private_key(data, password=None)
    except (OSError, TypeError, ValueError) as exc:
        raise ConfigurationError(f"could not load private key from {path}") from exc
    supported = isinstance(key, (ed25519.Ed25519PrivateKey, rsa.RSAPrivateKey)) or (
        isinstance(key, ec.EllipticCurvePrivateKey)
        and isinstance(key.curve, (ec.SECP256R1, ec.SECP384R1))
    )
    if not supported:
        raise ConfigurationError(f"private key {path} must be Ed25519, RSA, P-256, or P-384")
    return key


def _quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class RequestSigner:
    def __init__(
        self,
        key_id: str,
        private_key: PrivateKey,
        *,
        clock: Callable[[], float] = time.time,
        validity_seconds: int = 60,
    ) -> None:
        self.key_id = key_id
        self.private_key = private_key
        self.clock = clock
        self.validity_seconds = validity_seconds

    @classmethod
    def from_file(
        cls,
        key_id: str,
        path: Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> RequestSigner:
        return cls(key_id, load_private_key(path), clock=clock)

    def headers(
        self,
        method: str,
        target_uri: str,
        body: bytes | None,
        *,
        nonce: str | None = None,
    ) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        components = ["@method", "@target-uri"]
        if body is not None:
            digest = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
            headers["Content-Type"] = "application/json"
            headers["Content-Digest"] = f"sha-256=:{digest}:"
            components.extend(("content-type", "content-digest"))
        created = int(self.clock())
        params = (
            f"({' '.join(json.dumps(item) for item in components)})"
            f";created={created};expires={created + self.validity_seconds}"
            f";keyid={_quoted(self.key_id)}"
        )
        if nonce is not None:
            params += f";nonce={_quoted(nonce)}"
        params += ';tag="notification-hub-v1"'
        lines = []
        for component in components:
            if component == "@method":
                value = method.upper()
            elif component == "@target-uri":
                value = target_uri
            else:
                value = headers[component.title()]
            lines.append(f'"{component}": {value}')
        lines.append(f'"@signature-params": {params}')
        signature = self._sign("\n".join(lines).encode("utf-8"))
        headers["Signature-Input"] = f"sig1={params}"
        headers["Signature"] = f"sig1=:{base64.b64encode(signature).decode('ascii')}:"
        return headers

    def _sign(self, value: bytes) -> bytes:
        key = self.private_key
        if isinstance(key, ed25519.Ed25519PrivateKey):
            return key.sign(value)
        if isinstance(key, rsa.RSAPrivateKey):
            return key.sign(
                value,
                padding.PSS(mgf=padding.MGF1(hashes.SHA512()), salt_length=64),
                hashes.SHA512(),
            )
        algorithm = hashes.SHA256() if isinstance(key.curve, ec.SECP256R1) else hashes.SHA384()
        der = key.sign(value, ec.ECDSA(algorithm))
        r, s = decode_dss_signature(der)
        size = (key.curve.key_size + 7) // 8
        return r.to_bytes(size, "big") + s.to_bytes(size, "big")


def encode_json(value: Mapping[str, object]) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
