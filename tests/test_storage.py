from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from notification_hub.config import RetentionConfig
from notification_hub.domain import CreateNotification, MessageMode, ResponseOption, ResponseState
from notification_hub.storage import (
    AlreadyAnsweredError,
    CursorExpiredError,
    Database,
    DatabaseSecurityError,
    IdempotencyConflictError,
    NotificationQuery,
    NotificationRepository,
    PendingLimitError,
    RateLimitError,
)

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    instance = Database(tmp_path / "hub.sqlite3")
    instance.initialize()
    return instance


@pytest.fixture
def repository(database: Database) -> NotificationRepository:
    return NotificationRepository(database)


def request(*, options: bool = False, summary: str = "Build complete") -> CreateNotification:
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


def test_migration_and_connection_pragmas(database: Database) -> None:
    with database.connection() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 1
        columns = {row["name"] for row in connection.execute("PRAGMA table_info(notifications)")}
        assert "creator_principal" not in columns
    assert database.path.stat().st_mode & 0o777 == 0o600


def test_database_rejects_a_symlink_target(tmp_path: Path) -> None:
    real_path = tmp_path / "real.sqlite3"
    link_path = tmp_path / "linked.sqlite3"
    real_path.touch(mode=0o600)
    link_path.symlink_to(real_path)
    with pytest.raises(DatabaseSecurityError):
        Database(link_path).initialize()


def test_create_is_idempotent_and_event_is_atomic(repository: NotificationRepository) -> None:
    notification = request(options=True)
    first = repository.create(notification, now=NOW)
    retry = repository.create(notification, now=NOW + timedelta(seconds=1))
    assert first.changed
    assert not retry.changed
    assert first.event_seq == retry.event_seq
    assert len(repository.events()) == 1
    assert retry.notification.response_state == ResponseState.PENDING

    changed_request = CreateNotification(
        notification.id,
        notification.domain,
        notification.sender,
        "Different",
        response_options=notification.response_options,
    )
    with pytest.raises(IdempotencyConflictError):
        repository.create(changed_request, now=NOW)


def test_create_limits_are_atomic_and_do_not_block_exact_retries(
    repository: NotificationRepository,
) -> None:
    pending = request(options=True)
    repository.create(pending, now=NOW, max_creates_per_minute=10, max_pending_total=1)
    retry = repository.create(pending, now=NOW, max_creates_per_minute=1, max_pending_total=1)
    assert not retry.changed

    with pytest.raises(PendingLimitError):
        repository.create(
            request(options=True),
            now=NOW,
            max_creates_per_minute=10,
            max_pending_total=1,
        )
    with pytest.raises(RateLimitError):
        repository.create(request(), now=NOW, max_creates_per_minute=1, max_pending_total=1)


def test_response_is_terminal_and_message_mode_is_enforced(
    repository: NotificationRepository,
) -> None:
    created = repository.create(request(options=True), now=NOW)
    notification_id = created.notification.id
    with pytest.raises(ValueError):
        repository.respond(notification_id, str(uuid.uuid4()), "deny", None, "desktop", now=NOW)
    request_id = str(uuid.uuid4())
    result = repository.respond(notification_id, request_id, "approve", None, "desktop", now=NOW)
    assert result.notification.response_state == ResponseState.ANSWERED
    retry = repository.respond(notification_id, request_id, "approve", None, "desktop", now=NOW)
    assert not retry.changed
    with pytest.raises(AlreadyAnsweredError):
        repository.respond(notification_id, str(uuid.uuid4()), "approve", None, "desktop", now=NOW)


def test_cancel_is_idempotent_without_producer_ownership(
    repository: NotificationRepository,
) -> None:
    created = repository.create(request(options=True), now=NOW)
    result = repository.cancel(created.notification.id, "obsolete", now=NOW)
    assert result.notification.response_state == ResponseState.CANCELLED
    assert not repository.cancel(created.notification.id, "obsolete", now=NOW).changed
    with pytest.raises(IdempotencyConflictError):
        repository.cancel(created.notification.id, "different", now=NOW)


def test_read_state_does_not_change_domain_activity(repository: NotificationRepository) -> None:
    created = repository.create(request(), now=NOW)
    activity = repository.domains()[0].last_activity_at
    notifications, seq = repository.set_read_state(
        [created.notification.id], True, now=NOW + timedelta(minutes=1)
    )
    assert notifications[0].read_at is not None
    assert seq is not None
    assert repository.domains()[0].last_activity_at == activity
    _, repeated_seq = repository.set_read_state(
        [created.notification.id], True, now=NOW + timedelta(minutes=2)
    )
    assert repeated_seq is None


