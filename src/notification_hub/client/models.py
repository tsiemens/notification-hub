from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from notification_hub.domain import (
    DomainSummary,
    Notification,
    Response,
    ResponseOption,
    ResponseState,
    ValidationError,
    parse_timestamp,
)

from .errors import ProtocolError


def _object(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ProtocolError(f"{name} must be an object")
    return value


def _shape(value: dict[str, Any], fields: set[str], name: str) -> None:
    if set(value) != fields:
        raise ProtocolError(f"{name} has incompatible fields")


def parse_notification(value: object) -> Notification:
    item = _object(value, "notification")
    _shape(
        item,
        {
            "id",
            "domain",
            "sender",
            "summary",
            "message",
            "details",
            "tags",
            "priority",
            "source_created_at",
            "created_at",
            "updated_at",
            "read_at",
            "response_state",
            "response_options",
            "response",
            "version",
        },
        "notification",
    )
    if not isinstance(item["tags"], list) or not isinstance(item["response_options"], list):
        raise ProtocolError("notification arrays are malformed")
    if isinstance(item["version"], bool) or not isinstance(item["version"], int):
        raise ProtocolError("notification version is malformed")
    try:
        options = tuple(
            ResponseOption(**_object(option, "response option"))
            for option in item["response_options"]
        )
        response = (
            None if item["response"] is None else Response(**_object(item["response"], "response"))
        )
        return Notification(
            id=item["id"],
            domain=item["domain"],
            sender=item["sender"],
            summary=item["summary"],
            message=item["message"],
            details=item["details"],
            tags=tuple(item["tags"]),
            priority=item["priority"],
            source_created_at=item["source_created_at"],
            created_at=item["created_at"],
            updated_at=item["updated_at"],
            read_at=item["read_at"],
            response_state=item["response_state"],
            response_options=options,
            response=response,
            version=item["version"],
        )
    except (TypeError, KeyError, ValidationError, ValueError) as exc:
        raise ProtocolError("notification is malformed") from exc


def parse_domain(value: object) -> DomainSummary:
    item = _object(value, "domain summary")
    fields = {
        "name",
        "last_activity_at",
        "notification_count",
        "unread_count",
        "pending_response_count",
        "latest_summary",
    }
    _shape(item, fields, "domain summary")
    if (
        not isinstance(item["name"], str)
        or not item["name"]
        or not isinstance(item["latest_summary"], str)
        or any(
            isinstance(item[name], bool) or not isinstance(item[name], int) or item[name] < 0
            for name in ("notification_count", "unread_count", "pending_response_count")
        )
        or item["notification_count"] < 1
        or item["unread_count"] > item["notification_count"]
        or item["pending_response_count"] > item["notification_count"]
    ):
        raise ProtocolError("domain summary is malformed")
    try:
        parse_timestamp(item["last_activity_at"], require_canonical=True)
    except (ValidationError, TypeError) as exc:
        raise ProtocolError("domain summary is malformed") from exc
    return DomainSummary(**item)


@dataclass(frozen=True, slots=True)
class NotificationQuery:
    domains: tuple[str, ...] = ()
    senders: tuple[str, ...] = ()
    unread: bool | None = None
    response_states: tuple[str | ResponseState, ...] = ()
    tags: tuple[str, ...] = ()
    created_before: str | None = None
    created_after: str | None = None
    order: str = "desc"
    limit: int = 100
    cursor: str | None = None

    def parameters(self) -> list[tuple[str, str]]:
        if self.order not in {"asc", "desc"} or not 1 <= self.limit <= 500:
            raise ValueError("order must be asc or desc and limit must be between 1 and 500")
        if self.unread is not None and not isinstance(self.unread, bool):
            raise ValueError("unread must be a boolean or None")
        result: list[tuple[str, str]] = []
        result.extend(("domain", value) for value in self.domains)
        result.extend(("sender", value) for value in self.senders)
        if self.unread is not None:
            result.append(("unread", str(self.unread).lower()))
        for value in self.response_states:
            result.append(("response_state", ResponseState(value).value))
        result.extend(("tag", value) for value in self.tags)
        for name, value in (
            ("created_before", self.created_before),
            ("created_after", self.created_after),
        ):
            if value is not None:
                parse_timestamp(value, require_canonical=True)
                result.append((name, value))
        result.extend((("order", self.order), ("limit", str(self.limit))))
        if self.cursor is not None:
            result.append(("cursor", self.cursor))
        return result


@dataclass(frozen=True, slots=True)
class NotificationPage:
    items: tuple[Notification, ...]
    next_cursor: str | None

    def to_dict(self) -> dict[str, Any]:
        return {"items": [item.to_dict() for item in self.items], "next_cursor": self.next_cursor}


@dataclass(frozen=True, slots=True)
class DomainPage:
    items: tuple[DomainSummary, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"items": [item.to_dict() for item in self.items]}


@dataclass(frozen=True, slots=True)
class ClientSnapshot:
    sequence: int
    generated_at: str
    domains: tuple[DomainSummary, ...]
    notifications: tuple[Notification, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "generated_at": self.generated_at,
            "domains": [item.to_dict() for item in self.domains],
            "notifications": [item.to_dict() for item in self.notifications],
        }


@dataclass(frozen=True, slots=True)
class HubEvent:
    seq: int
    type: str
    occurred_at: str
    notification: Notification | None = None
    notification_ids: tuple[str, ...] = ()
    versions: tuple[int, ...] = ()
    read_at: str | None = None
    id: str | None = None
    name: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "seq": self.seq,
            "type": self.type,
            "occurred_at": self.occurred_at,
        }
        if self.notification is not None:
            value["notification"] = self.notification.to_dict()
        elif self.type == "notifications.read_state_changed":
            value.update(
                notification_ids=list(self.notification_ids),
                versions=list(self.versions),
                read_at=self.read_at,
            )
        elif self.type == "notification.deleted":
            value["id"] = self.id
        else:
            value["name"] = self.name
        return value


