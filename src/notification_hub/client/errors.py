from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class HubError(RuntimeError):
    """Base error raised while communicating with Notification Hub."""


class NetworkError(HubError):
    """The server could not be reached or the connection was interrupted."""


class ProtocolError(HubError):
    """The server returned a response outside the supported v1 protocol."""


class ServerError(HubError):
    """A well-formed error returned by the hub."""

    def __init__(
        self,
        status: int,
        code: str,
        message: str,
        details: object = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details
        self.headers = dict(headers or {})
        self.notification: Any | None = None

    @property
    def retryable(self) -> bool:
        return self.status == 429 or self.status >= 500


class ResetRequired(ProtocolError):
    """The local synchronized state must be replaced with a fresh snapshot."""
