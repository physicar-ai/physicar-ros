#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  Physicar Boot Script — runs as physicar user via physicar.service
#  Root-level init (via sudo) + user-level services in one file.
# ═══════════════════════════════════════════════════════════════════════════════

export HOME=/home/physicar

PHYSICAR_WS="$HOME/physicar_ws"
PHYSICAR_ROS_DIR="$PHYSICAR_WS/src/physicar-ros"
PHYSICAR_DIR="$PHYSICAR_WS/userdata"

# Load environment (.env)
ENV_FILE="$PHYSICAR_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    if bash -n "$ENV_FILE" 2>/dev/null; then
        set -a; . "$ENV_FILE"; set +a
    fi
fi

# ────────────────── Hardware Optimization ──────────────────

# Swap Memory (4GB) - Create if not exists
if [ -z "$(swapon --show)" ]; then
  if [ ! -f /swapfile ]; then
    sudo fallocate -l 4G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile
  fi
  sudo swapon /swapfile
  grep -q "/swapfile" /etc/fstab || echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab > /dev/null
fi

# Disable WiFi power save (connection stability)
sudo iw wlan0 set power_save off 2>/dev/null || true

# CPU Governor → performance (consistent performance)
for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
  echo performance | sudo tee "$cpu" > /dev/null 2>/dev/null || true
done

# ────────────────── Environment Variables ──────────────────

sudo mkdir -p "$PHYSICAR_DIR" "$PHYSICAR_WS/src"
sudo mkdir -p /var/lib/physicar /run/physicar
sudo chown -R physicar:physicar "$PHYSICAR_WS" /run/physicar

# ────────────────── Hostname/Password Setup ──────────────────

SERIAL=$(tr -d '\0' </sys/firmware/devicetree/base/serial-number 2>/dev/null || echo "unknown")
SERIAL_HASH=$(echo -n "$SERIAL" | sha256sum | head -c 16)

if [ -f "$PHYSICAR_DIR/hostname" ]; then
  DEVICE_HOSTNAME=$(tr -d '[:space:]' < "$PHYSICAR_DIR/hostname")
else
  DEVICE_HOSTNAME="physicar-${SERIAL_HASH:0:8}"
fi

