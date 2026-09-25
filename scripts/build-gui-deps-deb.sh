#!/bin/sh
set -eu

repo_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
control=$repo_root/packaging/ubuntu/gui-deps/DEBIAN/control
version=$(sed -n 's/^Version: //p' "$control")
output=$repo_root/src/notification_hub/gui/resources/notification-hub-gui-deps_${version}_all.deb
build_dir=$(mktemp -d)
trap 'rm -rf "$build_dir"' EXIT HUP INT TERM

chmod 755 "$build_dir"
mkdir -p "$build_dir/DEBIAN"
cp "$control" "$build_dir/DEBIAN/control"
SOURCE_DATE_EPOCH=0 dpkg-deb --build --root-owner-group "$build_dir" "$output"
