from __future__ import annotations

import threading
import time
import weakref
from collections import OrderedDict
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from logging import Logger
from secrets import token_urlsafe
from typing import Any

from flask import Flask, Response, g, jsonify, request
from werkzeug.exceptions import (
    BadRequest,
    HTTPException,
    RequestEntityTooLarge,
    UnsupportedMediaType,
)

from notification_hub.config import RetentionConfig, ServerConfig
from notification_hub.domain import (
    CreateNotification,
    ResponseOption,
    ResponseState,
    ValidationError,
    parse_timestamp,
)
from notification_hub.server.auth import (
    AuthenticatedPrincipal,
    AuthenticationError,
    AuthorizationError,
    RequestAuthenticator,
)
from notification_hub.storage import (
    AlreadyAnsweredError,
    CursorExpiredError,
    Database,
    IdempotencyConflictError,
    NonceError,
    NotFoundError,
    NotificationQuery,
    NotificationRepository,
    PendingLimitError,
    RateLimitError,
    Snapshot,
    StateConflictError,
)

JsonObject = Mapping[str, Any]

_CLEANUP_INTERVAL_SECONDS = 60 * 60
_SNAPSHOT_MAX_BYTES = 10 * 1024 * 1024
_SNAPSHOT_TOKEN_TTL_SECONDS = 60
_MAX_CACHED_SNAPSHOTS = 5


@dataclass(slots=True)
class _CachedSnapshot:
    expires_at: float
    value: Snapshot


class _SnapshotCache:
    """Keep a few immutable snapshots available while clients fetch their pages."""

    def __init__(self, *, ttl_seconds: float = _SNAPSHOT_TOKEN_TTL_SECONDS) -> None:
        self._ttl_seconds = ttl_seconds
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, _CachedSnapshot] = OrderedDict()

    def add(self, value: Snapshot) -> str:
        with self._lock:
            self._expire()
            while len(self._entries) >= _MAX_CACHED_SNAPSHOTS:
                self._entries.popitem(last=False)
            token = token_urlsafe(32)
            self._entries[token] = _CachedSnapshot(time.monotonic() + self._ttl_seconds, value)
            return token

    def get(self, token: str) -> Snapshot | None:
        with self._lock:
            self._expire()
            entry = self._entries.get(token)
            if entry is None:
                return None
            self._entries.move_to_end(token)
            return entry.value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def _expire(self) -> None:
        now = time.monotonic()
        expired = [token for token, entry in self._entries.items() if entry.expires_at <= now]
        for token in expired:
            del self._entries[token]


class _ApplicationLifecycle:
    """Coordinate request draining and background/long-poll shutdown."""

    def __init__(self, state_changed: threading.Condition, cleanup: _CleanupWorker) -> None:
        self._state_changed = state_changed
        self._cleanup = cleanup
        self._condition = threading.Condition()
        self._stopping = False
        self._active_requests = 0

    @property
    def stopping(self) -> bool:
        with self._condition:
            return self._stopping

    def begin_request(self) -> bool:
        with self._condition:
            if self._stopping:
                return False
            self._active_requests += 1
            return True

    def end_request(self) -> None:
        with self._condition:
            self._active_requests -= 1
            if self._active_requests == 0:
                self._condition.notify_all()

    def stop(self, *, join: bool = False) -> None:
        with self._condition:
            self._stopping = True
        self._cleanup.stop(join=join)
        with self._state_changed:
            self._state_changed.notify_all()

    def wait_for_idle(self) -> None:
        with self._condition:
            while self._active_requests:
                self._condition.wait()


