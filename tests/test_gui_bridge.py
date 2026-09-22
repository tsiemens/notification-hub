from __future__ import annotations

import uuid

from notification_hub.client import NetworkError, ServerError
from notification_hub.client.models import MutationResult, parse_notification
from notification_hub.gui.bridge import GuiBridge
from notification_hub.gui.settings import ClientSettingsStore, SettingsWriteError


def _notification():
    return parse_notification(
        {
            "id": str(uuid.uuid4()),
            "domain": "ops",
            "sender": "tests",
            "summary": "Choose",
            "message": "Choose a target",
            "details": None,
            "tags": [],
            "priority": "normal",
            "source_created_at": None,
            "created_at": "2026-01-01T00:00:00.000Z",
            "updated_at": "2026-01-01T00:00:00.000Z",
            "read_at": None,
            "response_state": "pending",
            "response_options": [
                {
                    "id": "deploy",
                    "label": "Deploy",
                    "message_mode": "required",
                    "appearance": "primary",
                }
            ],
            "response": None,
            "version": 1,
        }
    )


class StubController:
    def __init__(self) -> None:
        self.item = _notification()
        self.read_calls = []
        self.response_calls = []
        self.error: Exception | None = None

    def get_initial_state(self):
        return {
            "revision": 0,
            "connection": {"state": "starting", "message": None},
            "snapshot": None,
        }

    def get_updates(self, revision):
        return {"revision": revision, "updates": []}

    def notification(self, notification_id):
        return self.item if notification_id == self.item.id else None

    def set_read_state(self, ids, read):
        if self.error:
            raise self.error
        self.read_calls.append((ids, read))
        return MutationResult((self.item,), 3)

    def respond(self, notification_id, option_id, message):
        if self.error:
            raise self.error
        self.response_calls.append((notification_id, option_id, message))
        return MutationResult((self.item,), 4, True)


def test_read_state_arguments_are_strictly_validated() -> None:
    controller = StubController()
    bridge = GuiBridge(controller)  # type: ignore[arg-type]

    assert (
        bridge.set_read_state([controller.item.id, controller.item.id], True)["error"]["code"]
        == "invalid_request"
    )
    assert bridge.set_read_state([controller.item.id], 1)["ok"] is False
    result = bridge.set_read_state([controller.item.id], False)

    assert result["ok"] is True
    assert controller.read_calls == [([controller.item.id], False)]


def test_response_message_mode_is_validated_before_mutation() -> None:
    controller = StubController()
    bridge = GuiBridge(controller)  # type: ignore[arg-type]

    rejected = bridge.respond(controller.item.id, "deploy", "")
    accepted = bridge.respond(controller.item.id, "deploy", "production")

    assert rejected["error"]["code"] == "invalid_request"
    assert accepted["ok"] is True
    assert controller.response_calls == [(controller.item.id, "deploy", "production")]


def test_expected_failures_use_safe_structured_envelopes() -> None:
    controller = StubController()
    bridge = GuiBridge(controller)  # type: ignore[arg-type]
    controller.error = NetworkError("secret transport diagnostic")
    offline = bridge.set_read_state([controller.item.id], True)
    conflict = ServerError(409, "already_answered", "secret server message")
    conflict.notification = controller.item
    controller.error = conflict

    answered = bridge.respond(controller.item.id, "deploy", "production")

    assert offline["error"] == {
        "code": "offline",
        "message": "The server is unavailable. Try again later.",
        "retryable": True,
    }
    assert answered["error"]["code"] == "already_answered"
    assert answered["error"]["notification"]["id"] == controller.item.id


def test_external_urls_are_allowlisted_and_opened_outside_webview() -> None:
    opened = []
    bridge = GuiBridge(StubController(), external_opener=lambda url: opened.append(url))  # type: ignore[arg-type]

    assert bridge.open_external("javascript:alert(1)")["ok"] is False
    assert bridge.open_external("https://example.test/path")["ok"] is True
    assert opened == ["https://example.test/path"]


def test_settings_bridge_returns_only_presentation_values_and_safe_errors(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "client.toml"
    path.write_text(
        """[server]
url = "https://hub.example"
[auth]
key_id = "secret-key-id"
private_key_file = "/secret/private.pem"
""",
        encoding="utf-8",
    )
    store = ClientSettingsStore(path)
    bridge = GuiBridge(StubController(), settings_store=store)  # type: ignore[arg-type]

    loaded = bridge.get_settings()
    assert loaded["ok"] is True
    assert "secret" not in str(loaded)
    rejected = bridge.update_settings({"theme": "dark"})
    assert rejected["error"]["code"] == "invalid_settings"
    assert "secret" not in str(rejected)

    def fail_update(_value):
        raise SettingsWriteError("secret /private/path diagnostic")

    monkeypatch.setattr(store, "update", fail_update)
    write_error = bridge.update_settings(loaded["settings"])
    assert write_error["error"]["code"] == "settings_write_failed"
    assert "secret" not in str(write_error)
