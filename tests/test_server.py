from __future__ import annotations

import base64
import hashlib
import json
import logging
import signal
import threading
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock
from urllib.parse import urlencode

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
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
from notification_hub.server.runner import serve
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
    components: list[str] | None = None,
    signature_params_suffix: str = "",
):
    if query_string:
        path = f"{path}?{urlencode(query_string, doseq=True)}"
    data = None if body is None else json.dumps(body, separators=(",", ":")).encode()
    headers: dict[str, str] = {}
    covered_components = components or ["@method", "@target-uri"]
    if data is not None:
        headers["Content-Type"] = "application/json"
        digest = base64.b64encode(hashlib.sha256(data).digest()).decode()
        headers["Content-Digest"] = f"sha-256=:{digest}:"
        if components is None:
            covered_components.extend(["content-type", "content-digest"])
    timestamp = int(time.time()) if created is None else created
    params = (
        f"({' '.join(json.dumps(item) for item in covered_components)})"
        f';created={timestamp};expires={timestamp + 60};keyid="{key_id}"'
    )
    if nonce is not None:
        params += f';nonce="{nonce}"'
    params += f';tag="notification-hub-v1"{signature_params_suffix}'
    lines = []
    for component in covered_components:
        if component == "@method":
            value = method.upper()
        elif component == "@target-uri":
            value = f"http://localhost{path}"
        else:
            value = headers[component.title()]
        lines.append(f'"{component}": {value}')
    lines.append(f'"@signature-params": {params}')
    signing_key = private_key or client.application.extensions["test_private_key"]
    signature_base = "\n".join(lines).encode()
    if isinstance(signing_key, ed25519.Ed25519PrivateKey):
        signature_bytes = signing_key.sign(signature_base)
    elif isinstance(signing_key, rsa.RSAPrivateKey):
        signature_bytes = signing_key.sign(
            signature_base,
            padding.PSS(mgf=padding.MGF1(hashes.SHA512()), salt_length=64),
            hashes.SHA512(),
        )
    else:
        digest_algorithm = (
            hashes.SHA256() if isinstance(signing_key.curve, ec.SECP256R1) else hashes.SHA384()
        )
        der_signature = signing_key.sign(signature_base, ec.ECDSA(digest_algorithm))
        r, s = decode_dss_signature(der_signature)
        coordinate_size = (signing_key.curve.key_size + 7) // 8
        signature_bytes = r.to_bytes(coordinate_size, "big") + s.to_bytes(coordinate_size, "big")
    signature = base64.b64encode(signature_bytes).decode()
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
    assert single_event["versions"] == [single.get_json()["notifications"][0]["version"]]

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
    assert event["versions"] == [bulk.get_json()["notifications"][0]["version"]]
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


def test_large_snapshot_is_size_bounded_and_stable_across_pages(app) -> None:
    paged_app = create_app(
        app.config["NOTIFICATION_HUB_CONFIG"],
        repository=app.extensions["notification_hub_start"](),
        snapshot_max_bytes=1_600,
    )
    paged_app.extensions["test_private_key"] = app.extensions["test_private_key"]
    expected_ids = []
    with paged_app.test_client() as paged_client:
        for index in range(6):
            payload = notification()
            payload["summary"] = f"Snapshot item {index}"
            payload["message"] = "x" * 500
            expected_ids.append(payload["id"])
            paged_client.post("/api/v1/notifications", json=payload)

        response = signed_request(paged_client, "GET", "/api/v1/snapshot")
        assert response.status_code == 200
        assert len(response.data) <= 1_600
        page = response.get_json()
        assert page["snapshot_token"]
        snapshot_sequence = page["sequence"]
        seen_ids = [item["id"] for item in page["notifications"]]
        seen_domains = page["domains"]

        # The token identifies immutable materialized state, so this later
        # notification must not leak into subsequent pages.
        later = notification()
        paged_client.post("/api/v1/notifications", json=later)

        while page["next_cursor"] is not None:
            response = signed_request(
                paged_client,
                "GET",
                "/api/v1/snapshot",
                query_string={
                    "snapshot_token": page["snapshot_token"],
                    "cursor": page["next_cursor"],
                },
            )
            assert response.status_code == 200
            assert len(response.data) <= 1_600
            page = response.get_json()
            assert page["sequence"] == snapshot_sequence
            seen_ids.extend(item["id"] for item in page["notifications"])
            seen_domains.extend(page["domains"])

    paged_app.extensions["notification_hub_lifecycle"].stop(join=True)
    assert set(seen_ids) == set(expected_ids)
    assert later["id"] not in seen_ids
    assert len(seen_domains) == 1