def test_domain_summaries(repository: NotificationRepository) -> None:
    repository.create(request(summary="First"), now=NOW)
    repository.create(request(options=True, summary="Latest"), now=NOW + timedelta(seconds=1))
    domain = repository.domains()[0]
    assert domain.notification_count == 2
    assert domain.unread_count == 2
    assert domain.pending_response_count == 1
    assert domain.latest_summary == "Latest"


def test_cleanup_expires_then_later_prunes_pending_notifications(
    repository: NotificationRepository,
) -> None:
    pending = repository.create(request(options=True), now=NOW - timedelta(days=8))
    first = repository.cleanup(RetentionConfig(), now=NOW)
    assert first.expired == 1
    assert first.deleted_notifications == 0
    assert repository.get(pending.notification.id).response_state == ResponseState.EXPIRED
    second = repository.cleanup(RetentionConfig(), now=NOW + timedelta(days=8))
    assert second.deleted_notifications == 1
    assert second.deleted_domains == 1
    assert [event["type"] for event in repository.events()][-2:] == [
        "notification.deleted",
        "domain.deleted",
    ]


def test_cleanup_prunes_old_informational_history(repository: NotificationRepository) -> None:
    repository.create(request(), now=NOW - timedelta(days=8))
    result = repository.cleanup(RetentionConfig(), now=NOW)
    assert result.deleted_notifications == 1
    assert repository.list_notifications() == []


def test_query_notifications_uses_bound_stable_cursors(
    repository: NotificationRepository,
) -> None:
    first = repository.create(request(summary="First"), now=NOW)
    second = repository.create(request(summary="Second"), now=NOW)
    third_request = request(summary="Third")
    third_request = CreateNotification(
        third_request.id,
        "other-domain",
        "other-sender",
        third_request.summary,
        tags=("workspace:hub", "extra"),
    )
    third = repository.create(third_request, now=NOW + timedelta(seconds=1))

    query = NotificationQuery(order="asc", limit=2)
    page = repository.query_notifications(query)
    assert [item.id for item in page.items] == sorted(
        [first.notification.id, second.notification.id]
    )
    assert page.next_cursor is not None
    next_page = repository.query_notifications(
        NotificationQuery(order="asc", limit=2, cursor=page.next_cursor)
    )
    assert [item.id for item in next_page.items] == [third.notification.id]

    filtered = repository.query_notifications(
        NotificationQuery(
            domains=("other-domain",),
            senders=("other-sender",),
            tags=("workspace:hub", "extra"),
            unread=True,
            response_states=(ResponseState.NOT_REQUESTED,),
        )
    )
    assert [item.id for item in filtered.items] == [third.notification.id]
    with pytest.raises(ValueError, match="cursor is invalid"):
        repository.query_notifications(
            NotificationQuery(order="desc", limit=2, cursor=page.next_cursor)
        )


def test_keyset_pagination_stays_stable_when_rows_before_cursor_change(
    repository: NotificationRepository, database: Database
) -> None:
    initial = [
        repository.create(request(summary=f"Initial {index}"), now=NOW + timedelta(seconds=index))
        for index in range(4)
    ]
    first_page = repository.query_notifications(NotificationQuery(order="asc", limit=2))
    assert [item.id for item in first_page.items] == [
        initial[0].notification.id,
        initial[1].notification.id,
    ]
    assert first_page.next_cursor is not None

    inserted_before_cursor = repository.create(
        request(summary="Concurrent insertion"), now=NOW + timedelta(milliseconds=500)
    )
    with database.connection() as connection:
        connection.execute("DELETE FROM notifications WHERE id = ?", (initial[0].notification.id,))

    second_page = repository.query_notifications(
        NotificationQuery(order="asc", limit=2, cursor=first_page.next_cursor)
    )
    assert [item.id for item in second_page.items] == [
        initial[2].notification.id,
        initial[3].notification.id,
    ]
    assert inserted_before_cursor.notification.id not in {
        item.id for item in first_page.items + second_page.items
    }


def test_snapshot_and_event_pages_preserve_sequence_semantics(
    repository: NotificationRepository, database: Database
) -> None:
    first = repository.create(request(), now=NOW)
    second = repository.create(request(), now=NOW + timedelta(seconds=1))
    snapshot = repository.snapshot(now=NOW + timedelta(seconds=2))
    assert snapshot.sequence == second.event_seq
    assert [item.id for item in snapshot.notifications] == [
        second.notification.id,
        first.notification.id,
    ]

    page = repository.event_page(0, 1)
    assert [event["seq"] for event in page.events] == [first.event_seq]
    assert page.last_sequence == first.event_seq
    assert page.has_more
    final_page = repository.event_page(page.last_sequence, 10)
    assert [event["seq"] for event in final_page.events] == [second.event_seq]
    assert not final_page.has_more

    with database.connection() as connection:
        connection.execute("DELETE FROM events WHERE seq = ?", (first.event_seq,))
    with pytest.raises(CursorExpiredError):
        repository.event_page(0, 10)
