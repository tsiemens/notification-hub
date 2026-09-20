from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlencode

import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from notification_hub.config import (
    AuthConfig,
    ConfigurationError,
    LimitsConfig,
    RetentionConfig,
    ServerConfig,
    SigningKey,
)
from notification_hub.domain import (
    CreateNotification,
    ResponseOption,
    ResponseState,
    format_timestamp,
)
from notification_hub.server import create_app
from notification_hub.server.app import _CleanupWorker
from notification_hub.storage import Database, NotificationRepository


@pytest.fixture
def app(tmp_path: Path):
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key_path = tmp_path / "client.pub"
    public_key_path.write_bytes(
        private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    config = ServerConfig(
        database=tmp_path / "hub.sqlite3",
        auth=AuthConfig(
            signing_keys=(
                SigningKey(
                    "desktop-ui",
                    "desktop-ui",
                    public_key_path,
                    frozenset({"read", "respond", "read_state"}),
                ),
            )
        ),
    )
    result = create_app(config)
    result.extensions["test_private_key"] = private_key
    yield result
    worker = result.extensions.get("notification_hub_cleanup_worker")
    if worker is not None:
        worker.stop(join=True)


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


def signed_request(
    client,
    method: str,
    path: str,
    *,
    body: dict[str, object] | None = None,
    query_string: dict[str, object] | None = None,
    nonce: str | None = None,
    private_key=None,
    key_id: str = "desktop-ui",
    created: int | None = None,
):
    if query_string:
        path = f"{path}?{urlencode(query_string, doseq=True)}"
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    headers: dict[str, str] = {}
    components = ["@method", "@target-uri"]
    if data is not None:
        headers["Content-Type"] = "application/json"
        digest = base64.b64encode(hashlib.sha256(data).digest()).decode()
        headers["Content-Digest"] = f"sha-256=:{digest}:"
        components.extend(["content-type", "content-digest"])
    timestamp = int(time.time()) if created is None else created
    params = (
        f"({' '.join(json.dumps(item) for item in components)})"
        f';created={timestamp};expires={timestamp + 60};keyid="{key_id}"'
    )
    if nonce is not None:
        params += f';nonce="{nonce}"'
    params += ';tag="notification-hub-v1"'
    lines = []
    for component in components:
        if component == "@method":
            value = method.upper()
        elif component == "@target-uri":
            value = f"http://localhost{path}"
        else:
            value = headers[component.title()]
        lines.append(f'"{component}": {value}')
    lines.append(f'"@signature-params": {params}')
    signing_key = private_key or client.application.extensions["test_private_key"]
    signature = base64.b64encode(signing_key.sign("\n".join(lines).encode())).decode()
    headers["Signature-Input"] = f"sig1={params}"
    headers["Signature"] = f"sig1=:{signature}:"
    return client.open(path, method=method.upper(), data=data, headers=headers)


def issue_nonce(
    client,
    *,
    request_id: str | None = None,
    count: int = 1,
    private_key=None,
    key_id: str = "desktop-ui",
) -> str:
    response = signed_request(
        client,
        "POST",
        "/api/v1/auth/nonces",
        body={"request_id": request_id or str(uuid.uuid4()), "count": count},
        private_key=private_key,
        key_id=key_id,
    )
    assert response.status_code == 200
    return response.get_json()["nonces"][0]["value"]


def signed_mutation(client, path: str, body: dict[str, object]):
    return signed_request(client, "POST", path, body=body, nonce=issue_nonce(client))


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


def test_signing_key_files_are_validated_at_app_startup(tmp_path: Path) -> None:
    config = ServerConfig(
        database=tmp_path / "unused.sqlite3",
        auth=AuthConfig(
            signing_keys=(
                SigningKey(
                    "broken",
                    "broken",
                    tmp_path / "missing.pub",
                    frozenset({"read"}),
                ),
            )
        ),
    )
    with pytest.raises(ConfigurationError, match="could not load public key"):
        create_app(config)


def test_signed_endpoint_rejects_missing_signature_and_echoes_request_id(client) -> None:
    response = client.post("/api/v1/auth/nonces", headers={"X-Request-ID": "request-123"})
    assert response.status_code == 401
    assert response.content_type == "application/json"
    assert response.headers["X-Request-ID"] == "request-123"
    assert response.get_json() == {
        "error": {
            "code": "authentication_failed",
            "message": "a message signature is required",
            "details": {},
        }
    }


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/api/v1/notifications"),
        ("GET", "/api/v1/domains"),
        ("GET", "/api/v1/snapshot"),
        ("GET", "/api/v1/events"),
        ("POST", "/api/v1/read-state"),
        ("POST", "/api/v1/notifications/missing/response"),
    ],
)
def test_all_client_routes_require_a_signature(client, method: str, path: str) -> None:
    assert client.open(path, method=method).status_code == 401


