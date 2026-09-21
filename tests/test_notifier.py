from __future__ import annotations

import json
import uuid
from io import StringIO

import pytest

from notification_hub.config import NotifierConfig, RemoteServerConfig
from notification_hub.notifier import NetworkError, NotifierClient, Outcome, ServerError
from notification_hub.notifier.cli import _default_command, _parser, run


def server_config() -> RemoteServerConfig:
    return RemoteServerConfig("http://hub.example", request_timeout_seconds=5)


def test_create_retries_with_unchanged_payload() -> None:
    payload = {
        "id": str(uuid.uuid4()),
        "domain": "tests",
        "sender": "pytest",
        "summary": "Retry me",
    }
    bodies: list[bytes | None] = []
    sleeps: list[float] = []

    def transport(method: str, url: str, body: bytes | None, timeout: float):
        bodies.append(body)
        if len(bodies) == 1:
            raise NetworkError("connection reset after write")
        return 201, json.dumps({"notification": payload, "event_seq": 1}).encode()

    client = NotifierClient(server_config(), transport=transport, sleep=sleeps.append)
    result = client.create(payload)

    assert result["notification"]["id"] == payload["id"]
    assert bodies[0] == bodies[1]
    assert json.loads(bodies[0]) == payload
    assert sleeps == [0.25]


def test_create_does_not_retry_validation_error() -> None:
    calls = 0

    def transport(method: str, url: str, body: bytes | None, timeout: float):
        nonlocal calls
        calls += 1
        return 422, b'{"error":{"code":"invalid_data","message":"bad","details":{}}}'

    client = NotifierClient(server_config(), transport=transport, sleep=lambda _: None)
    with pytest.raises(ServerError, match="bad") as caught:
        client.create({"id": "bad"})
    assert caught.value.code == "invalid_data"
    assert calls == 1


def test_wait_long_polls_until_terminal() -> None:
    notification_id = str(uuid.uuid4())
    responses = [
        {"notification_id": notification_id, "state": "pending", "response": None},
        {
            "notification_id": notification_id,
            "state": "answered",
            "response": {
                "request_id": str(uuid.uuid4()),
                "option_id": "approve",
                "message": None,
                "responded_at": "2026-01-01T00:00:00.000Z",
                "responded_by": "desktop-ui",
            },
        },
    ]
    urls: list[str] = []

    def transport(method: str, url: str, body: bytes | None, timeout: float):
        urls.append(url)
        return 200, json.dumps(responses.pop(0)).encode()

    outcome = NotifierClient(server_config(), transport=transport).wait(notification_id)

    assert outcome.state == "answered"
    assert outcome.response is not None
    assert outcome.response["option_id"] == "approve"
    assert all("wait_seconds=4" in url for url in urls)


def test_poll_rejects_malformed_success_response() -> None:
    def transport(method: str, url: str, body: bytes | None, timeout: float):
        return 200, b'{"state":"surprising"}'

    with pytest.raises(NetworkError, match="malformed outcome"):
        NotifierClient(server_config(), transport=transport).poll(str(uuid.uuid4()))


def test_poll_accepts_informational_notification_outcome() -> None:
    notification_id = str(uuid.uuid4())

    def transport(method: str, url: str, body: bytes | None, timeout: float):
        value = {"notification_id": notification_id, "state": "not_requested", "response": None}
        return 200, json.dumps(value).encode()

    outcome = NotifierClient(server_config(), transport=transport).poll(notification_id)
    assert outcome.state == "not_requested"


class FakeClient:
    def __init__(self, outcome: Outcome | None = None) -> None:
        self.created: dict[str, object] | None = None
        self.outcome = outcome

    def create(self, notification):
        self.created = notification
        return {"notification": notification, "event_seq": 1}

    def wait(self, notification_id: str, *, timeout: float | None = None):
        assert self.outcome is not None
        return self.outcome


def test_send_is_default_and_builds_response_options() -> None:
    args = _parser().parse_args(
        _default_command(
            [
                "Run deployment",
                "--domain",
                "deploy",
                "--response-option",
                "go",
                "Proceed",
                "required",
                "primary",
            ]
        )
    )
    client = FakeClient()
    stdout = StringIO()
    status = run(
        args,
        NotifierConfig(server_config()),
        stdout=stdout,
        stderr=StringIO(),
        client=client,  # type: ignore[arg-type]
    )

    assert status == 0
    assert stdout.getvalue().strip() == client.created["id"]
    assert client.created["response_options"] == [
        {
            "id": "go",
            "label": "Proceed",
            "message_mode": "required",
            "appearance": "primary",
        }
    ]


def test_approve_denial_is_fail_closed() -> None:
    notification_id = str(uuid.uuid4())
    outcome = Outcome(
        notification_id,
        "answered",
        {"option_id": "deny", "message": "unsafe"},
    )
    args = _parser().parse_args(["approve", "Deploy?", "--domain", "deploy"])
    client = FakeClient(outcome)
    stdout = StringIO()
    status = run(
        args,
        NotifierConfig(server_config()),
        stdout=stdout,
        stderr=StringIO(),
        client=client,  # type: ignore[arg-type]
    )

    assert status == 2
    assert stdout.getvalue() == "unsafe\n"
    assert [item["id"] for item in client.created["response_options"]] == ["approve", "deny"]
