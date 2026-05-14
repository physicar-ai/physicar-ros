#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  Physicar Boot Script - Runs on every boot via physicar.service
# ═══════════════════════════════════════════════════════════════════════════════

# ────────────────── Hardware Optimization ──────────────────

# Swap Memory (4GB) - Create if not exists
if [ -z "$(swapon --show)" ]; then
  if [ ! -f /swapfile ]; then
    fallocate -l 4G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
  fi
  swapon /swapfile
  grep -q "/swapfile" /etc/fstab || echo "/swapfile none swap sw 0 0" >> /etc/fstab
fi

# Disable WiFi power save (connection stability)
iw wlan0 set power_save off 2>/dev/null || true

# CPU Governor → performance (consistent performance)
for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
  echo performance > "$cpu" 2>/dev/null || true
done

# ────────────────── Environment Variables ──────────────────

COMPOSE_FILE="/home/physicar/physicar-ros/docker-compose.yml"

PHYSICAR_DIR="/opt/physicar"
PHYSICAR_WS="/home/physicar/physicar_ws"
mkdir -p "$PHYSICAR_DIR" "$PHYSICAR_WS/src"

# Persistent state (cert fingerprint), runtime flags (cert-valid).  /run is
# tmpfs so flag is auto-cleared on boot — fetcher will re-establish it.
mkdir -p /var/lib/physicar /run/physicar

# ────────────────── Hostname/Password Setup ──────────────────

SERIAL=$(tr -d '\0' </sys/firmware/devicetree/base/serial-number 2>/dev/null || echo "unknown")

# Algorithm: SHA-256(serial) → hex 앞 16자리 → hostname(앞8) + password(뒤8)
SERIAL_HASH=$(echo -n "$SERIAL" | sha256sum | head -c 16)

# Hostname: file override or hash-derived
if [ -f "$PHYSICAR_DIR/hostname" ]; then
  DEVICE_HOSTNAME=$(tr -d '[:space:]' < "$PHYSICAR_DIR/hostname")
else
  DEVICE_HOSTNAME="physicar-${SERIAL_HASH:0:8}"
fi

