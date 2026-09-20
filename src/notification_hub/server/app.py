from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import (
    BadRequest,
    HTTPException,
    RequestEntityTooLarge,
    UnsupportedMediaType,
)

from notification_hub.config import ServerConfig
from notification_hub.domain import (
    CreateNotification,
    ResponseOption,
    ResponseState,
    ValidationError,
)
from notification_hub.storage import (
    Database,
    IdempotencyConflictError,
    NotFoundError,
    NotificationRepository,
    PendingLimitError,
    RateLimitError,
    StateConflictError,
)

StubView = Callable[..., tuple[Response, int]]
JsonObject = Mapping[str, Any]


def _error(
    code: str, message: str, status: int, details: Mapping[str, Any] | None = None
) -> tuple[Response, int]:
    return jsonify(error={"code": code, "message": message, "details": dict(details or {})}), status


def _not_implemented(endpoint: str) -> tuple[Response, int]:
    """Return the common error envelope while a client endpoint is a stub."""
    return _error(
        "not_implemented",
        "This API endpoint has not been implemented",
        501,
        {"endpoint": endpoint},
    )


def _stub(endpoint: str) -> StubView:
    def view(**_arguments: Any) -> tuple[Response, int]:
        return _not_implemented(endpoint)

    return view


def _json_object(
    *, allowed: set[str], required: set[str] = frozenset(), allow_empty_body: bool = False
) -> dict[str, Any]:
    raw_body = request.get_data(cache=True)
    if not raw_body and allow_empty_body:
        value: Any = {}
    else:
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


def create_app(
    config: ServerConfig | None = None, *, repository: NotificationRepository | None = None
) -> Flask:
    """Create the HTTP application.

    Producer endpoints are implemented without authentication. Signed hub/client
    endpoints remain explicit stubs until their authentication boundary is added.
    The database is opened lazily so route inspection does not touch the default
    user data directory.
    """
    config = config or ServerConfig()
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = config.request_body_limit_kib * 1024
    app.config["NOTIFICATION_HUB_CONFIG"] = config

    init_lock = threading.Lock()
    state_changed = threading.Condition()
    if repository is not None:
        app.extensions["notification_hub_repository"] = repository

    def get_repository() -> NotificationRepository:
        existing = app.extensions.get("notification_hub_repository")
        if existing is not None:
            return existing
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
        return existing

    def health() -> tuple[Response, int]:
        get_repository()
        return jsonify(status="ok"), 200

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
                if remaining <= 0:
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

    implemented_routes: tuple[tuple[str, str, tuple[str, ...], Callable[..., Any]], ...] = (
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
    stub_routes: tuple[tuple[str, str, tuple[str, ...]], ...] = (
        ("auth_nonces", "/api/v1/auth/nonces", ("POST",)),
        ("notifications_list", "/api/v1/notifications", ("GET",)),
        ("notification_get", "/api/v1/notifications/<notification_id>", ("GET",)),
        (
            "notification_response",
            "/api/v1/notifications/<notification_id>/response",
            ("POST",),
        ),
        (
            "notification_read_state",
            "/api/v1/notifications/<notification_id>/read-state",
            ("PATCH",),
        ),
        ("read_state", "/api/v1/read-state", ("POST",)),
        ("domains", "/api/v1/domains", ("GET",)),
        ("snapshot", "/api/v1/snapshot", ("GET",)),
        ("events", "/api/v1/events", ("GET",)),
    )
    for endpoint, rule, methods, view in implemented_routes:
        app.add_url_rule(rule, endpoint, view, methods=list(methods))
    for endpoint, rule, methods in stub_routes:
        app.add_url_rule(rule, endpoint, _stub(endpoint), methods=list(methods))

    @app.errorhandler(BadRequest)
    @app.errorhandler(UnsupportedMediaType)
    def malformed_input(error: BadRequest | UnsupportedMediaType) -> tuple[Response, int]:
        return _error("malformed_input", error.description, 400)

    @app.errorhandler(ValidationError)
    def invalid_data(error: ValidationError) -> tuple[Response, int]:
        return _error("invalid_data", str(error), 422)

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
        return response

    return app
