from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class ValidationError(ValueError):
    """A domain value does not satisfy the public contract."""


class Priority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class ResponseState(StrEnum):
    NOT_REQUESTED = "not_requested"
    PENDING = "pending"
    ANSWERED = "answered"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class MessageMode(StrEnum):
    NONE = "none"
    OPTIONAL = "optional"
    REQUIRED = "required"


class Appearance(StrEnum):
    DEFAULT = "default"
    PRIMARY = "primary"
    DANGER = "danger"


_OPTION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_UTC_MILLISECONDS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


def _enum_value(enum_type: type[StrEnum], value: Any, field_name: str) -> StrEnum:
    try:
        return enum_type(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"invalid {field_name}: {value!r}") from exc


def validate_text(value: Any, field_name: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise ValidationError(f"{field_name} must contain {minimum}..{maximum} characters")
    if "\x00" in value:
        raise ValidationError(f"{field_name} must not contain NUL characters")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValidationError(f"{field_name} must contain valid UTF-8 text") from exc
    return value


def _validated_tags(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (tuple, list)):
        raise ValidationError("tags must be an array")
    tags = tuple(value)
    if len(tags) > 32:
        raise ValidationError("tags must contain at most 32 unique values")
    for tag in tags:
        validate_text(tag, "tag", 1, 64)
    if len(set(tags)) != len(tags):
        raise ValidationError("tags must contain at most 32 unique values")
    return tags


def _validated_options(value: Any) -> tuple[ResponseOption, ...]:
    if not isinstance(value, (tuple, list)):
        raise ValidationError("response options must be an array")
    options = tuple(value)
    if any(not isinstance(option, ResponseOption) for option in options):
        raise ValidationError("response options must contain response options")
    if len(options) > 16 or len({option.id for option in options}) != len(options):
        raise ValidationError("response options must contain at most 16 unique ids")
    return options


def _uuid4(value: Any, field_name: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError(f"{field_name} must be a UUIDv4") from exc
    if parsed.version != 4 or str(parsed) != str(value).lower():
        raise ValidationError(f"{field_name} must be a canonical UUIDv4")
    return str(parsed)


def format_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValidationError("timestamp must be timezone-aware")
    value = value.astimezone(UTC)
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_timestamp(value: str, *, require_canonical: bool = False) -> datetime:
    if not isinstance(value, str):
        raise ValidationError("timestamp must be a string")
    validate_text(value, "timestamp", 1, 128)
    if require_canonical and not _UTC_MILLISECONDS.fullmatch(value):
        raise ValidationError("timestamp must use UTC with millisecond precision")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"invalid RFC 3339 timestamp: {value!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError("timestamp must include a UTC offset")
    return parsed.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class ResponseOption:
    id: str
    label: str
    message_mode: MessageMode = MessageMode.NONE
    appearance: Appearance = Appearance.DEFAULT

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _OPTION_ID.fullmatch(self.id):
            raise ValidationError("response option id is invalid")
        validate_text(self.label, "response option label", 1, 80)
        object.__setattr__(
            self, "message_mode", _enum_value(MessageMode, self.message_mode, "message_mode")
        )
        object.__setattr__(
            self, "appearance", _enum_value(Appearance, self.appearance, "appearance")
        )

    def validate_message(self, message: str | None) -> None:
        if message is not None:
            validate_text(message, "response message", 0, 16 * 1024)
        if self.message_mode is MessageMode.NONE and message not in (None, ""):
            raise ValidationError("this response option does not accept a message")
        if self.message_mode is MessageMode.REQUIRED and not message:
            raise ValidationError("this response option requires a message")

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "label": self.label,
            "message_mode": self.message_mode.value,
            "appearance": self.appearance.value,
        }


@dataclass(frozen=True, slots=True)
class CreateNotification:
    id: str
    domain: str
    sender: str
    summary: str
    message: str = ""
    details: str | None = None
    tags: tuple[str, ...] = field(default_factory=tuple)
    priority: Priority = Priority.NORMAL
    source_created_at: str | None = None
    response_options: tuple[ResponseOption, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _uuid4(self.id, "id"))
        validate_text(self.domain, "domain", 1, 128)
        validate_text(self.sender, "sender", 1, 128)
        validate_text(self.summary, "summary", 1, 256)
        if "\n" in self.summary or "\r" in self.summary:
            raise ValidationError("summary must be a single logical line")
        validate_text(self.message, "message", 0, 32 * 1024)
        if self.details is not None:
            validate_text(self.details, "details", 0, 128 * 1024)
        object.__setattr__(self, "tags", _validated_tags(self.tags))
        object.__setattr__(self, "priority", _enum_value(Priority, self.priority, "priority"))
        if self.source_created_at is not None:
            parse_timestamp(self.source_created_at)
        object.__setattr__(self, "response_options", _validated_options(self.response_options))

    @property
    def initial_state(self) -> ResponseState:
        return ResponseState.PENDING if self.response_options else ResponseState.NOT_REQUESTED

    def normalized_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "domain": self.domain,
            "sender": self.sender,
            "summary": self.summary,
            "message": self.message,
            "details": self.details,
            "tags": list(self.tags),
            "priority": self.priority.value,
            "source_created_at": self.source_created_at,
            "response_options": [option.to_dict() for option in self.response_options],
        }