if [ -f "$PHYSICAR_DIR/password" ]; then
  _PW=$(tr -d '[:space:]' < "$PHYSICAR_DIR/password")
  if [ ${#_PW} -ge 8 ] && [ ${#_PW} -le 63 ] && echo "$_PW" | grep -qP '^[\x20-\x7E]+$'; then
    PASSWORD="$_PW"
  else
    echo "[physicar] $PHYSICAR_DIR/password rejected (length/charset); using default" >&2
  fi
fi
if [ -z "$PASSWORD" ]; then
  PASSWORD="${SERIAL_HASH:8:8}"
fi

echo "physicar:${PASSWORD}" | sudo chpasswd

sudo hostnamectl set-hostname "$DEVICE_HOSTNAME"
sudo sed -i '/127.0.1.1/d' /etc/hosts
echo "127.0.1.1	$DEVICE_HOSTNAME" | sudo tee -a /etc/hosts > /dev/null

# Restrict avahi/mDNS to ap0 only
sudo mkdir -p /etc/avahi
if [ -f /etc/avahi/avahi-daemon.conf ]; then
  sudo sed -i \
    -e 's/^#*allow-interfaces=.*/allow-interfaces=ap0/' \
    -e 's/^#*deny-interfaces=.*/deny-interfaces=wlan0,eth0/' \
    /etc/avahi/avahi-daemon.conf
  grep -q '^allow-interfaces=' /etc/avahi/avahi-daemon.conf || \
    sudo sed -i '/^\[server\]/a allow-interfaces=ap0' /etc/avahi/avahi-daemon.conf
  grep -q '^deny-interfaces=' /etc/avahi/avahi-daemon.conf || \
    sudo sed -i '/^\[server\]/a deny-interfaces=wlan0,eth0' /etc/avahi/avahi-daemon.conf
fi
sudo systemctl restart avahi-daemon &>/dev/null || true

# ────────────────── WiFi Hotspot (AP+STA) ──────────────────

if ! iw dev ap0 info &>/dev/null; then
  sudo iw dev wlan0 interface add ap0 type __ap 2>/dev/null || true
fi

sudo mkdir -p /etc/NetworkManager/dnsmasq-shared.d
{
  echo "address=/$DEVICE_HOSTNAME.local/10.42.0.1"
  [ "$DEVICE_HOSTNAME" != "physicar" ] && echo "address=/physicar.local/10.42.0.1"
  echo "address=/device.physicar.ai/10.42.0.1"
  echo "address=/preview.physicar.ai/10.42.0.1"
  echo ""
  echo "no-resolv"
  echo "server=1.1.1.1"
  echo "server=8.8.8.8"
  echo "server=8.8.4.4"
} | sudo tee /etc/NetworkManager/dnsmasq-shared.d/physicar.conf > /dev/null

sudo nmcli connection delete physicar-hotspot &>/dev/null || true
sleep 1

if iw dev ap0 info &>/dev/null; then
  sudo nmcli connection add \
    type wifi ifname ap0 con-name physicar-hotspot \
    autoconnect no ssid "$DEVICE_HOSTNAME" \
    wifi.mode ap wifi.band bg \
    wifi-sec.key-mgmt wpa-psk wifi-sec.psk "$PASSWORD" \
    wifi-sec.wps-method 0x1 \
    ipv4.method shared ipv4.addresses 10.42.0.1/24 \
    &>/dev/null
  sudo nmcli connection up physicar-hotspot &>/dev/null && \
    echo "[physicar] Hotspot: $DEVICE_HOSTNAME (ap0, 10.42.0.1)" || \
    echo "[physicar] WARNING: Hotspot failed to start" >&2
else
  echo "[physicar] WARNING: Could not create AP interface" >&2
fi

# ────────────────── X11 Display ──────────────────

if ! pgrep -x "Xorg" > /dev/null; then
  sudo Xorg :0 &>/dev/null &
  for i in {1..30}; do
    xdpyinfo -display :0 &>/dev/null && break
    sleep 0.5
  done
  sudo xhost +local: &>/dev/null || true
fi

export DISPLAY=:0
xset s off; xset s noblank; xset -dpms; xset dpms force on

# Boot splash
SPLASH_IMG="$PHYSICAR_ROS_DIR/physicar_webserver/static/img/splash.png"
SPLASH_PID=""
if [ -f "$SPLASH_IMG" ]; then
  feh --bg-fill --no-fehbg "$SPLASH_IMG" &>/dev/null || true
  feh --fullscreen --hide-pointer --no-menus --image-bg black --no-fehbg \
      "$SPLASH_IMG" &>/dev/null &
  SPLASH_PID=$!
else
  xsetroot -solid '#0b0b0b' &>/dev/null || true
fi

# ────────────────── Virtual Display + VNC ──────────────────

Xvfb :1 -screen 0 800x600x24 &>/dev/null &
sleep 1
DISPLAY=:1 openbox &>/dev/null &
DISPLAY=:1 tint2 &>/dev/null &

sudo xhost +SI:localuser:physicar &>/dev/null || true

x11vnc -display :1 -forever -shared -nopw -rfbport 5901 -bg -o /dev/null
/usr/share/novnc/utils/novnc_proxy --vnc localhost:5901 --listen 6080 &>/dev/null &

# Bluetooth auto-pair agent
if command -v bt-agent >/dev/null 2>&1 && ! pgrep -x bt-agent >/dev/null; then
  sudo bt-agent --capability=NoInputNoOutput &>/dev/null &
fi

# ────────────────── Nginx Auth Maps ──────────────────

BOOT_TOKEN=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 24)
echo "$BOOT_TOKEN" | sudo tee /etc/nginx/html/boot_token > /dev/null
sudo chmod 644 /etc/nginx/html/boot_token

nginx_escape() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'
}

publish_nginx_map() {
  local target="$1" content="$2" tmp backup
  tmp="${target}.tmp"
  backup="${target}.bak"
  printf '%s\n' "$content" | sudo tee "$tmp" > /dev/null
  if [ -f "$target" ]; then sudo cp -p "$target" "$backup"; fi
  sudo mv "$tmp" "$target"
  if ! sudo nginx -t 2>/dev/null; then
    echo "[physicar] nginx -t failed after writing $target; restoring" >&2
    if [ -f "$backup" ]; then sudo mv "$backup" "$target"; else sudo rm -f "$target"; fi
    return 1
  fi
  sudo rm -f "$backup"
  return 0
}