class _CleanupWorker:
    """Run repository retention without tying it to request traffic."""

    def __init__(
        self,
        retention: RetentionConfig,
        state_changed: threading.Condition,
        logger: Logger,
        *,
        interval_seconds: float = _CLEANUP_INTERVAL_SECONDS,
    ) -> None:
        self._retention = retention
        self._state_changed = state_changed
        self._logger = logger
        self._interval_seconds = interval_seconds
        self._start_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._repository: NotificationRepository | None = None
        self._thread: threading.Thread | None = None

    def start(self, repository: NotificationRepository) -> None:
        """Run startup cleanup, then start the hourly maintenance thread once."""
        with self._start_lock:
            if self._thread is not None:
                return
            self._repository = repository
            self.run_once()
            self._thread = threading.Thread(
                target=self._run,
                name="notification-hub-cleanup",
                daemon=True,
            )
            self._thread.start()

    def run_once(self) -> None:
        repository = self._repository
        if repository is None:
            raise RuntimeError("cleanup worker has not been started")
        repository.cleanup(self._retention)
        # Cleanup can expire pending outcomes, append events, or invalidate an
        # event cursor. Wake both kinds of waiter so they immediately re-read.
        with self._state_changed:
            self._state_changed.notify_all()

    def stop(self, *, join: bool = False) -> None:
        self._stop_event.set()
        thread = self._thread
        if join and thread is not None and thread is not threading.current_thread():
            thread.join()

    def _run(self) -> None:
        while not self._stop_event.wait(self._interval_seconds):
            try:
                self.run_once()
            except Exception:
                # A transient cleanup failure must not permanently disable
                # retention. The next hourly iteration retries it.
                self._logger.exception("Notification retention cleanup failed")


def _error(
    code: str, message: str, status: int, details: Mapping[str, Any] | None = None
) -> tuple[Response, int]:
    return jsonify(error={"code": code, "message": message, "details": dict(details or {})}), status


def _json_object(
    *, allowed: set[str], required: set[str] = frozenset(), allow_empty_body: bool = False
) -> dict[str, Any]:
    raw_body = request.get_data(cache=True)
    if not raw_body and allow_empty_body:
        value: Any = {}
    else:
        if request.mimetype != "application/json":
            raise BadRequest("request Content-Type must be application/json")
        charset = request.mimetype_params.get("charset")
        if charset is not None and charset.lower() != "utf-8":
            raise BadRequest("request JSON must use UTF-8")
        try:
            raw_body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BadRequest("request JSON must use UTF-8") from exc
        try:
            value = request.get_json()
        except (BadRequest, UnsupportedMediaType) as exc:
            raise BadRequest("request body must be valid application/json") from exc
    if not isinstance(value, dict):
        raise BadRequest("request body must be a JSON object")
    unknown = set(value) - allowed
    if unknown:
        raise ValidationError(f"unknown field(s): {', '.join(sorted(unknown))}")
    missing = required - set(value)
    if missing:
        raise ValidationError(f"missing required field(s): {', '.join(sorted(missing))}")
    return value


def _create_request(value: JsonObject) -> CreateNotification:
    tags = value.get("tags", [])
    if not isinstance(tags, list):
        raise ValidationError("tags must be an array")
    option_values = value.get("response_options", [])
    if not isinstance(option_values, list):
        raise ValidationError("response_options must be an array")

    options: list[ResponseOption] = []
    option_allowed = {"id", "label", "message_mode", "appearance"}
    option_required = {"id", "label"}
    for index, option in enumerate(option_values):
        if not isinstance(option, dict):
            raise ValidationError(f"response_options[{index}] must be an object")
        unknown = set(option) - option_allowed
        missing = option_required - set(option)
        if unknown:
            raise ValidationError(
                f"unknown response_options[{index}] field(s): {', '.join(sorted(unknown))}"
            )
        if missing:
            raise ValidationError(
                f"missing response_options[{index}] field(s): {', '.join(sorted(missing))}"
            )
        options.append(
            ResponseOption(
                option["id"],
                option["label"],
                option.get("message_mode", "none"),
                option.get("appearance", "default"),
            )
        )

    return CreateNotification(
        id=value["id"],
        domain=value["domain"],
        sender=value["sender"],
        summary=value["summary"],
        message=value.get("message", ""),
        details=value.get("details"),
        tags=tuple(tags),
        priority=value.get("priority", "normal"),
        source_created_at=value.get("source_created_at"),
        response_options=tuple(options),
    )


