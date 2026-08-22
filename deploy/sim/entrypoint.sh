#!/bin/bash

CONF="/opt/physicar/src/physicar-ros/deploy/sim/supervisord.conf"
DEPLOY_DIR="$(dirname "$CONF")"

# ── Ensure src tree ownership (physicar) ──
# If the image build cloned as root, src/ ends up root-owned — then sim_api
# (running as physicar) dies with PermissionError → 502 on world imports
# (writes under share/), and the updater's git fetch cannot write .git so
# tag updates stop entirely (both seen in production). The entrypoint runs
# as physicar, so repair idempotently with sudo (NOPASSWD) on every boot.
if [ -d /opt/physicar/src ]; then
  sudo mkdir -p /opt/physicar/src/physicar-sim/share/worlds 2>/dev/null || true
  sudo chown -R physicar:physicar /opt/physicar/src 2>/dev/null || true
fi

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

# ── Stale bake self-heal (images built before current layout) ──
# (a) Root-owned /tmp/pc-*.conf|map baked into the image survive in sticky /tmp
#     and block the rewrites below → nginx dies (measured on cloud, twice).
# (b) A leftover /etc/nginx/conf.d/pc-gate.conf symlink from before the
#     zz-pc-gate.conf rename makes nginx fail: map_hash_bucket_size duplicate.
sudo rm -f /tmp/pc-root.conf /tmp/pc-gate.map 2>/dev/null || true
if [ -L /etc/nginx/conf.d/pc-gate.conf ] || [ -e /etc/nginx/conf.d/pc-gate.conf ]; then
  sudo rm -f /etc/nginx/conf.d/pc-gate.conf 2>/dev/null || true
  sudo ln -sf "$DEPLOY_DIR/etc/nginx/conf.d/zz-pc-gate.conf" /etc/nginx/conf.d/zz-pc-gate.conf 2>/dev/null || true
fi

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

# ── code-server version convergence (repo pin, idempotent) ──
# deploy/code-server-version is the single source of truth — install that
# version when the installed one differs. Prevents bake pinning: one pin-line
# update brings every instance along on its next boot, no AMI rebake.
# Before raising the pin, download the new release tar and grep that the
# patch patterns below (B/C·folder) and the branding paths still hold.
# Failure/offline/timeout keeps the current version — never block boot
# (retried next boot). supervisord (= code-server) is not running yet at
# this point, so no restart is needed. Codespaces does not use code-server,
# so skip there.
ROS_DIR="$(dirname "$(dirname "$DEPLOY_DIR")")"
if [ -z "${CODESPACE_NAME:-}" ]; then
  CS_PIN=$(tr -d '[:space:]' < "$ROS_DIR/deploy/code-server-version" 2>/dev/null || true)
  # The first --version line can be a config-file creation log line — pick only the version line
  CS_CUR=$(code-server --version 2>/dev/null | grep -oEm1 '^[0-9]+\.[0-9]+\.[0-9]+' || true)
  if [ -n "$CS_PIN" ] && [ "$CS_PIN" != "$CS_CUR" ]; then
    echo "[code-server] ${CS_CUR:-none} -> $CS_PIN"
    timeout 300 bash -c "curl -fsSL https://code-server.dev/install.sh | sudo sh -s -- --version='$CS_PIN'" \
      || echo "[code-server] WARNING: update failed — keeping ${CS_CUR:-none}"
  fi
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

  # CSP hash resync: recent code-server pins the webview index.html inline
  # script with a CSP 'sha256-…'. The moment pattern C edits that script
  # the hash mismatches and the browser blocks it → every webview
  # (extension panels·custom editors) renders blank.
  # → Recompute each patched HTML's inline-script sha256 and fix the CSP (idempotent).
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

