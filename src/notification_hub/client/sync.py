from __future__ import annotations

import threading
from collections.abc import Callable, Iterator

from .errors import NetworkError, ProtocolError, ServerError
from .hub import HubClient
from .models import HubEvent, SyncState


def watch_events(
    client: HubClient,
    *,
    state: SyncState | None = None,
    stop_event: threading.Event | None = None,
    wait_seconds: int = 25,
    on_reset: Callable[[SyncState], None] | None = None,
) -> Iterator[HubEvent]:
    """Follow committed events, repairing local state from snapshots when necessary."""
    stop = stop_event or threading.Event()
    needs_snapshot = state is None
    notify_reset = False
    state = state or SyncState()
    delay = 0.5
    while not stop.is_set():
        try:
            if needs_snapshot:
                snapshot = client.get_snapshot()
                state.replace(snapshot)
                needs_snapshot = False
                if notify_reset and on_reset is not None:
                    on_reset(state)
                notify_reset = False
                delay = 0.5
            page = client.get_events(state.last_sequence, wait_seconds=wait_seconds)
            applied = state.apply_page(page)
            delay = 0.5
            yield from applied
            while page.has_more and not stop.is_set():
                page = client.get_events(state.last_sequence, wait_seconds=0)
                applied = state.apply_page(page)
                yield from applied
        except ProtocolError:
            if needs_snapshot:
                raise
            needs_snapshot = True
            notify_reset = True
        except ServerError as exc:
            if (
                exc.code == "cursor_expired"
                and isinstance(exc.details, dict)
                and exc.details.get("reset_required") is True
            ):
                needs_snapshot = True
                notify_reset = True
                continue
            if not exc.retryable:
                raise
            if stop.wait(delay * (0.5 + client._random())):  # noqa: SLF001
                break
            delay = min(delay * 2, 30)
        except NetworkError:
            if stop.wait(delay * (0.5 + client._random())):  # noqa: SLF001
                break
            delay = min(delay * 2, 30)
