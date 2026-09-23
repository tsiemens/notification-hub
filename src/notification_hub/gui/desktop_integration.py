"""Install the packaged desktop entry and icon for the current user."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from importlib.resources import files
from pathlib import Path


def _data_home() -> Path:
    configured = os.environ.get("XDG_DATA_HOME", "")
    return Path(configured) if Path(configured).is_absolute() else Path.home() / ".local/share"


def _client_executable() -> Path:
    try:
        result = subprocess.run(
            ["uv", "tool", "dir", "--bin"], check=True, capture_output=True, text=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError("uv is required to locate the installed nh-client executable") from exc
    bin_dir = Path(result.stdout.strip())
    if not bin_dir.is_absolute():
        raise RuntimeError(f"uv tool executable directory is not absolute: {bin_dir}")
    client = bin_dir / "nh-client"
    if not client.is_file() or not os.access(client, os.X_OK):
        raise RuntimeError(
            f"nh-client is not installed at {client}; install notification-hub[gui] first"
        )
    return client


def _desktop_exec(client: Path) -> str:
    path = str(client)
    if any(character in path for character in ("=", "%", "\n", "\r")):
        raise RuntimeError(
            f"nh-client path contains a character unsupported by desktop entries: {path}"
        )
    escaped = path.replace("\\", "\\\\\\\\").replace('"', '\\\\"')
    escaped = escaped.replace("`", "\\\\`").replace("$", "\\\\$")
    return f'Exec="{escaped}"'


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nh-desktop-installer", description=__doc__)
    parser.add_argument("action", choices=("install", "uninstall"))
    args = parser.parse_args(argv)
    data_home = _data_home()
    desktop = data_home / "applications/notification-hub.desktop"
    icon = data_home / "icons/hicolor/scalable/apps/notification-hub.svg"

    if args.action == "uninstall":
        desktop.unlink(missing_ok=True)
        icon.unlink(missing_ok=True)
        print(f"Removed Notification Hub desktop entry and icon from {data_home}")
        return 0

    try:
        client = _client_executable()
        resources = files("notification_hub.gui").joinpath("resources")
        template = resources.joinpath("notification-hub.desktop.template").read_text(
            encoding="utf-8"
        )
        icon_data = resources.joinpath("notification-hub.svg").read_bytes()
        entry = template.replace("Exec=@NH_CLIENT_EXEC@", _desktop_exec(client))
        if entry == template:
            raise RuntimeError("packaged desktop entry is missing its Exec placeholder")
        desktop.parent.mkdir(parents=True, exist_ok=True)
        icon.parent.mkdir(parents=True, exist_ok=True)
        desktop.write_text(entry, encoding="utf-8")
        icon.write_bytes(icon_data)
        desktop.chmod(0o644)
        icon.chmod(0o644)
    except (OSError, RuntimeError) as exc:
        print(f"nh-desktop-installer: {exc}", file=sys.stderr)
        return 1

    print(f"Installed Notification Hub desktop entry: {desktop}")
    print(f"Installed Notification Hub icon: {icon}")
    return 0