# ── Webview service-worker non-blocking patch (idempotent, every boot) ──
# VS Code's webview bootstrap (pre/index.html) AWAITS a ServiceWorker
# update() before rendering any webview content, and Chrome throttles
# repeated update jobs on one registration by ~60s (several webviews x page
# reloads hit this constantly) — a throttled job blanked every panel for the
# full delay: the "sometimes instant, sometimes 1-2 min blank" lottery
# (real devices and cloud sim alike; measured 59s stalls on-device).
# The SW scope embeds the build commit (/stable-<commit>/...), so a
# code-server update lands on a fresh scope and stale resources are
# impossible — the blocking wait is redundant; make it fire-and-forget and
# resync the CSP script hash. A code-server update restores the stock file,
# so re-apply every boot (no-op when already patched).
patch_codeserver_webview_sw() {
  local cs_bin cs_vscode f
  cs_bin=$(readlink -f "$(command -v code-server)" 2>/dev/null) || return 0
  cs_vscode=$(dirname "$cs_bin")/../lib/vscode
  [ -d "$cs_vscode/out" ] || cs_vscode=/usr/lib/code-server/lib/vscode
  f="$cs_vscode/out/vs/workbench/contrib/webview/browser/pre/index.html"
  [ -f "$f" ] || { echo "[sw-patch] pre/index.html not found"; return 0; }
  sudo python3 - "$f" <<'PYSW' || true
import base64, hashlib, re, sys
p = sys.argv[1]
src = open(p, encoding="utf-8").read()
old = "registration = await registration.update();"
if old not in src:
    if "registration.update().catch" not in src:
        print("[sw-patch] WARNING: known pattern not found in this code-server version — webviews may stall behind Chrome's SW update throttle until this patch is updated")
    sys.exit(0)
m = re.search(r'(<script async type="module">)(.*?)(</script>)', src, re.S)
if not m:
    print("[sw-patch] WARNING: inline script block not found"); sys.exit(0)
script = m.group(2).replace(old,
    "/* PhysiCar: fire-and-forget — Chrome throttles repeated update() jobs\n"
    "\t\t\t\t\t\t   ~60s and awaiting blanks every webview for the delay; the SW\n"
    "\t\t\t\t\t\t   scope embeds the build commit, so stale resources are\n"
    "\t\t\t\t\t\t   impossible after a code-server update. */\n"
    "\t\t\t\t\t\tregistration.update().catch(() => {});", 1)
out = src[:m.start(2)] + script + src[m.end(2):]
h = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode()
out = re.sub(r"script-src 'sha256-[A-Za-z0-9+/=]+'", "script-src 'sha256-" + h + "'", out, count=1)
open(p, "w", encoding="utf-8", newline="\n").write(out)
print("[sw-patch] webview SW update made non-blocking")
PYSW
}
patch_codeserver_webview_sw || true

# ── code-server default folder patch (idempotent, every boot) ──
# Open a bare / (no query) at the default workspace (/home/physicar/physicar_ws).
# The workbench reads `folder` only from the browser address bar, so a
# server-side redirect (302) dirties the bar with ?folder=…, and briefly
# rewriting the bar races the moment the workbench reads the query →
# planting a "default folder when the query is absent" fallback directly in
# the workspace-provider bundle is the only deterministic option.
# (The nginx internal rewrite suppresses the server 302; this patch supplies
# the client-side default.)
patch_codeserver_default_folder() {
  local cs_bin cs_vscode wb
  cs_bin=$(readlink -f "$(command -v code-server)" 2>/dev/null) || return 0
  cs_vscode=$(dirname "$cs_bin")/../lib/vscode
  [ -d "$cs_vscode/out" ] || cs_vscode=/usr/lib/code-server/lib/vscode
  wb="$cs_vscode/out/vs/code/browser/workbench/workbench.js"
  [ -f "$wb" ] || { echo "[folder-patch] workbench.js not found"; return 0; }
  grep -q 'pc-default-folder' "$wb" && return 0
  sudo python3 - "$wb" <<'PYFOLD'
import sys
p = sys.argv[1]
s = open(p, encoding='utf-8').read()
old = 'new URL(document.location.href).searchParams.forEach'
new = ('(()=>{/*pc-default-folder*/const u=new URL(document.location.href);'
       'u.searchParams.has("folder")||u.searchParams.has("workspace")||u.searchParams.has("ew")||'
       'u.searchParams.set("folder","/home/physicar/physicar_ws");return u})().searchParams.forEach')
n = s.count(old)
if n != 1:
    print('[folder-patch] WARNING: pattern x%d (expected 1) — skipped, default folder inactive' % n)
    sys.exit(0)
open(p, 'w', encoding='utf-8').write(s.replace(old, new))
print('[folder-patch] default folder patched into workbench.js')
PYFOLD
}
patch_codeserver_default_folder || true

