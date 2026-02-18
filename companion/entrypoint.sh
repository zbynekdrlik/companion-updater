#!/bin/bash
set -euo pipefail

# Ensure runtime dirs exist and clean stale pid files
mkdir -p /var/run/dbus
rm -f /var/run/dbus/pid /run/dbus/pid

# Add companion user to USB group for Stream Deck access
# Create the group if it doesn't exist
if ! getent group 983 >/dev/null 2>&1; then
  groupadd -g 983 companionusb 2>/dev/null || true
fi
# Add companion user to the group
usermod -a -G 983 companion 2>/dev/null || true

if ! pgrep -x dbus-daemon >/dev/null 2>&1; then
  dbus-daemon --system
fi

if ! pgrep -x avahi-daemon >/dev/null 2>&1; then
  avahi-daemon -D
fi

# Start Cloudflare tunnel if token is provided
if [ -n "${CLOUDFLARE_TUNNEL_TOKEN:-}" ]; then
  if ! pgrep -x cloudflared >/dev/null 2>&1; then
    echo "[entrypoint] Starting cloudflared tunnel"
    cloudflared tunnel --no-autoupdate run --token "$CLOUDFLARE_TUNNEL_TOKEN" &
  fi
fi

MODULE_ROOT="/companion/modules/user"
if command -v npm >/dev/null 2>&1 && [ -d "$MODULE_ROOT" ]; then
  for module_dir in "$MODULE_ROOT"/presenter-*; do
    if [ -d "$module_dir" ] && [ -f "$module_dir/package.json" ]; then
      if [ ! -d "$module_dir/node_modules" ]; then
        echo "[entrypoint] Installing dependencies for $(basename "$module_dir")"
        runuser -u companion -- bash -lc "cd '$module_dir' && npm install --production --no-audit --no-fund"
      fi
    fi
  done
fi

exec /usr/sbin/runuser -u companion -- /docker-entrypoint.sh "$@"
