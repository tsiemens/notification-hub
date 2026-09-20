from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from notification_hub.domain import (
    Appearance,
    CreateNotification,
    MessageMode,
    ResponseOption,
    ValidationError,
    create_fingerprint,
    format_timestamp,
)


def request(**changes: object) -> CreateNotification:
    values = {
        "id": str(uuid.uuid4()),
        "domain": "build-container",
        "sender": "codex",
        "summary": "Approval required",
        "tags": ("task:test",),
    }
    values.update(changes)
    return CreateNotification(**values)  # type: ignore[arg-type]


def test_initial_state_tracks_options() -> None:
    assert request().initial_state == "not_requested"
    option = ResponseOption("approve", "Approve", MessageMode.NONE, Appearance.PRIMARY)
    assert request(response_options=(option,)).initial_state == "pending"


@pytest.mark.parametrize(
    "changes",
    (
        {"id": str(uuid.uuid1())},
        {"summary": "two\nlines"},
        {"message": "x" * (32 * 1024 + 1)},
        {"tags": ("same", "same")},
        {"source_created_at": "not-a-time"},
    ),
)
def test_rejects_invalid_public_fields(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        request(**changes)


def test_response_message_modes() -> None:
    ResponseOption("optional", "Optional", MessageMode.OPTIONAL).validate_message(None)
    with pytest.raises(ValidationError):
        ResponseOption("required", "Required", MessageMode.REQUIRED).validate_message("")
    with pytest.raises(ValidationError):
        ResponseOption("none", "None", MessageMode.NONE).validate_message("unexpected")


def test_fingerprint_is_stable_and_depends_only_on_request_content() -> None:
    notification = request()
    assert create_fingerprint(notification) == create_fingerprint(notification)
    assert create_fingerprint(notification) != create_fingerprint(request())


def test_timestamp_is_canonical_utc() -> None:
    assert format_timestamp(datetime(2026, 9, 19, 12, 34, 56, 123999, tzinfo=UTC)) == (
        "2026-09-19T12:34:56.123Z"
    )
