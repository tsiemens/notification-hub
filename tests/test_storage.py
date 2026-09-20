from __future__ import annotations

import tempfile
import unittest
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from notification_hub.config import RetentionConfig
from notification_hub.domain import CreateNotification, MessageMode, ResponseOption, ResponseState
from notification_hub.storage import (
    AlreadyAnsweredError,
    Database,
    DatabaseSecurityError,
    IdempotencyConflictError,
    NotificationRepository,
    PendingLimitError,
    RateLimitError,
)

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


class RepositoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database = Database(Path(self.temporary_directory.name) / "hub.sqlite3")
        self.database.initialize()
        self.repository = NotificationRepository(self.database)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def request(
        self, *, options: bool = False, summary: str = "Build complete"
    ) -> CreateNotification:
        response_options = (
            (
                ResponseOption("approve", "Approve"),
                ResponseOption("deny", "Deny", MessageMode.REQUIRED),
            )
            if options
            else ()
        )
        return CreateNotification(
            str(uuid.uuid4()),
            "build-container",
            "codex",
            summary,
            tags=("workspace:hub",),
            response_options=response_options,
        )

    def test_migration_and_connection_pragmas(self) -> None:
        with self.database.connection() as connection:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            self.assertEqual(connection.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(
                connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0], 1
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(notifications)")
            }
            self.assertNotIn("creator_principal", columns)
        self.assertEqual(self.database.path.stat().st_mode & 0o777, 0o600)

    def test_database_rejects_a_symlink_target(self) -> None:
        real_path = Path(self.temporary_directory.name) / "real.sqlite3"
        link_path = Path(self.temporary_directory.name) / "linked.sqlite3"
        real_path.touch(mode=0o600)
        link_path.symlink_to(real_path)
        with self.assertRaises(DatabaseSecurityError):
            Database(link_path).initialize()

    def test_create_is_idempotent_and_event_is_atomic(self) -> None:
        request = self.request(options=True)
        first = self.repository.create(request, now=NOW)
        retry = self.repository.create(request, now=NOW + timedelta(seconds=1))
        self.assertTrue(first.changed)
        self.assertFalse(retry.changed)
        self.assertEqual(first.event_seq, retry.event_seq)
        self.assertEqual(len(self.repository.events()), 1)
        self.assertEqual(retry.notification.response_state, ResponseState.PENDING)

        changed_request = CreateNotification(
            request.id,
            request.domain,
            request.sender,
            "Different",
            response_options=request.response_options,
        )
        with self.assertRaises(IdempotencyConflictError):
            self.repository.create(changed_request, now=NOW)

    def test_create_limits_are_atomic_and_do_not_block_exact_retries(self) -> None:
        pending = self.request(options=True)
        self.repository.create(
            pending, now=NOW, max_creates_per_minute=10, max_pending_total=1
        )
        retry = self.repository.create(
            pending, now=NOW, max_creates_per_minute=1, max_pending_total=1
        )
        self.assertFalse(retry.changed)

        with self.assertRaises(PendingLimitError):
            self.repository.create(
                self.request(options=True),
                now=NOW,
                max_creates_per_minute=10,
                max_pending_total=1,
            )
        with self.assertRaises(RateLimitError):
            self.repository.create(
                self.request(),
                now=NOW,
                max_creates_per_minute=1,
                max_pending_total=1,
            )

    def test_response_is_terminal_and_message_mode_is_enforced(self) -> None:
        created = self.repository.create(self.request(options=True), now=NOW)
        notification_id = created.notification.id
        with self.assertRaises(ValueError):
            self.repository.respond(
                notification_id, str(uuid.uuid4()), "deny", None, "desktop", now=NOW
            )
        request_id = str(uuid.uuid4())
        result = self.repository.respond(
            notification_id, request_id, "approve", None, "desktop", now=NOW
        )
        self.assertEqual(result.notification.response_state, ResponseState.ANSWERED)
        retry = self.repository.respond(
            notification_id, request_id, "approve", None, "desktop", now=NOW
        )
        self.assertFalse(retry.changed)
        with self.assertRaises(AlreadyAnsweredError):
            self.repository.respond(
                notification_id, str(uuid.uuid4()), "approve", None, "desktop", now=NOW
            )

    def test_cancel_is_idempotent_without_producer_ownership(self) -> None:
        created = self.repository.create(self.request(options=True), now=NOW)
        result = self.repository.cancel(created.notification.id, "obsolete", now=NOW)
        self.assertEqual(result.notification.response_state, ResponseState.CANCELLED)
        self.assertFalse(
            self.repository.cancel(created.notification.id, "obsolete", now=NOW).changed
        )
        with self.assertRaises(IdempotencyConflictError):
            self.repository.cancel(created.notification.id, "different", now=NOW)

    def test_read_state_does_not_change_domain_activity(self) -> None:
        created = self.repository.create(self.request(), now=NOW)
        activity = self.repository.domains()[0].last_activity_at
        notifications, seq = self.repository.set_read_state(
            [created.notification.id], True, now=NOW + timedelta(minutes=1)
        )
        self.assertIsNotNone(notifications[0].read_at)
        self.assertIsNotNone(seq)
        self.assertEqual(self.repository.domains()[0].last_activity_at, activity)
        _, repeated_seq = self.repository.set_read_state(
            [created.notification.id], True, now=NOW + timedelta(minutes=2)
        )
        self.assertIsNone(repeated_seq)

    def test_domain_summaries(self) -> None:
        self.repository.create(self.request(summary="First"), now=NOW)
        self.repository.create(
            self.request(options=True, summary="Latest"), now=NOW + timedelta(seconds=1)
        )
        domain = self.repository.domains()[0]
        self.assertEqual(domain.notification_count, 2)
        self.assertEqual(domain.unread_count, 2)
        self.assertEqual(domain.pending_response_count, 1)
        self.assertEqual(domain.latest_summary, "Latest")

    def test_cleanup_expires_then_later_prunes_pending_notifications(self) -> None:
        pending = self.repository.create(
            self.request(options=True), now=NOW - timedelta(days=8)
        )
        first = self.repository.cleanup(RetentionConfig(), now=NOW)
        self.assertEqual(first.expired, 1)
        self.assertEqual(first.deleted_notifications, 0)
        self.assertEqual(
            self.repository.get(pending.notification.id).response_state, ResponseState.EXPIRED
        )
        second = self.repository.cleanup(RetentionConfig(), now=NOW + timedelta(days=8))
        self.assertEqual(second.deleted_notifications, 1)
        self.assertEqual(second.deleted_domains, 1)
        self.assertEqual(
            [event["type"] for event in self.repository.events()][-2:],
            ["notification.deleted", "domain.deleted"],
        )

    def test_cleanup_prunes_old_informational_history(self) -> None:
        self.repository.create(self.request(), now=NOW - timedelta(days=8))
        result = self.repository.cleanup(RetentionConfig(), now=NOW)
        self.assertEqual(result.deleted_notifications, 1)
        self.assertEqual(self.repository.list_notifications(), [])


if __name__ == "__main__":
    unittest.main()
