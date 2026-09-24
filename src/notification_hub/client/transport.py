from __future__ import annotations

import http.client
import json
import ssl
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from notification_hub.config import RemoteServerConfig

from .errors import NetworkError, ProtocolError, ServerError


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


class JsonTransport:
    """Small JSON HTTP transport with a request budget and connect timeout cap."""

    def __init__(self, config: RemoteServerConfig) -> None:
        self.config = config

    def send(
        self,
        method: str,
        url: str,
        body: bytes | None,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HttpResponse:
        deadline = time.monotonic() + timeout
        parsed = urlsplit(url)
        target = parsed.path or "/"
        if parsed.query:
            target += f"?{parsed.query}"
        if parsed.scheme == "https":
            context = (
                None if self.config.verify_tls else ssl._create_unverified_context()  # noqa: SLF001
            )
            connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                parsed.hostname,
                parsed.port,
                timeout=min(self.config.connect_timeout_seconds, timeout),
                context=context,
            )
        else:
            connection = http.client.HTTPConnection(
                parsed.hostname,
                parsed.port,
                timeout=min(self.config.connect_timeout_seconds, timeout),
            )
        try:
            connection.connect()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("request deadline elapsed during connection")
            if connection.sock is not None:
                connection.sock.settimeout(remaining)
            connection.request(method, target, body=body, headers=dict(headers))
            response = connection.getresponse()
            return HttpResponse(response.status, response.read(), dict(response.getheaders()))
        except (OSError, http.client.HTTPException, TimeoutError) as exc:
            raise NetworkError(f"could not contact hub: {exc}") from exc
        finally:
            connection.close()


def decode_json_response(response: HttpResponse) -> dict[str, Any]:
    try:
        value = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("hub returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise ProtocolError("hub returned a non-object JSON response")
    if response.status >= 400:
        if set(value) != {"error"} or not isinstance(value["error"], dict):
            raise ProtocolError(f"hub returned a malformed HTTP {response.status} error")
        error = value["error"]
        if (
            set(error) != {"code", "message", "details"}
            or not isinstance(error["code"], str)
            or not isinstance(error["message"], str)
            or not isinstance(error["details"], dict)
        ):
            raise ProtocolError(f"hub returned a malformed HTTP {response.status} error")
        raise ServerError(
            response.status,
            error["code"],
            error["message"],
            error["details"],
            headers=response.headers,
        )
    return value
