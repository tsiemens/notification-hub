from __future__ import annotations

from pathlib import Path

import pytest

from notification_hub.config import (
    MAX_CUSTOM_VIEWS,
    MAX_VIEW_REGEX_LENGTH,
    MAX_VIEW_RULES,
    ConfigurationError,
    load_client_config,
    load_notifier_config,
    load_server_config,
)


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


def test_loads_notifier_configuration(tmp_path: Path) -> None:
    path = tmp_path / "notifier.toml"
    path.write_text(
        """[server]
url = "https://hub.example/base"
connect_timeout_seconds = 2

[defaults]
domain_from_hostname = false
sender = "build-agent"
priority = "urgent"
""",
        encoding="utf-8",
    )
    config = load_notifier_config(path)
    assert config.server.url == "https://hub.example/base"
    assert config.server.connect_timeout_seconds == 2
    assert config.domain_from_hostname is False
    assert config.sender == "build-agent"


@pytest.mark.parametrize(
    "url",
    ["http://", "ftp://hub.example", "http://user@hub.example", "http://hub.example:nope"],
)
def test_rejects_invalid_notifier_url(tmp_path: Path, url: str) -> None:
    path = tmp_path / "notifier.toml"
    path.write_text(f'[server]\nurl = "{url}"\n', encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_notifier_config(path)


def _client_config(settings: str = "") -> str:
    return f"""[server]
url = "https://hub.example"

[auth]
key_id = "desktop"
private_key_file = "/secret/client.pem"

{settings}
"""


@pytest.mark.parametrize(
    "settings",
    [
        "\n".join(
            f'[[views]]\nid = "v{index}"\nname = "View"\n[[views.rules]]\ndomain_regex = "."'
            for index in range(MAX_CUSTOM_VIEWS + 1)
        ),
        '[[views]]\nid = "v"\nname = "View"\n'
        + "\n".join('[[views.rules]]\ndomain_regex = "."' for _ in range(MAX_VIEW_RULES + 1)),
        '[[views]]\nid = "v"\nname = "View"\n[[views.rules]]\ndomain_regex = "'
        + ("x" * (MAX_VIEW_REGEX_LENGTH + 1))
        + '"',
    ],
)
def test_client_view_bounds_are_enforced(tmp_path: Path, settings: str) -> None:
    path = tmp_path / "client.toml"
    path.write_text(_client_config(settings), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_client_config(path)


def test_client_rejects_empty_rules_and_duplicate_view_ids(tmp_path: Path) -> None:
    path = tmp_path / "client.toml"
    path.write_text(
        _client_config(
            '[[views]]\nid = "same"\nname = "One"\n[[views.rules]]\ntag_regex = "x"\n'
            '[[views]]\nid = "same"\nname = "Two"\n[[views.rules]]\ntag_regex = "y"'
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="unique"):
        load_client_config(path)

    path.write_text(_client_config('[[views]]\nid = "empty"\nname = "Empty"'), encoding="utf-8")
    with pytest.raises(ConfigurationError, match="non-empty"):
        load_client_config(path)
