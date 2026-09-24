from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from werkzeug.serving import make_server

from notification_hub.config import AuthConfig, ServerConfig, SigningKey
from notification_hub.domain import CreateNotification
from notification_hub.server import create_app
from notification_hub.storage import Database, NotificationRepository


def main() -> None:
    executable = Path(sys.executable).with_name("nh-client")
    if not executable.is_file():
        raise SystemExit("GTK smoke test requires the installed nh-client entry point")

    with tempfile.TemporaryDirectory(prefix="notification-hub-gtk-smoke-") as temporary:
        root = Path(temporary)
        private_key = ed25519.Ed25519PrivateKey.generate()
        private_path = root / "client.pem"
        public_path = root / "client.pub"
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
        private_path.chmod(0o600)

        database = Database(root / "hub.sqlite3")
        database.initialize()
        repository = NotificationRepository(database)
        app = create_app(
            ServerConfig(
                database=database.path,
                auth=AuthConfig(
                    signing_keys=(
                        SigningKey(
                            "gtk-smoke",
                            "gtk-smoke",
                            public_path,
                            frozenset({"read", "respond", "read_state"}),
                        ),
                    )
                ),
            ),
            repository=repository,
        )
        app.extensions["notification_hub_start"]()
        repository.create(
            CreateNotification(
                str(uuid.uuid4()),
                "gtk-smoke",
                "packaged-wheel",
                "GTK/WebKitGTK smoke notification",
            )
        )
        server = make_server("127.0.0.1", 0, app, threaded=True)
        # Join request handlers before removing the temporary database.
        server.daemon_threads = False
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        config_path = root / "client.config.toml"
        config_path.write_text(
            "\n".join(
                (
                    "[server]",
                    f'url = "http://127.0.0.1:{server.server_port}"',
                    "connect_timeout_seconds = 1",
                    "request_timeout_seconds = 5",
                    "",
                    "[auth]",
                    'key_id = "gtk-smoke"',
                    f'private_key_file = "{private_path}"',
                    "",
                )
            ),
            encoding="utf-8",
        )

        environment = os.environ.copy()
        environment.setdefault("PYWEBVIEW_LOG", "debug")
        try:
            completed = subprocess.run(
                [str(executable), "--config", str(config_path), "--smoke-test"],
                cwd=root,
                env=environment,
                text=True,
                capture_output=True,
                timeout=35,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise SystemExit(
                "GTK smoke test timed out\n"
                f"stdout:\n{exc.stdout or ''}\nstderr:\n{exc.stderr or ''}"
            ) from exc
        finally:
            server.shutdown()
            app.extensions["notification_hub_lifecycle"].stop(join=True)
            server.server_close()
            server_thread.join(3)

        if completed.returncode:
            raise SystemExit(
                f"GTK smoke test exited {completed.returncode}\n"
                f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )
        print("GTK/WebKitGTK window, bridge, and packaged Vue application are ready")


if __name__ == "__main__":
    main()
