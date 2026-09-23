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

## Security and Caveats

- Approval gates can separate an agent from the person approving only if the
  agent cannot access the client's private key, use the trusted client to sign,
  or bypass the gate. Running both under the same OS account is not sufficient.
- Producer requests are unauthenticated. Anyone who can reach the server can
  forge a notification; someone with its ID can read the outcome or cancel it.
  Restrict server access and do not trust the displayed sender as proof of origin.
- Keep the server on a trusted network and use an encrypted tunnel for remote
  connections. Protect private keys and the server database from untrusted users.

Read [Security and caveats](docs/security.md) before using approvals to protect
consequential actions.

## Installation

For the server, notifier, and headless inspection client, install the base
package in an isolated environment:

```sh
uv tool install notification-hub
```

This installs `nh-server`, `nh-notifier`, `nh-client-cli`, `nh-client`, and
`nh-desktop-installer`. An editable install for development requires keeping
the checkout in place.

The desktop client only supports Linux right now. On Ubuntu 24.04 or 26.04, install its native
runtime libraries with:

```sh
sudo apt install --yes \
  libgtk-3-0t64 libwebkit2gtk-4.1-0 gir1.2-webkit2-4.1
```

The `gui` extra also installs PyGObject and pycairo into uv's isolated tool
environment. They are built from source on Linux, so install their build
prerequisites before running `uv tool install`:

```sh
sudo apt install --yes \
  libcairo2-dev libgirepository-2.0-dev pkg-config gcc python3-dev
```

Then install the optional GTK integration:

```sh
uv tool install 'notification-hub[gui]'
```

For an editable install, run this from the repository root instead:

```sh
uv tool install --force --editable '.[gui]'
```

`--force` replaces an existing base or editable tool install so that
`nh-client` runs with the `gui` extra.

The project requires Python 3.12 or newer and uses GTK 3 with the WebKitGTK 4.1
API. The `3`, `4.1`, and `2.0` in these Ubuntu package names identify library
interfaces; they do not pin the exact package release. Use development headers
for the Python version used by `uv` (for example, `python3.12-dev` in place of
`python3-dev` if using Python 3.12 on a system whose default `python3` is
newer). The compiler and development packages are needed to build the Python
bindings during installation, but not to run the installed client. The Ubuntu
24.04 package set above is used by the GTK smoke test in CI; the same package
names are available on Ubuntu 26.04. On other Linux distributions, install the
equivalent GTK 3, WebKitGTK 4.1, and PyGObject prerequisites. If the backend is
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

See [Configuration](docs/configuration.md) for setup examples and all TOML
settings for the server, notifier, and client.

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
