from __future__ import annotations

import argparse
import sys
import threading
import time
from collections.abc import Sequence
from contextlib import ExitStack
from importlib.resources import as_file, files
from pathlib import Path

from notification_hub.client import HubClient
from notification_hub.config import (
    ConfigurationError,
    default_client_config_path,
    load_client_config,
)

from .bridge import GuiBridge
from .controller import GuiController
from .settings import ClientSettingsStore


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="nh-client", description="Notification Hub desktop client"
    )
    parser.add_argument("--config", type=Path, help="client TOML configuration path")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    return parser


def _gtk_failure(exc: BaseException) -> str:
    return (
        "GTK/WebKitGTK could not be initialized. Install the GTK 3 and WebKitGTK "
        f"runtime libraries for your distribution, then retry ({exc})"
    )


def _install_smoke_probe(window: object, result: list[str]) -> None:
    """Close a real native window after Vue and the pywebview bridge are usable."""

    def probe() -> None:
        deadline = time.monotonic() + 15
        detail = "the packaged Vue application did not become ready"
        while time.monotonic() < deadline:
            try:
                ready = window.evaluate_js(  # type: ignore[attr-defined]
                    "Boolean(document.querySelector('.app-shell main') "
                    "&& window.pywebview && window.pywebview.api "
                    "&& window.pywebview.api.get_initial_state)"
                )
                if ready:
                    result.append("ready")
                    window.destroy()  # type: ignore[attr-defined]
                    return
            except Exception as exc:  # pywebview reports transient load errors here
                detail = str(exc)
            time.sleep(0.1)
        result.append(detail)
        window.destroy()  # type: ignore[attr-defined]

    threading.Thread(target=probe, name="nh-gui-smoke-probe", daemon=True).start()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config_path = (args.config or default_client_config_path()).expanduser()
        config = load_client_config(config_path)
        client = HubClient(config)
        try:
            import webview
        except ImportError as exc:
            print(f"nh-client: {_gtk_failure(exc)}", file=sys.stderr)
            return 1
    except (ConfigurationError, OSError, ValueError) as exc:
        print(f"nh-client: {exc}", file=sys.stderr)
        return 1

    controller = GuiController(client)
    bridge = GuiBridge(controller, settings_store=ClientSettingsStore(config_path, config.settings))
    resource = files("notification_hub.gui").joinpath("web")
    try:
        with ExitStack() as stack:
            web_root = stack.enter_context(as_file(resource))
            index = web_root / "index.html"
            if not index.is_file():
                raise FileNotFoundError(
                    "packaged frontend assets are missing; run the frontend build"
                )
            window = webview.create_window(
                "Notification Hub",
                index.resolve().as_uri(),
                js_api=bridge,
                min_size=(720, 480),
            )

            def choose_sound_file() -> str | None:
                selected = window.create_file_dialog(
                    webview.FileDialog.OPEN,
                    allow_multiple=False,
                    file_types=("Audio files (*.wav;*.mp3;*.ogg;*.oga;*.flac;*.m4a)",),
                )
                return str(selected[0]) if selected else None

            bridge.set_sound_file_chooser(choose_sound_file)
            window.events.closed += lambda *_args: controller.stop()
            smoke_result: list[str] = []
            if args.smoke_test:
                window.events.loaded += lambda *_args: _install_smoke_probe(window, smoke_result)
            controller.start()
            try:
                webview.start(gui="gtk", debug=False)
            except Exception as exc:
                from webview.errors import WebViewException

                if not isinstance(exc, (OSError, RuntimeError, WebViewException)):
                    raise
                print(f"nh-client: {_gtk_failure(exc)}", file=sys.stderr)
                return 1
            if args.smoke_test and smoke_result != ["ready"]:
                detail = smoke_result[0] if smoke_result else "the window closed before loading"
                print(f"nh-client: GTK smoke test failed: {detail}", file=sys.stderr)
                return 1
    except (OSError, RuntimeError) as exc:
        print(f"nh-client: {_gtk_failure(exc)}", file=sys.stderr)
        return 1
    finally:
        controller.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
