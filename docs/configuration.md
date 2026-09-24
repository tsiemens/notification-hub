# Configuration

Notification Hub uses three separate TOML files. By default they live under
`$XDG_CONFIG_HOME/notification-hub/` (or `~/.config/notification-hub/` when
`XDG_CONFIG_HOME` is unset): `server.config.toml`, `notifier.config.toml`, and
`client.config.toml`. Each command accepts `--config PATH` to select another
file. Paths in the examples are illustrative; replace them for your machines.
Unknown settings are rejected. Settings omitted from a file use the defaults
described below.

The desktop client can open without its default config file. It displays
"No server is configured" until you add the client `[server]` and `[auth]`
tables and restart it. Desktop presentation settings can be saved before the
connection is configured. An explicit `nh-client --config PATH` still requires
that file to exist. `nh-client-cli` always requires a complete client config.

## Server: `server.config.toml`

```toml
[server]
host = "127.0.0.1"
port = 8765
database = "/home/you/.local/share/notification-hub/server.sqlite3"
request_body_limit_kib = 256
strict_database_permissions = true

[retention]
history_days = 7
max_pending_days = 7
event_history_days = 8
# max_event_count = 100000

[limits]
creates_per_minute = 120
pending_total = 100

[auth]
signature_max_age_seconds = 60
nonce_ttl_seconds = 300
max_outstanding_nonces_per_key = 128

[[auth.signing_keys]]
principal = "desktop-ui"
key_id = "desktop-ui"
public_key_file = "/etc/notification-hub/desktop-ui.pub"
scopes = ["read", "respond", "read_state"]

```

The server listens on loopback by default. Keep remote access on a trusted
network or use an encrypted tunnel. A conventional TLS-terminating reverse
proxy currently breaks signed client requests because the built-in server does
not use the forwarded public URL when verifying signatures; see
[Security and caveats](security.md). The database defaults to
`$XDG_DATA_HOME/notification-hub/server.sqlite3` (or
`~/.local/share/notification-hub/server.sqlite3`).
`strict_database_permissions` checks that database access is restricted.
`request_body_limit_kib` limits incoming request size.

`history_days` retains completed notifications, `max_pending_days` bounds the
age of unanswered requests, and `event_history_days` retains synchronization
events. Event history must be at least one day longer than notification history.
`max_event_count` is optional; when omitted, there is no event count cap.
`creates_per_minute` limits notification creation and `pending_total` limits
pending requests across the server.

Each signing key needs a unique `key_id`, a `principal`, the corresponding
`public_key_file`, and a nonempty `scopes` list. Supported scopes are `read`,
`respond`, and `read_state`. Give an inspection client `read`, plus `respond`
and `read_state` when needed. Producer endpoints do not use signing keys. The
three `[auth]` timing and nonce settings control signature age, nonce lifetime,
and the per-key number of outstanding nonces. See [Creating client signing
keys](signing-keys.md) to create and install key pairs.

`signature_max_age_seconds` must be at least 60 because the shipped clients
sign requests for 60 seconds. `nonce_ttl_seconds` must be at least 6 so that
new nonces remain usable after the clients' five-second safety margin.
The client nonce pool reduces its request size when the outstanding nonce
limit is below its default batch of 32.

## Notifier: `notifier.config.toml`

```toml
[server]
url = "https://hub.example"
verify_tls = true
connect_timeout_seconds = 3
request_timeout_seconds = 15

[defaults]
domain_from_hostname = true
sender = "nh-notifier"
priority = "normal"
```

`url` is required and may include a path prefix. Set `verify_tls = false`
only for a trusted local or test endpoint. The timeout settings are positive
seconds. By default the notifier uses its host name as the domain; set
`domain_from_hostname = false` to require `--domain` on the command line.
`sender` identifies the producer. `priority` can be
`low`, `normal`, `high`, or `urgent`. Command-line options such as `--domain`,
`--sender`, and `--priority` can override these defaults for a notification.
When waiting for a response, `--timeout` sets a local deadline that includes
network time. A response received after that deadline is treated as a timeout;
the server request may still be pending.

## Desktop and inspection client: `client.config.toml`

```toml
[server]
url = "https://hub.example"
verify_tls = true
connect_timeout_seconds = 3
request_timeout_seconds = 15

[auth]
key_id = "desktop-ui"
private_key_file = "/home/you/.config/notification-hub/desktop-ui.key"

[ui]
theme = "system"
sound = "response_required"
response_required_sound_path = ""
informational_sound_path = ""
hide_read = false
raw_markdown = false

[[views]]
id = "builds"
name = "Builds"

[[views.rules]]
domain_regex = "^build"
sender_regex = "^ci"

[[views.rules]]
tag_regex = "release"
```

The `[server]` URL is required for a configured client. Its TLS and timeout
options work as in the notifier. The `[auth]` table requires a `key_id` that
matches a server signing key and the path to its unencrypted PEM private key.
The key stays on the client machine. See [Creating client signing
keys](signing-keys.md).

The desktop Settings dialog edits the `[ui]` values and custom `[[views]]`.
`theme` is `system`, `light`, or `dark`. `sound` is `never`,
`response_required`, or `all`. `response_required_sound_path` and
`informational_sound_path` optionally name separate local audio files. An empty
path uses that notification type's generated sound. Existing `sound_path`
values are loaded into both fields until the settings are saved. `hide_read`
hides read notifications, while `raw_markdown` displays
message source instead of rendered Markdown. A custom view needs a unique
local `id`, a display `name`, and at least one `[[views.rules]]` entry. Rule
fields are `domain_regex`, `sender_regex`, and `tag_regex`; a rule may use one
or more of them. Multiple rules give a view multiple ways to match. The client
supports up to 64 custom views, 32 rules per view, and 1024 characters per
regular expression.

The inspection command `nh-client-cli` shares the client file but does not
use the desktop presentation settings.