def _wait_seconds() -> int:
    unknown = set(request.args) - {"wait_seconds"}
    if unknown:
        raise ValidationError(f"unknown query parameter(s): {', '.join(sorted(unknown))}")
    values = request.args.getlist("wait_seconds")
    if len(values) > 1:
        raise ValidationError("wait_seconds may be supplied only once")
    raw_value = values[0] if values else "0"
    try:
        value = int(raw_value)
    except (TypeError, ValueError) as exc:
        raise ValidationError("wait_seconds must be an integer from 0 through 30") from exc
    if str(value) != raw_value or not 0 <= value <= 30:
        raise ValidationError("wait_seconds must be an integer from 0 through 30")
    return value


def _single_query_value(name: str, default: str | None = None) -> str | None:
    values = request.args.getlist(name)
    if len(values) > 1:
        raise ValidationError(f"{name} may be supplied only once")
    return values[0] if values else default


def _query_integer(name: str, default: int, minimum: int, maximum: int) -> int:
    raw_value = _single_query_value(name, str(default))
    assert raw_value is not None
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValidationError(
            f"{name} must be an integer from {minimum} through {maximum}"
        ) from exc
    if str(value) != raw_value or not minimum <= value <= maximum:
        raise ValidationError(f"{name} must be an integer from {minimum} through {maximum}")
    return value


def _parse_snapshot_cursor(raw_value: str | None) -> int:
    if raw_value is None:
        return 0
    try:
        value = int(raw_value)
    except ValueError as exc:
        raise ValidationError("cursor must be a non-negative integer") from exc
    if str(value) != raw_value or value < 0:
        raise ValidationError("cursor must be a non-negative integer")
    return value


def _reject_unknown_query(allowed: set[str]) -> None:
    unknown = set(request.args) - allowed
    if unknown:
        raise ValidationError(f"unknown query parameter(s): {', '.join(sorted(unknown))}")


def _notification_query() -> NotificationQuery:
    allowed = {
        "domain",
        "sender",
        "unread",
        "response_state",
        "tag",
        "created_before",
        "created_after",
        "order",
        "limit",
        "cursor",
    }
    _reject_unknown_query(allowed)
    unread_value = _single_query_value("unread")
    if unread_value not in {None, "true", "false"}:
        raise ValidationError("unread must be true or false")
    state_values: list[ResponseState] = []
    for value in request.args.getlist("response_state"):
        try:
            state_values.append(ResponseState(value))
        except ValueError as exc:
            raise ValidationError(f"invalid response_state: {value!r}") from exc
    timestamps: dict[str, str | None] = {}
    for name in ("created_before", "created_after"):
        value = _single_query_value(name)
        if value is not None:
            parse_timestamp(value, require_canonical=True)
        timestamps[name] = value
    order = _single_query_value("order", "desc")
    if order not in {"asc", "desc"}:
        raise ValidationError("order must be asc or desc")
    return NotificationQuery(
        domains=tuple(request.args.getlist("domain")),
        senders=tuple(request.args.getlist("sender")),
        unread=None if unread_value is None else unread_value == "true",
        response_states=tuple(state_values),
        tags=tuple(request.args.getlist("tag")),
        created_before=timestamps["created_before"],
        created_after=timestamps["created_after"],
        order=order,
        limit=_query_integer("limit", 100, 1, 500),
        cursor=_single_query_value("cursor"),
    )