# ── Branding re-apply (idempotent, every boot) ──
# Favicon/PWA/titlebar icons live inside the install tree, so a code-server
# update reverts them — re-cover them every boot for the same reason as the
# media patch (same logic as install-sim.sh's branding block; sudo is needed
# here because we run as the physicar user).
brand_codeserver() {
  local static res media om b64 _svg _png
  static="$ROS_DIR/physicar_webserver/static"
  [ -f "$static/favicon.ico" ] || return 0
  res=$(find /usr/lib /usr/local/lib -path '*code-server*/lib/vscode/resources/server' -type d 2>/dev/null | head -1)
  if [ -n "$res" ]; then
    sudo cp "$static/favicon.ico" "$res/favicon.ico"
    sudo cp "$static/img/code-192.png" "$res/code-192.png"
    sudo cp "$static/img/code-512.png" "$res/code-512.png"
  fi
  media=$(find /usr/lib /usr/local/lib -path '*code-server*/src/browser/media' -type d 2>/dev/null | head -1)
  if [ -n "$media" ]; then
    sudo cp "$static/favicon.ico" "$media/favicon.ico"
    b64=$(base64 -w0 "$static/img/code-192.png")
    for _svg in favicon.svg favicon-dark-support.svg; do
      printf '<svg xmlns="http://www.w3.org/2000/svg" width="192" height="192"><image width="192" height="192" href="data:image/png;base64,%s"/></svg>' "$b64" | sudo tee "$media/$_svg" >/dev/null
    done
    for _png in pwa-icon-192.png pwa-icon-maskable-192.png; do
      [ -f "$media/$_png" ] && sudo cp "$static/img/code-192.png" "$media/$_png"
    done
    for _png in pwa-icon-512.png pwa-icon-maskable-512.png; do
      [ -f "$media/$_png" ] && sudo cp "$static/img/code-512.png" "$media/$_png"
    done
  fi
  om=$(find /usr/lib /usr/local/lib -path '*code-server*/lib/vscode/out/media' -type d 2>/dev/null | head -1)
  if [ -n "$om" ] && [ -f "$om/code-icon.svg" ]; then
    b64=$(base64 -w0 "$static/img/code-192.png")
    printf '<svg xmlns="http://www.w3.org/2000/svg" width="192" height="192"><image width="192" height="192" href="data:image/png;base64,%s"/></svg>' "$b64" | sudo tee "$om/code-icon.svg" >/dev/null
  fi
  return 0
}
brand_codeserver || true

# ── Notification Do-Not-Disturb default seed (idempotent) ──
# Hide the bottom-right notification popups except errors. DND is not a
# settings.json key but a toggle in the global state DB (state.vscdb), so
# seed it here. If the key already exists (the user toggled it themselves),
# respect their choice. Failures never block boot (best-effort).
python3 - <<'PYDND' || true
import sqlite3, os
p = os.path.expanduser('~/.local/share/code-server/User/globalStorage/state.vscdb')
os.makedirs(os.path.dirname(p), exist_ok=True)
try:
    db = sqlite3.connect(p, timeout=3)
    db.execute('CREATE TABLE IF NOT EXISTS ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)')
    cur = db.execute("SELECT value FROM ItemTable WHERE key='notifications.doNotDisturbMode'").fetchone()
    if cur is None:
        db.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES ('notifications.doNotDisturbMode','true')")
        db.commit()
    db.close()
except Exception:
    pass
PYDND

# ── settings.json merge seed (symlink → real-file transition) ──
# The old symlink (repo file, root-owned/read-only) made user settings saves
# fail, resurrecting an unsaved settings.json editor every session.
# → Switch to a user-owned real file and merge managed defaults each boot
#   (user-changed keys win; new default keys keep propagating).
python3 - "$DEPLOY_DIR/home/physicar/.local/share/code-server/User/settings.json" <<'PYSET' || true
import json, os, sys
managed_path = sys.argv[1]
user_path = os.path.expanduser('~/.local/share/code-server/User/settings.json')
os.makedirs(os.path.dirname(user_path), exist_ok=True)
try:
    managed = json.load(open(managed_path))
except Exception:
    sys.exit(0)
