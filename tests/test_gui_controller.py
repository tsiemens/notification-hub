from __future__ import annotations

import uuid

from notification_hub.client import NetworkError
from notification_hub.client.models import ClientSnapshot, EventPage, HubEvent, parse_notification
from notification_hub.domain import DomainSummary
from notification_hub.gui.controller import GuiController


class StubClient:
    _random = staticmethod(lambda: 0.5)


class OfflineClient(StubClient):
    def get_snapshot(self):
        raise NetworkError("connection refused")


def _notification(*, version: int = 1):
    return parse_notification(
        {
            "id": str(uuid.uuid4()),
            "domain": "builds",
            "sender": "tests",
            "summary": "Build complete",
            "message": "All checks passed",
            "details": None,
            "tags": ["ci"],
            "priority": "normal",
            "source_created_at": None,
            "created_at": "2026-01-01T00:00:00.000Z",
            "updated_at": "2026-01-01T00:00:00.000Z",
            "read_at": None,
            "response_state": "pending",
            "response_options": [
                {
                    "id": "accept",
                    "label": "Accept",
                    "message_mode": "optional",
                    "appearance": "primary",
                }
            ],
            "response": None,
            "version": version,
        }
    )


def _snapshot(notification, sequence: int = 4) -> ClientSnapshot:
    domain = DomainSummary("builds", "2026-01-01T00:00:00.000Z", 1, 1, 1, notification.summary)
    return ClientSnapshot(sequence, "2026-01-01T00:00:01.000Z", (domain,), (notification,))


def test_initial_state_and_later_update_close_subscription_race() -> None:
    controller = GuiController(StubClient(), update_wait_seconds=0)  # type: ignore[arg-type]
    notification = _notification()
    controller._replace(_snapshot(notification))  # noqa: SLF001

    initial = controller.get_initial_state()
    controller._set_connection("connected")  # noqa: SLF001
    updates = controller.get_updates(initial["revision"])

    assert initial["snapshot"]["notifications"][0]["id"] == notification.id
    assert updates == {
        "revision": initial["revision"] + 1,
        "updates": [{"kind": "connection", "connection": {"state": "connected", "message": None}}],
    }


def test_initial_network_failure_is_published_to_the_frontend() -> None:
    controller = GuiController(OfflineClient(), update_wait_seconds=1)  # type: ignore[arg-type]
    controller.start()
    try:
        result = controller.get_updates(0)
    finally:
        controller.stop()

    assert result == {
        "revision": 1,
        "updates": [
            {
                "kind": "connection",
                "connection": {
                    "state": "reconnecting",
                    "message": "The server cannot be reached; retrying.",
                },
            }
        ],
    }


def test_log_overflow_returns_atomic_current_replacement() -> None:
    controller = GuiController(
        StubClient(),
        update_log_size=2,
        update_wait_seconds=0,  # type: ignore[arg-type]
    )
    notification = _notification()
    controller._replace(_snapshot(notification))  # noqa: SLF001
    controller._set_connection("connected")  # noqa: SLF001
    controller._set_connection("offline", "retrying")  # noqa: SLF001

    result = controller.get_updates(0)

    assert result["revision"] == 3
    assert result["updates"][0] == {
        "kind": "connection",
        "connection": {"state": "offline", "message": "retrying"},
    }
    assert result["updates"][1]["kind"] == "reset"
    assert result["updates"][1]["snapshot"]["sequence"] == 4


def test_event_page_is_published_with_rebuilt_domains() -> None:
    controller = GuiController(StubClient(), update_wait_seconds=0)  # type: ignore[arg-type]
    notification = _notification()
    controller._replace(_snapshot(notification))  # noqa: SLF001
    initial_revision = controller.get_initial_state()["revision"]
    updated_value = notification.to_dict()
    updated_value.update(
        summary="A newer result",
        updated_at="2026-01-01T00:00:02.000Z",
        version=2,
    )
    updated = parse_notification(updated_value)
    event = HubEvent(
        5,
        "notification.updated",
        "2026-01-01T00:00:02.000Z",
        notification=updated,
    )

    controller._apply_page(EventPage((event,), 5, False))  # noqa: SLF001
    result = controller.get_updates(initial_revision)

    assert result["updates"][0]["events"][0]["notification"]["version"] == 2
    assert result["updates"][0]["domains"][0]["latest_summary"] == "A newer result"