PW_ESC=$(nginx_escape "$PASSWORD")
publish_nginx_map /etc/nginx/conf.d/physicar_password.map "\"${PW_ESC}\" 1;" || true
publish_nginx_map /etc/nginx/conf.d/physicar_session.map  "\"${PW_ESC}.${BOOT_TOKEN}\" 1;" || true
sudo nginx -s reload 2>/dev/null || true

# ────────────────── LE Cert Fetcher ──────────────────

CERT_URL="https://device-cert.physicar.ai/current"
LE_CRT="/etc/nginx/ssl/le.crt"
LE_KEY="/etc/nginx/ssl/le.key"
LE_FP_FILE="/var/lib/physicar/cert-fingerprint"
LE_VALID_FLAG="/run/physicar/le-cert-valid"
SS_CRT="/etc/nginx/ssl/physicar.crt"
SS_KEY="/etc/nginx/ssl/physicar.key"

le_cert_ok() {
  [ -f "$LE_CRT" ] && [ -f "$LE_KEY" ] || return 1
  sudo openssl x509 -in "$LE_CRT" -noout -checkend 0 >/dev/null 2>&1 || return 1
  sudo openssl x509 -in "$LE_CRT" -noout -ext subjectAltName 2>/dev/null \
    | grep -q 'device\.physicar\.ai' || return 1
  return 0
}

le_revert_to_self_signed() {
  sudo rm -f "$LE_VALID_FLAG" "$LE_FP_FILE"
  [ -f "$SS_CRT" ] && [ -f "$SS_KEY" ] || return 0
  if ! sudo cmp -s "$SS_CRT" "$LE_CRT" 2>/dev/null; then
    sudo cp -p "$SS_CRT" "${LE_CRT}.new" && sudo mv "${LE_CRT}.new" "$LE_CRT"
    sudo cp -p "$SS_KEY" "${LE_KEY}.new" && sudo mv "${LE_KEY}.new" "$LE_KEY"
    sudo chmod 600 "$LE_KEY" 2>/dev/null || true
    sudo nginx -t >/dev/null 2>&1 && sudo nginx -s reload >/dev/null 2>&1 || true
  fi
}

le_install_new() {
  local new_crt="$1" new_key="$2" expected_fp="$3"
  local actual_fp
  actual_fp=$(sudo openssl x509 -in "$new_crt" -noout -fingerprint -sha256 2>/dev/null \
                | sed 's/^.*=//' | tr 'A-F' 'a-f')
  [ -n "$actual_fp" ] || return 1
  if [ "sha256:$actual_fp" != "$expected_fp" ]; then return 1; fi
  if ! sudo openssl x509 -in "$new_crt" -noout -checkend 0 >/dev/null 2>&1; then return 1; fi
  local k_hash c_hash
  k_hash=$(sudo openssl pkey -in "$new_key" -pubout -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1)
  c_hash=$(sudo openssl x509 -in "$new_crt" -pubkey -noout 2>/dev/null \
             | openssl pkey -pubin -outform DER 2>/dev/null | sha256sum | cut -d' ' -f1)
  if [ -z "$k_hash" ] || [ "$k_hash" != "$c_hash" ]; then return 1; fi
  sudo chmod 600 "$new_key"; sudo chmod 644 "$new_crt"
  sudo mv "$new_crt" "$LE_CRT"; sudo mv "$new_key" "$LE_KEY"
  printf '%s\n' "$expected_fp" | sudo tee "${LE_FP_FILE}.new" > /dev/null
  sudo mv "${LE_FP_FILE}.new" "$LE_FP_FILE"
  return 0
}