def create_fingerprint(request: CreateNotification) -> str:
    """Return the stable idempotency fingerprint for a producer request."""
    encoded = json.dumps(
        request.normalized_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class Response:
    request_id: str
    option_id: str
    message: str | None
    responded_at: str
    responded_by: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _uuid4(self.request_id, "request_id"))
        if not isinstance(self.option_id, str) or not _OPTION_ID.fullmatch(self.option_id):
            raise ValidationError("response option id is invalid")
        if self.message is not None:
            validate_text(self.message, "response message", 0, 16 * 1024)
        parse_timestamp(self.responded_at, require_canonical=True)
        validate_text(self.responded_by, "responded_by", 1, 256)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Notification:
    id: str
    domain: str
    sender: str
    summary: str
    message: str
    details: str | None
    tags: tuple[str, ...]
    priority: Priority
    source_created_at: str | None
    created_at: str
    updated_at: str
    read_at: str | None
    response_state: ResponseState
    response_options: tuple[ResponseOption, ...]
    response: Response | None
    version: int
    cancellation_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "id", _uuid4(self.id, "id"))
        validate_text(self.domain, "domain", 1, 128)
        validate_text(self.sender, "sender", 1, 128)
        validate_text(self.summary, "summary", 1, 256)
        if "\n" in self.summary or "\r" in self.summary:
            raise ValidationError("summary must be a single logical line")
        validate_text(self.message, "message", 0, 32 * 1024)
        if self.details is not None:
            validate_text(self.details, "details", 0, 128 * 1024)
        object.__setattr__(self, "tags", _validated_tags(self.tags))
        object.__setattr__(self, "priority", _enum_value(Priority, self.priority, "priority"))
        object.__setattr__(
            self,
            "response_state",
            _enum_value(ResponseState, self.response_state, "response_state"),
        )
        for value in (self.created_at, self.updated_at):
            parse_timestamp(value, require_canonical=True)
        if self.read_at is not None:
            parse_timestamp(self.read_at, require_canonical=True)
        if self.source_created_at is not None:
            parse_timestamp(self.source_created_at)
        if self.version < 1:
            raise ValidationError("version must be positive")
        options = _validated_options(self.response_options)
        object.__setattr__(self, "response_options", options)
        if not options and self.response_state is not ResponseState.NOT_REQUESTED:
            raise ValidationError("a notification without options must be not_requested")
        if options and self.response_state is ResponseState.NOT_REQUESTED:
            raise ValidationError("a notification with options cannot be not_requested")
        if self.response_state is ResponseState.ANSWERED and self.response is None:
            raise ValidationError("answered notifications require a response")
        if self.response_state is not ResponseState.ANSWERED and self.response is not None:
            raise ValidationError("only answered notifications may have a response")
        if self.response is not None:
            option = next((item for item in options if item.id == self.response.option_id), None)
            if option is None:
                raise ValidationError("response selected an unknown option")
            option.validate_message(self.response.message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "domain": self.domain,
            "sender": self.sender,
            "summary": self.summary,
            "message": self.message,
            "details": self.details,
            "tags": list(self.tags),
            "priority": self.priority.value,
            "source_created_at": self.source_created_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "read_at": self.read_at,
            "response_state": self.response_state.value,
            "response_options": [option.to_dict() for option in self.response_options],
            "response": self.response.to_dict() if self.response else None,
            "version": self.version,
        }


@dataclass(frozen=True, slots=True)
class DomainSummary:
    name: str
    last_activity_at: str
    notification_count: int
    unread_count: int
    pending_response_count: int
    latest_summary: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
