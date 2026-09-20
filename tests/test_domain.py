from __future__ import annotations

import unittest
import uuid
from datetime import UTC, datetime

from notification_hub.domain import (
    Appearance,
    CreateNotification,
    MessageMode,
    ResponseOption,
    ValidationError,
    create_fingerprint,
    format_timestamp,
)


class DomainModelTests(unittest.TestCase):
    def request(self, **changes: object) -> CreateNotification:
        values = {
            "id": str(uuid.uuid4()),
            "domain": "build-container",
            "sender": "codex",
            "summary": "Approval required",
            "tags": ("task:test",),
        }
        values.update(changes)
        return CreateNotification(**values)  # type: ignore[arg-type]

    def test_initial_state_tracks_options(self) -> None:
        self.assertEqual(self.request().initial_state, "not_requested")
        option = ResponseOption("approve", "Approve", MessageMode.NONE, Appearance.PRIMARY)
        self.assertEqual(self.request(response_options=(option,)).initial_state, "pending")

    def test_rejects_invalid_public_fields(self) -> None:
        invalid = (
            {"id": str(uuid.uuid1())},
            {"summary": "two\nlines"},
            {"message": "x" * (32 * 1024 + 1)},
            {"tags": ("same", "same")},
            {"source_created_at": "not-a-time"},
        )
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.request(**changes)

    def test_response_message_modes(self) -> None:
        ResponseOption("optional", "Optional", MessageMode.OPTIONAL).validate_message(None)
        with self.assertRaises(ValidationError):
            ResponseOption("required", "Required", MessageMode.REQUIRED).validate_message("")
        with self.assertRaises(ValidationError):
            ResponseOption("none", "None", MessageMode.NONE).validate_message("unexpected")

    def test_fingerprint_is_stable_and_depends_only_on_request_content(self) -> None:
        request = self.request()
        self.assertEqual(create_fingerprint(request), create_fingerprint(request))
        self.assertNotEqual(create_fingerprint(request), create_fingerprint(self.request()))

    def test_timestamp_is_canonical_utc(self) -> None:
        self.assertEqual(
            format_timestamp(datetime(2026, 9, 19, 12, 34, 56, 123999, tzinfo=UTC)),
            "2026-09-19T12:34:56.123Z",
        )


if __name__ == "__main__":
    unittest.main()
