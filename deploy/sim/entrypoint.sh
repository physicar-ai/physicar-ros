#!/bin/bash

CONF="/opt/physicar/src/physicar-ros/deploy/sim/supervisord.conf"
DEPLOY_DIR="$(dirname "$CONF")"

# ── Stop any previous stack first (script is safe to re-run) ──
# A SIGTERM'd supervisord needs several seconds to stop its children; starting
# a new instance too early fails with "Another program is already listening on
# a port". Shut it down gracefully, WAIT until it is gone, then sweep orphans
# that survive an unclean death and keep the managed ports bound.
if [ -S /tmp/supervisor.sock ]; then
  supervisorctl -c "$CONF" shutdown >/dev/null 2>&1 || true
fi
pkill -f "supervisord.*deploy/sim/supervisord.conf" 2>/dev/null || true
for _ in $(seq 1 20); do
  pgrep -f "supervisord.*deploy/sim/supervisord.conf" >/dev/null || break
  sleep 1
done
pkill -9 -f "supervisord.*deploy/sim/supervisord.conf" 2>/dev/null || true

# Orphan sweep: whatever still holds a managed port or the X display
# (fuser needs root for root-owned nginx; SIGKILL is fine for orphans)
sudo fuser -k 80/tcp 5000/tcp 5901/tcp 6080/tcp 8000/tcp 8080/tcp 9002/tcp 9003/tcp 2>/dev/null || true
pkill -f "gz sim" 2>/dev/null || true
pkill -f "gz-launch" 2>/dev/null || true
pkill Xvfb 2>/dev/null || true
sleep 1

# Select the nginx root (/) snippet BEFORE nginx starts (supervisord child).
# Codespaces: / is not served (VS Code web is the Codespace itself).
# Local sim: / proxies code-server (started by supervisord, non-Codespaces only).
# Copied (not symlinked): fs.protected_symlinks blocks root from following
# a physicar-owned symlink inside sticky /tmp.
if [ -n "${CODESPACE_NAME:-}" ]; then
  cp -f "$DEPLOY_DIR/etc/nginx/root-404.conf" /tmp/pc-root.conf
else
  cp -f "$DEPLOY_DIR/etc/nginx/root-code.conf" /tmp/pc-root.conf
fi
chmod 644 /tmp/pc-root.conf

# ── User-data layout migration (one-time) ──
# MyApp and DeepRacer user data moved into the student workspace:
#   ~/physicar_ws/myapp.sh|myapp.log      → ~/physicar_ws/myapp/run.sh|run.log
#   /opt/physicar/userdata/deepracer/...  → ~/physicar_ws/deepracer/...
STUDENT_WS="/home/physicar/physicar_ws"
mkdir -p "$STUDENT_WS/myapp" "$STUDENT_WS/deepracer/models"
if [ -f "$STUDENT_WS/myapp.sh" ] && [ ! -f "$STUDENT_WS/myapp/run.sh" ]; then
  mv "$STUDENT_WS/myapp.sh" "$STUDENT_WS/myapp/run.sh"
fi
if [ -f "$STUDENT_WS/myapp.log" ] && [ ! -f "$STUDENT_WS/myapp/run.log" ]; then
  mv "$STUDENT_WS/myapp.log" "$STUDENT_WS/myapp/run.log"
fi
if [ -d /opt/physicar/userdata/deepracer/models ]; then
  cp -an /opt/physicar/userdata/deepracer/. "$STUDENT_WS/deepracer/" \
    && rm -rf /opt/physicar/userdata/deepracer
fi

# ── code-server webview microphone/camera patch (idempotent, every boot) ──
# The install script patches once, but a code-server update restores the
# bundle — re-apply here (no-op when already patched, or in Codespaces
# where the bundle string simply won't match anything running).
patch_codeserver_webview_media() {
  local cs_bin cs_vscode
  cs_bin=$(readlink -f "$(command -v code-server)" 2>/dev/null) || return 0
  cs_vscode=$(dirname "$cs_bin")/../lib/vscode
  [ -d "$cs_vscode/out" ] || cs_vscode=/usr/lib/code-server/lib/vscode
  [ -d "$cs_vscode/out" ] || { echo "[media-patch] vscode bundle not found"; return 0; }

  # Allow-list patterns per code-server generation (each patched idempotently):
  #  A) legacy literal allow string
  #  B) 4.12x workbench JS — allow list built as a JS array
  #  C) 4.12x inner webview iframe (pre/index.html) — allowRules array
  local A_OLD='clipboard-read; clipboard-write'
  local A_NEW='clipboard-read; clipboard-write; microphone; camera'
  local B_OLD='"cross-origin-isolated","autoplay","local-network-access"'
  local B_NEW='"cross-origin-isolated","autoplay","local-network-access","microphone","camera"'
  local C_OLD="'cross-origin-isolated;', 'autoplay;', 'local-network-access;'"
  local C_NEW="'cross-origin-isolated;', 'autoplay;', 'local-network-access;', 'microphone;', 'camera;'"

  local n=0 f changed
  while IFS= read -r f; do
    changed=0
    if grep -qF "$A_OLD" "$f" && ! grep -qF "$A_NEW" "$f"; then
      sudo sed -i "s/$A_OLD/$A_NEW/g" "$f" && changed=1
    fi
    if grep -qF "$B_OLD" "$f" && ! grep -qF "$B_NEW" "$f"; then
      sudo sed -i "s/$B_OLD/$B_NEW/g" "$f" && changed=1
    fi
    if grep -qF "$C_OLD" "$f" && ! grep -qF "$C_NEW" "$f"; then
      sudo sed -i "s|$C_OLD|$C_NEW|g" "$f" && changed=1
    fi
    [ "$changed" = "1" ] && n=$((n+1))
  done < <(grep -rlF -e "$A_OLD" -e "$B_OLD" -e "$C_OLD" "$cs_vscode/out" 2>/dev/null)
  echo "[media-patch] patched $n file(s) under $cs_vscode/out"

  # Silent-failure guard: after patching, at least one file must carry one of
  # the patched allow-lists. If none do, a code-server update changed the
  # pattern shape (it happened at 4.12x already) — warn loudly so it shows up
  # in the boot log instead of mic/cam just silently breaking.
  if ! grep -rqF -e "$A_NEW" -e "$B_NEW" -e "$C_NEW" "$cs_vscode/out" 2>/dev/null; then
    echo "[media-patch] WARNING: no known allow-list pattern found in this code-server version — webview mic/cam will stay blocked until the patterns in this function are updated"
  fi
}
patch_codeserver_webview_media || true

# Start supervisord
supervisord -c "$CONF"
sleep 2

# Start the student app only if one has actually been deployed. The myapp program
# is autostart=false so that a fresh sim with no run.sh never creates an empty
# run.log; here we bring it up when the script already exists (e.g. after a
# container restart). The web UI starts/restarts it on deploy.
if [ -f "$STUDENT_WS/myapp/run.sh" ]; then
  supervisorctl -c "$CONF" start myapp 2>/dev/null || true
fi
