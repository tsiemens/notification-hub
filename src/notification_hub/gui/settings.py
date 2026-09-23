from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import threading
from contextlib import suppress
from pathlib import Path

from notification_hub.config import (
    ClientSettings,
    ConfigurationError,
    load_client_config,
    parse_client_settings,
)

_HEADER = re.compile(r"^\s*\[{1,2}\s*([^\]]+?)\s*\]{1,2}\s*(?:#.*)?$")


class SettingsWriteError(OSError):
    """A settings update could not be safely persisted."""


def _toml_string(value: str) -> str:
    # TOML basic strings and JSON strings share the escaping used here.
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007F")


def _serialize(settings: ClientSettings) -> str:
    lines = [
        "[ui]",
        f"theme = {_toml_string(settings.theme)}",
        f"sound = {_toml_string(settings.sound)}",
        f"sound_path = {_toml_string(settings.sound_path)}",
        f"hide_read = {str(settings.hide_read).lower()}",
        f"raw_markdown = {str(settings.raw_markdown).lower()}",
    ]
    for view in settings.views:
        lines.extend(
            [
                "",
                "[[views]]",
                f"id = {_toml_string(view.id)}",
                f"name = {_toml_string(view.name)}",
            ]
        )
        for rule in view.rules:
            lines.extend(["", "[[views.rules]]"])
            for key, value in (
                ("domain_regex", rule.domain_regex),
                ("sender_regex", rule.sender_regex),
                ("tag_regex", rule.tag_regex),
            ):
                if value is not None:
                    lines.append(f"{key} = {_toml_string(value)}")
    return "\n".join(lines) + "\n"


def _without_settings(source: str) -> str:
    """Remove top-level ui/views tables while retaining all unrelated text."""
    kept: list[str] = []
    skipping = False
    for line in source.splitlines(keepends=True):
        match = _HEADER.match(line.rstrip("\r\n"))
        if match:
            root = match.group(1).strip().split(".", 1)[0].strip().strip("\"'")
            skipping = root in {"ui", "views"}
        if not skipping:
            kept.append(line)
    return "".join(kept).rstrip() + "\n\n"


class ClientSettingsStore:
    """Validate and atomically persist the bridge-visible client settings."""

    def __init__(self, path: Path, settings: ClientSettings | None = None) -> None:
        self.path = path.expanduser()
        self._lock = threading.RLock()
        self._settings = settings or load_client_config(self.path).settings

    def get(self) -> ClientSettings:
        with self._lock:
            return self._settings

    def update(self, value: object) -> ClientSettings:
        settings = parse_client_settings(value)
        with self._lock:
            try:
                source = self.path.read_text(encoding="utf-8")
                existing_mode = stat.S_IMODE(self.path.stat().st_mode)
                candidate = _without_settings(source) + _serialize(settings)
                self._write_candidate(candidate, existing_mode)
            except ConfigurationError:
                raise
            except (OSError, UnicodeError) as exc:
                raise SettingsWriteError("settings could not be saved") from exc
            self._settings = settings
            return settings

    def _write_candidate(self, candidate: str, existing_mode: int | None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, existing_mode if existing_mode is not None else 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                descriptor = -1
                stream.write(candidate)
                stream.flush()
                os.fsync(stream.fileno())
            # Validate the whole file, including preserved server/auth values,
            # before it can replace the active configuration.
            load_client_config(temporary)
            os.replace(temporary, self.path)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            with suppress(FileNotFoundError):
                temporary.unlink()
