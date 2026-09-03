#!/bin/bash
# External liveness probe for sim_api.
#
# A gz-transport request that blocks while HOLDING THE GIL (seen when the gz
# server dies/restarts mid-request, e.g. a world switch racing a pose call)
# freezes the whole Python interpreter — even /status stops answering and the
# IN-PROCESS watchdog can never run again. Recovery therefore has to come
# from outside the process: three straight failed /status probes (~15-30s —
# /status stays responsive through world switches and heavy load, so
# consecutive failures really mean frozen) — kill it and let supervisord's
# autorestart bring up a fresh one.
FAILS=0
while true; do
  sleep 5
  if curl -sf -m 5 http://127.0.0.1:9003/status > /dev/null; then
    FAILS=0
    continue
  fi
  PID=$(pgrep -of 'sim_api\.py')
  if [ -z "$PID" ]; then
    FAILS=0   # not running at all — that is supervisord's department
    continue
  fi
  FAILS=$((FAILS + 1))
  echo "$(date -Is) sim_api probe failed ($FAILS/3) pid=$PID"
  if [ "$FAILS" -ge 3 ]; then
    echo "$(date -Is) sim_api unresponsive — killing pid $PID for respawn"
    kill -9 "$PID" 2>/dev/null
    # sim_api's respawn restarts gz, and the launch-managed ros_gz_bridge
    # stays wedged on the DEAD gz instance (driving silently stops working).
    # Bounce it too — the launch respawns it against the fresh gz.
    sleep 20
    pkill -9 -f parameter_bridge 2>/dev/null
    echo "$(date -Is) bounced ros_gz_bridge for the fresh gz instance"
    FAILS=0
  fi
done
