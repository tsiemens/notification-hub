from __future__ import annotations

import sys
import tarfile
import zipfile
from pathlib import Path


def fail(message: str) -> None:
    raise SystemExit(f"artifact verification failed: {message}")


def main() -> None:
    directory = Path(sys.argv[1] if len(sys.argv) > 1 else "dist")
    wheels = list(directory.glob("notification_hub-*.whl"))
    sdists = list(directory.glob("notification_hub-*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        fail("expected exactly one wheel and one sdist")

    with zipfile.ZipFile(wheels[0]) as archive:
        names = set(archive.namelist())
        required = {
            "notification_hub/gui/web/index.html",
            "notification_hub/gui/web/assets/index.js",
            "notification_hub/gui/web/assets/index.css",
            "notification_hub/gui/resources/notification-hub.desktop.template",
            "notification_hub/gui/resources/notification-hub.svg",
            "notification_hub/gui/resources/notification-hub-gui-deps_0.1.0-1_all.deb",
        }
        missing = required - names
        if missing:
            fail(f"wheel is missing {', '.join(sorted(missing))}")
        javascript = b"".join(archive.read(name) for name in names if name.endswith(".js"))
        if b"createOscillator" not in javascript:
            fail("wheel does not contain the generated bundled notification tone")
        entry_points = next(
            (name for name in names if name.endswith(".dist-info/entry_points.txt")), None
        )
        if entry_points is None:
            fail("wheel has no console entry points")
        content = archive.read(entry_points).decode()
        for command in (
            "nh-server",
            "nh-notifier",
            "nh-client-cli",
            "nh-client",
            "nh-desktop-installer",
        ):
            if f"{command} =" not in content:
                fail(f"wheel has no {command} entry point")

    with tarfile.open(sdists[0], "r:gz") as archive:
        names = archive.getnames()
        for suffix in (
            "/src/notification_hub/gui/web/index.html",
            "/src/notification_hub/gui/web/assets/index.js",
            "/src/notification_hub/gui/web/assets/index.css",
            "/src/notification_hub/gui/resources/notification-hub.desktop.template",
            "/src/notification_hub/gui/resources/notification-hub.svg",
            "/src/notification_hub/gui/resources/notification-hub-gui-deps_0.1.0-1_all.deb",
        ):
            if not any(name.endswith(suffix) for name in names):
                fail(f"sdist is missing {suffix.removeprefix('/')}")

    print(f"verified {wheels[0].name} and {sdists[0].name}")


if __name__ == "__main__":
    main()