def test_nonce_batch_is_idempotent_and_count_is_bound_to_request_id(client) -> None:
    request_id = str(uuid.uuid4())
    body = {"request_id": request_id, "count": 2}
    first = signed_request(client, "POST", "/api/v1/auth/nonces", body=body)
    retry = signed_request(client, "POST", "/api/v1/auth/nonces", body=body)
    assert first.status_code == 200
    assert retry.get_json() == first.get_json()
    assert len(first.get_json()["nonces"]) == 2

    conflict = signed_request(
        client,
        "POST",
        "/api/v1/auth/nonces",
        body={"request_id": request_id, "count": 1},
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["error"]["code"] == "idempotency_conflict"


def test_signature_expiry_bad_signature_and_body_digest_are_rejected(client) -> None:
    expired = signed_request(
        client,
        "GET",
        "/api/v1/snapshot",
        created=int(time.time()) - 120,
    )
    assert expired.status_code == 401

    wrong_key = signed_request(
        client,
        "GET",
        "/api/v1/snapshot",
        private_key=ed25519.Ed25519PrivateKey.generate(),
    )
    assert wrong_key.status_code == 401

    valid = signed_request(
        client,
        "POST",
        "/api/v1/auth/nonces",
        body={"request_id": str(uuid.uuid4()), "count": 1},
    )
    headers = {
        "Content-Type": "application/json",
        "Content-Digest": valid.request.headers["Content-Digest"],
        "Signature-Input": valid.request.headers["Signature-Input"],
        "Signature": valid.request.headers["Signature"],
    }
    tampered = client.post(
        "/api/v1/auth/nonces",
        data=b'{"request_id":"tampered","count":1}',
        headers=headers,
    )
    assert tampered.status_code == 401


def test_mutation_nonce_is_single_use(client) -> None:
    payload = notification(options=False)
    client.post("/api/v1/notifications", json=payload)
    body = {"notification_ids": [payload["id"]], "read": True}
    nonce = issue_nonce(client)
    first = signed_request(client, "POST", "/api/v1/read-state", body=body, nonce=nonce)
    replay = signed_request(client, "POST", "/api/v1/read-state", body=body, nonce=nonce)
    assert first.status_code == 200
    assert replay.status_code == 401
    assert "already used" in replay.get_json()["error"]["message"]


def test_concurrent_nonce_replay_allows_one_mutation(app, client) -> None:
    payload = notification(options=False)
    client.post("/api/v1/notifications", json=payload)
    body = {"notification_ids": [payload["id"]], "read": True}
    nonce = issue_nonce(client)
    barrier = threading.Barrier(2)
    statuses: list[int] = []

    def mutate() -> None:
        with app.test_client() as thread_client:
            barrier.wait()
            response = signed_request(
                thread_client, "POST", "/api/v1/read-state", body=body, nonce=nonce
            )
            statuses.append(response.status_code)

    threads = [threading.Thread(target=mutate) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(statuses) == [200, 401]


def test_signed_request_log_excludes_credentials_and_body(app, client, caplog) -> None:
    payload = notification()
    payload["response_options"][0]["message_mode"] = "optional"
    client.post("/api/v1/notifications", json=payload)
    nonce = issue_nonce(client)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=app.logger.name):
        response = signed_request(
            client,
            "POST",
            f"/api/v1/notifications/{payload['id']}/response",
            body={
                "request_id": str(uuid.uuid4()),
                "option_id": "approve",
                "message": "private response text",
            },
            nonce=nonce,
        )
    assert response.status_code == 200
    assert "principal='desktop-ui'" in caplog.text
    assert nonce not in caplog.text
    assert "private response text" not in caplog.text
    assert response.request.headers["Signature"] not in caplog.text


def test_business_conflict_still_consumes_nonce(client) -> None:
    payload = notification()
    client.post("/api/v1/notifications", json=payload)
    url = f"/api/v1/notifications/{payload['id']}/response"
    first = {
        "request_id": str(uuid.uuid4()),
        "option_id": "approve",
        "message": None,
    }
    assert signed_mutation(client, url, first).status_code == 200

    conflicting = {**first, "request_id": str(uuid.uuid4())}
    nonce = issue_nonce(client)
    conflict = signed_request(client, "POST", url, body=conflicting, nonce=nonce)
    replay = signed_request(client, "POST", url, body=conflicting, nonce=nonce)
    assert conflict.status_code == 409
    assert replay.status_code == 401


def test_scopes_and_nonce_key_binding_are_enforced(tmp_path: Path) -> None:
    read_key = ed25519.Ed25519PrivateKey.generate()
    respond_key = ed25519.Ed25519PrivateKey.generate()
    signing_keys = []
    for key_id, private_key, scopes in (
        ("reader", read_key, frozenset({"read"})),
        ("responder", respond_key, frozenset({"respond"})),
    ):
        path = tmp_path / f"{key_id}.pub"
        path.write_bytes(
            private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        )
        signing_keys.append(SigningKey(key_id, key_id, path, scopes))
    test_app = create_app(
        ServerConfig(
            database=tmp_path / "scopes.sqlite3",
            auth=AuthConfig(signing_keys=tuple(signing_keys)),
        )
    )
    test_app.extensions["test_private_key"] = read_key
    scoped_client = test_app.test_client()

    denied = signed_request(
        scoped_client,
        "GET",
        "/api/v1/snapshot",
        private_key=respond_key,
        key_id="responder",
    )
    assert denied.status_code == 403

    payload = notification()
    scoped_client.post("/api/v1/notifications", json=payload)
    reader_nonce = issue_nonce(scoped_client, private_key=read_key, key_id="reader")
    cross_key = signed_request(
        scoped_client,
        "POST",
        f"/api/v1/notifications/{payload['id']}/response",
        body={"request_id": str(uuid.uuid4()), "option_id": "approve", "message": None},
        nonce=reader_nonce,
        private_key=respond_key,
        key_id="responder",
    )
    assert cross_key.status_code == 401


def test_health_initializes_and_checks_the_database(client) -> None:
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}


