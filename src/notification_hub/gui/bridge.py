from __future__ import annotations

import webbrowser
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from notification_hub.client import NetworkError, ProtocolError, ServerError
from notification_hub.config import ConfigurationError
from notification_hub.domain import ResponseState, ValidationError

from .controller import GuiController
from .settings import ClientSettingsStore, SettingsWriteError

MAX_NOTIFICATION_IDS = 10_000
MAX_RESPONSE_MESSAGE = 16 * 1024


class GuiBridge:
    """Small JSON-compatible API exposed to the local webview."""

    def __init__(
        self,
        controller: GuiController,
        *,
        settings_store: ClientSettingsStore | None = None,
        external_opener: Callable[[str], object] = webbrowser.open,
        sound_file_chooser: Callable[[], str | None] | None = None,
    ) -> None:
        self._controller = controller
        self._settings_store = settings_store
        self._external_opener = external_opener
        self._sound_file_chooser = sound_file_chooser

    def set_sound_file_chooser(self, chooser: Callable[[], str | None]) -> None:
        self._sound_file_chooser = chooser

    def get_initial_state(self) -> dict[str, Any]:
        return self._controller.get_initial_state()

    def get_updates(self, after_revision: object) -> dict[str, Any]:
        if (
            isinstance(after_revision, bool)
            or not isinstance(after_revision, int)
            or after_revision < 0
        ):
            raise ValueError("after_revision must be a non-negative integer")
        return self._controller.get_updates(after_revision)

    def get_settings(self) -> dict[str, Any]:
        if self._settings_store is None:
            return self._failure(
                "settings_unavailable", "Settings are unavailable in this client.", False
            )
        return {"ok": True, "settings": self._settings_store.get().to_dict()}

    def update_settings(self, settings: object) -> dict[str, Any]:
        if self._settings_store is None:
            return self._failure(
                "settings_unavailable", "Settings are unavailable in this client.", False
            )
        try:
            saved = self._settings_store.update(settings)
            return {"ok": True, "settings": saved.to_dict()}
        except ConfigurationError as exc:
            return self._failure("invalid_settings", str(exc), False)
        except SettingsWriteError:
            return self._failure(
                "settings_write_failed",
                "Settings could not be saved. The previous settings are still active.",
                True,
            )

    def choose_sound_file(self) -> dict[str, Any]:
        if self._sound_file_chooser is None:
            return self._failure("chooser_unavailable", "The file chooser is unavailable.", False)
        try:
            return {"ok": True, "path": self._sound_file_chooser()}
        except (OSError, RuntimeError):
            return self._failure("chooser_failed", "The audio file could not be selected.", True)

    def resolve_sound_path(self, value: object) -> dict[str, Any]:
        """Validate a custom sound immediately before playback and return its local URI."""
        if not isinstance(value, str) or not value or "\x00" in value:
            return self._failure("invalid_sound", "Choose a local audio file.", False)
        path = Path(value).expanduser()
        try:
            path = path.resolve(strict=True)
            if not path.is_file() or not path.stat().st_size:
                raise OSError
            with path.open("rb") as stream:
                stream.read(1)
        except OSError:
            return self._failure(
                "invalid_sound",
                "The custom sound is missing or unreadable; the bundled sound will be used.",
                False,
            )
        if path.suffix.lower() not in {".wav", ".mp3", ".ogg", ".oga", ".flac", ".m4a"}:
            return self._failure(
                "unsupported_sound",
                "The custom sound format is unsupported; the bundled sound will be used.",
                False,
            )
        return {"ok": True, "path": str(path), "uri": path.as_uri()}

    def set_read_state(self, notification_ids: object, read: object) -> dict[str, Any]:
        try:
            ids = self._notification_ids(notification_ids)
            if not isinstance(read, bool):
                raise ValueError("read must be boolean")
            result = self._controller.set_read_state(ids, read)
            return {"ok": True, **result.to_dict()}
        except (ValueError, NetworkError, ProtocolError, ServerError) as exc:
            return self._error(exc)

    def respond(
        self, notification_id: object, option_id: object, message: object
    ) -> dict[str, Any]:
        try:
            notification_id = self._identifier(notification_id, "notification_id")
            option_id = self._identifier(option_id, "option_id")
            if message is not None and not isinstance(message, str):
                raise ValueError("message must be a string or null")
            if isinstance(message, str) and len(message) > MAX_RESPONSE_MESSAGE:
                raise ValueError("message must be at most 16 KiB")
            notification = self._controller.notification(notification_id)
            if notification is None:
                raise ValueError("notification does not exist in synchronized state")
            if notification.response_state is not ResponseState.PENDING:
                raise ValueError("notification is not awaiting a response")
            option = next(
                (item for item in notification.response_options if item.id == option_id), None
            )
            if option is None:
                raise ValueError("option does not exist for this notification")
            option.validate_message(message)
            result = self._controller.respond(notification_id, option_id, message)
            return {"ok": True, **result.to_dict()}
        except (ValueError, ValidationError, NetworkError, ProtocolError, ServerError) as exc:
            return self._error(exc)

    def open_external(self, url: object) -> dict[str, Any]:
        try:
            validated = self._external_url(url)
            opened = self._external_opener(validated)
            if opened is False:
                return self._failure("open_failed", "The system browser could not be opened.", True)
            return {"ok": True}
        except ValueError as exc:
            return self._error(exc)

    @staticmethod
    def _identifier(value: object, name: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError(f"{name} must be a non-empty string")
        return value

    @classmethod
    def _notification_ids(cls, value: object) -> list[str]:
        if not isinstance(value, list) or not value:
            raise ValueError("notification_ids must be a non-empty list")
        if len(value) > MAX_NOTIFICATION_IDS:
            raise ValueError("notification_ids exceeds the retained-state limit")
        ids = [cls._identifier(item, "notification id") for item in value]
        if len(set(ids)) != len(ids):
            raise ValueError("notification_ids must be unique")
        return ids

    @staticmethod
    def _external_url(value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("url must be a string")
        try:
            parsed = urlsplit(value)
            hostname = parsed.hostname
            _ = parsed.port
        except ValueError as exc:
            raise ValueError("url must be an absolute HTTP(S) URL") from exc
        if (
            parsed.scheme not in {"http", "https"}
            or not hostname
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("url must be an absolute HTTP(S) URL")
        return value

    @classmethod
    def _error(cls, error: Exception) -> dict[str, Any]:
        if isinstance(error, ServerError):
            extra: dict[str, Any] = {}
            if error.code == "already_answered" and error.notification is not None:
                extra["notification"] = error.notification.to_dict()
            return cls._failure(error.code, cls._server_message(error), error.retryable, **extra)
        if isinstance(error, NetworkError):
            return cls._failure("offline", "The server is unavailable. Try again later.", True)
        if isinstance(error, ProtocolError):
            return cls._failure("protocol_error", "The server returned an invalid response.", False)
        return cls._failure("invalid_request", str(error), False)

    @staticmethod
    def _server_message(error: ServerError) -> str:
        if error.code == "already_answered":
            return "Another client already answered this notification."
        if error.status in {401, 403}:
            return "The client is not authorized to perform this action."
        if error.retryable:
            return "The server is temporarily unavailable. Try again later."
        return "The server rejected this action."

    @staticmethod
    def _failure(code: str, message: str, retryable: bool, **extra: Any) -> dict[str, Any]:
        return {
            "ok": False,
            "error": {"code": code, "message": message, "retryable": retryable, **extra},
        }
