from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from flask import Request

from notification_hub.config import AuthConfig, ConfigurationError, SigningKey

type VerificationPublicKey = ed25519.Ed25519PublicKey | rsa.RSAPublicKey | ec.EllipticCurvePublicKey

# A complete Signature-Input dictionary member, for example:
# sig1=("@method" "@target-uri");created=1726800000;keyid="desktop-ui"
_INPUT_RE = re.compile(r'([a-z][a-z0-9_.*-]*)=((?:\("[^"]+"(?: "[^"]+")*\))(?:;.*)?)')

# A complete Signature dictionary member containing an RFC 8941 byte sequence,
# for example: sig1=:YWJjZA==:
_SIGNATURE_RE = re.compile(r"([a-z][a-z0-9_.*-]*)=:([A-Za-z0-9+/]*={0,2}):")

# One signature parameter with either a quoted string or integer value, for
# example: ;keyid="desktop-ui" or ;created=1726800000
_PARAM_RE = re.compile(r';([a-z][a-z0-9_.*-]*)=("(?:[^"\\]|\\["\\])*"|-?[0-9]+)')

# One covered-component identifier from the inner list, for example:
# "@method", "@target-uri", or "content-digest"
_COMPONENT_RE = re.compile(r'"([a-z0-9@_-]+)"')

# A backslash-escaped quote or backslash inside an RFC 8941 string, for example:
# \" in keyid="desktop-\"ui\""
_ESCAPED_STRING_CHAR_RE = re.compile(r'\\(["\\])')

# The required single SHA-256 Content-Digest member, for example:
# sha-256=:ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0=:
_CONTENT_DIGEST_RE = re.compile(r"sha-256=:([A-Za-z0-9+/]{43}=):")


class AuthenticationError(RuntimeError):
    """The request did not carry a valid Notification Hub signature."""


class AuthorizationError(RuntimeError):
    """The authenticated principal does not have the required scope."""


@dataclass(frozen=True, slots=True)
class AuthenticatedPrincipal:
    principal: str
    key_id: str
    scopes: frozenset[str]
    nonce: str | None


@dataclass(frozen=True, slots=True)
class _VerificationKey:
    config: SigningKey
    key: VerificationPublicKey


