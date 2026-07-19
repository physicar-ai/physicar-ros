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

# ── Origin gate map (see conf.d/pc-gate.conf) ──
# Cloud instances get $PHYSICAR_ORIGIN_GATE_SECRET from the control plane; the
# gate then 403s any request lacking the matching X-PhysiCar-Gate header (blocks
# gateway-bypassing proxying). Without a secret (localhost/Codespaces) → pass.
if [ -n "${PHYSICAR_ORIGIN_GATE_SECRET:-}" ]; then
  printf 'default "deny";\n"%s" "ok";\n' "$PHYSICAR_ORIGIN_GATE_SECRET" > /tmp/pc-gate.map
else
  printf 'default "pass";\n' > /tmp/pc-gate.map
fi
chmod 644 /tmp/pc-gate.map

STUDENT_WS="/home/physicar/physicar_ws"

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

  # CSP 해시 재동기화: 최신 code-server 의 webview index.html 은 인라인 스크립트를
  # CSP 'sha256-…' 로 고정한다. C 패턴이 그 스크립트를 수정하는 순간 해시가 어긋나
  # 브라우저가 스크립트를 차단 → webview(확장 패널·커스텀 에디터) 전면 빈 화면.
  # → 패치된 HTML 마다 인라인 스크립트의 sha256 을 재계산해 CSP 를 맞춘다 (멱등).
  while IFS= read -r f; do
    sudo python3 - "$f" <<'PYCSP'
import sys, re, hashlib, base64
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
m = re.search(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", s, re.S)
if not m: sys.exit(0)
h = base64.b64encode(hashlib.sha256(m.group(1).encode()).digest()).decode()
s2, n = re.subn(r"'sha256-[A-Za-z0-9+/=]+'", "'sha256-" + h + "'", s)
if n and s2 != s:
    open(p, 'w', encoding='utf-8').write(s2)
    print('[media-patch] CSP hash resynced: ' + p)
PYCSP
  done < <(grep -rlF "$C_NEW" "$cs_vscode/out" --include='*.html' 2>/dev/null)
}
patch_codeserver_webview_media || true


# Prune orphaned bytecode from the persistent pycache: entries whose source
# file was deleted or renamed (updates, student edits) would otherwise
# accumulate forever. Background — boot must not wait. A false delete is
# harmless (recompiles lazily); the cache stays bounded by the live sources.
(
  CACHE="/opt/physicar/pycache"
  if [ -d "$CACHE" ]; then
    find "$CACHE" -name '*.pyc' 2>/dev/null | while IFS= read -r pyc; do
      rel="${pyc#"$CACHE"}"
      src="$(dirname "$rel")/$(basename "$pyc" | cut -d. -f1).py"
      [ -f "$src" ] || rm -f "$pyc"
    done
    find "$CACHE" -type d -empty -delete 2>/dev/null
  fi
) &

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
