from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

import pytest

from notification_hub.config import LimitsConfig, ServerConfig
from notification_hub.server import create_app


@pytest.fixture
def app(tmp_path: Path):
    config = ServerConfig(database=tmp_path / "hub.sqlite3")
    return create_app(config)


@pytest.fixture
def client(app):
    return app.test_client()


def notification(*, options: bool = True) -> dict[str, object]:
    return {
        "id": str(uuid.uuid4()),
        "domain": "build-container",
        "sender": "codex",
        "summary": "Approval required",
        "message": "Run the test suite?",
        "tags": ["task:test"],
        "response_options": (
            [
                {
                    "id": "approve",
                    "label": "Approve",
                    "message_mode": "none",
                    "appearance": "primary",
                }
            ]
            if options
            else []
        ),
    }


def test_complete_v1_route_surface_is_registered() -> None:
    app = create_app()
    routes = {
        (rule.rule, method)
        for rule in app.url_map.iter_rules()
        for method in rule.methods
        if method not in {"HEAD", "OPTIONS"} and rule.endpoint != "static"
    }
    assert routes == {
        ("/healthz", "GET"),
        ("/api/v1/auth/nonces", "POST"),
        ("/api/v1/notifications", "POST"),
        ("/api/v1/notifications", "GET"),
        ("/api/v1/notifications/<notification_id>", "GET"),
        ("/api/v1/notifications/<notification_id>/cancel", "POST"),
        ("/api/v1/notifications/<notification_id>/outcome", "GET"),
        ("/api/v1/notifications/<notification_id>/response", "POST"),
        ("/api/v1/notifications/<notification_id>/read-state", "PATCH"),
        ("/api/v1/read-state", "POST"),
        ("/api/v1/domains", "GET"),
        ("/api/v1/snapshot", "GET"),
        ("/api/v1/events", "GET"),
    }


def test_stub_uses_error_envelope_and_echoes_request_id(client) -> None:
    response = client.get("/api/v1/notifications/example", headers={"X-Request-ID": "request-123"})
    assert response.status_code == 501
    assert response.content_type == "application/json"
    assert response.headers["X-Request-ID"] == "request-123"
    assert response.get_json() == {
        "error": {
            "code": "not_implemented",
            "message": "This API endpoint has not been implemented",
            "details": {"endpoint": "notification_get"},
        }
    }


def test_health_initializes_and_checks_the_database(client) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_create_retry_is_idempotent(client) -> None:
    payload = notification()
    first = client.post("/api/v1/notifications", json=payload)
    retry = client.post("/api/v1/notifications", json=payload)
    assert first.status_code == 201
    assert retry.status_code == 200
    assert first.get_json() == retry.get_json()
    assert first.get_json()["notification"]["response_state"] == "pending"


def test_create_rejects_unknown_fields_and_changed_retry(client) -> None:
    payload = notification()
    invalid = {**payload, "created_at": "2026-09-20T00:00:00.000Z"}
    response = client.post("/api/v1/notifications", json=invalid)
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "invalid_data"

    assert client.post("/api/v1/notifications", json=payload).status_code == 201
    changed = {**payload, "summary": "Different"}
    conflict = client.post("/api/v1/notifications", json=changed)
    assert conflict.status_code == 409
    assert conflict.get_json()["error"]["code"] == "idempotency_conflict"


def test_outcome_and_cancel(client) -> None:
    payload = notification()
    client.post("/api/v1/notifications", json=payload)
    outcome_url = f"/api/v1/notifications/{payload['id']}/outcome"
    pending = client.get(outcome_url)
    assert pending.get_json() == {
        "notification_id": payload["id"],
        "state": "pending",
        "response": None,
    }

    cancel_url = f"/api/v1/notifications/{payload['id']}/cancel"
    cancelled = client.post(cancel_url, json={"reason": "No longer needed"})
    retry = client.post(cancel_url, json={"reason": "No longer needed"})
    assert cancelled.status_code == 200
    assert cancelled.get_json() == retry.get_json()
    assert client.get(outcome_url).get_json()["state"] == "cancelled"


def test_outcome_validates_wait_and_unknown_notification(client) -> None:
    notification_id = str(uuid.uuid4())
    invalid = client.get(f"/api/v1/notifications/{notification_id}/outcome?wait_seconds=31")
    assert invalid.status_code == 422
    missing = client.get(f"/api/v1/notifications/{notification_id}/outcome")
    assert missing.status_code == 404
    assert missing.get_json()["error"]["details"]["notification_id"] == notification_id


def test_waiting_outcome_wakes_after_cancellation(app, client) -> None:
    payload = notification()
    client.post("/api/v1/notifications", json=payload)
    outcome_url = f"/api/v1/notifications/{payload['id']}/outcome?wait_seconds=2"
    cancel_url = f"/api/v1/notifications/{payload['id']}/cancel"
    started = threading.Event()
    result: list[tuple[int, dict[str, object]]] = []

    def wait_for_outcome() -> None:
        with app.test_client() as waiting_client:
            started.set()
            response = waiting_client.get(outcome_url)
            result.append((response.status_code, response.get_json()))

    waiter = threading.Thread(target=wait_for_outcome)
    waiter.start()
    assert started.wait(1)
    time.sleep(0.05)
    client.post(cancel_url)
    waiter.join(1)

    assert not waiter.is_alive()
    assert result[0][0] == 200
    assert result[0][1]["state"] == "cancelled"


def test_create_limits_return_rate_limited(tmp_path: Path) -> None:
    config = ServerConfig(
        database=tmp_path / "limited.sqlite3",
        limits=LimitsConfig(creates_per_minute=1, pending_total=1),
    )
    client = create_app(config).test_client()
    assert client.post("/api/v1/notifications", json=notification()).status_code == 201
    response = client.post("/api/v1/notifications", json=notification())
    assert response.status_code == 429
    assert response.get_json()["error"]["code"] == "rate_limited"