fetch_cert_once() {
  local meta="/tmp/physicar-cert-meta.json"
  local new_crt="${LE_CRT}.new" new_key="${LE_KEY}.new"
  if le_cert_ok; then sudo touch "$LE_VALID_FLAG"; else le_revert_to_self_signed; fi
  curl -fsS --max-time 5 -o "$meta" "$CERT_URL/meta.json" 2>/dev/null || return 0
  local remote_fp
  remote_fp=$(jq -r '.fingerprint // empty' "$meta" 2>/dev/null)
  rm -f "$meta"
  [ -n "$remote_fp" ] || return 0
  if le_cert_ok; then
    local local_fp; local_fp=$(cat "$LE_FP_FILE" 2>/dev/null)
    [ "$local_fp" = "$remote_fp" ] && return 0
  fi
  sudo rm -f "$new_crt" "$new_key"
  sudo curl -fsS --max-time 15 -o "$new_crt" "$CERT_URL/fullchain.pem" 2>/dev/null \
    && sudo curl -fsS --max-time 15 -o "$new_key" "$CERT_URL/privkey.pem" 2>/dev/null \
    || { sudo rm -f "$new_crt" "$new_key"; return 0; }
  if ! le_install_new "$new_crt" "$new_key" "$remote_fp"; then
    sudo rm -f "$new_crt" "$new_key"; return 0
  fi
  if sudo nginx -t >/dev/null 2>&1; then
    sudo nginx -s reload >/dev/null 2>&1 || true
    sudo touch "$LE_VALID_FLAG"
  fi
}

(
  SLEEP_PID=""
  trap '[ -n "$SLEEP_PID" ] && kill "$SLEEP_PID" 2>/dev/null; SLEEP_PID=""' USR1
  echo "$BASHPID" > /run/physicar/cert-fetcher.pid
  sleep 5 & SLEEP_PID=$!; wait "$SLEEP_PID" 2>/dev/null; SLEEP_PID=""
  while true; do
    fetch_cert_once || true
    sleep 180 & SLEEP_PID=$!; wait "$SLEEP_PID" 2>/dev/null; SLEEP_PID=""
  done
) &

if ! le_cert_ok; then le_revert_to_self_signed; fi

# ────────────────── Ownership ──────────────────

sudo chown -R physicar:physicar "$PHYSICAR_WS"

echo "[physicar] Root initialization complete."

# ════════════════════════════════════════════════════════════════════════════
#  User-level services (code-server, ROS, kiosk, updater, watchdog)
# ════════════════════════════════════════════════════════════════════════════

# ────────────────── code-server ──────────────────

APP_FILE="$PHYSICAR_WS/app.physicar"
chmod 644 "$APP_FILE" 2>/dev/null || true
echo "https://device.physicar.ai/studio" > "$APP_FILE"
chmod 444 "$APP_FILE" 2>/dev/null || true

CODE_USER_DIR="$HOME/.local/share/code-server/User"
mkdir -p "$CODE_USER_DIR"
if [ ! -f "$CODE_USER_DIR/settings.json" ]; then
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
fi

/usr/local/bin/code-server \
  --bind-addr 127.0.0.1:8080 \
  --auth none \
  --disable-telemetry \
  --disable-update-check \
  --proxy-domain preview.physicar.ai \
  "$PHYSICAR_WS" &>/dev/null &

# Pre-install extensions on first boot
EXT_MARKER="$HOME/.local/share/code-server/.physicar-ext-installed"
if [ ! -f "$EXT_MARKER" ]; then
  (
    for i in $(seq 1 60); do
      ss -tlnp 2>/dev/null | grep -q ':8080 ' && break
      sleep 2
    done
    VSIX_URL="https://raw.githubusercontent.com/physicar-ai/physicar-assets/refs/heads/main/physicar-browser-ext.vsix"
    VSIX_TMP="/tmp/physicar-browser-ext.vsix"
    if curl -fsSL "$VSIX_URL" -o "$VSIX_TMP"; then
      /usr/local/bin/code-server --install-extension "$VSIX_TMP" &>/dev/null || true
      rm -f "$VSIX_TMP"
    fi
    for EXT_ID in ms-python.python ms-python.debugpy redhat.vscode-xml redhat.vscode-yaml; do
      /usr/local/bin/code-server --install-extension "$EXT_ID" &>/dev/null || true
    done
    touch "$EXT_MARKER"
  ) &
fi

# ────────────────── Auto-launch terminal on :1 ──────────────────

DISPLAY=:1 xterm -geometry 80x24+80+50 -fa Monospace -fs 10 \
  -bg black -fg white -title Terminal \
  -e "cd ~ && bash -l" &>/dev/null &

# ────────────────── ROS2 Launch ──────────────────

export DISPLAY=:1
source /opt/ros/jazzy/setup.bash
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST

