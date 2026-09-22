from __future__ import annotations

import argparse
import sys
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config_path = (args.config or default_client_config_path()).expanduser()
        config = load_client_config(config_path)
        client = HubClient(config)
        import webview
    except (ConfigurationError, OSError, ValueError, ImportError) as exc:
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
            window.events.closed += lambda *_args: controller.stop()
            controller.start()
            webview.start(gui="gtk", debug=False)
    except (OSError, RuntimeError) as exc:
        print(f"nh-client: {exc}", file=sys.stderr)
        return 1
    finally:
        controller.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
