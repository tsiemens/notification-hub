#!/bin/sh
# Launch an isolated Notification Hub and walk through UI-oriented scenarios.
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
demo_dir=$(mktemp -d "${TMPDIR:-/tmp}/notification-hub-demo.XXXXXX")
config_home="$demo_dir/config"
config_dir="$config_home/notification-hub"
database="$demo_dir/server.sqlite3"
private_key="$demo_dir/desktop-ui.key"
public_key="$demo_dir/desktop-ui.pub"
server_config="$config_dir/server.config.toml"
notifier_config="$config_dir/notifier.config.toml"
client_config="$config_dir/client.config.toml"
server_log="$demo_dir/server.log"
client_log="$demo_dir/client.log"
approval_marker="$demo_dir/approved-command-ran"
server_pid=
client_pid=
approval_pid=
response_pid=

cleanup() {
    for pid in "$response_pid" "$approval_pid" "$client_pid" "$server_pid"; do
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
        fi
    done
    for pid in "$response_pid" "$approval_pid" "$client_pid" "$server_pid"; do
        if [ -n "$pid" ]; then
            wait "$pid" 2>/dev/null || true
        fi
    done
    if [ "${NH_DEMO_KEEP_STATE:-0}" = "1" ]; then
        printf '\nDemo state retained at %s\n' "$demo_dir"
    else
        rm -rf -- "$demo_dir"
    fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM HUP

for command in uv openssl curl; do
    if ! command -v "$command" >/dev/null 2>&1; then
        printf 'demo: required command not found: %s\n' "$command" >&2
        exit 1
    fi
done

case ${NH_DEMO_PORT:-} in
    '')
        port=$(uv run --project "$repo_root" python -c \
            'import socket; s=socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()')
        ;;
    *[!0-9]*)
        printf 'demo: NH_DEMO_PORT must be a numeric TCP port\n' >&2
        exit 1
        ;;
    *) port=$NH_DEMO_PORT ;;
esac
base_url="http://127.0.0.1:$port"
response_timeout=${NH_DEMO_RESPONSE_TIMEOUT:-300}

umask 077
mkdir -p "$config_dir"
openssl genpkey -algorithm ED25519 -out "$private_key" >/dev/null 2>&1
openssl pkey -in "$private_key" -pubout -out "$public_key" >/dev/null 2>&1
chmod 600 "$private_key"
chmod 644 "$public_key"

cat >"$server_config" <<EOF
[server]
host = "127.0.0.1"
port = $port
database = "$database"
strict_database_permissions = true

[limits]
creates_per_minute = 600
pending_total = 100

[[auth.signing_keys]]
principal = "demo-desktop-ui"
key_id = "demo-desktop-ui"
public_key_file = "$public_key"
scopes = ["read", "respond", "read_state"]
EOF

cat >"$notifier_config" <<EOF
[server]
url = "$base_url"
connect_timeout_seconds = 1
request_timeout_seconds = 5

[defaults]
domain_from_hostname = true
sender = "demo"
priority = "normal"
EOF

cat >"$client_config" <<EOF
[server]
url = "$base_url"
connect_timeout_seconds = 1
request_timeout_seconds = 30

[auth]
key_id = "demo-desktop-ui"
private_key_file = "$private_key"

[ui]
theme = "system"
sound = "response_required"
hide_read = false
raw_markdown = false
EOF

cd "$repo_root"
uv run nh-server --config "$server_config" >"$server_log" 2>&1 &
server_pid=$!

ready=0
attempt=0
while [ "$attempt" -lt 100 ]; do
    if curl --silent --fail --max-time 1 "$base_url/healthz" >/dev/null 2>&1; then
        ready=1
        break
    fi
    if ! kill -0 "$server_pid" 2>/dev/null; then
        break
    fi
    attempt=$((attempt + 1))
    sleep 0.1
done
if [ "$ready" -ne 1 ]; then
    printf 'demo: server did not become ready; log follows\n' >&2
    sed -n '1,160p' "$server_log" >&2
    exit 1
fi

printf '%s\n' \
    'Notification Hub visual demo' \
    "  server: $base_url" \
    "  fresh database: $database" \
    '' \
    'A desktop window will open. Keep this terminal visible for guided steps.'

uv run --extra gui nh-client --config "$client_config" >"$client_log" 2>&1 &
client_pid=$!
sleep 1
if ! kill -0 "$client_pid" 2>/dev/null; then
    printf 'demo: desktop client exited during startup; log follows\n' >&2
    sed -n '1,160p' "$client_log" >&2
    exit 1
fi

notify() {
    uv run nh-notifier --config "$notifier_config" "$@"
}

