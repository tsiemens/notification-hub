from __future__ import annotations

import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa

from notification_hub.client import HubClient, NotificationQuery, SyncState
from notification_hub.client.transport import HttpResponse
from notification_hub.config import (
    AuthConfig,
    ClientConfig,
    RemoteServerConfig,
    ServerConfig,
    SigningKey,
)
from notification_hub.server import create_app


class FlaskTransport:
    def __init__(self, app) -> None:
        self.app = app

    def send(self, method, url, body, headers, timeout):
        del timeout
        parsed = urlsplit(url)
        target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        with self.app.test_client() as client:
            response = client.open(target, method=method, data=body, headers=dict(headers))
        return HttpResponse(response.status_code, response.data, dict(response.headers))


def _notification(*, options: bool = True) -> dict[str, object]:
    return {
        "id": str(uuid.uuid4()),
        "domain": "tests",
        "sender": "pytest",
        "summary": "Exercise signed client",
        "message": "body",
        "tags": ["client"],
        "response_options": (
            [{"id": "ok", "label": "OK", "message_mode": "none", "appearance": "primary"}]
            if options
            else []
        ),
    }


@pytest.mark.parametrize(
    "private_key",
    [
        pytest.param(ed25519.Ed25519PrivateKey.generate(), id="ed25519"),
        pytest.param(rsa.generate_private_key(public_exponent=65537, key_size=2048), id="rsa"),
        pytest.param(ec.generate_private_key(ec.SECP256R1()), id="p256"),
        pytest.param(ec.generate_private_key(ec.SECP384R1()), id="p384"),
    ],
)
def test_signed_reads_and_mutations_interoperate(tmp_path: Path, private_key) -> None:
    private_path = tmp_path / "client.pem"
    public_path = tmp_path / "client.pub"
    private_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    app = create_app(
        ServerConfig(
            database=tmp_path / "hub.sqlite3",
            auth=AuthConfig(
                signing_keys=(
                    SigningKey(
                        "client",
                        "client-key",
                        public_path,
                        frozenset({"read", "respond", "read_state"}),
                    ),
                )
            ),
        )
    )
    payload = _notification()
    with app.test_client() as producer:
        created = producer.post("/api/v1/notifications", json=payload)
    assert created.status_code == 201

    client = HubClient(
        ClientConfig(RemoteServerConfig("http://localhost"), "client-key", private_path),
        transport=FlaskTransport(app),
    )
    page = client.list_notifications(NotificationQuery(domains=("tests",)))
    assert [item.id for item in page.items] == [payload["id"]]
    assert client.list_domains()[0].name == "tests"
    assert client.get_notification(payload["id"]).message == "body"

    read_result = client.set_read_state([payload["id"]], True)
    assert read_result.notifications[0].read_at is not None
    assert read_result.event_seq is not None
    read_event = client.get_events(read_result.event_seq - 1).events[0]
    assert read_event.versions == (read_result.notifications[0].version,)
    response_result = client.respond(payload["id"], "ok")
    assert response_result.notification.response.option_id == "ok"

    snapshot = client.get_snapshot()
    state = SyncState.from_snapshot(snapshot)
    later = _notification(options=False)
    with app.test_client() as producer:
        producer.post("/api/v1/notifications", json=later)
    events = client.get_events(state.last_sequence)
    state.apply_page(events)
    assert later["id"] in state.notifications
    app.extensions["notification_hub_lifecycle"].stop(join=True)


def test_fresh_one_nonce_clients_can_make_five_mutations(tmp_path: Path) -> None:
    private_key = ed25519.Ed25519PrivateKey.generate()
    private_path = tmp_path / "client.pem"
    public_path = tmp_path / "client.pub"
    private_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    app = create_app(
        ServerConfig(
            database=tmp_path / "hub.sqlite3",
            auth=AuthConfig(
                signing_keys=(
                    SigningKey("client", "client-key", public_path, frozenset({"read_state"})),
                )
            ),
        )
    )
    payload = _notification(options=False)
    with app.test_client() as producer:
        assert producer.post("/api/v1/notifications", json=payload).status_code == 201
    config = ClientConfig(RemoteServerConfig("http://localhost"), "client-key", private_path)
    try:
        for read in (True, False, True, False, True):
            client = HubClient(config, transport=FlaskTransport(app), nonce_batch_size=1)
            result = client.set_read_state([payload["id"]], read)
            assert (result.notifications[0].read_at is not None) is read
    finally:
        app.extensions["notification_hub_lifecycle"].stop(join=True)