user = {}
if os.path.islink(user_path):
    os.remove(user_path)   # remove the old symlink (cause of read-only save failures)
elif os.path.exists(user_path):
    try:
        user = json.load(open(user_path))
    except Exception:
        user = {}
merged = {**managed, **user}
if not os.path.exists(user_path) or merged != user:
    json.dump(merged, open(user_path, 'w'), indent=2, ensure_ascii=False)
PYSET
# If settings.json is root-owned (legacy of the golden bake's root cp),
# code-server (physicar) fails to save settings with EACCES (including the
# moment an extension such as Claude Code writes a settings key). The
# entrypoint runs as physicar, so repair with sudo — polluted instances
# heal on every boot.
sudo chown physicar:physicar /home/physicar/.local/share/code-server/User \
  /home/physicar/.local/share/code-server/User/settings.json 2>/dev/null || true



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

# ── physicar-ext freshness (prevents bake pinning) ─────────────────────────
# Extensions are pinned at AMI bake time, so on every boot (both fresh
# creation and resume re-run this script) check the marketplace for the
# latest and apply it before code-server starts.
# Failures (offline·open-vsx outage) are ignored — keep the baked version, never block boot.
if [ -z "${CODESPACE_NAME:-}" ]; then
  _ext_out=$(timeout 25 sudo -u physicar code-server --install-extension physicar.physicar-ext --force 2>&1) || true
  # The built-in baseline clone (install-sim.sh) makes code-server refuse
  # updates of the same id ("Incompatible: ... built-in extension") — on that
  # error, drop the clone, retry, and re-clone the fresh copy afterwards.
  if echo "$_ext_out" | grep -q "Incompatible"; then
    _cs_vscode=$(find /usr/lib /usr/local/lib -path '*code-server*/lib/vscode' -maxdepth 5 -type d 2>/dev/null | head -1)
    if [ -n "$_cs_vscode" ] && [ -d "$_cs_vscode/extensions/physicar-ext-builtin" ]; then
      sudo rm -rf "$_cs_vscode/extensions/physicar-ext-builtin"
      timeout 25 sudo -u physicar code-server --install-extension physicar.physicar-ext --force \
        >/dev/null 2>&1 || true
      _ext_dir=$(ls -d /home/physicar/.local/share/code-server/extensions/physicar.physicar-ext-* 2>/dev/null | sort -V | tail -1)
      [ -n "$_ext_dir" ] && sudo cp -r "$_ext_dir" "$_cs_vscode/extensions/physicar-ext-builtin" 2>/dev/null || true
    fi
  fi
  # jupyter without --force — self-healing install only when missing
  # (rescues generations whose golden image lacked it). Immediate no-op when
  # already present, so no boot delay.
  timeout 25 sudo -u physicar code-server --list-extensions 2>/dev/null | grep -qi '^ms-toolsai.jupyter$' \
    || timeout 60 sudo -u physicar code-server --install-extension ms-toolsai.jupyter \
      >/dev/null 2>&1 || true
fi

# ── Stale X lock cleanup (persistent container — /tmp survives restarts) ──
# After an instance stop→resume (or container restart), a leftover
# /tmp/.X1-lock and /tmp/.X11-unix/X1 from the previous Xvfb make Xvfb
# refuse to start ("Server is already active for display 1"). openbox/
# tint2/x11vnc then FATAL in a cascade, and with no DISPLAY Gazebo cannot
# render — simulator, camera and lidar all go blank (actual outage,
# 2026-08-04). Always clean before supervisord starts — if a live X exists,
# removing the lock is harmless (socket still in use); only dead leftovers
# are removed.
if ! pgrep -x Xvfb >/dev/null 2>&1; then
  rm -f /tmp/.X1-lock /tmp/.X11-unix/X1 2>/dev/null || true
fi

# Start supervisord
supervisord -c "$CONF"
sleep 2

# Start the student app only if one has actually been deployed. The myapp program
# is autostart=false so that a fresh sim with no run.sh never creates an empty
# run.log; here we bring it up when the script already exists (e.g. after a
# container restart). The web UI starts/restarts it on deploy.
if [ -f "/opt/physicar/userdata/myapp.sh" ]; then
  supervisorctl -c "$CONF" start myapp 2>/dev/null || true
fi
