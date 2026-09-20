from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from notification_hub.config import RetentionConfig
from notification_hub.domain import (
    Appearance,
    CreateNotification,
    DomainSummary,
    MessageMode,
    Notification,
    Priority,
    Response,
    ResponseOption,
    ResponseState,
    ValidationError,
    create_fingerprint,
    format_timestamp,
)
from notification_hub.storage.database import Database


class RepositoryError(RuntimeError):
    pass


class NotFoundError(RepositoryError):
    pass


class IdempotencyConflictError(RepositoryError):
    pass


class StateConflictError(RepositoryError):
    pass


class RateLimitError(RepositoryError):
    pass


class PendingLimitError(RateLimitError):
    pass


class AlreadyAnsweredError(StateConflictError):
    def __init__(self, notification: Notification) -> None:
        super().__init__("the notification already has a response")
        self.notification = notification


@dataclass(frozen=True, slots=True)
class MutationResult:
    notification: Notification
    event_seq: int
    changed: bool


@dataclass(frozen=True, slots=True)
class CleanupResult:
    expired: int = 0
    deleted_notifications: int = 0
    deleted_domains: int = 0
    deleted_events: int = 0
    deleted_nonce_batches: int = 0


class NotificationRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def create(
        self,
        request: CreateNotification,
        *,
        now: datetime | None = None,
        max_creates_per_minute: int | None = None,
        max_pending_total: int | None = None,
    ) -> MutationResult:
        current_time = now or datetime.now(UTC)
        timestamp = format_timestamp(current_time)
        fingerprint = create_fingerprint(request)
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                prior = connection.execute(
                    "SELECT create_fingerprint FROM notifications WHERE id = ?", (request.id,)
                ).fetchone()
                if prior is not None:
                    if prior["create_fingerprint"] != fingerprint:
                        raise IdempotencyConflictError(
                            "notification id was reused with different content"
                        )
                    notification = self._get(connection, request.id)
                    event_seq = self._latest_entity_event(connection, request.id)
                    connection.commit()
                    return MutationResult(notification, event_seq, False)

                if max_creates_per_minute is not None:
                    cutoff = format_timestamp(current_time - timedelta(minutes=1))
                    recent_creates = connection.execute(
                        "SELECT count(*) FROM notifications WHERE created_at > ?", (cutoff,)
                    ).fetchone()[0]
                    if recent_creates >= max_creates_per_minute:
                        raise RateLimitError("notification create rate limit exceeded")
                if max_pending_total is not None and request.response_options:
                    pending = connection.execute(
                        "SELECT count(*) FROM notifications WHERE response_state = 'pending'"
                    ).fetchone()[0]
                    if pending >= max_pending_total:
                        raise PendingLimitError("pending notification limit exceeded")

                connection.execute(
                    "INSERT INTO domains(name, created_at, last_activity_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET last_activity_at = "
                    "max(domains.last_activity_at, excluded.last_activity_at)",
                    (request.domain, timestamp, timestamp),
                )
                domain_id = connection.execute(
                    "SELECT id FROM domains WHERE name = ?", (request.domain,)
                ).fetchone()["id"]
                connection.execute(
                    """INSERT INTO notifications(
                        id, domain_id, sender, summary, message_markdown, details_markdown,
                        tags_json, priority, source_created_at, created_at, updated_at, read_at,
                        response_state, cancelled_at, cancellation_reason, version,
                        create_fingerprint
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, NULL, 1, ?)""",
                    (
                        request.id,
                        domain_id,
                        request.sender,
                        request.summary,
                        request.message,
                        request.details,
                        json.dumps(request.tags, ensure_ascii=False, separators=(",", ":")),
                        request.priority.value,
                        request.source_created_at,
                        timestamp,
                        timestamp,
                        request.initial_state.value,
                        fingerprint,
                    ),
                )
                connection.executemany(
                    "INSERT INTO response_options("
                    "notification_id, position, option_id, label, message_mode, appearance"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            request.id,
                            position,
                            option.id,
                            option.label,
                            option.message_mode.value,
                            option.appearance.value,
                        )
                        for position, option in enumerate(request.response_options)
                    ],
                )
                notification = self._get(connection, request.id)
                seq = self._event(
                    connection,
                    "notification.created",
                    request.id,
                    timestamp,
                    notification.to_dict(),
                )
                connection.commit()
                return MutationResult(notification, seq, True)
            except Exception:
                connection.rollback()
                raise

    def get(self, notification_id: str) -> Notification:
        with self.database.connection() as connection:
            return self._get(connection, notification_id)

    def list_notifications(self) -> list[Notification]:
        with self.database.connection() as connection:
            ids = connection.execute(
                "SELECT id FROM notifications ORDER BY created_at, id"
            ).fetchall()
            return [self._get(connection, row["id"]) for row in ids]

    def domains(self) -> list[DomainSummary]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT d.name, d.last_activity_at,
                       count(n.id) AS notification_count,
                       sum(CASE WHEN n.read_at IS NULL THEN 1 ELSE 0 END) AS unread_count,
                       sum(CASE WHEN n.response_state = 'pending' THEN 1 ELSE 0 END)
                           AS pending_response_count,
                       (SELECT newest.summary FROM notifications newest
                        WHERE newest.domain_id = d.id
                        ORDER BY newest.created_at DESC, newest.id DESC LIMIT 1) AS latest_summary
                   FROM domains d JOIN notifications n ON n.domain_id = d.id
                   GROUP BY d.id
                   ORDER BY d.last_activity_at DESC, d.name ASC"""
            ).fetchall()
            return [DomainSummary(**dict(row)) for row in rows]

    def respond(
        self,
        notification_id: str,
        request_id: str,
        option_id: str,
        message: str | None,
        responder_principal: str,
        *,
        now: datetime | None = None,
    ) -> MutationResult:
        timestamp = format_timestamp(now or datetime.now(UTC))
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT notification_id, option_id, message, responder_principal "
                    "FROM responses WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["notification_id"] != notification_id
                        or existing["option_id"] != option_id
                        or existing["message"] != message
                        or existing["responder_principal"] != responder_principal
                    ):
                        raise IdempotencyConflictError("response request id was reused")
                    notification = self._get(connection, notification_id)
                    connection.commit()
                    return MutationResult(
                        notification, self._latest_entity_event(connection, notification_id), False
                    )

                notification = self._get(connection, notification_id)
                if notification.response_state is ResponseState.ANSWERED:
                    raise AlreadyAnsweredError(notification)
                if notification.response_state is not ResponseState.PENDING:
                    raise StateConflictError(
                        f"cannot respond while state is {notification.response_state}"
                    )
                option = next(
                    (item for item in notification.response_options if item.id == option_id), None
                )
                if option is None:
                    raise ValidationError("response option does not exist")
                option.validate_message(message)
                Response(request_id, option_id, message, timestamp, responder_principal)
                connection.execute(
                    "INSERT INTO responses("
                    "notification_id, request_id, option_id, message, responded_at, "
                    "responder_principal) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        notification_id,
                        request_id,
                        option_id,
                        message,
                        timestamp,
                        responder_principal,
                    ),
                )
                changed = connection.execute(
                    "UPDATE notifications SET response_state = 'answered', updated_at = ?, "
                    "version = version + 1 WHERE id = ? AND response_state = 'pending'",
                    (timestamp, notification_id),
                ).rowcount
                if changed != 1:
                    raise StateConflictError("notification state changed concurrently")
                connection.execute(
                    "UPDATE domains SET last_activity_at = ? WHERE id = "
                    "(SELECT domain_id FROM notifications WHERE id = ?)",
                    (timestamp, notification_id),
                )
                notification = self._get(connection, notification_id)
                seq = self._event(
                    connection,
                    "notification.updated",
                    notification_id,
                    timestamp,
                    notification.to_dict(),
                )
                connection.commit()
                return MutationResult(notification, seq, True)
            except Exception:
                connection.rollback()
                raise

    def cancel(
        self,
        notification_id: str,
        reason: str | None = None,
        *,
        now: datetime | None = None,
    ) -> MutationResult:
        if reason is not None and len(reason) > 16 * 1024:
            raise ValidationError("cancellation reason exceeds 16 KiB")
        timestamp = format_timestamp(now or datetime.now(UTC))
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT response_state, cancellation_reason FROM notifications WHERE id = ?",
                    (notification_id,),
                ).fetchone()
                if row is None:
                    raise NotFoundError(f"unknown notification: {notification_id}")
                if row["response_state"] == ResponseState.CANCELLED.value:
                    if row["cancellation_reason"] != reason:
                        raise IdempotencyConflictError(
                            "notification was cancelled with a different reason"
                        )
                    result = self._get(connection, notification_id)
                    connection.commit()
                    return MutationResult(
                        result, self._latest_entity_event(connection, notification_id), False
                    )
                if row["response_state"] != ResponseState.PENDING.value:
                    raise StateConflictError(
                        f"cannot cancel while state is {row['response_state']}"
                    )
                connection.execute(
                    "UPDATE notifications SET response_state = 'cancelled', cancelled_at = ?, "
                    "cancellation_reason = ?, updated_at = ?, version = version + 1 WHERE id = ?",
                    (timestamp, reason, timestamp, notification_id),
                )
                connection.execute(
                    "UPDATE domains SET last_activity_at = ? WHERE id = "
                    "(SELECT domain_id FROM notifications WHERE id = ?)",
                    (timestamp, notification_id),
                )
                notification = self._get(connection, notification_id)
                seq = self._event(
                    connection,
                    "notification.updated",
                    notification_id,
                    timestamp,
                    notification.to_dict(),
                )
                connection.commit()
                return MutationResult(notification, seq, True)
            except Exception:
                connection.rollback()
                raise

    def set_read_state(
        self, notification_ids: list[str], read: bool, *, now: datetime | None = None
    ) -> tuple[list[Notification], int | None]:
        if (
            not notification_ids
            or len(notification_ids) > 500
            or len(set(notification_ids)) != len(notification_ids)
        ):
            raise ValidationError("notification ids must contain 1..500 unique values")
        timestamp = format_timestamp(now or datetime.now(UTC))
        target = timestamp if read else None
        placeholders = ",".join("?" for _ in notification_ids)
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                found = connection.execute(
                    f"SELECT id FROM notifications WHERE id IN ({placeholders})", notification_ids
                ).fetchall()
                if len(found) != len(notification_ids):
                    raise NotFoundError("one or more notifications do not exist")
                state_predicate = "read_at IS NULL" if read else "read_at IS NOT NULL"
                changed_ids = [
                    row["id"]
                    for row in connection.execute(
                        f"SELECT id FROM notifications WHERE id IN ({placeholders}) "
                        f"AND {state_predicate}",
                        notification_ids,
                    )
                ]
                changed = connection.execute(
                    "UPDATE notifications SET read_at = ?, updated_at = ?, "
                    f"version = version + 1 WHERE id IN ({placeholders}) "
                    f"AND {state_predicate}",
                    [target, timestamp, *notification_ids],
                ).rowcount
                seq = None
                if changed:
                    event_type = (
                        "notification.updated"
                        if len(notification_ids) == 1
                        else "notifications.read_state_changed"
                    )
                    payload: dict[str, Any]
                    if event_type == "notification.updated":
                        payload = self._get(connection, notification_ids[0]).to_dict()
                    else:
                        payload = {"notification_ids": changed_ids, "read_at": target}
                    seq = self._event(
                        connection,
                        event_type,
                        notification_ids[0] if len(notification_ids) == 1 else None,
                        timestamp,
                        payload,
                    )
                notifications = [self._get(connection, item) for item in notification_ids]
                connection.commit()
                return notifications, seq
            except Exception:
                connection.rollback()
                raise

    def cleanup(self, retention: RetentionConfig, *, now: datetime | None = None) -> CleanupResult:
        now = now or datetime.now(UTC)
        timestamp = format_timestamp(now)
        history_cutoff = format_timestamp(now - timedelta(days=retention.history_days))
        pending_cutoff = format_timestamp(now - timedelta(days=retention.max_pending_days))
        event_cutoff = format_timestamp(now - timedelta(days=retention.event_history_days))
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                expired_ids = [
                    row["id"]
                    for row in connection.execute(
                        "SELECT id FROM notifications WHERE response_state = 'pending' "
                        "AND created_at < ?",
                        (pending_cutoff,),
                    )
                ]
                for notification_id in expired_ids:
                    connection.execute(
                        "UPDATE notifications SET response_state = 'expired', updated_at = ?, "
                        "version = version + 1 WHERE id = ? AND response_state = 'pending'",
                        (timestamp, notification_id),
                    )
                    connection.execute(
                        "UPDATE domains SET last_activity_at = ? WHERE id = "
                        "(SELECT domain_id FROM notifications WHERE id = ?)",
                        (timestamp, notification_id),
                    )
                    notification = self._get(connection, notification_id)
                    self._event(
                        connection,
                        "notification.updated",
                        notification_id,
                        timestamp,
                        notification.to_dict(),
                    )

                delete_ids = [
                    row["id"]
                    for row in connection.execute(
                        """SELECT id FROM notifications
                           WHERE (response_state = 'not_requested' AND created_at < ?)
                               OR (response_state IN ('answered', 'cancelled', 'expired')
                                   AND updated_at < ?)""",
                        (history_cutoff, history_cutoff),
                    )
                ]
                for notification_id in delete_ids:
                    self._event(
                        connection,
                        "notification.deleted",
                        notification_id,
                        timestamp,
                        {"id": notification_id},
                    )
                    connection.execute("DELETE FROM notifications WHERE id = ?", (notification_id,))

                empty_domains = connection.execute(
                    "SELECT id, name FROM domains WHERE NOT EXISTS "
                    "(SELECT 1 FROM notifications WHERE domain_id = domains.id)"
                ).fetchall()
                for domain in empty_domains:
                    self._event(
                        connection,
                        "domain.deleted",
                        domain["name"],
                        timestamp,
                        {"name": domain["name"]},
                    )
                    connection.execute("DELETE FROM domains WHERE id = ?", (domain["id"],))

                deleted_events = connection.execute(
                    "DELETE FROM events WHERE occurred_at < ?", (event_cutoff,)
                ).rowcount
                if retention.max_event_count is not None:
                    deleted_events += connection.execute(
                        "DELETE FROM events WHERE seq <= coalesce("
                        "(SELECT seq FROM events ORDER BY seq DESC LIMIT 1 OFFSET ?), 0)",
                        (retention.max_event_count,),
                    ).rowcount
                deleted_batches = connection.execute(
                    "DELETE FROM auth_nonce_batches WHERE expires_at < ?", (timestamp,)
                ).rowcount
                connection.commit()
                connection.execute("PRAGMA optimize")
                return CleanupResult(
                    len(expired_ids),
                    len(delete_ids),
                    len(empty_domains),
                    deleted_events,
                    deleted_batches,
                )
            except Exception:
                connection.rollback()
                raise

    def events(self) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            return [
                {
                    "seq": row["seq"],
                    "type": row["event_type"],
                    "entity_id": row["entity_id"],
                    "occurred_at": row["occurred_at"],
                    "payload": json.loads(row["payload_json"]),
                }
                for row in connection.execute("SELECT * FROM events ORDER BY seq")
            ]

    def _get(self, connection: sqlite3.Connection, notification_id: str) -> Notification:
        row = connection.execute(
            """SELECT n.*, d.name AS domain_name FROM notifications n
               JOIN domains d ON d.id = n.domain_id WHERE n.id = ?""",
            (notification_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"unknown notification: {notification_id}")
        options = tuple(
            ResponseOption(
                item["option_id"],
                item["label"],
                MessageMode(item["message_mode"]),
                Appearance(item["appearance"]),
            )
            for item in connection.execute(
                "SELECT * FROM response_options WHERE notification_id = ? ORDER BY position",
                (notification_id,),
            )
        )
        response_row = connection.execute(
            "SELECT * FROM responses WHERE notification_id = ?", (notification_id,)
        ).fetchone()
        response = (
            Response(
                response_row["request_id"],
                response_row["option_id"],
                response_row["message"],
                response_row["responded_at"],
                response_row["responder_principal"],
            )
            if response_row
            else None
        )
        return Notification(
            row["id"],
            row["domain_name"],
            row["sender"],
            row["summary"],
            row["message_markdown"],
            row["details_markdown"],
            tuple(json.loads(row["tags_json"])),
            Priority(row["priority"]),
            row["source_created_at"],
            row["created_at"],
            row["updated_at"],
            row["read_at"],
            ResponseState(row["response_state"]),
            options,
            response,
            row["version"],
            row["cancellation_reason"],
        )

    @staticmethod
    def _event(
        connection: sqlite3.Connection,
        event_type: str,
        entity_id: str | None,
        occurred_at: str,
        payload: dict[str, Any],
    ) -> int:
        cursor = connection.execute(
            "INSERT INTO events(event_type, entity_id, occurred_at, payload_json) "
            "VALUES (?, ?, ?, ?)",
            (
                event_type,
                entity_id,
                occurred_at,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            ),
        )
        return int(cursor.lastrowid)

    @staticmethod
    def _latest_entity_event(connection: sqlite3.Connection, entity_id: str) -> int:
        row = connection.execute(
            "SELECT coalesce(max(seq), 0) AS seq FROM events WHERE entity_id = ?", (entity_id,)
        ).fetchone()
        return int(row["seq"])
