from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

from notification_hub.client.errors import HubError, NetworkError, ServerError
from notification_hub.client.transport import JsonTransport
from notification_hub.config import RemoteServerConfig


class WaitTimeout(HubError):
    """A local deadline elapsed while the server-side request remains pending."""


@dataclass(frozen=True, slots=True)
class Outcome:
    notification_id: str
    state: str
    response: Mapping[str, Any] | None

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> Outcome:
        notification_id = value.get("notification_id")
        state = value.get("state")
        response = value.get("response")
        if (
            not isinstance(notification_id, str)
            or state not in {"not_requested", "pending", "answered", "cancelled", "expired"}
            or (response is not None and not isinstance(response, dict))
        ):
            raise NetworkError("hub returned a malformed outcome")
        return cls(notification_id, state, response)

    def to_dict(self) -> dict[str, Any]:
        return {
            "notification_id": self.notification_id,
            "state": self.state,
            "response": dict(self.response) if self.response is not None else None,
        }


Transport = Callable[[str, str, bytes | None, float], tuple[int, bytes]]


class NotifierClient:
    """Minimal unauthenticated producer client for the public hub endpoints."""

    def __init__(
        self,
        config: RemoteServerConfig,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        create_attempts: int = 3,
    ) -> None:
        self.config = config
        self._transport = transport or self._urlopen_transport
        self._sleep = sleep
        self._create_attempts = create_attempts

    def create(self, notification: Mapping[str, Any]) -> dict[str, Any]:
        """Create idempotently, retrying only failures which may have committed."""
        last_error: HubError | None = None
        for attempt in range(self._create_attempts):
            try:
                value = self._request("POST", "/api/v1/notifications", notification)
                if not isinstance(value.get("notification"), dict):
                    raise NetworkError("hub returned a malformed create response")
                return value
            except (NetworkError, ServerError) as exc:
                if isinstance(exc, ServerError) and not exc.retryable:
                    raise
                last_error = exc
                if attempt + 1 < self._create_attempts:
                    self._sleep(0.25 * (2**attempt))
        assert last_error is not None
        raise last_error

    def poll(
        self,
        notification_id: str,
        *,
        wait_seconds: int = 0,
        request_timeout: float | None = None,
    ) -> Outcome:
        if request_timeout is not None and request_timeout <= 0:
            raise ValueError("request_timeout must be greater than zero")
        query = urlencode({"wait_seconds": wait_seconds})
        transport_timeout = max(self.config.request_timeout_seconds, wait_seconds + 1)
        if request_timeout is not None:
            transport_timeout = min(transport_timeout, request_timeout)
        value = self._request(
            "GET",
            f"/api/v1/notifications/{quote(notification_id, safe='')}/outcome?{query}",
            timeout=transport_timeout,
        )
        return Outcome.from_dict(value)

    def wait(self, notification_id: str, *, timeout: float | None = None) -> Outcome:
        """Long-poll until terminal; the local deadline includes network time."""
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        deadline = None if timeout is None else time.monotonic() + timeout
        retry_delay = 0.25
        while True:
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                raise WaitTimeout("timed out while the notification remains pending")
            wait_seconds = min(30, max(1, self.config.request_timeout_seconds - 1))
            if remaining is not None:
                wait_seconds = max(0, min(wait_seconds, int(remaining)))
            try:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise WaitTimeout("timed out while the notification remains pending")
                outcome = self.poll(
                    notification_id, wait_seconds=wait_seconds, request_timeout=remaining
                )
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise WaitTimeout("timed out while the notification remains pending")
                retry_delay = 0.25
                if outcome.state != "pending":
                    return outcome
                if wait_seconds == 0 and remaining is not None:
                    self._sleep(min(0.25, remaining))
            except (NetworkError, ServerError) as exc:
                if isinstance(exc, ServerError) and not exc.retryable:
                    raise
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise WaitTimeout("timed out while the notification remains pending") from exc
                delay = retry_delay if remaining is None else min(retry_delay, max(remaining, 0))
                self._sleep(delay)
                retry_delay = min(retry_delay * 2, 5)

    def cancel(self, notification_id: str, *, reason: str | None = None) -> dict[str, Any]:
        body = {} if reason is None else {"reason": reason}
        return self._request(
            "POST", f"/api/v1/notifications/{quote(notification_id, safe='')}/cancel", body
        )

    def _request(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None = None,
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        encoded = (
            None
            if body is None
            else json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        )
        status, response_body = self._transport(
            method,
            f"{self.config.url}{path}",
            encoded,
            timeout or self.config.request_timeout_seconds,
        )
        try:
            value = json.loads(response_body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NetworkError("hub returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise NetworkError("hub returned a non-object JSON response")
        if status >= 400:
            error = value.get("error")
            if isinstance(error, dict):
                code = error.get("code", "http_error")
                message = error.get("message", f"hub returned HTTP {status}")
                details = error.get("details")
            else:
                code, message, details = "http_error", f"hub returned HTTP {status}", None
            raise ServerError(status, str(code), str(message), details)
        return value

    def _urlopen_transport(
        self, method: str, url: str, body: bytes | None, timeout: float
    ) -> tuple[int, bytes]:
        headers = {"Accept": "application/json", "X-Request-ID": _request_id()}
        if body is not None:
            headers["Content-Type"] = "application/json"
        response = JsonTransport(self.config).send(method, url, body, headers, timeout)
        return response.status, response.body


def _request_id() -> str:
    # Kept local to avoid exposing a stable producer credential: this is only a
    # log-correlation value and is intentionally distinct for every attempt.
    import uuid

    return str(uuid.uuid4())
