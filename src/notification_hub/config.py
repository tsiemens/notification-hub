from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from notification_hub.domain import Priority

VALID_SCOPES = frozenset({"read", "respond", "read_state"})
_LOCAL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_CUSTOM_VIEWS = 64
MAX_VIEW_RULES = 32
MAX_VIEW_REGEX_LENGTH = 1024


class ConfigurationError(ValueError):
    """Configuration is missing, unsafe, or malformed."""


def _xdg_path(env_name: str, fallback: str, filename: str) -> Path:
    root = Path(os.environ.get(env_name, fallback)).expanduser()
    return root / "notification-hub" / filename


def default_server_config_path() -> Path:
    return _xdg_path("XDG_CONFIG_HOME", "~/.config", "server.config.toml")


def default_notifier_config_path() -> Path:
    return _xdg_path("XDG_CONFIG_HOME", "~/.config", "notifier.config.toml")


def default_client_config_path() -> Path:
    return _xdg_path("XDG_CONFIG_HOME", "~/.config", "client.config.toml")


def default_database_path() -> Path:
    return _xdg_path("XDG_DATA_HOME", "~/.local/share", "server.sqlite3")


def _table(data: dict[str, Any], key: str) -> dict[str, Any]:
    """Return the dictionary stored under key, defaulting to an empty dictionary."""
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ConfigurationError(f"{key} must be a TOML table")
    return value


def _only(data: dict[str, Any], allowed: set[str], context: str) -> None:
    """Reject settings outside the allowed names for a configuration context."""
    unknown = set(data) - allowed
    if unknown:
        raise ConfigurationError(f"unknown {context} setting(s): {', '.join(sorted(unknown))}")


