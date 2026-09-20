from __future__ import annotations

from pathlib import Path

import pytest

from notification_hub.config import ConfigurationError, load_server_config


def write_config(root: Path, content: str, mode: int = 0o600) -> Path:
    path = root / "server.toml"
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)
    return path


def test_loads_defaults_and_signing_key(tmp_path: Path) -> None:
    content = """
[server]
database = "/tmp/hub.sqlite3"

[[auth.signing_keys]]
principal = "desktop-ui"
key_id = "desktop-ui"
public_key_file = "/shared-config/notification-hub/ui.pub"
scopes = ["read", "respond", "read_state"]
"""
    config = load_server_config(write_config(tmp_path, content))
    assert config.port == 8765
    assert config.auth.signing_keys[0].principal == "desktop-ui"
    assert config.retention.event_history_days == 8


def test_rejects_removed_producer_token_configuration(tmp_path: Path) -> None:
    content = """
[[auth.tokens]]
principal = "producer"
token = "secret"
scopes = ["notify"]
"""
    with pytest.raises(ConfigurationError):
        load_server_config(write_config(tmp_path, content))


def test_uses_server_wide_pending_limit(tmp_path: Path) -> None:
    content = """
[limits]
pending_total = 25
"""
    config = load_server_config(write_config(tmp_path, content))
    assert config.limits.pending_total == 25


def test_rejects_removed_authentication_toggle(tmp_path: Path) -> None:
    content = """
[auth]
enabled = false
"""
    with pytest.raises(ConfigurationError):
        load_server_config(write_config(tmp_path, content))


def test_rejects_short_event_retention(tmp_path: Path) -> None:
    content = """
[retention]
history_days = 7
event_history_days = 7
"""
    with pytest.raises(ConfigurationError):
        load_server_config(write_config(tmp_path, content))