UPDATE_SIGNAL="/tmp/.physicar-update-ready"

git config --global --add safe.directory "$PHYSICAR_ROS_DIR" 2>/dev/null || true

clean_build() {
    echo "[physicar] Running clean build..."
    rm -rf "$PHYSICAR_WS/build" "$PHYSICAR_WS/install" "$PHYSICAR_WS/log"
    cd "$PHYSICAR_WS" && colcon build --symlink-install 2>&1
}

do_build() {
    rm -f "$PHYSICAR_ROS_DIR/camera_ros/COLCON_IGNORE" 2>/dev/null
    rm -f "$PHYSICAR_ROS_DIR/rplidar_ros/COLCON_IGNORE" 2>/dev/null

    echo "[physicar] Building..."
    cd "$PHYSICAR_WS" && colcon build --symlink-install 2>&1
    local exit_code=$?

    if [ $exit_code -ne 0 ]; then
        echo "[physicar] Build failed. Retrying clean..."
        clean_build
        exit_code=$?
    fi

    source "$PHYSICAR_WS/install/setup.bash"
    return $exit_code
}

python3 -m pip install --upgrade physicar 2>/dev/null || true

rm -f "$UPDATE_SIGNAL"

while true; do
    do_build

    echo "[physicar] Launching..."
    ros2 launch physicar_bringup robot.launch.py &
    LAUNCH_PID=$!
    wait $LAUNCH_PID 2>/dev/null

    if [ -f "$UPDATE_SIGNAL" ]; then
        rm -f "$UPDATE_SIGNAL"
        echo "[physicar] Update detected → rebuilding..."
        continue
    fi

    echo "[physicar] Launch exited. Staying alive for debugging."
    exec sleep infinity
done &
ROS_LOOP_PID=$!

# ────────────────── Updater ──────────────────

if [ "$DEV" != "true" ] && [ -f "$PHYSICAR_ROS_DIR/updater.sh" ]; then
  bash "$PHYSICAR_ROS_DIR/updater.sh" &
fi

# ────────────────── Kiosk Browser ──────────────────

(
  FIRST=1
  while true; do
    if ! pgrep -f "chromium.*--kiosk" > /dev/null 2>&1; then
      until [[ "$(curl -sk -o /dev/null -w '%{http_code}' https://localhost/kiosk)" == "200" ]]; do
        sleep 0.5
      done

      sudo DISPLAY=:0 chromium-browser \
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

      DISPLAY=:0 unclutter -idle 0.1 -root &>/dev/null &

      if [ "$FIRST" = "1" ] && [ -n "$SPLASH_PID" ]; then
        for _ in {1..120}; do
          if DISPLAY=:0 xwininfo -root -tree 2>/dev/null | grep -qi 'chromium'; then
            break
          fi
          sleep 0.25
        done
        sleep 4
        kill "$SPLASH_PID" 2>/dev/null || true
        FIRST=0
      fi
    fi
    sleep 0.5
  done
) &

# ────────────────── Service Watchdog ──────────────────

(
  while true; do
    sleep 5
    if ! pgrep -x Xvfb > /dev/null 2>&1; then
      Xvfb :1 -screen 0 800x600x24 &>/dev/null &
      sleep 1
    fi
    if ! pgrep -x openbox > /dev/null 2>&1; then
      DISPLAY=:1 openbox &>/dev/null &
    fi
    if ! pgrep -x tint2 > /dev/null 2>&1; then
      DISPLAY=:1 tint2 &>/dev/null &
    fi
    if ! pgrep -x x11vnc > /dev/null 2>&1; then
      x11vnc -display :1 -forever -shared -nopw -rfbport 5901 -bg -o /dev/null
    fi
    if ! pgrep -f "novnc_proxy" > /dev/null 2>&1; then
      /usr/share/novnc/utils/novnc_proxy --vnc localhost:5901 --listen 6080 &>/dev/null &
    fi
    if ! ss -tlnp 2>/dev/null | grep -q ':8080 '; then
      /usr/local/bin/code-server \
        --bind-addr 127.0.0.1:8080 \
        --auth none \
        --disable-telemetry \
        --disable-update-check \
        --proxy-domain preview.physicar.ai \
        "$PHYSICAR_WS" &>/dev/null &
    fi
  done
) &

# Keep service alive
wait