def parse_event(value: object) -> HubEvent:
    item = _object(value, "event")
    base = {"seq", "type", "occurred_at"}
    event_type = item.get("type")
    fields = {
        "notification.created": base | {"notification"},
        "notification.updated": base | {"notification"},
        "notifications.read_state_changed": base | {"notification_ids", "versions", "read_at"},
        "notification.deleted": base | {"id"},
        "domain.deleted": base | {"name"},
    }.get(event_type)
    if fields is None:
        raise ProtocolError(f"unknown event type: {event_type!r}")
    _shape(item, fields, "event")
    if isinstance(item["seq"], bool) or not isinstance(item["seq"], int) or item["seq"] < 1:
        raise ProtocolError("event sequence is malformed")
    try:
        parse_timestamp(item["occurred_at"], require_canonical=True)
    except (ValidationError, TypeError) as exc:
        raise ProtocolError("event timestamp is malformed") from exc
    if event_type in {"notification.created", "notification.updated"}:
        return HubEvent(
            item["seq"],
            event_type,
            item["occurred_at"],
            notification=parse_notification(item["notification"]),
        )
    if event_type == "notifications.read_state_changed":
        ids = item["notification_ids"]
        versions = item["versions"]
        if (
            not isinstance(ids, list)
            or not ids
            or any(not isinstance(value, str) or not value for value in ids)
            or len(set(ids)) != len(ids)
            or not isinstance(versions, list)
            or len(versions) != len(ids)
            or any(
                isinstance(value, bool) or not isinstance(value, int) or value < 1
                for value in versions
            )
        ):
            raise ProtocolError("read-state event ids are malformed")
        if item["read_at"] is not None:
            try:
                parse_timestamp(item["read_at"], require_canonical=True)
            except (ValidationError, TypeError) as exc:
                raise ProtocolError("read-state event timestamp is malformed") from exc
        return HubEvent(
            item["seq"],
            event_type,
            item["occurred_at"],
            notification_ids=tuple(ids),
            versions=tuple(versions),
            read_at=item["read_at"],
        )
    key = "id" if event_type == "notification.deleted" else "name"
    if not isinstance(item[key], str) or not item[key]:
        raise ProtocolError("deletion event is malformed")
    return HubEvent(
        item["seq"],
        event_type,
        item["occurred_at"],
        id=item[key] if key == "id" else None,
        name=item[key] if key == "name" else None,
    )


@dataclass(frozen=True, slots=True)
class EventPage:
    events: tuple[HubEvent, ...]
    last_sequence: int
    has_more: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "events": [event.to_dict() for event in self.events],
            "last_sequence": self.last_sequence,
            "has_more": self.has_more,
        }


