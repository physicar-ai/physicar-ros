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
  # Anchored: the supervisord wrapper's command line (bash -c "updater.sh --boot; exec python3 ...sim_api.py")
  # also contains "sim_api.py" — matching it killed the boot update pass 15 s in (seen 2026-09-13 01:30)
  PID=$(pgrep -of '^python3 [^ ]*sim_api\.py')
  if [ -z "$PID" ]; then
    FAILS=0   # not running at all — that is supervisord's department
    continue
  fi
  # Startup grace: a freshly exec'd sim_api spends 10-20 s importing under boot
  # load and cannot answer /status yet — that is not a freeze. Counting those
  # killed a healthy boot 11 s in (2026-09-14 23:47:23); worse, the SIGUSR1 landed
  # before faulthandler was registered and terminated the process outright.
  AGE=$(ps -o etimes= -p "$PID" 2>/dev/null | tr -d ' ')
  if [ "${AGE:-0}" -lt 90 ]; then
    FAILS=0
    continue
  fi
  FAILS=$((FAILS + 1))
  echo "$(date -Is) sim_api probe failed ($FAILS/3) pid=$PID"
  if [ "$FAILS" -ge 3 ]; then
    echo "$(date -Is) sim_api unresponsive — killing pid $PID for respawn"
    # Evidence first, kill second: sim_api registers faulthandler on SIGUSR1, so this
    # dumps every thread's stack into its log — the exact gz call it froze in. Plus a
    # gz-side snapshot (does gz-transport answer? how busy is gz?) to tell "gz went
    # silent" apart from "sim_api wedged on its own".
    kill -USR1 "$PID" 2>/dev/null; sleep 1
    GZPID=$(pgrep -of 'gz sim -s' || true)
    echo "$(date -Is) gz pid=${GZPID:-none} cpu=$( [ -n "$GZPID" ] && ps -o %cpu= -p "$GZPID" | tr -d ' ' ) load=$(cut -d' ' -f1-3 /proc/loadavg)"
    T0=$(date +%s%N); if GZ_PARTITION=physicar timeout 8 gz topic -l >/dev/null 2>&1; then GZR=ok; else GZR=FAIL; fi
    echo "$(date -Is) gz-transport probe: $GZR in $(( ($(date +%s%N) - T0) / 1000000 )) ms"
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
