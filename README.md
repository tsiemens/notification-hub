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

This installs `nh-server`, `nh-notifier`, and `nh-client-cli`. Keep the checkout
in place while using the editable installation.

For development, also create the repository-local environment with the test
and lint dependencies:

```sh
uv sync
```

## Development progress

Phases 1 through 4 are implemented: domain models and validation, TOML
configuration, SQLite migrations and repository operations, retention, the
complete Flask API, RFC 9421 public-key request authentication, RFC 9530 content
digests, scope enforcement, transactional single-use mutation nonces, and the
producer-side `nh-notifier` CLI. Phase 4 adds the reusable signed Python client,
paginated snapshot and durable event synchronization, and the headless
`nh-client-cli` inspection and mutation tool.
Phase 5 is complete. The GTK launcher, revisioned Python bridge/controller,
windowed Vue feed, notification and mutation controls, hardened Markdown
renderer, reconnect/reset handling, and native-window-independent real-server
integration coverage are available.
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

## Inspecting the hub

Configure a signed client in `~/.config/notification-hub/client.config.toml`:

```toml
[server]
url = "https://hub.example"

[auth]
key_id = "desktop-ui"
private_key_file = "/path/to/desktop-ui.pem"
```

The private key may be Ed25519, RSA, P-256, or P-384. Use a dedicated identity;
do not reuse an SSH key. See [Creating client signing keys](docs/signing-keys.md)
for generation and installation instructions. List current state, follow the
event feed, and perform mutations with:

```sh
nh-client-cli domains
nh-client-cli list --unread true --json
nh-client-cli watch --json
nh-client-cli read NOTIFICATION_ID
nh-client-cli respond NOTIFICATION_ID approve
```

## Desktop client development

Install the optional GTK desktop runtime and launch the packaged frontend with
the same signed-client configuration:

```sh
uv sync --extra gui
uv run nh-client --config /path/to/client.config.toml
```

The Linux runtime requires GTK and WebKitGTK libraries supplied by the host
distribution. The launcher explicitly uses pywebview's GTK backend and does not
fall back to a general-purpose browser.

For a guided visual demo, run:

```sh
./examples/demo.sh
```

The script starts an isolated server and desktop client, creates a fresh
temporary database and signing keypair, and sends several UI-focused scenarios.
It also pauses for signed responses in the UI to verify the approval wrapper and
response-message controls. Closing the window removes all temporary state; set
`NH_DEMO_KEEP_STATE=1` to retain it for debugging.

For frontend development, use Node.js 22 or newer:

```sh
cd frontend
npm ci
npm run dev
```

The Vite development server uses an in-browser fake bridge. A production build
writes relative assets to `src/notification_hub/gui/web`, where the Python
launcher loads them through package resources. Run `npm run verify` to type
check, test, and rebuild those assets.

## Testing

Run the tests with:

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
cd frontend && npm run verify
```
