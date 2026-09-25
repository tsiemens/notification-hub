"""Manage desktop integration and Ubuntu GUI dependencies."""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tempfile
from importlib.resources import files
from pathlib import Path

_DEPS_PACKAGE = "notification-hub-gui-deps"
_DEPS_VERSION = "0.1.0-1"
_DEPS_DEB = f"{_DEPS_PACKAGE}_{_DEPS_VERSION}_all.deb"


def _supported_ubuntu() -> bool:
    try:
        release = platform.freedesktop_os_release()
    except OSError:
        return False
    return release.get("ID") == "ubuntu" and release.get("VERSION_ID") in {"24.04", "26.04"}


def _apt(*arguments: str) -> None:
    command = [] if os.geteuid() == 0 else ["sudo"]
    command.extend(("apt-get", *arguments))
    try:
        subprocess.run(command, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(f"could not manage {_DEPS_PACKAGE} with apt: {exc}") from exc


def _installed_deps_version() -> str | None:
    try:
        result = subprocess.run(
            ["dpkg-query", "-W", "-f=${db:Status-Status}\t${Version}", _DEPS_PACKAGE],
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        raise RuntimeError(f"could not query {_DEPS_PACKAGE}: {exc}") from exc
    status, _, version = result.stdout.partition("\t")
    if result.returncode != 0 or status != "installed":
        return None
    return version.strip() or None


def _deps_current() -> bool:
    installed = _installed_deps_version()
    if installed is None:
        return False
    try:
        result = subprocess.run(
            ["dpkg", "--compare-versions", installed, "ge", _DEPS_VERSION], check=False
        )
    except OSError as exc:
        raise RuntimeError(f"could not compare {_DEPS_PACKAGE} versions: {exc}") from exc
    return result.returncode == 0


def _install_gui_deps() -> None:
    if not _supported_ubuntu():
        print(
            "nh-desktop-installer: automatic native dependency installation supports "
            "Ubuntu 24.04 and 26.04; install the equivalent packages for this distribution",
            file=sys.stderr,
        )
        return
    if _deps_current():
        return
    resource = files("notification_hub.gui").joinpath("resources", _DEPS_DEB)
    if not resource.is_file():
        raise RuntimeError(f"packaged dependency file is missing: {_DEPS_DEB}")
    with tempfile.NamedTemporaryFile(prefix="notification-hub-gui-deps-", suffix=".deb") as deb:
        deb.write(resource.read_bytes())
        deb.flush()
        os.chmod(deb.name, 0o644)
        print(f"Installing {_DEPS_PACKAGE} with apt", flush=True)
        _apt("install", "--yes", "--no-install-recommends", deb.name)


def _remove_gui_deps() -> None:
    if not shutil.which("dpkg-query"):
        return
    if _installed_deps_version() is not None:
        print(f"Removing {_DEPS_PACKAGE} with apt", flush=True)
        _apt("remove", "--autoremove", _DEPS_PACKAGE)


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
            f"nh-client is not installed at {client}; install notification-hub first"
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
        try:
            _remove_gui_deps()
            desktop.unlink(missing_ok=True)
            icon.unlink(missing_ok=True)
        except (OSError, RuntimeError) as exc:
            print(f"nh-desktop-installer: {exc}", file=sys.stderr)
            return 1
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
        _install_gui_deps()
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
