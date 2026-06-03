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
(
  sudo chattr -i app.physicar 2>/dev/null
  if [[ -n "${CODESPACE_NAME:-}" ]]; then
    echo "https://${CODESPACE_NAME}-80.app.github.dev/studio" > app.physicar
  else
    echo "http://localhost/studio" > app.physicar
  fi
  chmod 444 app.physicar
  sudo chattr +i app.physicar
) 2>/dev/null &
