from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path

import pytest

from notification_hub.config import ConfigurationError, load_client_config
from notification_hub.gui.settings import ClientSettingsStore, SettingsWriteError


def _write_config(path: Path) -> None:
    path.write_text(
        """# retain this comment
[server]
url = "https://hub.example/base"
verify_tls = false

[auth]
key_id = "private-identity"
private_key_file = "/very/secret/client.pem"

[ui]
theme = "light"
sound = "never"

[[views]]
id = "old"
name = "Old"
[[views.rules]]
domain_regex = "old"
""",
        encoding="utf-8",
    )


def _settings() -> dict[str, object]:
    return {
        "theme": "dark",
        "sound": "all",
        "sound_path": "/tmp/chime.wav",
        "hide_read": True,
        "raw_markdown": True,
        "views": [
            {
                "id": "quoted",
                "name": 'A "quoted" \\ view',
                "rules": [
                    {"domain_regex": '^build\\d+"$', "sender_regex": "agent\nname"},
                    {"tag_regex": "release"},
                ],
            }
        ],
    }


def test_settings_round_trip_escaping_and_preserve_private_configuration(tmp_path: Path) -> None:
    path = tmp_path / "client.toml"
    _write_config(path)
    path.chmod(0o640)
    before = tomllib.loads(path.read_text(encoding="utf-8"))
    store = ClientSettingsStore(path)

    saved = store.update(_settings())
    after_text = path.read_text(encoding="utf-8")
    after = tomllib.loads(after_text)

    assert saved.to_dict() == _settings()
    assert load_client_config(path).settings == saved
    assert after["server"] == before["server"]
    assert after["auth"] == before["auth"]
    assert "# retain this comment" in after_text
    assert "very/secret" in after_text
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_invalid_update_does_not_change_file_or_active_settings(tmp_path: Path) -> None:
    path = tmp_path / "client.toml"
    _write_config(path)
    store = ClientSettingsStore(path)
    original_text = path.read_text(encoding="utf-8")
    original_settings = store.get()

    invalid = _settings()
    invalid["views"] = [{"id": "empty", "name": "Empty", "rules": []}]
    with pytest.raises(ConfigurationError):
        store.update(invalid)

    assert path.read_text(encoding="utf-8") == original_text
    assert store.get() == original_settings


def test_replace_failure_is_atomic_and_cleans_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "client.toml"
    _write_config(path)
    store = ClientSettingsStore(path)
    original_text = path.read_text(encoding="utf-8")
    original_settings = store.get()

    def fail_replace(_source: os.PathLike[str], _target: os.PathLike[str]) -> None:
        raise OSError("disk failure containing /very/secret/client.pem")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(SettingsWriteError, match="settings could not be saved"):
        store.update(_settings())

    assert path.read_text(encoding="utf-8") == original_text
    assert store.get() == original_settings
    assert list(tmp_path.glob(".*.tmp")) == []


def test_new_atomic_file_uses_owner_only_permissions(tmp_path: Path) -> None:
    source = tmp_path / "source.toml"
    _write_config(source)
    store = ClientSettingsStore(source)
    target = tmp_path / "new.toml"
    store.path = target

    candidate = source.read_text(encoding="utf-8")
    store._write_candidate(candidate, None)  # noqa: SLF001 - exercise creation permissions

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_settings_can_be_saved_before_server_is_configured(tmp_path: Path) -> None:
    path = tmp_path / "client.toml"
    store = ClientSettingsStore(path)
    saved = store.update(_settings())
    assert saved == store.get()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert ClientSettingsStore(path).get() == saved
