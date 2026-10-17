#!/bin/bash

CONF="/opt/physicar/src/physicar-ros/deploy/sim/supervisord.conf"
DEPLOY_DIR="$(dirname "$CONF")"

# ── Ensure src tree ownership (physicar) ──
# If the image build cloned as root, src/ ends up root-owned — then the
# updater's git fetch cannot write .git so tag updates stop entirely (seen in
# production). The entrypoint runs as physicar, so repair idempotently with
# sudo (NOPASSWD) on every boot.
if [ -d /opt/physicar/src ]; then
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
# (fuser needs root for root-owned nginx; SIGKILL is fine for orphans).
# The simulator (virtual hardware) runs in its own container on the docker
# network — nothing here starts or kills it.
sudo fuser -k 80/tcp 5000/tcp 5901/tcp 6080/tcp 8000/tcp 8080/tcp 2>/dev/null || true
pkill Xvfb 2>/dev/null || true
# The webserver's DEV auto-reload keeps a reloader parent that is not bound to
# :8000, so the port sweep above never reaches it — it then respawns the server
# with the environment of a previous supervisord generation (measured 2026-09-25:
# a stale webserver with the previous DDS environment kept serving 503 camera
# frames). supervisord is being restarted anyway, so sweep it explicitly.
pkill -f "webserver_node.py" 2>/dev/null || true
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

# Write the nginx root (/) snippet BEFORE nginx starts (supervisord child).
# / proxies code-server (started by supervisord).
# Copied (not symlinked): fs.protected_symlinks blocks root from following
# a physicar-owned symlink inside sticky /tmp.
cp -f "$DEPLOY_DIR/etc/nginx/root-code.conf" /tmp/pc-root.conf
chmod 644 /tmp/pc-root.conf

# ── Origin gate map (see conf.d/pc-gate.conf) ──
# Cloud instances get $PHYSICAR_ORIGIN_GATE_SECRET from the control plane; the
# gate then 403s any request lacking the matching X-PhysiCar-Gate header (blocks
# gateway-bypassing proxying). Without a secret (localhost) → pass.
if [ -n "${PHYSICAR_ORIGIN_GATE_SECRET:-}" ]; then
  printf 'default "deny";\n"%s" "ok";\n' "$PHYSICAR_ORIGIN_GATE_SECRET" > /tmp/pc-gate.map
else
  printf 'default "pass";\n' > /tmp/pc-gate.map
fi
chmod 644 /tmp/pc-gate.map

STUDENT_WS="/home/physicar/physicar_ws"

# ── code-server convergence (version pin · webview patches · branding · settings) ──
# Lives in deploy/sim/codeserver.sh so physicar.sh can re-run it after the boot-time tag
# update (the pin of a NEW tag otherwise lands one boot late).
ROS_DIR="$(dirname "$(dirname "$DEPLOY_DIR")")"
. "$DEPLOY_DIR/codeserver.sh"
codeserver_converge_all

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
    except Exception as e:
        # JSONC (comments, trailing commas) or a broken file: do not touch it.
        # Treating it as empty rewrote the user's whole file with the seed (2026-10-14 finding).
        # New seed keys do not reach such a file; the chat switch is inserted separately
        # (converge_codeserver_settings) and survives this skip.
        print("[settings] merge skipped — user settings.json is not plain JSON: " + str(e))
        sys.exit(0)
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
# The built-in baseline clone (install-sim.sh) makes code-server refuse a
# marketplace update of the same id. That refusal used to be matched on the
# text "Incompatible", but code-server 4.131 prints it as "Error while
# installing extension physicar.physicar-ext: [object Object]" — nothing to
# match, so every workspace silently stayed on its baked version (2026-09-17:
# sim fleet on 0.2.16 while 0.2.19 had been published for two days). Move the
# clone out of the way before installing — the update is then a plain user
# install — and re-clone the freshest user copy afterwards (or put the old
# clone back if that fails, so the Uninstall-proof copy never goes missing).
_cs_vscode=$(find /usr/lib /usr/local/lib -path '*code-server*/lib/vscode' -maxdepth 5 -type d 2>/dev/null | head -1)
_ext_bi="${_cs_vscode:+$_cs_vscode/extensions/physicar-ext-builtin}"
# The parked copy must leave lib/vscode/extensions ENTIRELY: code-server treats
# every directory in there as a built-in extension whatever its name, and then
# refuses the install with "is a built-in extension and not allowed to be
# updated" (measured 2026-09-25 with a sibling ".prev" — real version bumps never
# landed). Park it under /tmp instead.
_ext_park="/tmp/pc-ext-builtin.prev"
if [ -n "$_ext_bi" ] && [ -d "$_ext_bi" ]; then
  sudo rm -rf "$_ext_park"; sudo mv "$_ext_bi" "$_ext_park" 2>/dev/null || true