def test_snapshot_rejects_invalid_or_expired_pagination_state(app) -> None:
    with app.test_client() as test_client:
        missing_token = signed_request(test_client, "GET", "/api/v1/snapshot?cursor=1")
        expired = signed_request(
            test_client,
            "GET",
            "/api/v1/snapshot?snapshot_token=unknown&cursor=0",
        )
    assert missing_token.status_code == 422
    assert expired.status_code == 409
    assert expired.get_json()["error"]["details"] == {"reset_required": True}


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


def test_application_stop_wakes_long_polls_and_rejects_new_work(app, client) -> None:
    payload = notification()
    created = client.post("/api/v1/notifications", json=payload).get_json()
    started = threading.Barrier(3)
    results: list[tuple[str, int, dict[str, object]]] = []

    def wait_for_outcome() -> None:
        with app.test_client() as waiting_client:
            started.wait()
            response = waiting_client.get(
                f"/api/v1/notifications/{payload['id']}/outcome?wait_seconds=30"
            )
            results.append(("outcome", response.status_code, response.get_json()))

    def wait_for_events() -> None:
        with app.test_client() as waiting_client:
            started.wait()
            response = signed_request(
                waiting_client,
                "GET",
                f"/api/v1/events?after={created['event_seq']}&wait_seconds=30",
            )
            results.append(("events", response.status_code, response.get_json()))

    threads = [threading.Thread(target=wait_for_outcome), threading.Thread(target=wait_for_events)]
    for thread in threads:
        thread.start()
    started.wait()
    time.sleep(0.05)
    app.extensions["notification_hub_lifecycle"].stop()
    for thread in threads:
        thread.join(1)

    assert all(not thread.is_alive() for thread in threads)
    assert {kind for kind, status, _body in results if status == 200} == {"outcome", "events"}
    event_body = next(body for kind, _status, body in results if kind == "events")
    assert event_body["events"] == []
    assert event_body["last_sequence"] == created["event_seq"]
    assert client.get("/healthz").status_code == 503


def test_server_runner_handles_signal_and_closes_listener(tmp_path: Path, monkeypatch) -> None:
    config = ServerConfig(database=tmp_path / "runner.sqlite3")
    handlers = {}

    class FakeServer:
        daemon_threads = True

        def __init__(self) -> None:
            self.shutdown_called = threading.Event()
            self.closed = False

        def serve_forever(self) -> None:
            handlers[signal.SIGTERM](signal.SIGTERM, None)
            assert self.shutdown_called.wait(1)

        def shutdown(self) -> None:
            self.shutdown_called.set()

        def server_close(self) -> None:
            self.closed = True

    fake_server = FakeServer()
    monkeypatch.setattr(
        "notification_hub.server.runner.make_server", lambda *args, **kwargs: fake_server
    )
    monkeypatch.setattr(
        "notification_hub.server.runner.signal.signal",
        lambda signum, handler: handlers.setdefault(signum, handler),
    )

    serve(config)

    assert fake_server.daemon_threads is False
    assert fake_server.shutdown_called.is_set()
    assert fake_server.closed


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


def assert_error_contract(response, status: int, code: str) -> dict[str, object]:
    assert response.status_code == status
    assert response.content_type == "application/json"
    assert set(response.get_json()) == {"error"}
    error = response.get_json()["error"]
    assert set(error) == {"code", "message", "details"}
    assert error["code"] == code
    assert isinstance(error["message"], str) and error["message"]
    assert isinstance(error["details"], dict)
    return error


