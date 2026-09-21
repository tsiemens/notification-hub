# Notification Hub

Notification Hub is a small, single-user service for collecting notifications
and resolving approval requests from remote tools. The implementation is being
delivered in the phases described in `local_md/design.md`.

## Installation

From the repository root, install the command-line tools in an isolated
environment. The editable install makes changes in this checkout available
without reinstalling:

```sh
uv tool install --editable .
```

This installs both `nh-server` and `nh-notifier`; Keep the checkout in place while using the editable installation.

For development, also create the repository-local environment with the test
and lint dependencies:

```sh
uv sync
```

## Development progress

Phases 1 through 3 are implemented: domain models and validation, TOML
configuration, SQLite migrations and repository operations, retention, the
complete Flask API, RFC 9421 public-key request authentication, RFC 9530 content
digests, scope enforcement, transactional single-use mutation nonces, and the
producer-side `nh-notifier` CLI.
Producer create, outcome, and cancellation routes remain intentionally
unauthenticated as specified in the design.

## Running the server

Start the service with its default XDG configuration path, or provide one
explicitly:

```sh
nh-server --config /path/to/server.config.toml
```

## Sending notifications

Configure the producer-side server URL in
`~/.config/notification-hub/notifier.config.toml`, then send an informational
notification:

```sh
nh-notifier send "Build complete" --message "All tests passed" --tag build
```

`send` is the default command, so it may be omitted. Message and details content
also accept `--message-file` and `--details-file`; use `-` as the path to read
standard input. A custom response choice has the form:

```sh
nh-notifier send "Choose target" --wait \
  --response-option staging "Deploy staging" none primary \
  --response-option production "Deploy production" required danger
```

For the standard approve/deny flow, `approve` adds both choices and waits. It
exits successfully only when `approve` is selected, making it suitable for
fail-closed shell gates. See
[`examples/approval-wrapper.sh`](examples/approval-wrapper.sh).

## Testing

Run the tests with:

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
```
