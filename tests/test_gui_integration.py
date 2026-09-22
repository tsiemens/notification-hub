from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from werkzeug.serving import make_server

from notification_hub.client import HubClient
from notification_hub.config import (
    AuthConfig,
    ClientConfig,
    RemoteServerConfig,
    RetentionConfig,
    ServerConfig,
    SigningKey,
)
from notification_hub.domain import CreateNotification, ResponseOption
from notification_hub.gui import GuiBridge, GuiController
from notification_hub.server import create_app
from notification_hub.storage import Database, NotificationRepository


def _wait_until(predicate, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("timed out waiting for GUI synchronization")


def _create(repository: NotificationRepository, *, options: bool = False) -> str:
    notification_id = str(uuid.uuid4())
    repository.create(
        CreateNotification(
            notification_id,
            "integration",
            "pytest",
            f"Notification {notification_id}",
            response_options=(ResponseOption("approve", "Approve"),) if options else (),
        )
    )
    return notification_id


def test_real_server_gui_reconnect_cursor_reset_mutations_and_shutdown(tmp_path: Path) -> None:
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
    database = Database(tmp_path / "hub.sqlite3")
    database.initialize()
    repository = NotificationRepository(database)
    app = create_app(
        ServerConfig(
            database=database.path,
            auth=AuthConfig(
                signing_keys=(
                    SigningKey(
                        "desktop",
                        "desktop-key",
                        public_path,
                        frozenset({"read", "respond", "read_state"}),
                    ),
                )
            ),
        ),
        repository=repository,
    )
    app.extensions["notification_hub_start"]()
    original_id = _create(repository, options=True)

    server = make_server("127.0.0.1", 0, app, threaded=True)
    port = server.server_port
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    client = HubClient(
        ClientConfig(
            RemoteServerConfig(
                f"http://127.0.0.1:{port}",
                connect_timeout_seconds=1,
                request_timeout_seconds=2,
            ),
            "desktop-key",
            private_path,
        ),
        random_source=lambda: 0.5,
    )
    controller = GuiController(
        client,
        update_wait_seconds=0.05,
        event_wait_seconds=1,
    )
    bridge = GuiBridge(controller)
    replacement = None
    replacement_thread = None
    controller.start()
    try:
        _wait_until(
            lambda: (
                bridge.get_initial_state()["connection"]["state"] == "connected"
                and bridge.get_initial_state()["snapshot"] is not None
            )
        )
        assert bridge.set_read_state([original_id], True)["ok"] is True
        assert bridge.respond(original_id, "approve", None)["ok"] is True
        _wait_until(
            lambda: (
                controller.notification(original_id) is not None
                and controller.notification(original_id).response is not None
            )  # type: ignore[union-attr]
        )

        server.shutdown()
        server.server_close()
        server_thread.join(2)
        _wait_until(lambda: bridge.get_initial_state()["connection"]["state"] == "offline")

        created_while_offline = [_create(repository) for _ in range(3)]
        repository.cleanup(RetentionConfig(max_event_count=1))
        replacement = make_server("127.0.0.1", port, app, threaded=True)
        replacement_thread = threading.Thread(target=replacement.serve_forever, daemon=True)
        replacement_thread.start()

        _wait_until(
            lambda: (
                bridge.get_initial_state()["connection"]["state"] == "connected"
                and all(controller.notification(item) is not None for item in created_while_offline)
            )
        )
        snapshot = bridge.get_initial_state()["snapshot"]
        assert snapshot is not None
        assert set(created_while_offline) <= {item["id"] for item in snapshot["notifications"]}
    finally:
        controller.stop(join_timeout=3)
        if replacement is not None:
            replacement.shutdown()
            replacement.server_close()
        if replacement_thread is not None:
            replacement_thread.join(2)
        if server_thread.is_alive():
            server.shutdown()
            server.server_close()
            server_thread.join(2)
        app.extensions["notification_hub_lifecycle"].stop(join=True)

    assert controller.stopped
    assert controller._worker is not None and not controller._worker.is_alive()  # noqa: SLF001
