#!/bin/bash

CONF="/opt/physicar/src/physicar-ros/deploy/sim/supervisord.conf"
DEPLOY_DIR="$(dirname "$CONF")"

# Clean up existing supervisord
pkill -f "supervisord.*supervisord.conf" 2>/dev/null || true
sleep 1

# Start supervisord
supervisord -c "$CONF"
sleep 2

# app.physicar bookmark
if [[ -n "${CODESPACE_NAME:-}" ]]; then
  (
    sudo chattr -i app.physicar 2>/dev/null
    echo "https://${CODESPACE_NAME}-80.app.github.dev/studio" > app.physicar
    chmod 444 app.physicar
    sudo chattr +i app.physicar
  ) 2>/dev/null &
else
  (
    sudo chattr -i app.physicar 2>/dev/null
    echo "http://localhost/studio" > app.physicar
    chmod 444 app.physicar
    sudo chattr +i app.physicar
  ) 2>/dev/null &
fi

# Codespaces-only services
if [[ -n "${CODESPACE_NAME:-}" ]]; then
  bash "$DEPLOY_DIR/memory-setup.sh" &>/tmp/memory-setup.log &
  bash "$DEPLOY_DIR/port-watchdog.sh" &>/tmp/port-watchdog.log &
fi