printf '\n[1/5] Sending cards that exercise domains, priorities, tags, Markdown, and details.\n'
notify send 'Release build completed' \
    --domain 'build-container-3' --sender 'ci' --priority low \
    --message '**Build 1842** completed successfully in 3m 17s.' \
    --tag 'workspace:notification-hub' --tag 'build:release' >/dev/null
notify send 'Test suite needs attention' \
    --domain 'test-container-1' --sender 'pytest' --priority high \
    --message '2 tests failed; 418 passed. See [testing guidance](https://docs.pytest.org/).' \
    --details '```text
FAILED tests/test_worker.py::test_retry
AssertionError: expected connected
```

Raw HTML stays inert: <script>alert("not run")</script>' \
    --tag 'workspace:notification-hub' --tag 'task:test' >/dev/null
notify send 'Production latency alert' \
    --domain 'production' --sender 'monitor' --priority urgent \
    --message 'The p95 latency crossed **750 ms** for five minutes.' \
    --details '- Region: `us-west`
- Current p95: `812 ms`
- Runbook link is intentionally omitted for this demo.' \
    --tag 'service:api' --tag 'alert:latency' >/dev/null
printf '%s\n' \
    'In the UI: switch between domains, expand “Test suite needs attention”,' \
    'and try marking one card read and unread.'

printf '\n[2/5] Creating a cancelled request so terminal response state is visible.\n'
cancelled_id=$(notify approve 'Obsolete staging deploy' \
    --domain 'deploy-container' --sender 'release-agent' --priority normal \
    --message 'This request will be cancelled by its producer.' --no-wait)
notify cancel "$cancelled_id" --reason 'superseded by a newer build' >/dev/null 2>&1

printf '\n[3/5] Verifying the fail-closed approval wrapper end to end.\n'
printf '%s\n' \
    'In the UI, find “Run the gated demo command?” and select Approve.' \
    'The terminal is waiting; only the signed UI response can release the command.'
XDG_CONFIG_HOME="$config_home" uv run "$repo_root/examples/approval-wrapper.sh" \
    'Run the gated demo command?' touch "$approval_marker" &
approval_pid=$!
set +e
wait "$approval_pid"
approval_status=$?
set -e
approval_pid=
if [ "$approval_status" -eq 0 ] && [ -f "$approval_marker" ]; then
    printf 'Verified: approval was received and the gated command ran.\n'
elif [ ! -e "$approval_marker" ]; then
    printf '%s\n' \
        'The gated command did not run (denied, timed out, or interrupted).' \
        'That demonstrates fail-closed behavior; rerun and approve to verify release.'
else
    printf 'demo: approval wrapper result was inconsistent\n' >&2
    exit 1
fi

printf '\n[4/5] Demonstrating optional and required response messages.\n'
printf '%s\n' \
    'Respond to “Choose a rollout action” in the UI.' \
    'Choosing Abort rollout exercises the required-message validation.'
notify send 'Choose a rollout action' \
    --domain 'deploy-container' --sender 'release-agent' --priority high \
    --message 'Canary error rate is **2.4%**. Choose how the automation should proceed.' \
    --details 'Retry continues immediately. Defer accepts an optional note. Abort requires a rationale.' \
    --tag 'release:2026.09' \
    --response-option retry 'Retry now' none primary \
    --response-option defer 'Defer' optional default \
    --response-option abort 'Abort rollout' required danger \
    --wait --timeout "$response_timeout" &
response_pid=$!
set +e
wait "$response_pid"
response_status=$?
set -e
response_pid=
if [ "$response_status" -eq 0 ]; then
    printf 'Verified: the custom response reached the waiting notifier.\n'
else
    printf 'The response waiter ended with status %s; its card remains available for inspection.\n' \
        "$response_status"
fi

printf '\n[5/5] Sending a small live burst. Watch the newest cards arrive at the top.\n'
burst=1
while [ "$burst" -le 5 ]; do
    notify send "Worker $burst finished" \
        --domain 'batch-workers' --sender "worker-$burst" --priority normal \
        --message "Processed **$((burst * 125))** records without errors." \
        --tag 'job:nightly-import' >/dev/null
    burst=$((burst + 1))
    sleep 0.15
done

printf '%s\n' \
    '' \
    'Demo scenarios are complete. Continue inspecting the UI, then close its' \
    'window (or press Ctrl-C here) to stop the server and remove all demo state.'
set +e
wait "$client_pid"
client_status=$?
set -e
client_pid=
if [ "$client_status" -ne 0 ]; then
    printf 'demo: desktop client exited with status %s; log follows\n' "$client_status" >&2
    sed -n '1,160p' "$client_log" >&2
    exit "$client_status"
fi
