#!/bin/sh
# Gate a command on an approval selected in Notification Hub.
set -eu

if [ "$#" -lt 2 ]; then
    echo "usage: approval-wrapper.sh SUMMARY COMMAND [ARG ...]" >&2
    exit 4
fi

summary=$1
shift

# nh-notifier exits 0 only for "approve". Denial, cancellation, and expiry are
# fail-closed and leave the wrapped command unexecuted.
set +e
nh-notifier approve "$summary" --message "Command: $*"
status=$?
set -e
if [ "$status" -eq 0 ]; then
    exec "$@"
fi

echo "approval not granted; command was not run" >&2
exit "$status"
