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
        ("/api/v1/read-state", "POST"),
        ("/api/v1/domains", "GET"),
        ("/api/v1/snapshot", "GET"),
        ("/api/v1/events", "GET"),
    }


def test_remaining_stub_uses_error_envelope_and_echoes_request_id(client) -> None:
    response = client.post("/api/v1/auth/nonces", headers={"X-Request-ID": "request-123"})
    assert response.status_code == 501
    assert response.content_type == "application/json"
    assert response.headers["X-Request-ID"] == "request-123"
    assert response.get_json() == {
        "error": {
            "code": "not_implemented",
            "message": "This API endpoint has not been implemented",
            "details": {"endpoint": "auth_nonces"},
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


def test_response_is_validated_committed_and_idempotent(client) -> None:
    payload = notification()
    payload["response_options"] = [
        {
            "id": "deny",
            "label": "Deny",
            "message_mode": "required",
            "appearance": "danger",
        }
    ]
    client.post("/api/v1/notifications", json=payload)
    url = f"/api/v1/notifications/{payload['id']}/response"
    request_id = str(uuid.uuid4())

    invalid = client.post(
        url, json={"request_id": request_id, "option_id": "deny", "message": None}
    )
    assert invalid.status_code == 422

    body = {
        "request_id": request_id,
        "option_id": "deny",
        "message": "Use the sandbox instead.",
    }
    first = client.post(url, json=body)
    retry = client.post(url, json=body)
    assert first.status_code == 200
    assert retry.get_json() == first.get_json()
    assert first.get_json()["notification"]["response"] == {
        "request_id": request_id,
        "option_id": "deny",
        "message": "Use the sandbox instead.",
        "responded_at": first.get_json()["notification"]["response"]["responded_at"],
        "responded_by": "unauthenticated-client",
    }

    conflict = client.post(
        url,
        json={"request_id": str(uuid.uuid4()), "option_id": "deny", "message": "Different"},
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["error"]["code"] == "already_answered"
    assert (
        conflict.get_json()["error"]["details"]["notification"]["response"]
        == first.get_json()["notification"]["response"]
    )


def test_read_state_handles_one_or_many_ids_and_emits_only_for_changes(client) -> None:
    first = notification(options=False)
    second = notification(options=False)
    client.post("/api/v1/notifications", json=first)
    client.post("/api/v1/notifications", json=second)

    single = client.post(
        "/api/v1/read-state",
        json={"notification_ids": [first["id"]], "read": True},
    )
    assert single.status_code == 200
    assert single.get_json()["notifications"][0]["read_at"] is not None
    assert single.get_json()["event_seq"] is not None
    single_event = client.get(
        "/api/v1/events", query_string={"after": single.get_json()["event_seq"] - 1}
    ).get_json()["events"][0]
    assert single_event["type"] == "notifications.read_state_changed"
    assert single_event["notification_ids"] == [first["id"]]

    unchanged = client.post(
        "/api/v1/read-state",
        json={"notification_ids": [first["id"]], "read": True},
    )
    assert unchanged.status_code == 200
    assert unchanged.get_json()["event_seq"] is None

    bulk = client.post(
        "/api/v1/read-state",
        json={"notification_ids": [first["id"], second["id"]], "read": False},
    )
    assert bulk.status_code == 200
    assert [item["id"] for item in bulk.get_json()["notifications"]] == [
        first["id"],
        second["id"],
    ]
    assert all(item["read_at"] is None for item in bulk.get_json()["notifications"])
    event = client.get(
        "/api/v1/events", query_string={"after": bulk.get_json()["event_seq"] - 1}
    ).get_json()["events"][0]
    assert event["type"] == "notifications.read_state_changed"
    assert event["notification_ids"] == [first["id"]]
    assert event["read_at"] is None


@pytest.mark.parametrize(
    ("path", "method", "body"),
    [
        (
            "/api/v1/notifications/missing/response",
            "post",
            {"request_id": 1, "option_id": "approve"},
        ),
        ("/api/v1/read-state", "post", {"notification_ids": ["missing"], "read": "true"}),
        ("/api/v1/read-state", "post", {"notification_ids": "missing", "read": True}),
        ("/api/v1/read-state", "post", {"notification_ids": [], "read": True}),
    ],
)
def test_write_routes_reject_invalid_bodies(client, path, method, body) -> None:
    response = getattr(client, method)(path, json=body)
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "invalid_data"


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


def test_read_notification_list_filters_and_paginates(client) -> None:
    first = notification(options=False)
    first["domain"] = "alpha"
    first["sender"] = "one"
    first["tags"] = ["shared", "first"]
    second = notification()
    second["domain"] = "beta"
    second["sender"] = "two"
    second["tags"] = ["shared", "second"]
    client.post("/api/v1/notifications", json=first)
    client.post("/api/v1/notifications", json=second)

    page_one = client.get("/api/v1/notifications?order=asc&limit=1").get_json()
    assert len(page_one["items"]) == 1
    assert page_one["next_cursor"] is not None
    page_two = client.get(
        "/api/v1/notifications",
        query_string={"order": "asc", "limit": 1, "cursor": page_one["next_cursor"]},
    ).get_json()
    assert len(page_two["items"]) == 1
    assert page_two["next_cursor"] is None
    assert {page_one["items"][0]["id"], page_two["items"][0]["id"]} == {
        first["id"],
        second["id"],
    }

    filtered = client.get(
        "/api/v1/notifications?domain=beta&sender=two&unread=true&response_state=pending"
        "&tag=shared&tag=second"
    )
    assert [item["id"] for item in filtered.get_json()["items"]] == [second["id"]]

    cursor_mismatch = client.get(
        "/api/v1/notifications",
        query_string={"order": "desc", "limit": 1, "cursor": page_one["next_cursor"]},
    )
    assert cursor_mismatch.status_code == 422


def test_get_domains_snapshot_and_events(client) -> None:
    payload = notification()
    created = client.post("/api/v1/notifications", json=payload).get_json()

    fetched = client.get(f"/api/v1/notifications/{payload['id']}")
    assert fetched.status_code == 200
    assert fetched.get_json()["notification"] == created["notification"]

    domains = client.get("/api/v1/domains").get_json()["items"]
    assert domains[0]["name"] == payload["domain"]
    assert domains[0]["notification_count"] == 1

    snapshot = client.get("/api/v1/snapshot").get_json()
    assert snapshot["sequence"] == created["event_seq"]
    assert snapshot["notifications"] == [created["notification"]]
    assert snapshot["domains"] == domains

    events = client.get("/api/v1/events?after=0&limit=1").get_json()
    assert events == {
        "events": [
            {
                "seq": created["event_seq"],
                "type": "notification.created",
                "occurred_at": created["notification"]["created_at"],
                "notification": created["notification"],
            }
        ],
        "last_sequence": created["event_seq"],
        "has_more": False,
    }


def test_read_routes_reject_unknown_or_invalid_query_values(client) -> None:
    assert client.get("/api/v1/domains?extra=true").status_code == 422
    assert client.get("/api/v1/events?after=-1").status_code == 422
    assert client.get("/api/v1/events?wait_seconds=01").status_code == 422
    assert client.get("/api/v1/notifications?unread=yes").status_code == 422
    assert client.get("/api/v1/notifications?created_after=yesterday").status_code == 422


def test_waiting_events_wakes_after_create(app) -> None:
    started = threading.Event()
    result: list[dict[str, object]] = []

    def wait_for_event() -> None:
        with app.test_client() as waiting_client:
            started.set()
            response = waiting_client.get("/api/v1/events?after=0&wait_seconds=2")
            result.append(response.get_json())

    waiter = threading.Thread(target=wait_for_event)
    waiter.start()
    assert started.wait(1)
    time.sleep(0.05)
    with app.test_client() as creating_client:
        creating_client.post("/api/v1/notifications", json=notification())
    waiter.join(1)

    assert not waiter.is_alive()
    assert result[0]["events"][0]["type"] == "notification.created"


def test_events_reports_an_expired_cursor(app, client) -> None:
    first = client.post("/api/v1/notifications", json=notification()).get_json()
    client.post("/api/v1/notifications", json=notification())
    repository = app.extensions["notification_hub_repository"]
    with repository.database.connection() as connection:
        connection.execute("DELETE FROM events WHERE seq = ?", (first["event_seq"],))

    response = client.get("/api/v1/events?after=0")
    assert response.status_code == 409
    assert response.get_json() == {
        "error": {
            "code": "cursor_expired",
            "message": "event cursor predates retained history",
            "details": {"reset_required": True},
        }
    }
