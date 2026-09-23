# Notification Hub

Notification Hub is a small, single-user service for collecting notifications
and resolving approval requests from remote tools.

At a high-level, a small server is run somewhere, and notifications or approval
requests can be sent to it from the provided notifier executable. A "hub" GUI
can be run from your local machine, which monitors, displays and allows you to
reply (if necessary) to these notifications.

Notifications are given a customizable attributes so you can easily see where
they came from (for example, from which remote server or container), from which
tool, and include tags. They also support markdown messages and details.

## Installation

For the server, notifier, and headless inspection client, install the base
package in an isolated environment:

```sh
uv tool install notification-hub
```

This installs `nh-server`, `nh-notifier`, `nh-client-cli`, `nh-client`, and
`nh-desktop-installer`. Keep the checkout in place only when using
`uv tool install --editable .` for development.

The desktop client is Linux-only. Install its optional GTK integration with:

```sh
uv tool install 'notification-hub[gui]'
```

Python 3.12 or newer, GTK 3, and WebKitGTK 4.1 are supported. The native GTK
and WebKitGTK libraries come from the operating system, not PyPI. For example,
install `gir1.2-webkit2-4.1`, `libwebkit2gtk-4.1-0`, `libcairo2-dev`,
`libgirepository-2.0-dev`, `pkg-config`, and the development package matching
your Python on current Debian/Ubuntu, or the equivalent WebKitGTK 4.1, GTK 3,
and PyGObject prerequisites on your distribution. If the backend is
unavailable, `nh-client` exits with an actionable diagnostic.

The wheel includes freedesktop metadata, but uv does not install shared desktop
data or run a post-install hook. Register the desktop entry and icon for the
current user with the packaged command:

```sh
nh-desktop-installer install
```

For an install directly from a Git repository, use the same command after
installing (replace the example URL with the repository URL):

```sh
uv tool install 'notification-hub[gui] @ git+https://github.com/OWNER/notification-hub.git' &&
nh-desktop-installer install
```

Remove the integration with `nh-desktop-installer uninstall` before uninstalling
the uv tool. If uv's tool executable directory is not on your `PATH`, run
`"$(uv tool dir --bin)/nh-desktop-installer" install` instead. The command uses
`$XDG_DATA_HOME` when set to an absolute path, otherwise `~/.local/share`. It
writes the absolute `nh-client` path into the desktop entry, so the desktop
session does not need uv's executable directory on its `PATH`. Re-run the
command if you move that directory.

For development, also create the repository-local environment with the test
and lint dependencies:

```sh
uv sync
```

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

`npm run dev` starts Vite's local development server. Open the URL it prints in
a browser; edits to the frontend appear there as you work. This browser preview
uses fake notifications and a fake Python bridge, so it does not connect to the
desktop client or server.

From `frontend`, run `npm run check` to type check and test. Run `npm run build` when you want to update the packaged frontend.
The build writes readable JS and CSS with stable filenames
to `src/notification_hub/gui/web`, where the Python launcher loads them through
package resources. Commit those generated files along with your frontend changes.

For a release, `./scripts/check-release-assets.sh` performs a clean frontend
production build and fails when the committed packaged output is missing or
stale. Then build and inspect both Python artifacts with:

```sh
uv build --out-dir dist
uv run python scripts/verify_artifacts.py dist
```

The separate Linux CI smoke job installs the built wheel in a fresh environment
and opens a real GTK/WebKitGTK window under Xvfb. It waits for both the
pywebview bridge and packaged Vue application before closing the window.

## Testing

Run the tests with:

```sh
uv run ruff check .
uv run ruff format --check .
uv run pytest
cd frontend && npm run check
```