def test_malformed_json_and_request_media_type_contract(client) -> None:
    malformed = client.post(
        "/api/v1/notifications",
        data=b'{"id":',
        content_type="application/json",
    )
    assert_error_contract(malformed, 400, "malformed_input")

    for content_type in (
        None,
        "text/plain",
        "application/merge-patch+json",
        "application/json; charset=iso-8859-1",
    ):
        response = client.post(
            "/api/v1/notifications",
            data=json.dumps(notification()).encode(),
            content_type=content_type,
        )
        assert_error_contract(response, 400, "malformed_input")

    accepted = client.post(
        "/api/v1/notifications",
        data=json.dumps(notification()).encode(),
        content_type="application/json; charset=UTF-8",
    )
    assert accepted.status_code == 201
    assert accepted.content_type == "application/json"


def test_request_body_limit_is_inclusive_and_checked_before_json_parsing(tmp_path: Path) -> None:
    test_app = create_app(
        ServerConfig(database=tmp_path / "body-limit.sqlite3", request_body_limit_kib=1)
    )
    test_client = test_app.test_client()
    encoded = json.dumps(notification(options=False), separators=(",", ":")).encode()
    boundary_body = encoded + b" " * (1024 - len(encoded))
    assert len(boundary_body) == 1024
    assert (
        test_client.post(
            "/api/v1/notifications", data=boundary_body, content_type="application/json"
        ).status_code
        == 201
    )

    too_large = test_client.post(
        "/api/v1/notifications",
        data=boundary_body + b"{",
        content_type="application/json",
    )
    error = assert_error_contract(too_large, 413, "body_too_large")
    assert error["message"] == "Request body exceeds the configured limit"


def test_unexpected_failure_has_stable_500_contract_and_does_not_spend_nonce(
    app, client, monkeypatch, caplog
) -> None:
    payload = notification(options=False)
    client.post("/api/v1/notifications", json=payload)
    repository = app.extensions["notification_hub_repository"]
    original_event = repository._event

    def fail_event(*_args, **_kwargs):
        raise RuntimeError("injected database failure")

    monkeypatch.setattr(repository, "_event", fail_event)
    nonce = issue_nonce(client)
    body = {"notification_ids": [payload["id"]], "read": True}
    with caplog.at_level(logging.CRITICAL):
        failed = signed_request(client, "POST", "/api/v1/read-state", body=body, nonce=nonce)
    error = assert_error_contract(failed, 500, "internal_error")
    assert error == {
        "code": "internal_error",
        "message": "An unexpected server error occurred",
        "details": {},
    }

    monkeypatch.setattr(repository, "_event", original_event)
    retry = signed_request(client, "POST", "/api/v1/read-state", body=body, nonce=nonce)
    assert retry.status_code == 200
    assert retry.get_json()["notifications"][0]["read_at"] is not None


