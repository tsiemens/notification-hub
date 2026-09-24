from __future__ import annotations

import argparse
import sys
import threading
from collections.abc import Sequence
from contextlib import ExitStack
from importlib.resources import as_file, files
from pathlib import Path

from notification_hub.client import HubClient
from notification_hub.config import (
    ConfigurationError,
    default_client_config_path,
    load_desktop_config,
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


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config_path = (args.config or default_client_config_path()).expanduser()
        if args.config is not None and not config_path.exists():
            raise ConfigurationError(f"configuration file does not exist: {config_path}")
        config, settings = load_desktop_config(config_path)
        client = HubClient(config) if config is not None else None
        try:
            import webview
        except ImportError as exc:
            print(f"nh-client: {_gtk_failure(exc)}", file=sys.stderr)
            return 1
    except (ConfigurationError, OSError, ValueError) as exc:
        print(f"nh-client: {exc}", file=sys.stderr)
        return 1

    controller = GuiController(client)
    smoke_result: list[str] = []
    smoke_lock = threading.Lock()

    def finish_smoke(detail: str = "ready") -> None:
        with smoke_lock:
            if smoke_result:
                return
            smoke_result.append(detail)
        print(f"nh-client: GTK smoke test: {detail}; closing window", file=sys.stderr, flush=True)
        caller = threading.current_thread()
        if detail == "ready":
            # Vue immediately polls again when this bridge call resolves.
            # Suspend subsequent polls while the current reply is delivered.
            window.evaluate_js(
                "window.pywebview.api.get_updates = () => new Promise(() => {}); void 0"
            )

        def close_after_reply() -> None:
            if detail == "ready":
                # pywebview sends the JS reply after get_updates returns. Closing
                # earlier strands its non-daemon worker in GTK evaluate_js.
                caller.join()
            # The backend schedules GTK cleanup without waiting for shown.
            window.gui.destroy_window(window.uid)

        threading.Thread(
            target=close_after_reply, name="nh-gui-smoke-close", daemon=True
        ).start()

    # Vue requests updates only after mounting and consuming the initial bridge state.
    bridge = GuiBridge(
        controller,
        settings_store=ClientSettingsStore(config_path, settings),
        on_updates_requested=finish_smoke if args.smoke_test else None,
    )
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
                str(index.resolve()),
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
            smoke_timer = None
            if args.smoke_test:
                smoke_timer = threading.Timer(
                    20, finish_smoke, args=("the packaged Vue application did not request updates",)
                )
                smoke_timer.daemon = True
                smoke_timer.start()
            controller.start()
            try:
                webview.start(gui="gtk", debug=False)
            except Exception as exc:
                from webview.errors import WebViewException

                if not isinstance(exc, (OSError, RuntimeError, WebViewException)):
                    raise
                print(f"nh-client: {_gtk_failure(exc)}", file=sys.stderr)
                return 1
            finally:
                if smoke_timer is not None:
                    smoke_timer.cancel()
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
