#!/bin/bash
# code-server convergence for the sim workspace — sourced, not executed.
#   version pin (deploy/code-server-version) · webview mic/cam allow-list · service-worker
#   non-blocking patch · default folder · branding · settings (chat switch)
# Used by entrypoint.sh (every boot, before supervisord) AND by physicar.sh right after the
# boot-time tag update changed the checkout: the entrypoint ran with the OLD pin, so a pin
# raised by a new tag would otherwise only take effect on the following boot (seen 2026-10-14:
# v1.2.3 checked out at boot, code-server still on the old pin until the next restart).
# Every function is idempotent; codeserver_converge_all never fails the caller.
ROS_DIR="${ROS_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
CS_UPDATED="${CS_UPDATED:-0}"

# ── code-server settings convergence (idempotent, every boot) ──
# The seed (deploy/*/home/.../User/settings.json) is copied ONCE; a key added to the
# seed later never reaches an existing instance (install-sim.sh pointed at a "boot
# merge" that did not exist). Only one key is converged on purpose — the AI chat
# switch: without it the stock Chat view sits next to PHYSICAR AI in the secondary
# side bar. A missing key is inserted; a key the user set (true or false) is left
# alone. Text insertion after the first "{" keeps comments and formatting intact.
converge_codeserver_settings() {
  local f="$1"
  [ -f "$f" ] && [ ! -L "$f" ] || return 0
  grep -q '"chat.disableAIFeatures"' "$f" && return 0
  python3 - "$f" <<'PYSET' || echo "[settings] WARNING: could not insert chat.disableAIFeatures"
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
i = s.find("{")
if i < 0: sys.exit(1)
nl = "\n" if "\n" in s[i:i + 3] else ""
s = s[:i + 1] + nl + '  "chat.disableAIFeatures": true,' + (nl or " ") + s[i + 1:].lstrip("\n") if nl else s[:i + 1] + ' "chat.disableAIFeatures": true,' + s[i + 1:]
open(p, "w", encoding="utf-8").write(s)
print("[settings] chat.disableAIFeatures: true added to " + p)
PYSET
}

# ── code-server version convergence (repo pin, idempotent) ──
# deploy/code-server-version is the single source of truth — install that
# version when the installed one differs. Prevents bake pinning: one pin-line
# update brings every instance along on its next boot, no AMI rebake.
# Before raising the pin, download the new release tar and grep that the
# patch patterns below (B/C·folder) and the branding paths still hold.
# Failure/offline/timeout keeps the current version — never block boot
# (retried next boot). supervisord (= code-server) is not running yet at
# this point, so no restart is needed.
# Sets CS_UPDATED=1 when a different version was installed (caller restarts code-server if it is running).
converge_codeserver_version() {
  CS_PIN=$(tr -d '[:space:]' < "$ROS_DIR/deploy/code-server-version" 2>/dev/null || true)
  # The first --version line can be a config-file creation log line — pick only the version line
  CS_CUR=$(code-server --version 2>/dev/null | grep -oEm1 '^[0-9]+\.[0-9]+\.[0-9]+' || true)
  if [ -n "$CS_PIN" ] && [ "$CS_PIN" != "$CS_CUR" ]; then
    echo "[code-server] ${CS_CUR:-none} -> $CS_PIN"
    # pipefail: `curl | sh` otherwise reports sh's status (0 on an empty download). Then verify
    # what is actually installed — only a confirmed switch counts (and triggers a restart).
    if timeout 300 bash -c "set -o pipefail; curl -fsSL https://code-server.dev/install.sh | sudo sh -s -- --version='$CS_PIN'" \
       && [ "$(code-server --version 2>/dev/null | grep -oEm1 '^[0-9]+\.[0-9]+\.[0-9]+' || true)" = "$CS_PIN" ]; then
      echo "[code-server] now $CS_PIN"
      CS_UPDATED=1
    else
      echo "[code-server] WARNING: update failed — keeping ${CS_CUR:-none}"
    fi
  fi
}

# ── code-server webview microphone/camera patch (idempotent, every boot) ──
# The install script patches once, but a code-server update restores the
# bundle — re-apply here (no-op when already patched).
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

# ── Built-in chat extension: disabled by default (one-time seed) ──
# The stock Chat view is the built-in GitHub.copilot-chat extension. VS Code keeps
# "chat.disableAIFeatures" in step with that extension's ENABLEMENT: while the extension
# is enabled, every extensions-service change sets the setting back to false (= removes
# the key), which is why the seeded key kept vanishing minutes after start (2026-10-14,
# verified in the 4.141 workbench bundle). Disabling the extension is therefore the
# switch VS Code itself honours — it then writes the setting to true and keeps it.
# One-time: a marker row means the seed ran once; whatever the user does afterwards
# (enabling Copilot) is respected. Skipped while code-server runs: it loads this list
# at start and would overwrite an external edit on its next write.
seed_disable_builtin_chat() {
  pgrep -f "code-server --bind-addr" >/dev/null 2>&1 && { echo "[chat-seed] code-server running — seed deferred to the next boot"; return 0; }
  python3 - <<'PYCHAT' || true
import json, os, sqlite3
p = os.path.expanduser('~/.local/share/code-server/User/globalStorage/state.vscdb')
os.makedirs(os.path.dirname(p), exist_ok=True)
EXT = 'GitHub.copilot-chat'
try:
    db = sqlite3.connect(p, timeout=3)
    db.execute('CREATE TABLE IF NOT EXISTS ItemTable (key TEXT UNIQUE ON CONFLICT REPLACE, value BLOB)')
    if db.execute("SELECT 1 FROM ItemTable WHERE key='physicar.builtinChatSeeded'").fetchone() is None:
        row = db.execute("SELECT value FROM ItemTable WHERE key='extensionsIdentifiers/disabled'").fetchone()
        lst = []
        if row and row[0]:
            try: lst = json.loads(row[0].decode() if isinstance(row[0], bytes) else row[0]) or []
            except Exception: lst = []
        if not any(isinstance(x, dict) and str(x.get('id', '')).lower() == EXT.lower() for x in lst):
            lst.append({'id': EXT})
            db.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES ('extensionsIdentifiers/disabled', ?)", (json.dumps(lst),))
            print('[chat-seed] built-in chat extension disabled (' + EXT + ')')
        db.execute("INSERT OR REPLACE INTO ItemTable (key, value) VALUES ('physicar.builtinChatSeeded', '1')")
        db.commit()
    db.close()
except Exception as e:
    print('[chat-seed] skipped: ' + str(e))
PYCHAT
}

# Run everything in the boot order the entrypoint always used. After a version install the
# stock bundle is back, so the patches and branding must follow it (they do).
codeserver_converge_all() {
  converge_codeserver_version || true
  seed_disable_builtin_chat || true
  converge_codeserver_settings "/home/physicar/.local/share/code-server/User/settings.json" || true
  patch_codeserver_webview_media || true
  patch_codeserver_webview_sw || true
  patch_codeserver_default_folder || true
  brand_codeserver || true
  return 0
}