def test_startup_cleanup_expires_pending_notifications(tmp_path: Path) -> None:
    database = Database(tmp_path / "startup-cleanup.sqlite3")
    database.initialize()
    repository = NotificationRepository(database)
    created = repository.create(
        CreateNotification(
            str(uuid.uuid4()),
            "build-container",
            "codex",
            "Stale approval",
            response_options=(ResponseOption("approve", "Approve"),),
        ),
        now=datetime.now(UTC) - timedelta(days=2),
    )

    test_app = create_app(
        ServerConfig(database=database.path, retention=RetentionConfig(max_pending_days=1)),
        repository=repository,
    )
    try:
        assert repository.get(created.notification.id).response_state is ResponseState.EXPIRED
    finally:
        test_app.extensions["notification_hub_cleanup_worker"].stop(join=True)


def test_cleanup_worker_repeats_after_startup() -> None:
    repeated = threading.Event()
    repository = Mock()

    def cleanup(_retention) -> None:
        if repository.cleanup.call_count >= 2:
            repeated.set()

    repository.cleanup.side_effect = cleanup
    worker = _CleanupWorker(
        RetentionConfig(),
        threading.Condition(),
        logging.getLogger(__name__),
        interval_seconds=0.01,
    )
    worker.start(repository)
    try:
        assert repeated.wait(1)
    finally:
        worker.stop(join=True)

    assert repository.cleanup.call_count >= 2


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

    invalid = signed_mutation(
        client,
        url,
        {"request_id": request_id, "option_id": "deny", "message": None},
    )
    assert invalid.status_code == 422

    body = {
        "request_id": request_id,
        "option_id": "deny",
        "message": "Use the sandbox instead.",
    }
    first = signed_mutation(client, url, body)
    retry = signed_mutation(client, url, body)
    assert first.status_code == 200
    assert retry.get_json() == first.get_json()
    assert first.get_json()["notification"]["response"] == {
        "request_id": request_id,
        "option_id": "deny",
        "message": "Use the sandbox instead.",
        "responded_at": first.get_json()["notification"]["response"]["responded_at"],
        "responded_by": "desktop-ui",
    }

    conflict = signed_mutation(
        client,
        url,
        {"request_id": str(uuid.uuid4()), "option_id": "deny", "message": "Different"},
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

    single = signed_mutation(
        client,
        "/api/v1/read-state",
        {"notification_ids": [first["id"]], "read": True},
    )
    assert single.status_code == 200
    assert single.get_json()["notifications"][0]["read_at"] is not None
    assert single.get_json()["event_seq"] is not None
    single_event = signed_request(
        client,
        "GET",
        "/api/v1/events",
        query_string={"after": single.get_json()["event_seq"] - 1},
    ).get_json()["events"][0]
    assert single_event["type"] == "notifications.read_state_changed"
    assert single_event["notification_ids"] == [first["id"]]

    unchanged = signed_mutation(
        client,
        "/api/v1/read-state",
        {"notification_ids": [first["id"]], "read": True},
    )
    assert unchanged.status_code == 200
    assert unchanged.get_json()["event_seq"] is None

    bulk = signed_mutation(
        client,
        "/api/v1/read-state",
        {"notification_ids": [first["id"], second["id"]], "read": False},
    )
    assert bulk.status_code == 200
    assert [item["id"] for item in bulk.get_json()["notifications"]] == [
        first["id"],
        second["id"],
    ]
    assert all(item["read_at"] is None for item in bulk.get_json()["notifications"])
    event = signed_request(
        client,
        "GET",
        "/api/v1/events",
        query_string={"after": bulk.get_json()["event_seq"] - 1},
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
    assert method == "post"
    response = signed_mutation(client, path, body)
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

    page_one = signed_request(client, "GET", "/api/v1/notifications?order=asc&limit=1").get_json()
    assert len(page_one["items"]) == 1
    assert page_one["next_cursor"] is not None
    page_two = signed_request(
        client,
        "GET",
        "/api/v1/notifications",
        query_string={"order": "asc", "limit": 1, "cursor": page_one["next_cursor"]},
    ).get_json()
    assert len(page_two["items"]) == 1
    assert page_two["next_cursor"] is None
    assert {page_one["items"][0]["id"], page_two["items"][0]["id"]} == {
        first["id"],
        second["id"],
    }

    filtered = signed_request(
        client,
        "GET",
        "/api/v1/notifications?domain=beta&sender=two&unread=true&response_state=pending"
        "&tag=shared&tag=second",
    )
    assert [item["id"] for item in filtered.get_json()["items"]] == [second["id"]]

    cursor_mismatch = signed_request(
        client,
        "GET",
        "/api/v1/notifications",
        query_string={"order": "desc", "limit": 1, "cursor": page_one["next_cursor"]},
    )
    assert cursor_mismatch.status_code == 422


def test_get_domains_snapshot_and_events(client) -> None:
    payload = notification()
    created = client.post("/api/v1/notifications", json=payload).get_json()

    fetched = signed_request(client, "GET", f"/api/v1/notifications/{payload['id']}")
    assert fetched.status_code == 200
    assert fetched.get_json()["notification"] == created["notification"]

    domains = signed_request(client, "GET", "/api/v1/domains").get_json()["items"]
    assert domains[0]["name"] == payload["domain"]
    assert domains[0]["notification_count"] == 1

    snapshot = signed_request(client, "GET", "/api/v1/snapshot").get_json()
    assert snapshot["sequence"] == created["event_seq"]
    assert snapshot["notifications"] == [created["notification"]]
    assert snapshot["domains"] == domains

    events = signed_request(client, "GET", "/api/v1/events?after=0&limit=1").get_json()
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
    assert signed_request(client, "GET", "/api/v1/domains?extra=true").status_code == 422
    assert signed_request(client, "GET", "/api/v1/events?after=-1").status_code == 422
    assert signed_request(client, "GET", "/api/v1/events?wait_seconds=01").status_code == 422
    assert signed_request(client, "GET", "/api/v1/notifications?unread=yes").status_code == 422
    assert (
        signed_request(client, "GET", "/api/v1/notifications?created_after=yesterday").status_code
        == 422
    )


def test_waiting_events_wakes_after_create(app) -> None:
    started = threading.Event()
    result: list[dict[str, object]] = []

    def wait_for_event() -> None:
        with app.test_client() as waiting_client:
            started.set()
            response = signed_request(
                waiting_client, "GET", "/api/v1/events?after=0&wait_seconds=2"
            )
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


def test_cleanup_expiry_wakes_outcome_and_event_waiters(app, client) -> None:
    payload = notification()
    created = client.post("/api/v1/notifications", json=payload).get_json()
    repository = app.extensions["notification_hub_repository"]
    with repository.database.connection() as connection:
        connection.execute(
            "UPDATE notifications SET created_at = ? WHERE id = ?",
            (format_timestamp(datetime.now(UTC) - timedelta(days=8)), payload["id"]),
        )

    started = threading.Barrier(3)
    outcome_result: list[dict[str, object]] = []
    event_result: list[dict[str, object]] = []

    def wait_for_outcome() -> None:
        with app.test_client() as waiting_client:
            started.wait()
            response = waiting_client.get(
                f"/api/v1/notifications/{payload['id']}/outcome?wait_seconds=2"
            )
            outcome_result.append(response.get_json())

    def wait_for_event() -> None:
        with app.test_client() as waiting_client:
            started.wait()
            response = signed_request(
                waiting_client,
                "GET",
                f"/api/v1/events?after={created['event_seq']}&wait_seconds=2",
            )
            event_result.append(response.get_json())

    outcome_waiter = threading.Thread(target=wait_for_outcome)
    event_waiter = threading.Thread(target=wait_for_event)
    outcome_waiter.start()
    event_waiter.start()
    started.wait()
    time.sleep(0.05)

    app.extensions["notification_hub_cleanup_worker"].run_once()
    outcome_waiter.join(1)
    event_waiter.join(1)

    assert not outcome_waiter.is_alive()
    assert not event_waiter.is_alive()
    assert outcome_result[0]["state"] == "expired"
    assert event_result[0]["events"][0]["type"] == "notification.updated"
    assert event_result[0]["events"][0]["notification"]["response_state"] == "expired"


def test_events_reports_an_expired_cursor(app, client) -> None:
    first = client.post("/api/v1/notifications", json=notification()).get_json()
    client.post("/api/v1/notifications", json=notification())
    repository = app.extensions["notification_hub_repository"]
    with repository.database.connection() as connection:
        connection.execute("DELETE FROM events WHERE seq = ?", (first["event_seq"],))

    response = signed_request(client, "GET", "/api/v1/events?after=0")
    assert response.status_code == 409
    assert response.get_json() == {
        "error": {
            "code": "cursor_expired",
            "message": "event cursor predates retained history",
            "details": {"reset_required": True},
        }
    }
