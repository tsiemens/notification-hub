#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$repo_root/frontend"
npm ci
npm run build
cd "$repo_root"

if [ ! -f src/notification_hub/gui/web/index.html ] \
    || [ ! -f src/notification_hub/gui/web/assets/index.js ] \
    || [ ! -f src/notification_hub/gui/web/assets/index.css ]; then
    echo "release assets are incomplete: the packaged HTML, JS, and CSS are required" >&2
    exit 1
fi

if [ -n "$(git status --porcelain -- src/notification_hub/gui/web)" ]; then
    echo "release assets are stale; commit the frontend production build" >&2
    git status --short -- src/notification_hub/gui/web >&2
    exit 1
fi