# Password priority: 1) custom file  2) serial hash
#
# Constraints: 8..63 printable ASCII (WPA2 PSK length, nginx-quotable).
# Invalid file is left in place but ignored — the user can fix it, or the
# next valid save through the web UI overwrites it.  No .bak files.
if [ -f "$PHYSICAR_DIR/password" ]; then
  _PW=$(tr -d '[:space:]' < "$PHYSICAR_DIR/password")
  if [ ${#_PW} -ge 8 ] && [ ${#_PW} -le 63 ] && echo "$_PW" | grep -qP '^[\x20-\x7E]+$'; then
    PASSWORD="$_PW"
  else
    echo "[physicar.sh] /opt/physicar/password rejected (length/charset); using default" >&2
  fi
fi
if [ -z "$PASSWORD" ]; then
  PASSWORD="${SERIAL_HASH:8:8}"
fi

# Set SSH password
echo "physicar:${PASSWORD}" | chpasswd

# Set hostname
hostnamectl set-hostname "$DEVICE_HOSTNAME"
sed -i '/127.0.1.1/d' /etc/hosts
echo "127.0.1.1	$DEVICE_HOSTNAME" >> /etc/hosts

# Restrict avahi/mDNS broadcasting to the hotspot interface (ap0) only.
# We don't want the device to advertise hostname.local on home WiFi or
# Ethernet — clients on those networks should reach it by IP. The hotspot
# is the one place where physicar.local / hostname.local must resolve.
mkdir -p /etc/avahi
if [ -f /etc/avahi/avahi-daemon.conf ]; then
  sed -i \
    -e 's/^#*allow-interfaces=.*/allow-interfaces=ap0/' \
    -e 's/^#*deny-interfaces=.*/deny-interfaces=wlan0,eth0/' \
    /etc/avahi/avahi-daemon.conf
  # If keys were missing entirely, append them under [server]
  grep -q '^allow-interfaces=' /etc/avahi/avahi-daemon.conf || \
    sed -i '/^\[server\]/a allow-interfaces=ap0' /etc/avahi/avahi-daemon.conf
  grep -q '^deny-interfaces=' /etc/avahi/avahi-daemon.conf || \
    sed -i '/^\[server\]/a deny-interfaces=wlan0,eth0' /etc/avahi/avahi-daemon.conf
fi
systemctl restart avahi-daemon &>/dev/null || true

# ────────────────── WiFi Hotspot (AP+STA) ──────────────────

# Create virtual AP interface
if ! iw dev ap0 info &>/dev/null; then
  iw dev wlan0 interface add ap0 type __ap 2>/dev/null || true
fi

# DNS for hotspot clients: hostname.local + physicar.local → 10.42.0.1
# Also point device.physicar.ai and *.preview.physicar.ai (LE-trusted names)
# at the local AP so the LE cert is served directly to hotspot clients.
# dnsmasq's address=/domain/... matches that domain AND any subdomain.
mkdir -p /etc/NetworkManager/dnsmasq-shared.d
{
  echo "address=/$DEVICE_HOSTNAME.local/10.42.0.1"
  [ "$DEVICE_HOSTNAME" != "physicar" ] && echo "address=/physicar.local/10.42.0.1"
  echo "address=/device.physicar.ai/10.42.0.1"
  echo "address=/preview.physicar.ai/10.42.0.1"
  echo ""
  echo "# Upstream DNS: explicit servers (bypass systemd-resolved instability in AP+STA mode)"
  echo "no-resolv"
  echo "server=1.1.1.1"
  echo "server=8.8.8.8"
  echo "server=8.8.4.4"
} > /etc/NetworkManager/dnsmasq-shared.d/physicar.conf

# Remove stale connection
nmcli connection delete physicar-hotspot &>/dev/null || true
sleep 1

if iw dev ap0 info &>/dev/null; then
  nmcli connection add \
    type wifi ifname ap0 con-name physicar-hotspot \
    autoconnect no ssid "$DEVICE_HOSTNAME" \
    wifi.mode ap wifi.band bg \
    wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PASSWORD" \
    wifi-sec.wps-method 0x1 \
    ipv4.method shared ipv4.addresses 10.42.0.1/24 \
    &>/dev/null
  nmcli connection up physicar-hotspot &>/dev/null && \
    echo "[physicar] Hotspot: $DEVICE_HOSTNAME (ap0, 10.42.0.1)" || \
    echo "[physicar] WARNING: Hotspot failed to start" >&2
else
  echo "[physicar] WARNING: Could not create AP interface" >&2
fi

# ────────────────── X11 Display ──────────────────

if ! pgrep -x "Xorg" > /dev/null; then
  Xorg :0 &>/dev/null &
  for i in {1..30}; do
    xdpyinfo -display :0 &>/dev/null && break
    sleep 0.5
  done
  xhost +local: &>/dev/null || true
fi

export DISPLAY=:0

# Disable screen saver & DPMS (prevent color cycling / sleep)
xset s off          # disable screen saver timeout
xset s noblank      # disable screen blanking
xset -dpms          # disable DPMS (Display Power Management)
xset dpms force on  # wake monitor if already off

# Boot splash: launch fullscreen image NOW (right after Xorg) so it covers
# all subsequent init noise (Xvfb startup, service launches, monitor resync).
# Also paint X root with the same image as a fallback if feh dies.
SPLASH_IMG="/home/physicar/physicar-ros/physicar_webserver/static/img/splash.png"
SPLASH_PID=""
if [ -f "$SPLASH_IMG" ]; then
  feh --bg-fill --no-fehbg "$SPLASH_IMG" &>/dev/null || true
  feh --fullscreen --hide-pointer --no-menus --image-bg black --no-fehbg \
      "$SPLASH_IMG" &>/dev/null &
  SPLASH_PID=$!
else
  xsetroot -solid '#0b0b0b' &>/dev/null || true
fi

# ────────────────── Host Services ──────────────────

# Bluetooth auto-pair agent (NoInputNoOutput → accepts pair requests without
# passkey input). Required for Xbox controllers, headphones, etc.
if command -v bt-agent >/dev/null 2>&1 && ! pgrep -x bt-agent >/dev/null; then
  bt-agent --capability=NoInputNoOutput &>/dev/null &
fi

# Virtual display for GUI apps (students use :1 via noVNC)
Xvfb :1 -screen 0 800x600x24 &>/dev/null &
sleep 1
DISPLAY=:1 openbox &>/dev/null &
DISPLAY=:1 tint2 &>/dev/null &

# Auto-launch terminal on :1 (physicar user, centered)
xhost +SI:localuser:physicar &>/dev/null || true
runuser -u physicar -- env DISPLAY=:1 HOME=/home/physicar \
  xterm -geometry 80x24+80+50 -fa Monospace -fs 10 \
        -bg black -fg white -title Terminal \
        -e "cd ~ && bash -l" &>/dev/null &

x11vnc -display :1 -forever -shared -nopw -rfbport 5901 -bg -o /dev/null
/usr/share/novnc/utils/novnc_proxy --vnc localhost:5901 --listen 6080 &>/dev/null &

# nginx 인증용 비밀번호 맵 주입 (부팅마다 갱신)
#
# Two separate maps so cookies are auto-invalidated on reboot but API clients
# (curl, etc.) using ?password=xxx can keep working across reboots.
#
#   physicar_password.map  →  used by ?password=xxx           (just <PW>)
#   physicar_session.map   →  used by physicar_auth cookie    (<PW>.<BOOT_TOKEN>)
#
# BOOT_TOKEN is regenerated every boot, so all previously-issued cookies are
# instantly invalid — users have to re-enter the password.  login.html fetches
# the current token from /auth/nonce before setting its cookie.
BOOT_TOKEN=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 24)
echo "$BOOT_TOKEN" > /etc/nginx/html/boot_token
chmod 644 /etc/nginx/html/boot_token

# Escape password for nginx map syntax.  Map keys are double-quoted strings,
# so backslash and double-quote in the password must be backslash-escaped or
# the .map file will be syntactically invalid and nginx -s reload will fail
# (which would lock SSH/AP/web all at once because we'd lose auth + nginx).
nginx_escape() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

# Atomic publish:  write to .tmp → nginx -t → mv (or restore on failure).
# This guarantees we never leave a half-written or syntactically-broken map
# in place; if validation fails we keep the previous map and just warn.
publish_nginx_map() {
  local target="$1" content="$2" tmp backup
  tmp="${target}.tmp"
  backup="${target}.bak"
  printf '%s\n' "$content" > "$tmp"
  if [ -f "$target" ]; then cp -p "$target" "$backup"; fi
  mv "$tmp" "$target"
  if ! nginx -t 2>/dev/null; then
    echo "[physicar.sh] nginx -t failed after writing $target; restoring previous map" >&2
    if [ -f "$backup" ]; then mv "$backup" "$target"; else rm -f "$target"; fi
    return 1
  fi
  rm -f "$backup"
  return 0
}

PW_ESC=$(nginx_escape "$PASSWORD")
publish_nginx_map /etc/nginx/conf.d/physicar_password.map "\"${PW_ESC}\" 1;" || true
publish_nginx_map /etc/nginx/conf.d/physicar_session.map  "\"${PW_ESC}.${BOOT_TOKEN}\" 1;" || true
nginx -s reload 2>/dev/null || true

# code-server Web IDE (accessible via https://hostname/code)
VNC_FILE="$PHYSICAR_WS/app.physicar"
chmod 644 "$VNC_FILE" 2>/dev/null || true
echo "https://$(hostname).local/studio" > "$VNC_FILE"
chmod 444 "$VNC_FILE"
chown -R physicar:physicar "$PHYSICAR_WS"

# code-server user settings (User level — student can override per-workspace).
# code-server uses ~/.local/share/code-server/User/, NOT ~/.vscode-server/.
CODE_USER_DIR="/home/physicar/.local/share/code-server/User"
runuser -u physicar -- mkdir -p "$CODE_USER_DIR"
cat > "$CODE_USER_DIR/settings.json" <<'CODE_SETTINGS'
{
  "chat.sendElementsToChat.enabled": false,
  "editor.fontSize": 14,
  "editor.tabSize": 4,
  "files.autoSave": "afterDelay",
  "python.defaultInterpreterPath": "/usr/bin/python3",
  "simpleBrowser.focusLockIndicator.enabled": false,
  "telemetry.telemetryLevel": "off"
}
CODE_SETTINGS
chown physicar:physicar "$CODE_USER_DIR/settings.json"

(
  runuser -u physicar -- env DISPLAY=:1 /usr/local/bin/code-server \
    --bind-addr 127.0.0.1:8080 \
    --auth none \
    --disable-telemetry \
    --disable-update-check \
    --proxy-domain preview.physicar.ai \
    /home/physicar/physicar_ws &>/dev/null
) &

# Pre-install extensions on first boot.  code-server pulls from Open VSX
# by default, so all installs are license-clean (no MS Marketplace).
EXT_MARKER="/home/physicar/.local/share/code-server/.physicar-ext-installed"
if [ ! -f "$EXT_MARKER" ]; then
  (
    # Wait for code-server to be listening before installing extensions.
    for i in $(seq 1 60); do
      ss -tlnp 2>/dev/null | grep -q ':8080 ' && break
      sleep 2
    done
    VSIX_URL="https://raw.githubusercontent.com/physicar-ai/physicar-assets/refs/heads/main/physicar-browser-ext.vsix"
    VSIX_TMP="/tmp/physicar-browser-ext.vsix"
    if curl -fsSL "$VSIX_URL" -o "$VSIX_TMP"; then
      runuser -u physicar -- /usr/local/bin/code-server \
        --install-extension "$VSIX_TMP" &>/dev/null || true
      rm -f "$VSIX_TMP"
    fi
    for EXT_ID in ms-python.python ms-python.debugpy redhat.vscode-xml redhat.vscode-yaml; do
      runuser -u physicar -- /usr/local/bin/code-server \
        --install-extension "$EXT_ID" &>/dev/null || true
    done
    runuser -u physicar -- touch "$EXT_MARKER"
  ) &
fi

# ────────────────── Docker Container ──────────────────

export TZ="$(cat /etc/timezone 2>/dev/null || echo UTC)"

docker compose -f "$COMPOSE_FILE" --profile device up -d 2>/dev/null

# ────────────────── Background Processes ──────────────────

# ── LE cert fetcher ──
# Polls https://device-cert.physicar.ai/current every 3 minutes.  Replaces
# /etc/nginx/ssl/le.{crt,key} in place when a new Let's Encrypt cert is
# published.  The LE server block in sites-available/physicar always exists,
# so le.{crt,key} must always be present — setup.sh seeds them with copies
# of the self-signed cert at install time.
#
# Robustness:
#   - Atomic install (.new → openssl validate → mv → reload).  Power loss
#     between writes leaves a working state because we always validate
#     before swapping; partial .new files are deleted on the next cycle.
#   - On expiry: copy self-signed back over le.{crt,key} so nginx never
#     serves an expired LE cert (browsers reject expired with a hard error
#     that's worse than self-signed warning).  The /run/physicar/le-cert-valid
#     flag is dropped so /code stops redirecting hotspot clients to
#     device.physicar.ai.
#   - Network failures (no internet, DNS, TLS) only skip a cycle; they
#     never touch the installed cert.

CERT_URL="https://device-cert.physicar.ai/current"
LE_CRT="/etc/nginx/ssl/le.crt"
LE_KEY="/etc/nginx/ssl/le.key"
LE_FP_FILE="/var/lib/physicar/cert-fingerprint"
LE_VALID_FLAG="/run/physicar/le-cert-valid"
SS_CRT="/etc/nginx/ssl/physicar.crt"
SS_KEY="/etc/nginx/ssl/physicar.key"

# True iff on-disk le.{crt,key} is parseable AND not expired AND covers
# device.physicar.ai (i.e. it's a real LE cert, not the self-signed seed).
le_cert_ok() {
  [ -f "$LE_CRT" ] && [ -f "$LE_KEY" ] || return 1
  openssl x509 -in "$LE_CRT" -noout -checkend 0 >/dev/null 2>&1 || return 1
  openssl x509 -in "$LE_CRT" -noout -ext subjectAltName 2>/dev/null \
    | grep -q 'device\.physicar\.ai' || return 1
  return 0
}

# Replace le.{crt,key} with self-signed copies (used at expiry / corruption).
# Idempotent.  Triggers nginx reload only if files actually changed.
le_revert_to_self_signed() {
  rm -f "$LE_VALID_FLAG" "$LE_FP_FILE"
  [ -f "$SS_CRT" ] && [ -f "$SS_KEY" ] || return 0
  if ! cmp -s "$SS_CRT" "$LE_CRT" 2>/dev/null; then
    cp -p "$SS_CRT" "${LE_CRT}.new" && mv "${LE_CRT}.new" "$LE_CRT"
    cp -p "$SS_KEY" "${LE_KEY}.new" && mv "${LE_KEY}.new" "$LE_KEY"
    chmod 600 "$LE_KEY" 2>/dev/null || true
    nginx -t >/dev/null 2>&1 && nginx -s reload >/dev/null 2>&1 || true
  fi
}

# Atomically install a new LE cert/key pair.  Inputs are absolute paths to
# already-downloaded .new files.  Validates fingerprint + key/cert pair
# match + not-expired before swap.  Returns 0 on success.
le_install_new() {
  local new_crt="$1" new_key="$2" expected_fp="$3"

  # 1) cert parses + matches expected fingerprint
  local actual_fp
  actual_fp=$(openssl x509 -in "$new_crt" -noout -fingerprint -sha256 2>/dev/null \
                | sed 's/^.*=//' | tr 'A-F' 'a-f')
  [ -n "$actual_fp" ] || { echo "[physicar.sh] cert parse failed" >&2; return 1; }
  if [ "sha256:$actual_fp" != "$expected_fp" ]; then
    echo "[physicar.sh] fingerprint mismatch (expected=$expected_fp got=sha256:$actual_fp)" >&2
    return 1
  fi

  # 2) cert not expired
  if ! openssl x509 -in "$new_crt" -noout -checkend 0 >/dev/null 2>&1; then
    echo "[physicar.sh] new cert already expired; ignoring" >&2
    return 1
  fi

  # 3) key + cert pair match (compare pubkey hashes)
  local k_hash c_hash
  k_hash=$(openssl pkey -in "$new_key" -pubout -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1)
  c_hash=$(openssl x509 -in "$new_crt" -pubkey -noout 2>/dev/null \
             | openssl pkey -pubin -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1)
  if [ -z "$k_hash" ] || [ "$k_hash" != "$c_hash" ]; then
    echo "[physicar.sh] cert/key pair mismatch" >&2
    return 1
  fi

  # 4) atomic swap (rename within same fs)
  chmod 600 "$new_key"
  chmod 644 "$new_crt"
  mv "$new_crt" "$LE_CRT"
  mv "$new_key" "$LE_KEY"
  printf '%s\n' "$expected_fp" > "${LE_FP_FILE}.new"
  mv "${LE_FP_FILE}.new" "$LE_FP_FILE"
  return 0
}

# One fetch cycle.  Always finishes cleanly; never aborts the loop.
fetch_cert_once() {
  local meta="/tmp/physicar-cert-meta.json"
  local new_crt="${LE_CRT}.new"
  local new_key="${LE_KEY}.new"

  # 0) Refresh validity flag based on current on-disk state.  If the on-disk
  # cert is broken/expired/missing, fall back to self-signed copies.
  if le_cert_ok; then
    touch "$LE_VALID_FLAG"
  else
    le_revert_to_self_signed
  fi

  # 1) Fetch metadata.  Failure (no internet, DNS, etc.) → skip silently.
  curl -fsS --max-time 5 -o "$meta" "$CERT_URL/meta.json" 2>/dev/null || return 0

  local remote_fp
  remote_fp=$(jq -r '.fingerprint // empty' "$meta" 2>/dev/null)
  rm -f "$meta"
  [ -n "$remote_fp" ] || return 0

  # 2) Already up-to-date and currently valid?  Done.
  if le_cert_ok; then
    local local_fp
    local_fp=$(cat "$LE_FP_FILE" 2>/dev/null)
    [ "$local_fp" = "$remote_fp" ] && return 0
  fi

  # 3) Download new cert/key (atomic via temp files).
  rm -f "$new_crt" "$new_key"
  curl -fsS --max-time 15 -o "$new_crt" "$CERT_URL/fullchain.pem" 2>/dev/null \
    && curl -fsS --max-time 15 -o "$new_key" "$CERT_URL/privkey.pem" 2>/dev/null \
    || { rm -f "$new_crt" "$new_key"; return 0; }

  # 4) Validate + install.  On any failure, leave existing cert untouched.
  if ! le_install_new "$new_crt" "$new_key" "$remote_fp"; then
    rm -f "$new_crt" "$new_key"
    return 0
  fi

  # 5) Reload nginx to pick up new cert.
  if nginx -t >/dev/null 2>&1; then
    nginx -s reload >/dev/null 2>&1 || true
    touch "$LE_VALID_FLAG"
    echo "[physicar.sh] LE cert installed: $remote_fp" >&2
  fi
}

# Background fetch loop (every 3 minutes).  Best-effort; survives all errors.
# A NetworkManager dispatcher script (90-physicar-cert) sends SIGUSR1 when an
# interface comes up so we kick off a fetch immediately instead of waiting
# up to 3 minutes for the next tick.
(
  SLEEP_PID=""
  # SIGUSR1 wakes us up by killing the inner `sleep`; the loop then falls
  # through to the next fetch_cert_once() iteration.  We clear SLEEP_PID
  # right after signalling so a SIGUSR1 arriving mid-fetch (when the saved
  # PID would be stale and possibly recycled by the kernel) is a no-op
  # instead of accidentally killing some unrelated process.
  trap '[ -n "$SLEEP_PID" ] && kill "$SLEEP_PID" 2>/dev/null; SLEEP_PID=""' USR1

  # $BASHPID is this subshell's real PID; $$ would be the parent script's
  # PID and signaling that would kill physicar.service itself.
  echo "$BASHPID" > /run/physicar/cert-fetcher.pid

  # Initial 5s grace for nginx/network init, then fetch loop.
  sleep 5 & SLEEP_PID=$!
  wait "$SLEEP_PID" 2>/dev/null
  SLEEP_PID=""
  while true; do
    fetch_cert_once || true
    sleep 180 & SLEEP_PID=$!
    wait "$SLEEP_PID" 2>/dev/null
    SLEEP_PID=""
  done
) &

# Synchronous cleanup at boot: if the on-disk LE cert is broken/expired,
# revert to self-signed immediately so nginx never serves an expired cert
# during the ~5s window before the fetcher loop wakes up.
if ! le_cert_ok; then
  le_revert_to_self_signed
fi
# Kiosk browser (auto-restart). $SPLASH_PID was started early (before Host
# Services) and remains visible until chromium has painted the kiosk page.
(
  FIRST=1
  while true; do
    if ! pgrep -f "chromium.*--kiosk" > /dev/null 2>&1; then
      # Wait for server (require HTTP 200 — avoid starting chromium on 5xx)
      until [[ "$(curl -sk -o /dev/null -w '%{http_code}' https://localhost/kiosk)" == "200" ]]; do
        sleep 0.5
      done

      DISPLAY=:0 chromium-browser \
        --no-sandbox \
        --disable-gpu \
        --disable-software-rasterizer \
        --disable-dev-shm-usage \
        --disable-features=VizDisplayCompositor \
        --disable-pinch \
        --kiosk \
        --noerrdialogs \
        --disable-infobars \
        --no-first-run \
        --start-fullscreen \
        --start-maximized \
        --disable-translate \
        --disable-features=TranslateUI,Translate \
        --disable-session-crashed-bubble \
        --disable-component-update \
        --check-for-update-interval=31536000 \
        --window-position=0,0 \
        --window-size=800,480 \
        --force-device-scale-factor=1 \
        --disable-popup-blocking \
        --ignore-certificate-errors \
        --allow-insecure-localhost \
        --test-type \
        --default-background-color=000000 \
        --user-data-dir=/tmp/chromium-kiosk \
        https://localhost/kiosk &>/dev/null &

      # Hide cursor immediately, show only on mouse movement
      DISPLAY=:0 unclutter -idle 0.1 -root &>/dev/null &

      # On first launch only: keep splash up until chromium has painted
      if [ "$FIRST" = "1" ] && [ -n "$SPLASH_PID" ]; then
        for _ in {1..120}; do
          if DISPLAY=:0 xwininfo -root -tree 2>/dev/null | grep -qi 'chromium'; then
            break
          fi
          sleep 0.25
        done
        # Chromium window mapped, but content not yet painted. Wait for the
        # kiosk page to render. The page itself includes the same splash
        # image as a fade-out overlay, so we only need to bridge the gap
        # until first paint (~3-4s on Pi 5).
        sleep 4
        kill "$SPLASH_PID" 2>/dev/null || true
        FIRST=0
      fi
    fi
    sleep 0.5
  done
) &

# Auto-update image (every 3 minutes, applied on next reboot)
(
  while true; do
    sleep 180
    docker compose -f "$COMPOSE_FILE" --profile device pull 2>/dev/null || true
  done
) &

# Service watchdog (auto-restart crashed components)
(
  while true; do
    sleep 5
    # Xvfb :1
    if ! pgrep -x Xvfb > /dev/null 2>&1; then
      Xvfb :1 -screen 0 800x600x24 &>/dev/null &
      sleep 1
    fi
    # openbox
    if ! pgrep -x openbox > /dev/null 2>&1; then
      DISPLAY=:1 openbox &>/dev/null &
    fi
    # tint2
    if ! pgrep -x tint2 > /dev/null 2>&1; then
      DISPLAY=:1 tint2 &>/dev/null &
    fi
    # x11vnc
    if ! pgrep -x x11vnc > /dev/null 2>&1; then
      x11vnc -display :1 -forever -shared -nopw -rfbport 5901 -bg -o /dev/null
    fi
    # novnc_proxy (websockify)
    if ! pgrep -f "novnc_proxy" > /dev/null 2>&1; then
      /usr/share/novnc/utils/novnc_proxy --vnc localhost:5901 --listen 6080 &>/dev/null &
    fi
    # code-server (Web IDE on port 8080)
    if ! ss -tlnp 2>/dev/null | grep -q ':8080 '; then
      runuser -u physicar -- env DISPLAY=:1 /usr/local/bin/code-server \
        --bind-addr 127.0.0.1:8080 \
        --auth none \
        --disable-telemetry \
        --disable-update-check \
        --proxy-domain preview.physicar.ai \
        /home/physicar/physicar_ws &>/dev/null &
    fi
    # host_api (port 8001 — /api/host/ via nginx)
    if ! ss -tlnp 2>/dev/null | grep -q ':8001 '; then
      python3 /home/physicar/physicar-ros/host/host_api.py &>/dev/null &
    fi
  done
) &

# Keep service alive
wait
