from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from werkzeug.serving import make_server

from notification_hub.config import RemoteServerConfig, ServerConfig
from notification_hub.notifier import NotifierClient
from notification_hub.server import create_app
from notification_hub.storage import Database, NotificationQuery, NotificationRepository


@pytest.fixture
def running_hub(tmp_path: Path):
    database = Database(tmp_path / "hub.sqlite3")
    database.initialize()
    repository = NotificationRepository(database)
    app = create_app(ServerConfig(database=database.path), repository=repository)
    app.extensions["notification_hub_start"]()
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield url, repository
    finally:
        app.extensions["notification_hub_lifecycle"].stop()
        server.shutdown()
        server.server_close()
        thread.join(2)
        app.extensions["notification_hub_lifecycle"].stop(join=True)


def write_notifier_config(tmp_path: Path, url: str) -> Path:
    path = tmp_path / "notifier.toml"
    path.write_text(
        f'''[server]
url = "{url}"
connect_timeout_seconds = 1
request_timeout_seconds = 2

[defaults]
domain_from_hostname = false
sender = "e2e-notifier"
priority = "high"
''',
        encoding="utf-8",
    )
    return path


def notifier_command(config: Path, *arguments: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "notification_hub.notifier.cli",
        "--config",
        str(config),
        *arguments,
    ]


def subprocess_environment() -> dict[str, str]:
    environment = os.environ.copy()
    source = str(Path(__file__).parents[1] / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (source, environment.get("PYTHONPATH")) if value
    )
    return environment


def test_notifier_subprocess_creates_notification(running_hub, tmp_path: Path) -> None:
    url, repository = running_hub
    config = write_notifier_config(tmp_path, url)

    result = subprocess.run(
        notifier_command(
            config,
            "send",
            "Build complete",
            "--domain",
            "build-container",
            "--message",
            "All tests passed",
            "--tag",
            "workspace:notification-hub",
        ),
        text=True,
        capture_output=True,
        timeout=10,
        env=subprocess_environment(),
        check=False,
    )

    assert result.returncode == 0, result.stderr
    notification = repository.get(result.stdout.strip())
    assert notification.summary == "Build complete"
    assert notification.sender == "e2e-notifier"
    assert notification.priority.value == "high"


def test_approval_waiter_observes_public_cancellation(running_hub, tmp_path: Path) -> None:
    url, repository = running_hub
    config = write_notifier_config(tmp_path, url)
    process = subprocess.Popen(
        notifier_command(config, "approve", "Deploy release", "--domain", "release-job"),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=subprocess_environment(),
    )
    notification_id = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            page = repository.query_notifications(NotificationQuery())
            if page.items:
                notification_id = page.items[0].id
                break
            time.sleep(0.02)
        assert notification_id is not None

        NotifierClient(RemoteServerConfig(url)).cancel(
            notification_id, reason="cancelled by e2e harness"
        )
        stdout, stderr = process.communicate(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()

    assert process.returncode == 2, stderr
    assert stdout == ""
    assert f"Notification {notification_id} is cancelled" in stderr