def create_app(
    config: ServerConfig | None = None,
    *,
    repository: NotificationRepository | None = None,
    snapshot_max_bytes: int = _SNAPSHOT_MAX_BYTES,
) -> Flask:
    """Create the HTTP application.

    Producer endpoints are intentionally unauthenticated. Hub/client routes use
    the configured RFC 9421 public-key authentication boundary.
    The database is opened lazily so route inspection does not touch the default
    user data directory.
    """
    config = config or ServerConfig()
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = config.request_body_limit_kib * 1024
    app.config["NOTIFICATION_HUB_CONFIG"] = config
    authenticator = RequestAuthenticator(config.auth)
    app.extensions["notification_hub_authenticator"] = authenticator

    init_lock = threading.Lock()
    state_changed = threading.Condition()
    cleanup_worker = _CleanupWorker(config.retention, state_changed, app.logger)
    lifecycle = _ApplicationLifecycle(state_changed, cleanup_worker)
    snapshot_cache = _SnapshotCache()
    app.extensions["notification_hub_cleanup_worker"] = cleanup_worker
    app.extensions["notification_hub_lifecycle"] = lifecycle
    app.extensions["notification_hub_snapshot_cache"] = snapshot_cache
    weakref.finalize(app, lifecycle.stop)
    if repository is not None:
        app.extensions["notification_hub_repository"] = repository

    def get_repository() -> NotificationRepository:
        existing = app.extensions.get("notification_hub_repository")
        if existing is None:
            with init_lock:
                existing = app.extensions.get("notification_hub_repository")
                if existing is None:
                    database = Database(
                        config.database, strict_permissions=config.strict_database_permissions
                    )
                    database.initialize()
                    existing = NotificationRepository(database)
                    app.extensions["notification_hub_database"] = database
                    app.extensions["notification_hub_repository"] = existing
        cleanup_worker.start(existing)
        return existing

    # The executable calls this before binding its listening socket, ensuring
    # migrations and startup cleanup have completed before the process accepts
    # traffic. Tests and embedders retain the existing lazy-start behavior.
    app.extensions["notification_hub_start"] = get_repository

    def health() -> tuple[Response, int]:
        get_repository()
        return jsonify(status="ok"), 200

    def signed(
        view: Callable[..., tuple[Response, int]],
        scope: str | None,
        *,
        mutation: bool = False,
    ) -> Callable[..., tuple[Response, int]]:
        """Wrap a view with signature, scope, and optional mutation-nonce checks.

        The authenticated principal is stored on Flask's request-local ``g`` so
        mutation handlers can audit the principal and atomically consume its
        verified nonce with the business operation.
        """

        def authenticated_view(**arguments: Any) -> tuple[Response, int]:
            g.authenticated_principal = authenticator.authenticate(
                request, scope, require_nonce=mutation
            )
            return view(**arguments)

        authenticated_view.__name__ = f"signed_{view.__name__}"
        return authenticated_view

    def auth_nonces() -> tuple[Response, int]:
        value = _json_object(allowed={"request_id", "count"}, required={"request_id", "count"})
        principal: AuthenticatedPrincipal = g.authenticated_principal
        nonces = get_repository().issue_nonces(
            principal.key_id,
            value["request_id"],
            value["count"],
            ttl_seconds=config.auth.nonce_ttl_seconds,
            max_outstanding=config.auth.max_outstanding_nonces_per_key,
        )
        return jsonify(
            nonces=[{"value": item.value, "expires_at": item.expires_at} for item in nonces]
        ), 200

    def notifications_create() -> tuple[Response, int]:
        value = _json_object(
            allowed={
                "id",
                "domain",
                "sender",
                "summary",
                "message",
                "details",
                "tags",
                "priority",
                "source_created_at",
                "response_options",
            },
            required={"id", "domain", "sender", "summary"},
        )
        create_request = _create_request(value)
        result = get_repository().create(
            create_request,
            max_creates_per_minute=config.limits.creates_per_minute,
            max_pending_total=config.limits.pending_total,
        )
        with state_changed:
            state_changed.notify_all()
        return (
            jsonify(notification=result.notification.to_dict(), event_seq=result.event_seq),
            201 if result.changed else 200,
        )

    def notification_cancel(notification_id: str) -> tuple[Response, int]:
        value = _json_object(allowed={"reason"}, allow_empty_body=True)
        reason = value.get("reason")
        if reason is not None and not isinstance(reason, str):
            raise ValidationError("reason must be a string or null")
        result = get_repository().cancel(notification_id, reason)
        with state_changed:
            state_changed.notify_all()
        return jsonify(notification=result.notification.to_dict(), event_seq=result.event_seq), 200

    def notification_outcome(notification_id: str) -> tuple[Response, int]:
        wait_seconds = _wait_seconds()
        deadline = time.monotonic() + wait_seconds
        with state_changed:
            while True:
                notification = get_repository().get(notification_id)
                if notification.response_state is not ResponseState.PENDING:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0 or lifecycle.stopping:
                    break
                state_changed.wait(remaining)
        return (
            jsonify(
                notification_id=notification.id,
                state=notification.response_state.value,
                response=notification.response.to_dict() if notification.response else None,
            ),
            200,
        )

    def notification_response(notification_id: str) -> tuple[Response, int]:
        value = _json_object(
            allowed={"request_id", "option_id", "message"},
            required={"request_id", "option_id"},
        )
        request_id = value["request_id"]
        option_id = value["option_id"]
        message = value.get("message")
        if not isinstance(request_id, str):
            raise ValidationError("request_id must be a string")
        if not isinstance(option_id, str):
            raise ValidationError("option_id must be a string")
        if message is not None and not isinstance(message, str):
            raise ValidationError("message must be a string or null")

        principal: AuthenticatedPrincipal = g.authenticated_principal
        assert principal.nonce is not None
        result = get_repository().respond(
            notification_id,
            request_id,
            option_id,
            message,
            principal.principal,
            auth_nonce=(principal.key_id, principal.nonce),
        )
        if result.changed:
            with state_changed:
                state_changed.notify_all()
        return jsonify(notification=result.notification.to_dict(), event_seq=result.event_seq), 200

    def read_state() -> tuple[Response, int]:
        value = _json_object(
            allowed={"notification_ids", "read"},
            required={"notification_ids", "read"},
        )
        notification_ids = value["notification_ids"]
        read = value["read"]
        if not isinstance(notification_ids, list) or any(
            not isinstance(item, str) for item in notification_ids
        ):
            raise ValidationError("notification_ids must be an array of strings")
        if not isinstance(read, bool):
            raise ValidationError("read must be a boolean")
        principal: AuthenticatedPrincipal = g.authenticated_principal
        assert principal.nonce is not None
        notifications, event_seq = get_repository().set_read_state(
            notification_ids,
            read,
            auth_nonce=(principal.key_id, principal.nonce),
        )
        if event_seq is not None:
            with state_changed:
                state_changed.notify_all()
        return jsonify(
            notifications=[item.to_dict() for item in notifications], event_seq=event_seq
        ), 200

    def notifications_list() -> tuple[Response, int]:
        page = get_repository().query_notifications(_notification_query())
        return jsonify(
            items=[item.to_dict() for item in page.items], next_cursor=page.next_cursor
        ), 200

    def notification_get(notification_id: str) -> tuple[Response, int]:
        _reject_unknown_query(set())
        return jsonify(notification=get_repository().get(notification_id).to_dict()), 200

    def domains() -> tuple[Response, int]:
        _reject_unknown_query(set())
        return jsonify(items=[item.to_dict() for item in get_repository().domains()]), 200

    def snapshot() -> tuple[Response, int]:
        _reject_unknown_query({"snapshot_token", "cursor"})
        token = _single_query_value("snapshot_token")
        raw_cursor = _single_query_value("cursor")
        if token is None and raw_cursor is not None:
            raise ValidationError("cursor requires snapshot_token")

        if token is None:
            value = get_repository().snapshot()
            complete_payload = {
                "sequence": value.sequence,
                "generated_at": value.generated_at,
                "domains": [item.to_dict() for item in value.domains],
                "notifications": [item.to_dict() for item in value.notifications],
            }
            complete_response = app.json.response(complete_payload)
            if len(complete_response.get_data()) <= snapshot_max_bytes:
                return complete_response, 200
            token = snapshot_cache.add(value)
            cursor = 0
        else:
            value = snapshot_cache.get(token)
            if value is None:
                return _error(
                    "snapshot_expired",
                    "The snapshot token is invalid or has expired",
                    409,
                    {"reset_required": True},
                )
            cursor = _parse_snapshot_cursor(raw_cursor)

        domain_values = [item.to_dict() for item in value.domains]
        notification_values = [item.to_dict() for item in value.notifications]
        total = len(domain_values) + len(notification_values)
        if cursor >= total:
            raise ValidationError("cursor is outside the snapshot")

        def page_response(end: int) -> Response:
            domain_end = min(end, len(domain_values))
            notification_start = max(cursor - len(domain_values), 0)
            notification_end = max(end - len(domain_values), 0)
            payload = {
                "sequence": value.sequence,
                "generated_at": value.generated_at,
                "domains": domain_values[cursor:domain_end] if cursor < len(domain_values) else [],
                "notifications": notification_values[notification_start:notification_end],
                "snapshot_token": token,
                "next_cursor": str(end) if end < total else None,
            }
            return app.json.response(payload)

        low, high = cursor + 1, total
        best: Response | None = None
        while low <= high:
            end = (low + high) // 2
            candidate = page_response(end)
            if len(candidate.get_data()) <= snapshot_max_bytes:
                best = candidate
                low = end + 1
            else:
                high = end - 1
        if best is None:
            return _error(
                "snapshot_item_too_large",
                "A snapshot item exceeds the 10 MiB encoded response limit",
                500,
            )
        return best, 200

    def events() -> tuple[Response, int]:
        _reject_unknown_query({"after", "wait_seconds", "limit"})
        after = _query_integer("after", 0, 0, 2**63 - 1)
        wait_seconds = _query_integer("wait_seconds", 0, 0, 30)
        limit = _query_integer("limit", 200, 1, 500)
        deadline = time.monotonic() + wait_seconds
        with state_changed:
            while True:
                page = get_repository().event_page(after, limit)
                if page.events or page.has_more:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0 or lifecycle.stopping:
                    break
                state_changed.wait(remaining)
        return jsonify(
            events=page.events,
            last_sequence=page.last_sequence,
            has_more=page.has_more,
        ), 200

    public_routes: tuple[tuple[str, str, tuple[str, ...], Callable[..., Any]], ...] = (
        ("health", "/healthz", ("GET",), health),
        ("notifications_create", "/api/v1/notifications", ("POST",), notifications_create),
        (
            "notification_cancel",
            "/api/v1/notifications/<notification_id>/cancel",
            ("POST",),
            notification_cancel,
        ),
        (
            "notification_outcome",
            "/api/v1/notifications/<notification_id>/outcome",
            ("GET",),
            notification_outcome,
        ),
    )
    signed_routes: tuple[
        tuple[str, str, tuple[str, ...], Callable[..., Any], str | None, bool], ...
    ] = (
        ("auth_nonces", "/api/v1/auth/nonces", ("POST",), auth_nonces, None, False),
        (
            "notification_response",
            "/api/v1/notifications/<notification_id>/response",
            ("POST",),
            notification_response,
            "respond",
            True,
        ),
        ("read_state", "/api/v1/read-state", ("POST",), read_state, "read_state", True),
        (
            "notifications_list",
            "/api/v1/notifications",
            ("GET",),
            notifications_list,
            "read",
            False,
        ),
        (
            "notification_get",
            "/api/v1/notifications/<notification_id>",
            ("GET",),
            notification_get,
            "read",
            False,
        ),
        ("domains", "/api/v1/domains", ("GET",), domains, "read", False),
        ("snapshot", "/api/v1/snapshot", ("GET",), snapshot, "read", False),
        ("events", "/api/v1/events", ("GET",), events, "read", False),
    )
    for endpoint, rule, methods, view in public_routes:
        app.add_url_rule(rule, endpoint, view, methods=list(methods))
    for endpoint, rule, methods, view, scope, mutation in signed_routes:
        app.add_url_rule(
            rule,
            endpoint,
            signed(view, scope, mutation=mutation),
            methods=list(methods),
        )

    @app.errorhandler(BadRequest)
    @app.errorhandler(UnsupportedMediaType)
    def malformed_input(error: BadRequest | UnsupportedMediaType) -> tuple[Response, int]:
        return _error("malformed_input", error.description, 400)

    @app.errorhandler(ValidationError)
    def invalid_data(error: ValidationError) -> tuple[Response, int]:
        return _error("invalid_data", str(error), 422)

    @app.errorhandler(AuthenticationError)
    @app.errorhandler(NonceError)
    def authentication_failed(error: AuthenticationError | NonceError) -> tuple[Response, int]:
        return _error("authentication_failed", str(error), 401)

    @app.errorhandler(AuthorizationError)
    def permission_denied(error: AuthorizationError) -> tuple[Response, int]:
        return _error("permission_denied", str(error), 403)

    @app.errorhandler(NotFoundError)
    def unknown_notification(_error_value: NotFoundError) -> tuple[Response, int]:
        notification_id = (request.view_args or {}).get("notification_id")
        return _error(
            "not_found",
            "The notification does not exist",
            404,
            {"notification_id": notification_id},
        )

    @app.errorhandler(IdempotencyConflictError)
    def idempotency_conflict(error: IdempotencyConflictError) -> tuple[Response, int]:
        details = {}
        if request.view_args and "notification_id" in request.view_args:
            details["notification_id"] = request.view_args["notification_id"]
        else:
            body = request.get_json(silent=True)
            if isinstance(body, dict) and isinstance(body.get("id"), str):
                details["notification_id"] = body["id"]
        return _error("idempotency_conflict", str(error), 409, details)

    @app.errorhandler(CursorExpiredError)
    def cursor_expired(error: CursorExpiredError) -> tuple[Response, int]:
        return _error("cursor_expired", str(error), 409, {"reset_required": True})

    @app.errorhandler(AlreadyAnsweredError)
    def already_answered(error: AlreadyAnsweredError) -> tuple[Response, int]:
        return _error(
            "already_answered",
            "The notification already has a response",
            409,
            {"notification": error.notification.to_dict()},
        )

    @app.errorhandler(StateConflictError)
    def state_conflict(error: StateConflictError) -> tuple[Response, int]:
        notification_id = (request.view_args or {}).get("notification_id")
        return _error(
            "state_conflict",
            str(error),
            409,
            {"notification_id": notification_id},
        )

    @app.errorhandler(PendingLimitError)
    @app.errorhandler(RateLimitError)
    def rate_limited(error: RateLimitError) -> tuple[Response, int]:
        return _error("rate_limited", str(error), 429)

    @app.errorhandler(RequestEntityTooLarge)
    def body_too_large(_error_value: RequestEntityTooLarge) -> tuple[Response, int]:
        return _error("body_too_large", "Request body exceeds the configured limit", 413)

    @app.errorhandler(404)
    def route_not_found(_error_value: Any) -> tuple[Response, int]:
        return _error("not_found", "The requested resource does not exist", 404)

    @app.errorhandler(HTTPException)
    def http_error(error: HTTPException) -> tuple[Response, int]:
        return _error("http_error", error.description, error.code or 500)

    @app.errorhandler(Exception)
    def unexpected_error(error: Exception) -> tuple[Response, int]:
        app.logger.exception("Unhandled request failure", exc_info=error)
        return _error("internal_error", "An unexpected server error occurred", 500)

    @app.after_request
    def echo_request_id(response: Response) -> Response:
        request_id = request.headers.get("X-Request-ID")
        if request_id is not None:
            response.headers["X-Request-ID"] = request_id
        principal = getattr(g, "authenticated_principal", None)
        if principal is not None:
            app.logger.info(
                "signed request principal=%r key_id=%r route=%r status=%d request_id=%r",
                principal.principal,
                principal.key_id,
                request.url_rule.rule if request.url_rule is not None else None,
                response.status_code,
                request_id,
            )
        return response

    @app.before_request
    def reject_requests_during_shutdown() -> tuple[Response, int] | None:
        if lifecycle.begin_request():
            g.notification_hub_request_active = True
            return None
        return _error("server_stopping", "The server is shutting down", 503)

    @app.teardown_request
    def finish_request(_error_value: BaseException | None) -> None:
        if getattr(g, "notification_hub_request_active", False):
            lifecycle.end_request()

    if repository is not None:
        # An injected repository is already active, so application creation is
        # its startup boundary. Default storage remains intentionally lazy until
        # the first request needs it.
        cleanup_worker.start(repository)

    return app
