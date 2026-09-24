#!/bin/sh
set -eu

# /data is the add-on's persistent local volume. Make only that application
# state directory writable by the service account; never chown NAS media/share
# mounts, because doing so could alter ownership on the host or remote filesystem.
mkdir -p /data
chown xrob:xrob /data 2>/dev/null || true
find /data -maxdepth 1 -mindepth 1 -exec chown -h xrob:xrob {} + 2>/dev/null || true

exec gosu xrob:xrob "$@"
