from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from notification_hub.domain import parse_timestamp

from .errors import ProtocolError


@dataclass(frozen=True, slots=True)
class Nonce:
    value: str
    expires_at: float


class NoncePool:
    """A bounded, synchronized pool in which every allocated nonce is removed."""

    def __init__(
        self,
        fetch: Callable[[str, int], object],
        *,
        batch_size: int = 32,
        clock: Callable[[], float] = time.time,
        skew_seconds: float = 5,
    ) -> None:
        if not 1 <= batch_size <= 128:
            raise ValueError("nonce batch size must be between 1 and 128")
        self._fetch = fetch
        self._batch_size = batch_size
        self._clock = clock
        self._skew_seconds = skew_seconds
        self._refill_at = max(1, batch_size // 4)
        self._lock = threading.Lock()
        self._items: list[Nonce] = []

    def take(self) -> str:
        with self._lock:
            now = self._clock() + self._skew_seconds
            self._items = [item for item in self._items if item.expires_at > now]
            if len(self._items) <= self._refill_at:
                request_id = str(uuid.uuid4())
                raw = self._fetch(request_id, self._batch_size)
                additions = self._parse(raw)
                existing = {item.value for item in self._items}
                if any(item.value in existing for item in additions):
                    raise ProtocolError("hub reissued an outstanding nonce")
                self._items.extend(additions)
                self._items = [item for item in self._items if item.expires_at > now]
                if not self._items:
                    raise ProtocolError("hub returned no usable nonces")
            return self._items.pop(0).value

    def _parse(self, value: object) -> list[Nonce]:
        if not isinstance(value, list) or not value:
            raise ProtocolError("nonce response is malformed")
        result = []
        seen = set()
        for item in value:
            if (
                not isinstance(item, dict)
                or set(item) != {"value", "expires_at"}
                or not isinstance(item["value"], str)
                or not item["value"]
                or item["value"] in seen
            ):
                raise ProtocolError("nonce response is malformed")
            try:
                expires_at = parse_timestamp(item["expires_at"], require_canonical=True).timestamp()
            except (TypeError, ValueError) as exc:
                raise ProtocolError("nonce response is malformed") from exc
            seen.add(item["value"])
            result.append(Nonce(item["value"], expires_at))
        return result