def _positive_int(value: Any, name: str, *, minimum: int = 1, maximum: int | None = None) -> int:
    """Validate and return an integer within the configured inclusive bounds."""
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigurationError(f"{name} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigurationError(f"{name} must be <= {maximum}")
    return value


def _boolean(value: Any, name: str) -> bool:
    """Validate and return a boolean configuration value."""
    if not isinstance(value, bool):
        raise ConfigurationError(f"{name} must be boolean")
    return value


def _scopes(value: Any, context: str) -> frozenset[str]:
    """Validate a non-empty list of recognized scope strings and return it as a set."""
    if not isinstance(value, list) or not value or any(not isinstance(item, str) for item in value):
        raise ConfigurationError(f"{context}.scopes must be a non-empty string array")
    result = frozenset(value)
    unknown = result - VALID_SCOPES
    if unknown:
        raise ConfigurationError(f"unknown scope(s): {', '.join(sorted(unknown))}")
    return result


def _load_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as stream:
            return tomllib.load(stream)
    except FileNotFoundError as exc:
        raise ConfigurationError(f"configuration file does not exist: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigurationError(f"invalid TOML in {path}: {exc}") from exc


@dataclass(frozen=True, slots=True)
class SigningKey:
    principal: str
    key_id: str
    public_key_file: Path
    scopes: frozenset[str]


@dataclass(frozen=True, slots=True)
class AuthConfig:
    signature_max_age_seconds: int = 60
    nonce_ttl_seconds: int = 300
    max_outstanding_nonces_per_key: int = 128
    signing_keys: tuple[SigningKey, ...] = ()


@dataclass(frozen=True, slots=True)
class RetentionConfig:
    history_days: int = 7
    max_pending_days: int = 7
    event_history_days: int = 8
    max_event_count: int | None = None

    def __post_init__(self) -> None:
        if self.event_history_days < self.history_days + 1:
            raise ConfigurationError(
                "event_history_days must exceed history_days by at least one day"
            )


@dataclass(frozen=True, slots=True)
class LimitsConfig:
    creates_per_minute: int = 120
    pending_total: int = 100


@dataclass(frozen=True, slots=True)
class ServerConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    database: Path = field(default_factory=default_database_path)
    request_body_limit_kib: int = 256
    strict_database_permissions: bool = True
    retention: RetentionConfig = field(default_factory=RetentionConfig)
    limits: LimitsConfig = field(default_factory=LimitsConfig)
    auth: AuthConfig = field(default_factory=AuthConfig)


@dataclass(frozen=True, slots=True)
class RemoteServerConfig:
    url: str
    verify_tls: bool = True
    connect_timeout_seconds: int = 3
    request_timeout_seconds: int = 15


@dataclass(frozen=True, slots=True)
class NotifierConfig:
    server: RemoteServerConfig
    domain_from_hostname: bool = True
    sender: str = "nh-notifier"
    priority: Priority = Priority.NORMAL


@dataclass(frozen=True, slots=True)
class ViewRule:
    domain_regex: str | None = None
    sender_regex: str | None = None
    tag_regex: str | None = None


@dataclass(frozen=True, slots=True)
class CustomView:
    id: str
    name: str
    rules: tuple[ViewRule, ...]


@dataclass(frozen=True, slots=True)
class ClientConfig:
    server: RemoteServerConfig
    key_id: str
    private_key_file: Path
    theme: str = "system"
    sound: str = "response_required"
    hide_read: bool = False
    raw_markdown: bool = False
    views: tuple[CustomView, ...] = ()
    sound_path: str = ""

    @property
    def settings(self) -> ClientSettings:
        return ClientSettings(
            theme=self.theme,
            sound=self.sound,
            hide_read=self.hide_read,
            raw_markdown=self.raw_markdown,
            views=self.views,
            sound_path=self.sound_path,
        )


@dataclass(frozen=True, slots=True)
class ClientSettings:
    theme: str = "system"
    sound: str = "response_required"
    hide_read: bool = False
    raw_markdown: bool = False
    views: tuple[CustomView, ...] = ()
    sound_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "theme": self.theme,
            "sound": self.sound,
            "sound_path": self.sound_path,
            "hide_read": self.hide_read,
            "raw_markdown": self.raw_markdown,
            "views": [
                {
                    "id": view.id,
                    "name": view.name,
                    "rules": [
                        {
                            key: value
                            for key, value in (
                                ("domain_regex", rule.domain_regex),
                                ("sender_regex", rule.sender_regex),
                                ("tag_regex", rule.tag_regex),
                            )
                            if value is not None
                        }
                        for rule in view.rules
                    ],
                }
                for view in self.views
            ],
        }


def load_server_config(path: Path | None = None) -> ServerConfig:
    path = (path or default_server_config_path()).expanduser()
    data = _load_toml(path)
    _only(data, {"server", "retention", "limits", "auth"}, "top-level")
    server = _table(data, "server")
    retention = _table(data, "retention")
    limits = _table(data, "limits")
    auth = _table(data, "auth")
    _only(
        server,
        {"host", "port", "database", "request_body_limit_kib", "strict_database_permissions"},
        "server",
    )
    _only(
        retention,
        {"history_days", "max_pending_days", "event_history_days", "max_event_count"},
        "retention",
    )
    _only(limits, {"creates_per_minute", "pending_total"}, "limits")
    _only(
        auth,
        {
            "signature_max_age_seconds",
            "nonce_ttl_seconds",
            "max_outstanding_nonces_per_key",
            "signing_keys",
        },
        "auth",
    )

    key_configs: list[SigningKey] = []
    for entry in auth.get("signing_keys", []):
        if not isinstance(entry, dict):
            raise ConfigurationError("auth.signing_keys entries must be tables")
        _only(entry, {"principal", "key_id", "public_key_file", "scopes"}, "auth signing key")
        try:
            key_configs.append(
                SigningKey(
                    str(entry["principal"]),
                    str(entry["key_id"]),
                    Path(entry["public_key_file"]).expanduser(),
                    _scopes(entry.get("scopes"), "auth.signing_keys"),
                )
            )
        except KeyError as exc:
            raise ConfigurationError(f"missing signing key setting: {exc.args[0]}") from exc
    if len({item.key_id for item in key_configs}) != len(key_configs):
        raise ConfigurationError("signing key ids must be unique")

    host = server.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host:
        raise ConfigurationError("server.host must be a non-empty string")
    retention_config = RetentionConfig(
        _positive_int(retention.get("history_days", 7), "retention.history_days"),
        _positive_int(retention.get("max_pending_days", 7), "retention.max_pending_days"),
        _positive_int(retention.get("event_history_days", 8), "retention.event_history_days"),
        None
        if retention.get("max_event_count") is None
        else _positive_int(retention["max_event_count"], "retention.max_event_count"),
    )
    return ServerConfig(
        host=str(host),
        port=_positive_int(server.get("port", 8765), "server.port", maximum=65535),
        database=Path(server.get("database", default_database_path())).expanduser(),
        request_body_limit_kib=_positive_int(
            server.get("request_body_limit_kib", 256), "server.request_body_limit_kib"
        ),
        strict_database_permissions=_boolean(
            server.get("strict_database_permissions", True),
            "server.strict_database_permissions",
        ),
        retention=retention_config,
        limits=LimitsConfig(
            _positive_int(limits.get("creates_per_minute", 120), "limits.creates_per_minute"),
            _positive_int(limits.get("pending_total", 100), "limits.pending_total"),
        ),
        auth=AuthConfig(
            _positive_int(
                auth.get("signature_max_age_seconds", 60), "auth.signature_max_age_seconds"
            ),
            _positive_int(auth.get("nonce_ttl_seconds", 300), "auth.nonce_ttl_seconds"),
            _positive_int(
                auth.get("max_outstanding_nonces_per_key", 128),
                "auth.max_outstanding_nonces_per_key",
            ),
            tuple(key_configs),
        ),
    )


def _remote_server(data: dict[str, Any]) -> RemoteServerConfig:
    _only(
        data,
        {
            "url",
            "verify_tls",
            "connect_timeout_seconds",
            "request_timeout_seconds",
        },
        "server",
    )
    url = data.get("url")
    try:
        parsed_url = urlsplit(url) if isinstance(url, str) else None
        hostname = parsed_url.hostname if parsed_url is not None else None
        if parsed_url is not None:
            _ = parsed_url.port  # Validate a configured numeric port.
    except ValueError as exc:
        raise ConfigurationError("server.url must be an HTTP(S) URL") from exc
    if (
        parsed_url is None
        or parsed_url.scheme not in {"http", "https"}
        or not hostname
        or parsed_url.username is not None
        or parsed_url.fragment
        or parsed_url.query
    ):
        raise ConfigurationError("server.url must be an HTTP(S) URL")
    return RemoteServerConfig(
        url.rstrip("/"),
        _boolean(data.get("verify_tls", True), "server.verify_tls"),
        _positive_int(data.get("connect_timeout_seconds", 3), "connect timeout"),
        _positive_int(data.get("request_timeout_seconds", 15), "request timeout"),
    )


def load_notifier_config(path: Path | None = None) -> NotifierConfig:
    path = (path or default_notifier_config_path()).expanduser()
    data = _load_toml(path)
    _only(data, {"server", "defaults"}, "top-level")
    defaults = _table(data, "defaults")
    _only(defaults, {"domain_from_hostname", "sender", "priority"}, "defaults")
    try:
        priority = Priority(defaults.get("priority", "normal"))
    except ValueError as exc:
        raise ConfigurationError("defaults.priority is invalid") from exc
    sender = defaults.get("sender", "nh-notifier")
    if not isinstance(sender, str) or not sender:
        raise ConfigurationError("defaults.sender must be a non-empty string")
    return NotifierConfig(
        _remote_server(_table(data, "server")),
        _boolean(defaults.get("domain_from_hostname", True), "defaults.domain_from_hostname"),
        sender,
        priority,
    )


def _client_settings(data: dict[str, Any]) -> ClientSettings:
    ui = _table(data, "ui")
    _only(ui, {"theme", "sound", "sound_path", "hide_read", "raw_markdown"}, "ui")
    theme = ui.get("theme", "system")
    sound = ui.get("sound", "response_required")
    sound_path = ui.get("sound_path", "")
    if theme not in {"light", "dark", "system"} or sound not in {
        "never",
        "response_required",
        "all",
    }:
        raise ConfigurationError("ui theme or sound setting is invalid")
    if not isinstance(sound_path, str) or "\x00" in sound_path:
        raise ConfigurationError("ui.sound_path must be a path string")
    views: list[CustomView] = []
    view_values = data.get("views", [])
    if not isinstance(view_values, list):
        raise ConfigurationError("views must be an array of tables")
    if len(view_values) > MAX_CUSTOM_VIEWS:
        raise ConfigurationError(f"views must contain at most {MAX_CUSTOM_VIEWS} entries")
    for entry in view_values:
        if not isinstance(entry, dict):
            raise ConfigurationError("views entries must be tables")
        _only(entry, {"id", "name", "rules"}, "view")
        view_id = entry.get("id")
        name = entry.get("name")
        if not isinstance(view_id, str) or not _LOCAL_ID.fullmatch(view_id):
            raise ConfigurationError("view.id must be a valid local identifier")
        if not isinstance(name, str) or not 1 <= len(name) <= 80:
            raise ConfigurationError("view.name must contain 1..80 characters")
        rule_values = entry.get("rules", [])
        if not isinstance(rule_values, list) or not rule_values:
            raise ConfigurationError("view.rules must be a non-empty array of tables")
        if len(rule_values) > MAX_VIEW_RULES:
            raise ConfigurationError(f"view.rules must contain at most {MAX_VIEW_RULES} entries")
        rules = []
        for rule in rule_values:
            if not isinstance(rule, dict):
                raise ConfigurationError("view.rules entries must be tables")
            _only(rule, {"domain_regex", "sender_regex", "tag_regex"}, "view rule")
            if not rule:
                raise ConfigurationError("view rules must contain at least one expression")
            if any(not isinstance(value, str) for value in rule.values()):
                raise ConfigurationError("view rule expressions must be strings")
            if any(not value for value in rule.values()):
                raise ConfigurationError("view rule expressions must not be empty")
            if any(len(value) > MAX_VIEW_REGEX_LENGTH for value in rule.values()):
                raise ConfigurationError(
                    f"view rule expressions must be at most {MAX_VIEW_REGEX_LENGTH} characters"
                )
            rules.append(
                ViewRule(rule.get("domain_regex"), rule.get("sender_regex"), rule.get("tag_regex"))
            )
        views.append(CustomView(view_id, name, tuple(rules)))
    if len({view.id for view in views}) != len(views):
        raise ConfigurationError("view ids must be unique")
    return ClientSettings(
        theme=theme,
        sound=sound,
        hide_read=_boolean(ui.get("hide_read", False), "ui.hide_read"),
        raw_markdown=_boolean(ui.get("raw_markdown", False), "ui.raw_markdown"),
        views=tuple(views),
        sound_path=sound_path,
    )


def parse_client_settings(value: object) -> ClientSettings:
    """Validate the bridge-visible presentation settings object."""
    if not isinstance(value, dict):
        raise ConfigurationError("settings must be an object")
    _only(value, {"theme", "sound", "sound_path", "hide_read", "raw_markdown", "views"}, "settings")
    missing = {"theme", "sound", "sound_path", "hide_read", "raw_markdown", "views"} - set(value)
    if missing:
        raise ConfigurationError(f"missing settings value(s): {', '.join(sorted(missing))}")
    return _client_settings(
        {
            "ui": {
                key: value[key]
                for key in ("theme", "sound", "sound_path", "hide_read", "raw_markdown")
            },
            "views": value["views"],
        }
    )


def load_client_config(path: Path | None = None) -> ClientConfig:
    path = (path or default_client_config_path()).expanduser()
    data = _load_toml(path)
    _only(data, {"server", "auth", "ui", "views"}, "top-level")
    auth = _table(data, "auth")
    _only(auth, {"key_id", "private_key_file"}, "auth")
    settings = _client_settings(data)
    try:
        key_id, key_file = auth["key_id"], auth["private_key_file"]
    except KeyError as exc:
        raise ConfigurationError(f"missing client auth setting: {exc.args[0]}") from exc
    if not isinstance(key_id, str) or not key_id or len(key_id) > 256:
        raise ConfigurationError("auth.key_id must be a non-empty string of at most 256 characters")
    if not isinstance(key_file, str) or not key_file:
        raise ConfigurationError("auth.private_key_file must be a non-empty path string")
    return ClientConfig(
        server=_remote_server(_table(data, "server")),
        key_id=key_id,
        private_key_file=Path(key_file).expanduser(),
        theme=settings.theme,
        sound=settings.sound,
        hide_read=settings.hide_read,
        raw_markdown=settings.raw_markdown,
        views=settings.views,
        sound_path=settings.sound_path,
    )


def load_desktop_config(path: Path) -> tuple[ClientConfig | None, ClientSettings]:
    """Allow the desktop to open before a server and signing key are configured."""
    path = path.expanduser()
    if not path.exists():
        return None, ClientSettings()
    data = _load_toml(path)
    if set(data) <= {"ui", "views"}:
        return None, _client_settings(data)
    config = load_client_config(path)
    return config, config.settings
