from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from dataclasses import replace
from pathlib import Path
from typing import TextIO

from notification_hub.client import (
    HubClient,
    HubError,
    NotificationQuery,
    ServerError,
    watch_events,
)
from notification_hub.config import ClientConfig, ConfigurationError, load_client_config
from notification_hub.domain import ValidationError

EXIT_OK = 0
EXIT_CONFLICT = 2
EXIT_USAGE = 4
EXIT_FAILURE = 5


class CliUsageError(ValueError):
    pass


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliUsageError(message)


def _output_option(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit stable JSON")


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="nh-client-cli", description="Inspect and change hub state")
    parser.add_argument("--config", type=Path, help="client TOML configuration path")
    commands = parser.add_subparsers(dest="command", required=True)

    domains = commands.add_parser("domains", help="list domain summaries")
    _output_option(domains)

    listing = commands.add_parser("list", help="list notifications")
    for option in ("domain", "sender", "response-state", "tag"):
        listing.add_argument(f"--{option}", action="append", default=[])
    listing.add_argument("--unread", choices=("true", "false"))
    listing.add_argument("--created-before")
    listing.add_argument("--created-after")
    listing.add_argument("--order", choices=("asc", "desc"), default="desc")
    listing.add_argument("--limit", type=int, default=100)
    listing.add_argument("--cursor")
    listing.add_argument("--all", action="store_true", help="follow every page")
    _output_option(listing)

    show = commands.add_parser("show", help="show one notification")
    show.add_argument("id")
    _output_option(show)

    watch = commands.add_parser("watch", help="follow the durable event feed")
    _output_option(watch)

    for name in ("read", "unread"):
        mutation = commands.add_parser(name, help=f"mark notifications {name}")
        mutation.add_argument("id", nargs="+")
        _output_option(mutation)

    respond = commands.add_parser("respond", help="answer a notification")
    respond.add_argument("id")
    respond.add_argument("option")
    message = respond.add_mutually_exclusive_group()
    message.add_argument("--message")
    message.add_argument("--message-file", type=Path)
    _output_option(respond)
    return parser


def _json(value: object, stdout: TextIO, *, flush: bool = False) -> None:
    print(json.dumps(value, ensure_ascii=False, separators=(",", ":")), file=stdout, flush=flush)


def _table(headers: tuple[str, ...], rows: list[tuple[object, ...]], stdout: TextIO) -> None:
    values = [headers, *[tuple(str(value) for value in row) for row in rows]]
    widths = [max(len(row[index]) for row in values) for index in range(len(headers))]
    for row_index, row in enumerate(values):
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)), file=stdout)
        if row_index == 0:
            print("  ".join("-" * width for width in widths), file=stdout)


def _message(args: argparse.Namespace, stdin: TextIO) -> str | None:
    if args.message_file is None:
        return args.message
    if str(args.message_file) == "-":
        return stdin.read()
    try:
        return args.message_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"could not read message file {args.message_file}: {exc}") from exc


def run(
    args: argparse.Namespace,
    config: ClientConfig,
    *,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    client: HubClient | None = None,
) -> int:
    del stderr
    client = client or HubClient(config)
    if args.command == "domains":
        items = client.list_domains()
        if args.json:
            _json({"items": [item.to_dict() for item in items]}, stdout)
        else:
            _table(
                ("DOMAIN", "TOTAL", "UNREAD", "PENDING", "LATEST"),
                [
                    (
                        item.name,
                        item.notification_count,
                        item.unread_count,
                        item.pending_response_count,
                        item.latest_summary,
                    )
                    for item in items
                ],
                stdout,
            )
        return EXIT_OK
    if args.command == "list":
        query = NotificationQuery(
            tuple(args.domain),
            tuple(args.sender),
            None if args.unread is None else args.unread == "true",
            tuple(args.response_state),
            tuple(args.tag),
            args.created_before,
            args.created_after,
            args.order,
            args.limit,
            args.cursor,
        )
        page = client.list_notifications(query)
        items = list(page.items)
        while args.all and page.next_cursor is not None:
            page = client.list_notifications(replace(query, cursor=page.next_cursor))
            items.extend(page.items)
        if args.json:
            _json(
                {
                    "items": [item.to_dict() for item in items],
                    "next_cursor": page.next_cursor,
                },
                stdout,
            )
        else:
            _table(
                ("ID", "DOMAIN", "STATE", "SUMMARY"),
                [(item.id, item.domain, item.response_state.value, item.summary) for item in items],
                stdout,
            )
        return EXIT_OK
    if args.command == "show":
        item = client.get_notification(args.id)
        if args.json:
            _json({"notification": item.to_dict()}, stdout)
        else:
            for label, value in item.to_dict().items():
                if isinstance(value, (dict, list)):
                    value = json.dumps(value, ensure_ascii=False)
                print(f"{label}: {value}", file=stdout)
        return EXIT_OK
    if args.command == "watch":
        stop = threading.Event()
        previous = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, lambda _signum, _frame: stop.set())
        try:
            for event in watch_events(client, stop_event=stop):
                if args.json:
                    _json(event.to_dict(), stdout, flush=True)
                else:
                    print(
                        f"{event.seq} {event.occurred_at} {event.type}",
                        file=stdout,
                        flush=True,
                    )
        finally:
            signal.signal(signal.SIGINT, previous)
        return EXIT_OK
    if args.command in {"read", "unread"}:
        result = client.set_read_state(args.id, args.command == "read")
        if args.json:
            _json(result.to_dict(), stdout)
        else:
            print(
                f"Marked {len(result.notifications)} notification(s) {args.command}",
                file=stdout,
            )
        return EXIT_OK
    if args.command == "respond":
        result = client.respond(args.id, args.option, _message(args, stdin))
        if args.json:
            _json(result.to_dict(), stdout)
        else:
            print(f"Responded to {args.id} with {args.option}", file=stdout)
        return EXIT_OK
    raise AssertionError("unhandled command")


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        config = load_client_config(args.config)
        return run(args, config)
    except (CliUsageError, ConfigurationError, ValidationError, ValueError) as exc:
        print(f"nh-client-cli: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except ServerError as exc:
        print(f"nh-client-cli: {exc.code}: {exc}", file=sys.stderr)
        conflicts = {"already_answered", "state_conflict", "idempotency_conflict"}
        return EXIT_CONFLICT if exc.code in conflicts else EXIT_FAILURE
    except HubError as exc:
        print(f"nh-client-cli: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    except KeyboardInterrupt:
        return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