def test_long_poll_timeout_contracts(app, client) -> None:
    payload = notification()
    created = client.post("/api/v1/notifications", json=payload).get_json()
    results: dict[str, tuple[float, dict[str, object]]] = {}

    def wait_for_outcome() -> None:
        started = time.monotonic()
        with app.test_client() as waiting_client:
            response = waiting_client.get(
                f"/api/v1/notifications/{payload['id']}/outcome?wait_seconds=1"
            )
        results["outcome"] = (time.monotonic() - started, response.get_json())

    def wait_for_events() -> None:
        started = time.monotonic()
        with app.test_client() as waiting_client:
            response = signed_request(
                waiting_client,
                "GET",
                f"/api/v1/events?after={created['event_seq']}&wait_seconds=1",
            )
        results["events"] = (time.monotonic() - started, response.get_json())

    threads = [threading.Thread(target=wait_for_outcome), threading.Thread(target=wait_for_events)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(2)
    assert all(not thread.is_alive() for thread in threads)
    assert results["outcome"][0] >= 0.8
    assert results["outcome"][1] == {
        "notification_id": payload["id"],
        "state": "pending",
        "response": None,
    }
    assert results["events"][0] >= 0.8
    assert results["events"][1] == {
        "events": [],
        "last_sequence": created["event_seq"],
        "has_more": False,
    }


def test_missing_signature_components_and_malformed_parameters(client) -> None:
    missing_body_components = signed_request(
        client,
        "POST",
        "/api/v1/auth/nonces",
        body={"request_id": str(uuid.uuid4()), "count": 1},
        components=["@method", "@target-uri"],
    )
    assert_error_contract(missing_body_components, 401, "authentication_failed")
    assert "required components" in missing_body_components.get_json()["error"]["message"]

    missing_target = signed_request(client, "GET", "/api/v1/snapshot", components=["@method"])
    assert_error_contract(missing_target, 401, "authentication_failed")

    duplicate_parameter = signed_request(
        client,
        "GET",
        "/api/v1/snapshot",
        signature_params_suffix=";created=1",
    )
    assert_error_contract(duplicate_parameter, 401, "authentication_failed")
    assert "must not be repeated" in duplicate_parameter.get_json()["error"]["message"]

    malformed_parameter = client.get(
        "/api/v1/snapshot",
        headers={
            "Signature-Input": 'sig1=("@method" "@target-uri");created="not-an-integer"',
            "Signature": "sig1=:YWJjZA==:",
        },
    )
    assert_error_contract(malformed_parameter, 401, "authentication_failed")


def test_expired_nonce_and_nonce_issuance_limits(tmp_path: Path) -> None:
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key_path = tmp_path / "limited-client.pub"
    public_key_path.write_bytes(
        private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    test_app = create_app(
        ServerConfig(
            database=tmp_path / "nonce-limits.sqlite3",
            auth=AuthConfig(
                nonce_ttl_seconds=300,
                max_outstanding_nonces_per_key=2,
                signing_keys=(
                    SigningKey(
                        "client",
                        "client",
                        public_key_path,
                        frozenset({"read_state"}),
                    ),
                ),
            ),
        )
    )
    test_app.extensions["test_private_key"] = private_key
    test_client = test_app.test_client()
    payload = notification(options=False)
    test_client.post("/api/v1/notifications", json=payload)

    issued = signed_request(
        test_client,
        "POST",
        "/api/v1/auth/nonces",
        body={"request_id": str(uuid.uuid4()), "count": 2},
        private_key=private_key,
        key_id="client",
    )
    assert issued.status_code == 200
    issuance_limited = signed_request(
        test_client,
        "POST",
        "/api/v1/auth/nonces",
        body={"request_id": str(uuid.uuid4()), "count": 1},
        private_key=private_key,
        key_id="client",
    )
    assert_error_contract(issuance_limited, 429, "rate_limited")

    repository = test_app.extensions["notification_hub_repository"]
    old = format_timestamp(datetime.now(UTC) - timedelta(minutes=2))
    expired = format_timestamp(datetime.now(UTC) - timedelta(seconds=1))
    with repository.database.connection() as connection:
        connection.execute("UPDATE auth_nonce_batches SET created_at = ?", (old,))
    outstanding_limited = signed_request(
        test_client,
        "POST",
        "/api/v1/auth/nonces",
        body={"request_id": str(uuid.uuid4()), "count": 1},
        private_key=private_key,
        key_id="client",
    )
    assert_error_contract(outstanding_limited, 429, "rate_limited")
    assert "outstanding" in outstanding_limited.get_json()["error"]["message"]

    with repository.database.connection() as connection:
        connection.execute("UPDATE auth_nonces SET expires_at = ?", (expired,))
        connection.execute("UPDATE auth_nonce_batches SET expires_at = ?", (expired,))
    replacement = issue_nonce(test_client, private_key=private_key, key_id="client")
    assert replacement

    with repository.database.connection() as connection:
        connection.execute(
            "UPDATE auth_nonces SET expires_at = ? WHERE nonce = ?", (expired, replacement)
        )
    denied = signed_request(
        test_client,
        "POST",
        "/api/v1/read-state",
        body={"notification_ids": [payload["id"]], "read": True},
        nonce=replacement,
        private_key=private_key,
        key_id="client",
    )
    assert_error_contract(denied, 401, "authentication_failed")
    assert "expired" in denied.get_json()["error"]["message"]


@pytest.mark.parametrize(
    "private_key",
    [
        pytest.param(rsa.generate_private_key(public_exponent=65537, key_size=2048), id="rsa"),
        pytest.param(ec.generate_private_key(ec.SECP256R1()), id="p256"),
        pytest.param(ec.generate_private_key(ec.SECP384R1()), id="p384"),
    ],
)
def test_supported_rsa_and_ecdsa_signature_paths(tmp_path: Path, private_key) -> None:
    key_id = private_key.__class__.__name__
    public_key_path = tmp_path / f"{key_id}.pub"
    public_key_path.write_bytes(
        private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
    )
    test_app = create_app(
        ServerConfig(
            database=tmp_path / f"{key_id}.sqlite3",
            auth=AuthConfig(
                signing_keys=(
                    SigningKey("crypto-client", key_id, public_key_path, frozenset({"read"})),
                )
            ),
        )
    )
    test_app.extensions["test_private_key"] = private_key
    response = signed_request(
        test_app.test_client(),
        "GET",
        "/api/v1/snapshot",
        private_key=private_key,
        key_id=key_id,
    )
    assert response.status_code == 200


def test_complete_read_respond_and_read_state_scope_matrix(tmp_path: Path) -> None:
    keys: dict[str, object] = {}
    signing_keys = []
    for scope in ("read", "respond", "read_state"):
        private_key = ed25519.Ed25519PrivateKey.generate()
        keys[scope] = private_key
        path = tmp_path / f"{scope}.pub"
        path.write_bytes(
            private_key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo)
        )
        signing_keys.append(SigningKey(scope, scope, path, frozenset({scope})))
    test_app = create_app(
        ServerConfig(
            database=tmp_path / "scope-matrix.sqlite3",
            auth=AuthConfig(signing_keys=tuple(signing_keys)),
        )
    )
    test_app.extensions["test_private_key"] = keys["read"]
    test_client = test_app.test_client()

    read_routes = (
        "/api/v1/notifications",
        "/api/v1/notifications/missing",
        "/api/v1/domains",
        "/api/v1/snapshot",
        "/api/v1/events",
    )
    for scope, private_key in keys.items():
        for path in read_routes:
            response = signed_request(
                test_client, "GET", path, private_key=private_key, key_id=scope
            )
            if scope == "read":
                assert response.status_code in {200, 404}
            else:
                assert_error_contract(response, 403, "permission_denied")

    for scope, private_key in keys.items():
        payload = notification()
        test_client.post("/api/v1/notifications", json=payload)
        nonce = (
            issue_nonce(test_client, private_key=private_key, key_id=scope)
            if scope == "respond"
            else "scope-check"
        )
        response = signed_request(
            test_client,
            "POST",
            f"/api/v1/notifications/{payload['id']}/response",
            body={"request_id": str(uuid.uuid4()), "option_id": "approve"},
            nonce=nonce,
            private_key=private_key,
            key_id=scope,
        )
        if scope == "respond":
            assert response.status_code == 200
        else:
            assert_error_contract(response, 403, "permission_denied")

    for scope, private_key in keys.items():
        payload = notification(options=False)
        test_client.post("/api/v1/notifications", json=payload)
        nonce = (
            issue_nonce(test_client, private_key=private_key, key_id=scope)
            if scope == "read_state"
            else "scope-check"
        )
        response = signed_request(
            test_client,
            "POST",
            "/api/v1/read-state",
            body={"notification_ids": [payload["id"]], "read": True},
            nonce=nonce,
            private_key=private_key,
            key_id=scope,
        )
        if scope == "read_state":
            assert response.status_code == 200
        else:
            assert_error_contract(response, 403, "permission_denied")
