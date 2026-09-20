from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from notification_hub.domain import Priority

VALID_SCOPES = frozenset({"read", "respond", "read_state"})


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
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
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
    return NotifierConfig(
        _remote_server(_table(data, "server")),
        _boolean(defaults.get("domain_from_hostname", True), "defaults.domain_from_hostname"),
        defaults.get("sender", "nh-notifier"),
        priority,
    )


def load_client_config(path: Path | None = None) -> ClientConfig:
    path = (path or default_client_config_path()).expanduser()
    data = _load_toml(path)
    _only(data, {"server", "auth", "ui", "views"}, "top-level")
    auth = _table(data, "auth")
    ui = _table(data, "ui")
    _only(auth, {"key_id", "private_key_file"}, "auth")
    _only(ui, {"theme", "sound", "hide_read", "raw_markdown"}, "ui")
    theme = ui.get("theme", "system")
    sound = ui.get("sound", "response_required")
    if theme not in {"light", "dark", "system"} or sound not in {
        "never",
        "response_required",
        "all",
    }:
        raise ConfigurationError("ui theme or sound setting is invalid")
    views: list[CustomView] = []
    for entry in data.get("views", []):
        _only(entry, {"id", "name", "rules"}, "view")
        rules = []
        for rule in entry.get("rules", []):
            _only(rule, {"domain_regex", "sender_regex", "tag_regex"}, "view rule")
            rules.append(
                ViewRule(rule.get("domain_regex"), rule.get("sender_regex"), rule.get("tag_regex"))
            )
        views.append(CustomView(str(entry.get("id", "")), str(entry.get("name", "")), tuple(rules)))
    try:
        key_id, key_file = auth["key_id"], auth["private_key_file"]
    except KeyError as exc:
        raise ConfigurationError(f"missing client auth setting: {exc.args[0]}") from exc
    return ClientConfig(
        _remote_server(_table(data, "server")),
        str(key_id),
        Path(key_file).expanduser(),
        theme,
        sound,
        _boolean(ui.get("hide_read", False), "ui.hide_read"),
        _boolean(ui.get("raw_markdown", False), "ui.raw_markdown"),
        tuple(views),
    )
