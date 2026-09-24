from __future__ import annotations

import argparse
import json
import socket
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any, TextIO

from notification_hub.config import ConfigurationError, NotifierConfig, load_notifier_config
from notification_hub.domain import (
    Appearance,
    CreateNotification,
    MessageMode,
    Priority,
    ResponseOption,
)
from notification_hub.notifier.client import (
    HubError,
    NotifierClient,
    Outcome,
    ServerError,
    WaitTimeout,
)

EXIT_OK = 0
EXIT_REJECTED = 2
EXIT_TIMEOUT = 3
EXIT_USAGE = 4
EXIT_NETWORK = 5
EXIT_INTERRUPTED = 130


class CliUsageError(ValueError):
    """Command-line arguments are incomplete or invalid."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CliUsageError(message)


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="nh-notifier", description="Send hub notifications")
    parser.add_argument("--config", type=Path, help="notifier TOML configuration path")
    subparsers = parser.add_subparsers(dest="command", required=True)

    send = subparsers.add_parser("send", help="create a notification")
    _add_create_arguments(send)
    send.add_argument(
        "--response-option",
        action="append",
        nargs="+",
        metavar="FIELD",
        default=[],
        help="ID LABEL [MESSAGE_MODE [APPEARANCE]]; repeat for each choice",
    )
    send.add_argument("--wait", action="store_true", help="wait for a terminal outcome")
    _add_wait_arguments(send)

    approve = subparsers.add_parser("approve", help="request approval and wait for it")
    _add_create_arguments(approve)
    approve.add_argument(
        "--wait",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="wait for a terminal outcome (the default)",
    )
    _add_wait_arguments(approve)

    wait = subparsers.add_parser("wait", help="wait for an existing notification")
    wait.add_argument("id")
    wait.add_argument("--json", action="store_true")
    _add_wait_arguments(wait)

    poll = subparsers.add_parser("poll", help="fetch an existing outcome once")
    poll.add_argument("id")
    poll.add_argument("--json", action="store_true")

    cancel = subparsers.add_parser("cancel", help="cancel a pending notification")
    cancel.add_argument("id")
    cancel.add_argument("--reason")
    cancel.add_argument("--json", action="store_true")
    return parser


def _add_create_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("summary")
    parser.add_argument("--message")
    parser.add_argument("--message-file", type=Path, metavar="PATH")
    parser.add_argument("--details")
    parser.add_argument("--details-file", type=Path, metavar="PATH")
    parser.add_argument("--domain")
    parser.add_argument("--sender")
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--priority", choices=[item.value for item in Priority])
    parser.add_argument("--source-created-at")
    parser.add_argument("--json", action="store_true")


def _add_wait_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--timeout", type=float, help="local wait deadline in seconds, including network time"
    )
    parser.add_argument(
        "--cancel-on-interrupt",
        action="store_true",
        help="cancel the server request when local waiting is interrupted",
    )


def _default_command(arguments: list[str]) -> list[str]:
    """Make `send` optional while preserving leading global options."""
    commands = {"send", "approve", "wait", "poll", "cancel"}
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value in {"-h", "--help"} or value in commands:
            return arguments
        if value == "--config":
            index += 2
            continue
        if value.startswith("--config="):
            index += 1
            continue
        return [*arguments[:index], "send", *arguments[index:]]
    return arguments


def _read_content(inline: str | None, path: Path | None, name: str, stdin: TextIO) -> str | None:
    if inline is not None and path is not None:
        raise ValueError(f"--{name} and --{name}-file cannot be used together")
    if path is None:
        return inline
    if str(path) == "-":
        return stdin.read()
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"could not read {name} file {path}: {exc}") from exc


def _response_options(values: list[list[str]]) -> list[dict[str, str]]:
    options = []
    for value in values:
        if not 2 <= len(value) <= 4:
            raise ValueError("--response-option requires 2 through 4 fields")
        option_id, label = value[:2]
        message_mode = value[2] if len(value) >= 3 else MessageMode.NONE.value
        appearance = value[3] if len(value) == 4 else Appearance.DEFAULT.value
        if message_mode not in {item.value for item in MessageMode}:
            raise ValueError(f"invalid response option message mode: {message_mode}")
        if appearance not in {item.value for item in Appearance}:
            raise ValueError(f"invalid response option appearance: {appearance}")
        options.append(
            {
                "id": option_id,
                "label": label,
                "message_mode": message_mode,
                "appearance": appearance,
            }
        )
    return options


def _notification(
    args: argparse.Namespace, config: NotifierConfig, stdin: TextIO
) -> dict[str, Any]:
    if args.message_file == Path("-") and args.details_file == Path("-"):
        raise ValueError("standard input cannot supply both message and details")
    message = _read_content(args.message, args.message_file, "message", stdin)
    details = _read_content(args.details, args.details_file, "details", stdin)
    domain = args.domain
    if domain is None and config.domain_from_hostname:
        domain = socket.gethostname()
    if not domain:
        raise ValueError("--domain is required when domain_from_hostname is false")
    if args.command == "approve":
        options = [
            {
                "id": "approve",
                "label": "Approve",
                "message_mode": "none",
                "appearance": "primary",
            },
            {
                "id": "deny",
                "label": "Deny",
                "message_mode": "optional",
                "appearance": "danger",
            },
        ]
    else:
        options = _response_options(args.response_option)
    value: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "domain": domain,
        "sender": args.sender or config.sender,
        "summary": args.summary,
        "message": message or "",
        "tags": args.tag,
        "priority": args.priority or config.priority.value,
        "response_options": options,
    }
    if details is not None:
        value["details"] = details
    if args.source_created_at is not None:
        value["source_created_at"] = args.source_created_at
    # Give local input errors the documented validation exit status instead of
    # sending them to the server and reporting a network/server failure.
    CreateNotification(
        id=value["id"],
        domain=value["domain"],
        sender=value["sender"],
        summary=value["summary"],
        message=value["message"],
        details=value.get("details"),
        tags=tuple(value["tags"]),
        priority=value["priority"],
        source_created_at=value.get("source_created_at"),
        response_options=tuple(ResponseOption(**option) for option in options),
    )
    return value


def _print_outcome(outcome: Outcome, *, json_output: bool, stdout: TextIO, stderr: TextIO) -> None:
    if json_output:
        print(json.dumps(outcome.to_dict(), ensure_ascii=False), file=stdout)
        return
    if outcome.state == "answered" and outcome.response is not None:
        option = outcome.response.get("option_id")
        message = outcome.response.get("message")
        print(message if message else option, file=stdout)
    else:
        print(f"Notification {outcome.notification_id} is {outcome.state}", file=stderr)


def _wait(
    client: NotifierClient,
    notification_id: str,
    args: argparse.Namespace,
    *,
    stderr: TextIO,
) -> Outcome:
    try:
        return client.wait(notification_id, timeout=args.timeout)
    except KeyboardInterrupt:
        if args.cancel_on_interrupt:
            try:
                client.cancel(notification_id, reason="local waiter interrupted")
                print(f"Cancelled notification {notification_id}", file=stderr)
            except HubError as exc:
                print(f"Could not cancel notification {notification_id}: {exc}", file=stderr)
        raise


def run(
    args: argparse.Namespace,
    config: NotifierConfig,
    *,
    stdin: TextIO = sys.stdin,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
    client: NotifierClient | None = None,
) -> int:
    client = client or NotifierClient(config.server)
    if args.command in {"send", "approve"}:
        if args.wait and args.timeout is not None and args.timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        notification = _notification(args, config, stdin)
        created = client.create(notification)
        notification_id = notification["id"]
        if not args.wait:
            if args.json:
                print(json.dumps(created, ensure_ascii=False), file=stdout)
            else:
                print(notification_id, file=stdout)
            return EXIT_OK
        if not args.json:
            print(f"Created notification {notification_id}; waiting for a response", file=stderr)
        outcome = _wait(client, notification_id, args, stderr=stderr)
        if args.json:
            print(
                json.dumps({"created": created, "outcome": outcome.to_dict()}, ensure_ascii=False),
                file=stdout,
            )
        else:
            _print_outcome(outcome, json_output=False, stdout=stdout, stderr=stderr)
        if args.command == "approve" and (
            outcome.state != "answered"
            or outcome.response is None
            or outcome.response.get("option_id") != "approve"
        ):
            return EXIT_REJECTED
        return EXIT_OK
    if args.command == "wait":
        outcome = _wait(client, args.id, args, stderr=stderr)
        _print_outcome(outcome, json_output=args.json, stdout=stdout, stderr=stderr)
        return EXIT_OK
    if args.command == "poll":
        outcome = client.poll(args.id)
        _print_outcome(outcome, json_output=args.json, stdout=stdout, stderr=stderr)
        return EXIT_OK
    if args.command == "cancel":
        result = client.cancel(args.id, reason=args.reason)
        if args.json:
            print(json.dumps(result, ensure_ascii=False), file=stdout)
        else:
            print(f"Cancelled notification {args.id}", file=stderr)
        return EXIT_OK
    raise AssertionError(f"unknown command: {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _default_command(list(argv) if argv is not None else sys.argv[1:])
    parser = _parser()
    try:
        args = parser.parse_args(arguments)
        config = load_notifier_config(args.config)
        return run(args, config)
    except (ConfigurationError, ValueError) as exc:
        print(f"nh-notifier: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except WaitTimeout as exc:
        print(f"nh-notifier: {exc}", file=sys.stderr)
        return EXIT_TIMEOUT
    except ServerError as exc:
        print(f"nh-notifier: hub error ({exc.code}): {exc}", file=sys.stderr)
        return EXIT_NETWORK
    except HubError as exc:
        print(f"nh-notifier: {exc}", file=sys.stderr)
        return EXIT_NETWORK
    except KeyboardInterrupt:
        print("nh-notifier: interrupted; the server request may still be pending", file=sys.stderr)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    raise SystemExit(main())
