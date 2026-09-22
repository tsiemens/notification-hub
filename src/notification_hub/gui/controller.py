from __future__ import annotations

import copy
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from notification_hub.client import HubClient, NetworkError, ProtocolError, ServerError, SyncState
from notification_hub.client.models import ClientSnapshot, EventPage, HubEvent, MutationResult
from notification_hub.domain import Notification

ConnectionState = Literal["starting", "connected", "reconnecting", "offline", "fatal"]


@dataclass(frozen=True, slots=True)
class ConnectionStatus:
    state: ConnectionState
    message: str | None = None

    def to_dict(self) -> dict[str, str | None]:
        return {"state": self.state, "message": self.message}


class GuiController:
    """Own synchronized hub state and expose a revisioned, thread-safe view of it."""

    def __init__(
        self,
        client: HubClient,
        *,
        update_log_size: int = 256,
        update_wait_seconds: float = 1.0,
        event_wait_seconds: int = 25,
        thread_factory: Callable[..., threading.Thread] = threading.Thread,
    ) -> None:
        if update_log_size < 1:
            raise ValueError("update_log_size must be positive")
        if update_wait_seconds < 0:
            raise ValueError("update_wait_seconds cannot be negative")
        if not 0 <= event_wait_seconds <= 30:
            raise ValueError("event_wait_seconds must be between 0 and 30")
        self.client = client
        self._state = SyncState()
        self._has_snapshot = False
        self._connection = ConnectionStatus("starting")
        self._condition = threading.Condition(threading.RLock())
        self._revision = 0
        self._updates: deque[tuple[int, dict[str, Any]]] = deque(maxlen=update_log_size)
        self._update_wait_seconds = update_wait_seconds
        self._event_wait_seconds = event_wait_seconds
        self._stop_event = threading.Event()
        self._thread_factory = thread_factory
        self._worker: threading.Thread | None = None

    def start(self) -> None:
        with self._condition:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop_event.clear()
            self._worker = self._thread_factory(
                target=self._run,
                name="notification-hub-gui-sync",
                daemon=True,
            )
            self._worker.start()

    def stop(self, *, join_timeout: float = 2.0) -> None:
        self._stop_event.set()
        with self._condition:
            self._condition.notify_all()
            worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=max(0.0, join_timeout))

    @property
    def stopped(self) -> bool:
        return self._stop_event.is_set()

    def get_initial_state(self) -> dict[str, Any]:
        with self._condition:
            return copy.deepcopy(
                {
                    "revision": self._revision,
                    "connection": self._connection.to_dict(),
                    "snapshot": self._snapshot_dict() if self._has_snapshot else None,
                }
            )

    def get_updates(self, after_revision: int) -> dict[str, Any]:
        if (
            isinstance(after_revision, bool)
            or not isinstance(after_revision, int)
            or after_revision < 0
        ):
            raise ValueError("after_revision must be a non-negative integer")
        with self._condition:
            if after_revision > self._revision:
                raise ValueError("after_revision is newer than the controller revision")
            if after_revision == self._revision and not self._stop_event.is_set():
                self._condition.wait_for(
                    lambda: self._revision > after_revision or self._stop_event.is_set(),
                    timeout=self._update_wait_seconds,
                )
            if self._updates and after_revision < self._updates[0][0] - 1:
                updates: list[dict[str, Any]] = [
                    {"kind": "connection", "connection": self._connection.to_dict()}
                ]
                if self._has_snapshot:
                    updates.append({"kind": "reset", "snapshot": self._snapshot_dict()})
            else:
                updates = [value for revision, value in self._updates if revision > after_revision]
            return copy.deepcopy({"revision": self._revision, "updates": updates})

    def notification(self, notification_id: str) -> Notification | None:
        with self._condition:
            return self._state.notifications.get(notification_id)

    def mutations_available(self) -> bool:
        with self._condition:
            return self._connection.state == "connected"

    def set_read_state(self, notification_ids: list[str], read: bool) -> MutationResult:
        self._require_connected()
        return self.client.set_read_state(notification_ids, read)

    def respond(self, notification_id: str, option_id: str, message: str | None) -> MutationResult:
        self._require_connected()
        return self.client.respond(notification_id, option_id, message)

    def _require_connected(self) -> None:
        if not self.mutations_available():
            raise NetworkError("mutations are unavailable while disconnected")

    def _snapshot_dict(self) -> dict[str, Any]:
        return {
            "sequence": self._state.last_sequence,
            "domains": [item.to_dict() for item in self._state.domains.values()],
            "notifications": [item.to_dict() for item in self._state.notifications.values()],
        }

    def _publish(self, update: dict[str, Any]) -> None:
        self._revision += 1
        self._updates.append((self._revision, update))
        self._condition.notify_all()

    def _set_connection(self, state: ConnectionState, message: str | None = None) -> None:
        with self._condition:
            status = ConnectionStatus(state, message)
            if status == self._connection:
                return
            self._connection = status
            self._publish({"kind": "connection", "connection": status.to_dict()})

    def _replace(self, snapshot: ClientSnapshot) -> None:
        with self._condition:
            self._state.replace(snapshot)
            self._has_snapshot = True
            self._publish({"kind": "reset", "snapshot": self._snapshot_dict()})

    def _apply_page(self, page: EventPage) -> tuple[HubEvent, ...]:
        with self._condition:
            applied = self._state.apply_page(page)
            if applied:
                self._publish(
                    {
                        "kind": "events",
                        "events": [event.to_dict() for event in applied],
                        "domains": [item.to_dict() for item in self._state.domains.values()],
                    }
                )
            return applied

    def _run(self) -> None:
        needs_snapshot = True
        delay = 0.5
        while not self._stop_event.is_set():
            try:
                if needs_snapshot:
                    self._replace(self.client.get_snapshot())
                    needs_snapshot = False
                    self._set_connection("connected")
                page = self.client.get_events(
                    self._state.last_sequence, wait_seconds=self._event_wait_seconds
                )
                self._apply_page(page)
                self._set_connection("connected")
                delay = 0.5
                while page.has_more and not self._stop_event.is_set():
                    page = self.client.get_events(self._state.last_sequence, wait_seconds=0)
                    self._apply_page(page)
            except ProtocolError:
                if not self._has_snapshot:
                    self._set_connection("fatal", "The server returned an incompatible response.")
                    return
                needs_snapshot = True
                self._set_connection("reconnecting", "Refreshing synchronized state.")
            except ServerError as exc:
                if (
                    exc.code == "cursor_expired"
                    and isinstance(exc.details, dict)
                    and exc.details.get("reset_required") is True
                ):
                    needs_snapshot = True
                    self._set_connection("reconnecting", "Refreshing expired synchronized state.")
                    continue
                if not exc.retryable:
                    self._set_connection("fatal", "Authentication or server access was rejected.")
                    return
                self._set_connection(
                    "offline" if self._has_snapshot else "reconnecting",
                    "The server is temporarily unavailable; retrying.",
                )
                if self._stop_event.wait(delay * (0.5 + self.client._random())):  # noqa: SLF001
                    return
                delay = min(delay * 2, 30)
            except NetworkError:
                self._set_connection(
                    "offline" if self._has_snapshot else "reconnecting",
                    "The server cannot be reached; retrying.",
                )
                if self._stop_event.wait(delay * (0.5 + self.client._random())):  # noqa: SLF001
                    return
                delay = min(delay * 2, 30)
