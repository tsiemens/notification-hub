#!/bin/sh
set -eu

usage() {
    cat <<'EOF'
Usage: scripts/desktop-integration.sh {install|uninstall}

Install or remove the Notification Hub desktop entry and icon for the current user.
EOF
}

fail() {
    printf '%s\n' "$1" >&2
    exit 1
}

if [ "$#" -ne 1 ]; then
    usage >&2
    exit 2
fi

action=$1
case "$action" in
    install|uninstall) ;;
    -h|--help)
        usage
        exit 0
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac

case ${XDG_DATA_HOME:-} in
    /*) data_home=$XDG_DATA_HOME ;;
    *)
        : "${HOME:?HOME must be set when XDG_DATA_HOME is not an absolute path}"
        data_home=$HOME/.local/share
        ;;
esac

desktop_target=$data_home/applications/notification-hub.desktop
icon_target=$data_home/icons/hicolor/scalable/apps/notification-hub.svg

if [ "$action" = uninstall ]; then
    rm -f "$desktop_target" "$icon_target"
    printf 'Removed Notification Hub desktop entry and icon from %s\n' "$data_home"
    exit 0
fi

command -v uv >/dev/null 2>&1 || fail "uv is required to locate the installed Notification Hub package"
tool_dir=$(uv tool dir) || fail "Could not determine the uv tool directory"
tool_bin_dir=$(uv tool dir --bin) || fail "Could not determine the uv tool executable directory"
case $tool_bin_dir in
    /*) ;;
    *) fail "uv tool executable directory must be an absolute path: $tool_bin_dir" ;;
esac
client_path=$tool_bin_dir/nh-client
[ -x "$client_path" ] || fail "nh-client is not installed at $client_path; install notification-hub[gui] with uv first"
# Desktop Exec values do not expand shell variables. Quote the absolute path,
# escaping it for both the desktop-file string and Exec command-line syntax.
case $client_path in
    *'='*|*'%'*|*'
'*) fail "nh-client path contains a character unsupported by desktop entries: $client_path" ;;
esac
desktop_client_path=$(printf '%s' "$client_path" | sed \
    -e 's/\\/\\\\\\\\/g' \
    -e 's/"/\\\\"/g' \
    -e 's/`/\\\\`/g' \
    -e 's/\$/\\\\$/g')
tool_env=$tool_dir/notification-hub
resource_dir=

if [ -d "$tool_env" ]; then
    desktop_source=$(find "$tool_env" -type f \
        -path '*/notification_hub/gui/resources/notification-hub.desktop.template' -print -quit)
    if [ -n "$desktop_source" ] && [ -f "${desktop_source%.desktop.template}.svg" ]; then
        resource_dir=${desktop_source%/*}
    fi
fi

# Editable uv tool installs point back to the source checkout instead of
# copying package resources into site-packages.
if [ -z "$resource_dir" ]; then
    script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
    checkout_resources=$script_dir/../src/notification_hub/gui/resources
    if [ -f "$checkout_resources/notification-hub.desktop.template" ] \
        && [ -f "$checkout_resources/notification-hub.svg" ]; then
        resource_dir=$checkout_resources
    fi
fi

[ -n "$resource_dir" ] || fail "Could not find desktop resources; install notification-hub[gui] with uv first"

mkdir -p "${desktop_target%/*}" "${icon_target%/*}"
desktop_temp=$(mktemp "${desktop_target}.XXXXXX")
trap 'rm -f "$desktop_temp"' EXIT HUP INT TERM
while IFS= read -r line || [ -n "$line" ]; do
    case $line in
        Exec=@NH_CLIENT_EXEC@) printf 'Exec="%s"\n' "$desktop_client_path" ;;
        *) printf '%s\n' "$line" ;;
    esac
done < "$resource_dir/notification-hub.desktop.template" > "$desktop_temp"
install -m 644 "$desktop_temp" "$desktop_target"
install -m 644 "$resource_dir/notification-hub.svg" "$icon_target"

printf 'Installed Notification Hub desktop entry: %s\n' "$desktop_target"
printf 'Installed Notification Hub icon: %s\n' "$icon_target"
