from __future__ import annotations

import argparse
import logging
import signal
import threading
from collections.abc import Sequence
from pathlib import Path
from types import FrameType

from werkzeug.serving import BaseWSGIServer, make_server

from notification_hub.config import ConfigurationError, ServerConfig, load_server_config
from notification_hub.server.app import create_app


def serve(config: ServerConfig) -> None:
    """Run the HTTP service until SIGINT or SIGTERM requests a graceful stop."""
    app = create_app(config)
    # Fail startup before opening the listening socket if configuration,
    # migrations, database permissions, or startup cleanup are invalid.
    app.extensions["notification_hub_start"]()
    lifecycle = app.extensions["notification_hub_lifecycle"]
    http_server: BaseWSGIServer = make_server(config.host, config.port, app, threaded=True)
    # Werkzeug normally makes request threads daemonic. Non-daemon workers let
    # server_close wait for transactions already inside the application.
    http_server.daemon_threads = False
    shutdown_started = threading.Event()

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        if shutdown_started.is_set():
            return
        shutdown_started.set()
        lifecycle.stop()
        # BaseServer.shutdown must be invoked from a thread other than the one
        # running serve_forever.
        threading.Thread(
            target=http_server.shutdown,
            name="notification-hub-http-shutdown",
            daemon=True,
        ).start()

    previous_handlers = {
        signum: signal.signal(signum, request_stop) for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        http_server.serve_forever()
    finally:
        lifecycle.stop()
        http_server.server_close()
        lifecycle.wait_for_idle()
        lifecycle.stop(join=True)
        app.extensions["notification_hub_snapshot_cache"].clear()
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nh-server", description="Run Notification Hub")
    parser.add_argument("--config", type=Path, help="server TOML configuration path")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    try:
        serve(load_server_config(args.config))
    except ConfigurationError as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