fi
# Major line = platform generation injected by the cloud (physicar → 1, physicar2 → 2).
_ext_gen="${PHYSICAR_GENERATION:-physicar}"
_ext_major="${_ext_gen#physicar}"; _ext_major="${_ext_major:-1}"
case "$_ext_major" in *[!0-9]*) _ext_major=1;; esac
# Only versions of the same major line are eligible: the extension's major
# tracks the platform generation (physicar → 1.x, physicar2 → 2.x), so a
# newer major published for a later generation never lands here.
_ext_want=$(timeout 15 curl -sf https://open-vsx.org/api/physicar/physicar-ext 2>/dev/null \
  | python3 -c '
import sys, json
major = int(sys.argv[1])
vs = []
for v in json.load(sys.stdin).get("allVersions", {}):
    p = v.split(".")
    if len(p) == 3 and all(x.isdigit() for x in p) and int(p[0]) == major:
        vs.append(tuple(int(x) for x in p))
print(".".join(map(str, max(vs))) if vs else "")' "$_ext_major" 2>/dev/null)
if [ -n "$_ext_want" ]; then
  _ext_out=$(timeout 60 sudo -u physicar code-server --install-extension "physicar.physicar-ext@$_ext_want" --force 2>&1) || true
  echo "$_ext_out" | grep -i "successfully installed\|already installed\|Error\|Failed" | head -3 | sed 's/^/[ext] /'
else
  echo "[ext] no physicar-ext ${_ext_major}.x on open-vsx (or offline) - keeping the installed version"
fi
if [ -n "$_ext_bi" ]; then
  _ext_dir=$(ls -d /home/physicar/.local/share/code-server/extensions/physicar.physicar-ext-* 2>/dev/null | sort -V | tail -1)
  if [ -n "$_ext_dir" ] && sudo cp -r "$_ext_dir" "$_ext_bi" 2>/dev/null; then
    basename "$_ext_dir" | sudo tee "$_ext_bi/.src" >/dev/null 2>&1 || true
    sudo rm -rf "$_ext_park"
    echo "[ext] built-in baseline = $(basename "$_ext_dir")"
  elif [ -d "$_ext_park" ]; then
    sudo mv "$_ext_park" "$_ext_bi" 2>/dev/null || true
  fi
fi
# Guard extension built-in (deploy/ext-guard): re-enables physicar-ext in-session
# when a student clicks Disable. Refreshed from the checkout on every boot so a
# ros tag update delivers it (install-sim.sh stages the image copy). Its own id —
# it never blocks the marketplace update above, so it needs no parking.
_ext_guard_src="$DEPLOY_DIR/../ext-guard"
if [ -n "$_cs_vscode" ] && [ -f "$_ext_guard_src/package.json" ]; then
  sudo rm -rf "$_cs_vscode/extensions/physicar-ext-guard"
  sudo mkdir -p "$_cs_vscode/extensions/physicar-ext-guard" 2>/dev/null \
    && sudo cp "$_ext_guard_src/package.json" "$_ext_guard_src/extension.js" "$_cs_vscode/extensions/physicar-ext-guard/" 2>/dev/null \
    || echo "[ext] WARNING: physicar-ext-guard built-in refresh failed"
fi
# jupyter without --force — self-healing install only when missing
# (rescues generations whose golden image lacked it). Immediate no-op when
# already present, so no boot delay.
timeout 25 sudo -u physicar code-server --list-extensions 2>/dev/null | grep -qi '^ms-toolsai.jupyter$' \
  || timeout 60 sudo -u physicar code-server --install-extension ms-toolsai.jupyter \
    >/dev/null 2>&1 || true

# ── Stale X lock cleanup (persistent container — /tmp survives restarts) ──
# After an instance stop→resume (or container restart), a leftover
# /tmp/.X1-lock and /tmp/.X11-unix/X1 from the previous Xvfb make Xvfb
# refuse to start ("Server is already active for display 1"). openbox/
# tint2/x11vnc then FATAL in a cascade and the desktop goes blank (actual
# outage, 2026-08-04). Always clean before supervisord starts — if a live X exists,
# removing the lock is harmless (socket still in use); only dead leftovers
# are removed.
if ! pgrep -x Xvfb >/dev/null 2>&1; then
  rm -f /tmp/.X1-lock /tmp/.X11-unix/X1 2>/dev/null || true
fi

# The startup script (System tab): the user's own boot commands. Created empty and
# physicar-owned, so the editor can save it and the `startup` program has a file to run.
if [ ! -f /opt/physicar/userdata/startup.sh ]; then
  sudo install -o physicar -g physicar -m 644 /dev/null /opt/physicar/userdata/startup.sh
fi

# Start supervisord
supervisord -c "$CONF"
# Second pass ~90s after code-server is up: a settings.json editor restored at startup can
# save its pre-boot content over the file (autosave) and undo the convergence above
# (seen 2026-10-14: the key written at boot was gone 6-17s after code-server start).
( sleep 90; converge_codeserver_settings "/home/physicar/.local/share/code-server/User/settings.json" ) >/dev/null 2>&1 &
sleep 2
