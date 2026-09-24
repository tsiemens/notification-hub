from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from notification_hub.gui.desktop_integration import main as desktop_main


@pytest.mark.parametrize(
    "directory", ["tool bin", "tool $bin", "tool %bin", 'tool "bin', "tool `bin", "tool \\bin"]
)
@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required")
def test_install_resolves_uv_executable_without_session_path(
    tmp_path: Path, directory: str
) -> None:
    executable_dir = tmp_path / directory
    executable_dir.mkdir()
    client = executable_dir / "nh-client"
    client.write_text('#!/bin/sh\nprintf launched > "$NH_LAUNCH_MARKER"\n', encoding="utf-8")
    client.chmod(0o755)

    data_home = tmp_path / "desktop data"
    script = Path(__file__).resolve().parents[1] / "scripts/desktop-integration.sh"
    env = os.environ | {
        "UV_TOOL_BIN_DIR": str(executable_dir),
        "XDG_DATA_HOME": str(data_home),
    }
    result = subprocess.run([str(script), "install"], env=env, capture_output=True, text=True)
    if "%" in directory:
        assert result.returncode != 0
        assert "unsupported by desktop entries" in result.stderr
        assert not (data_home / "applications/notification-hub.desktop").exists()
        return
    assert result.returncode == 0, result.stderr

    desktop = data_home / "applications/notification-hub.desktop"
    assert 'Exec="' in desktop.read_text(encoding="utf-8")
    validator = shutil.which("desktop-file-validate")
    if validator:
        result = subprocess.run([validator, str(desktop)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    gio = shutil.which("gio")
    if gio:
        marker = tmp_path / "launched"
        result = subprocess.run(
            [gio, "launch", str(desktop)],
            env=env | {"NH_LAUNCH_MARKER": str(marker)},
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        for _ in range(20):
            if marker.exists():
                break
            time.sleep(0.05)
        assert marker.read_text(encoding="utf-8") == "launched"


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is required")
def test_packaged_desktop_command_without_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable_dir = tmp_path / "tool $bin"
    executable_dir.mkdir()
    client = executable_dir / "nh-client"
    client.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    client.chmod(0o755)
    data_home = tmp_path / "desktop data"
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(executable_dir))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.chdir(tmp_path)

    assert desktop_main(["install"]) == 0
    desktop = data_home / "applications/notification-hub.desktop"
    icon = data_home / "icons/hicolor/scalable/apps/notification-hub.svg"
    assert desktop.exists() and icon.exists()
    assert 'Exec="' in desktop.read_text(encoding="utf-8")
    validator = shutil.which("desktop-file-validate")
    if validator:
        result = subprocess.run([validator, str(desktop)], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr

    assert desktop_main(["uninstall"]) == 0
    assert not desktop.exists() and not icon.exists()
