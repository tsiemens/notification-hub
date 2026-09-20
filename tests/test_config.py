from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from notification_hub.config import ConfigurationError, load_server_config


class ServerConfigurationTests(unittest.TestCase):
    def write(self, root: str, content: str, mode: int = 0o600) -> Path:
        path = Path(root) / "server.toml"
        path.write_text(content, encoding="utf-8")
        path.chmod(mode)
        return path

    def test_loads_defaults_and_signing_key(self) -> None:
        content = """
[server]
database = "/tmp/hub.sqlite3"

[[auth.signing_keys]]
principal = "desktop-ui"
key_id = "desktop-ui"
public_key_file = "/shared-config/notification-hub/ui.pub"
scopes = ["read", "respond", "read_state"]
"""
        with tempfile.TemporaryDirectory() as root:
            config = load_server_config(self.write(root, content))
        self.assertEqual(config.port, 8765)
        self.assertEqual(config.auth.signing_keys[0].principal, "desktop-ui")
        self.assertEqual(config.retention.event_history_days, 8)

    def test_rejects_removed_producer_token_configuration(self) -> None:
        content = """
[[auth.tokens]]
principal = "producer"
token = "secret"
scopes = ["notify"]
"""
        with tempfile.TemporaryDirectory() as root, self.assertRaises(ConfigurationError):
            load_server_config(self.write(root, content))

    def test_uses_server_wide_pending_limit(self) -> None:
        content = """
[limits]
pending_total = 25
"""
        with tempfile.TemporaryDirectory() as root:
            config = load_server_config(self.write(root, content))
        self.assertEqual(config.limits.pending_total, 25)

    def test_rejects_removed_authentication_toggle(self) -> None:
        content = """
[auth]
enabled = false
"""
        with tempfile.TemporaryDirectory() as root, self.assertRaises(ConfigurationError):
            load_server_config(self.write(root, content))

    def test_rejects_short_event_retention(self) -> None:
        content = """
[retention]
history_days = 7
event_history_days = 7
"""
        with tempfile.TemporaryDirectory() as root, self.assertRaises(ConfigurationError):
            load_server_config(self.write(root, content))


if __name__ == "__main__":
    unittest.main()
