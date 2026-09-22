from __future__ import annotations

import subprocess
import sys
from importlib.resources import as_file, files
from pathlib import Path

import pytest


def test_gui_package_import_does_not_import_pywebview() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, notification_hub.gui; assert 'webview' not in sys.modules",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_packaged_frontend_entrypoint_exists_outside_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    resource = files("notification_hub.gui").joinpath("web", "index.html")
    with as_file(resource) as path:
        assert path.is_file()
        html = path.read_text(encoding="utf-8")
        assert "Content-Security-Policy" in html
        # pywebview generates API proxy methods with `new Function`, which the
        # packaged page must permit or window.pywebview.api remains empty.
        assert "script-src 'self' 'unsafe-eval'" in html