class RequestAuthenticator:
    """Verify the strict RFC 9421 profile used by Notification Hub."""

    def __init__(self, config: AuthConfig) -> None:
        self.max_age = config.signature_max_age_seconds
        self.keys = {item.key_id: self._load_key(item) for item in config.signing_keys}

    @staticmethod
    def _load_key(config: SigningKey) -> _VerificationKey:
        path = Path(config.public_key_file)
        try:
            data = path.read_bytes()
            key = serialization.load_pem_public_key(data)
        except (OSError, ValueError) as exc:
            raise ConfigurationError(
                f"could not load public key {config.key_id!r} from {path}: {exc}"
            ) from exc
        supported = isinstance(key, (ed25519.Ed25519PublicKey, rsa.RSAPublicKey)) or (
            isinstance(key, ec.EllipticCurvePublicKey)
            and isinstance(key.curve, (ec.SECP256R1, ec.SECP384R1))
        )
        if not supported:
            raise ConfigurationError(
                f"public key {config.key_id!r} must be Ed25519, RSA, P-256, or P-384"
            )
        return _VerificationKey(config, key)

    def authenticate(
        self, request: Request, required_scope: str | None, *, require_nonce: bool = False
    ) -> AuthenticatedPrincipal:
        input_value = request.headers.get("Signature-Input")
        signature_value = request.headers.get("Signature")
        if input_value is None or signature_value is None:
            raise AuthenticationError("a message signature is required")

        input_match = _INPUT_RE.fullmatch(input_value.strip())
        signature_match = _SIGNATURE_RE.fullmatch(signature_value.strip())
        if input_match is None or signature_match is None:
            raise AuthenticationError("signature fields are malformed or ambiguous")
        if input_match.group(1) != signature_match.group(1):
            raise AuthenticationError("signature labels do not match")

        signature_params = input_match.group(2)
        close = signature_params.find(")")
        components_text = signature_params[1:close]
        components = _COMPONENT_RE.findall(components_text)
        if not components or " ".join(f'"{item}"' for item in components) != components_text:
            raise AuthenticationError("covered components are malformed")
        if len(components) != len(set(components)):
            raise AuthenticationError("covered components must not be repeated")

        params_text = signature_params[close + 1 :]
        params: dict[str, str | int] = {}
        position = 0
        for match in _PARAM_RE.finditer(params_text):
            if match.start() != position:
                raise AuthenticationError("signature parameters are malformed")
            name, raw = match.groups()
            if name in params:
                raise AuthenticationError("signature parameters must not be repeated")
            params[name] = self._parse_parameter(raw)
            position = match.end()
        if position != len(params_text):
            raise AuthenticationError("signature parameters are malformed")

        required_params = {"created", "expires", "keyid", "tag"}
        if not required_params <= params.keys() or params["tag"] != "notification-hub-v1":
            raise AuthenticationError("required signature parameters are missing or invalid")
        allowed_params = required_params | {"nonce"}
        if set(params) - allowed_params:
            raise AuthenticationError("unsupported signature parameter")
        if not isinstance(params["created"], int) or not isinstance(params["expires"], int):
            raise AuthenticationError("signature times must be integer Unix timestamps")
        now = int(time.time())
        created, expires = params["created"], params["expires"]
        if created > now or created < now - self.max_age or expires < now:
            raise AuthenticationError("signature is not currently valid")
        if expires <= created or expires - created > self.max_age:
            raise AuthenticationError("signature validity interval is invalid")
        if not isinstance(params["keyid"], str):
            raise AuthenticationError("signature keyid is invalid")
        verification_key = self.keys.get(params["keyid"])
        if verification_key is None:
            raise AuthenticationError("signature key is unknown")

        nonce = params.get("nonce")
        if require_nonce and (not isinstance(nonce, str) or not nonce):
            raise AuthenticationError("a signed nonce is required")
        if nonce is not None and not isinstance(nonce, str):
            raise AuthenticationError("signature nonce is invalid")

        required_components = {"@method", "@target-uri"}
        body = request.get_data(cache=True)
        if body:
            required_components |= {"content-type", "content-digest"}
        if not required_components <= set(components):
            raise AuthenticationError("signature does not cover all required components")
        if body:
            self._verify_content_digest(request, body)

        signature_base = self._signature_base(request, components, signature_params)
        try:
            signature = base64.b64decode(signature_match.group(2), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise AuthenticationError("signature value is not valid base64") from exc
        self._verify_signature(verification_key.key, signature, signature_base)

        key_config = verification_key.config
        if required_scope is not None and required_scope not in key_config.scopes:
            raise AuthorizationError(f"principal lacks the {required_scope!r} scope")
        return AuthenticatedPrincipal(
            key_config.principal, key_config.key_id, key_config.scopes, nonce
        )

    @staticmethod
    def _parse_parameter(raw: str) -> str | int:
        if not raw.startswith('"'):
            return int(raw)
        value = raw[1:-1]
        if "\\" in value:
            value = _ESCAPED_STRING_CHAR_RE.sub(r"\1", value)
        return value

    @staticmethod
    def _verify_content_digest(request: Request, body: bytes) -> None:
        value = request.headers.get("Content-Digest")
        match = _CONTENT_DIGEST_RE.fullmatch(value or "")
        if match is None:
            raise AuthenticationError("Content-Digest must contain one sha-256 digest")
        expected = base64.b64encode(hashlib.sha256(body).digest()).decode("ascii")
        if not hmac.compare_digest(match.group(1), expected):
            raise AuthenticationError("Content-Digest does not match the request body")

    @staticmethod
    def _signature_base(request: Request, components: list[str], params: str) -> bytes:
        lines: list[str] = []
        for component in components:
            if component == "@method":
                value = request.method
            elif component == "@target-uri":
                value = RequestAuthenticator._target_uri(request)
            elif component.startswith("@"):
                raise AuthenticationError(f"unsupported derived component: {component}")
            else:
                values = request.headers.getlist(component)
                if not values:
                    raise AuthenticationError(f"covered field is missing: {component}")
                value = ", ".join(item.strip() for item in values)
            lines.append(f'"{component}": {value}')
        lines.append(f'"@signature-params": {params}')
        return "\n".join(lines).encode("utf-8")

    @staticmethod
    def _target_uri(request: Request) -> str:
        # Flask's request.url decodes percent-encoded query characters. The
        # signature must cover the request target as it arrived at the server.
        raw_target = request.environ.get("RAW_URI") or request.environ.get("REQUEST_URI")
        if not isinstance(raw_target, str) or not raw_target:
            raise AuthenticationError("raw request target is unavailable")
        if raw_target.startswith("/"):
            return f"{request.scheme}://{request.host}{raw_target}"
        if raw_target.startswith(("http://", "https://")):
            return raw_target
        raise AuthenticationError("raw request target is invalid")

    @staticmethod
    def _verify_signature(
        key: VerificationPublicKey, signature: bytes, signature_base: bytes
    ) -> None:
        try:
            if isinstance(key, ed25519.Ed25519PublicKey):
                key.verify(signature, signature_base)
            elif isinstance(key, rsa.RSAPublicKey):
                key.verify(
                    signature,
                    signature_base,
                    padding.PSS(mgf=padding.MGF1(hashes.SHA512()), salt_length=64),
                    hashes.SHA512(),
                )
            elif isinstance(key.curve, ec.SECP256R1):
                if len(signature) != 64:
                    raise InvalidSignature
                der = encode_dss_signature(
                    int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
                )
                key.verify(der, signature_base, ec.ECDSA(hashes.SHA256()))
            else:
                if len(signature) != 96:
                    raise InvalidSignature
                der = encode_dss_signature(
                    int.from_bytes(signature[:48], "big"), int.from_bytes(signature[48:], "big")
                )
                key.verify(der, signature_base, ec.ECDSA(hashes.SHA384()))
        except InvalidSignature as exc:
            raise AuthenticationError("message signature verification failed") from exc
