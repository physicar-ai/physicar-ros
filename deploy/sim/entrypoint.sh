#!/bin/bash

CONF="/opt/physicar/src/physicar-ros/deploy/sim/supervisord.conf"
DEPLOY_DIR="$(dirname "$CONF")"

# Clean up existing supervisord
pkill -f "supervisord.*supervisord.conf" 2>/dev/null || true
sleep 1

# Start supervisord
supervisord -c "$CONF"
sleep 2

# Start the student app only if one has actually been deployed. The myapp program
# is autostart=false so that a fresh sim with no myapp.sh never creates an empty
# myapp.log; here we bring it up when the script already exists (e.g. after a
# container restart). The web UI starts/restarts it on deploy.
if [ -f /home/physicar/physicar_ws/myapp.sh ]; then
  supervisorctl -c "$CONF" start myapp 2>/dev/null || true
fi