@dataclass(frozen=True, slots=True)
class MutationResult:
    notifications: tuple[Notification, ...]
    event_seq: int | None
    singular: bool = False

    @property
    def notification(self) -> Notification | None:
        return self.notifications[0] if self.singular and self.notifications else None

    def to_dict(self) -> dict[str, Any]:
        key = "notification" if self.singular else "notifications"
        value: Any = (
            self.notifications[0].to_dict()
            if self.singular
            else [item.to_dict() for item in self.notifications]
        )
        return {key: value, "event_seq": self.event_seq}


@dataclass(slots=True)
class SyncState:
    notifications: dict[str, Notification] = field(default_factory=dict)
    domains: dict[str, DomainSummary] = field(default_factory=dict)
    last_sequence: int = 0
    _activity: dict[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def from_snapshot(cls, snapshot: ClientSnapshot) -> SyncState:
        state = cls(
            {item.id: item for item in snapshot.notifications},
            {item.name: item for item in snapshot.domains},
            snapshot.sequence,
            {item.name: item.last_activity_at for item in snapshot.domains},
        )
        return state

    def replace(self, snapshot: ClientSnapshot) -> None:
        fresh = self.from_snapshot(snapshot)
        self.notifications = fresh.notifications
        self.domains = fresh.domains
        self.last_sequence = fresh.last_sequence
        self._activity = fresh._activity

    def apply_page(self, page: EventPage) -> tuple[HubEvent, ...]:
        notifications = dict(self.notifications)
        domains = dict(self.domains)
        activity = dict(self._activity)
        expected = self.last_sequence + 1
        for event in page.events:
            if event.seq != expected:
                raise ProtocolError("event sequence gap")
            expected += 1
            if event.type in {"notification.created", "notification.updated"}:
                assert event.notification is not None
                old = notifications.get(event.notification.id)
                if old is None or event.notification.version > old.version:
                    notifications[event.notification.id] = event.notification
                    activity[event.notification.domain] = event.occurred_at
            elif event.type == "notifications.read_state_changed":
                for notification_id, version in zip(
                    event.notification_ids, event.versions, strict=True
                ):
                    old = notifications.get(notification_id)
                    if old is None:
                        raise ProtocolError("read-state event refers to an unknown notification")
                    if version != old.version + 1:
                        raise ProtocolError("read-state event version is inconsistent")
                    value = old.to_dict()
                    value["read_at"] = event.read_at
                    value["updated_at"] = event.occurred_at
                    value["version"] = version
                    notifications[notification_id] = parse_notification(value)
            elif event.type == "notification.deleted":
                if event.id not in notifications:
                    raise ProtocolError("deletion event refers to an unknown notification")
                del notifications[event.id]
            else:
                assert event.name is not None
                if event.name not in domains or any(
                    item.domain == event.name for item in notifications.values()
                ):
                    raise ProtocolError("domain deletion cannot be reconciled")
                domains.pop(event.name, None)
                activity.pop(event.name, None)
        if page.events and page.last_sequence != page.events[-1].seq:
            raise ProtocolError("event page cursor is inconsistent")
        if not page.events and page.last_sequence != self.last_sequence:
            raise ProtocolError("empty event page cursor is inconsistent")
        self.notifications = notifications
        self._activity = activity
        self.domains = _rebuild_domains(notifications, domains, activity)
        self.last_sequence = page.last_sequence
        return page.events


def _rebuild_domains(
    notifications: dict[str, Notification],
    previous: dict[str, DomainSummary],
    activity: dict[str, str],
) -> dict[str, DomainSummary]:
    grouped: dict[str, list[Notification]] = {}
    for notification in notifications.values():
        grouped.setdefault(notification.domain, []).append(notification)
    result: dict[str, DomainSummary] = {}
    for name, items in grouped.items():
        latest = max(items, key=lambda item: (item.created_at, item.id))
        last_activity = activity.get(name)
        if last_activity is None:
            last_activity = (
                previous.get(name).last_activity_at if name in previous else latest.updated_at
            )
            activity[name] = last_activity
        result[name] = DomainSummary(
            name,
            last_activity,
            len(items),
            sum(item.read_at is None for item in items),
            sum(item.response_state is ResponseState.PENDING for item in items),
            latest.summary,
        )
    return dict(
        sorted(
            result.items(),
            key=lambda pair: (-parse_timestamp(pair[1].last_activity_at).timestamp(), pair[0]),
        )
    )
