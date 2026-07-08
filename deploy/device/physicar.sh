#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════════
#  Physicar Boot Script — runs as physicar user via physicar.service
#  Root-level init (via sudo) + user-level services in one file.
# ═══════════════════════════════════════════════════════════════════════════════

export HOME=/home/physicar

PHYSICAR_WS="/opt/physicar"
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

# Auto-register unbound USB WiFi adapters with Realtek drivers
# Scans for USB interfaces with vendor-specific class (ff:ff:ff) that have no
# driver bound — covers adapters plugged in before boot completed.
# Uses ref-ID so driver_info (chip type) is passed correctly.
for _iface in /sys/bus/usb/devices/*/bInterfaceClass; do
  [ -f "$_iface" ] || continue
  _dir=$(dirname "$_iface")
  [ "$(cat "$_dir/bInterfaceClass")" = "ff" ] || continue
  [ "$(cat "$_dir/bInterfaceSubClass")" = "ff" ] || continue
  [ "$(cat "$_dir/bInterfaceProtocol")" = "ff" ] || continue
  [ -L "$_dir/driver" ] && continue  # already bound
  _parent=$(dirname "$_dir")
  [ -f "$_parent/idVendor" ] || continue
  _vid=$(cat "$_parent/idVendor")
  _pid=$(cat "$_parent/idProduct")
  [ "$_vid:$_pid" = "0bda:1a2b" ] && continue  # USB modeswitch
  # Skip devices that still have a mass storage interface (needs modeswitch first)
  _has_ms=0
  for _sib in "$_parent"/*/bInterfaceClass; do
    [ -f "$_sib" ] || continue
    [ "$(cat "$_sib")" = "08" ] && { _has_ms=1; break; }
  done
  [ "$_has_ms" = "1" ] && continue
  # ref-ID: inherit driver_info from known table entry
  sudo sh -c "echo '$_vid $_pid ff 0bda 8832' > /sys/bus/usb/drivers/rtl8852au/new_id" 2>/dev/null || true
  sudo sh -c "echo '$_vid $_pid ff 0bda b832' > /sys/bus/usb/drivers/rtl8852bu/new_id" 2>/dev/null || true
  sudo sh -c "echo '$_vid $_pid ff 0bda 8832' > /sys/bus/usb/drivers/rtl8852cu/new_id" 2>/dev/null || true
done

# Disable WiFi power save (connection stability)
sudo iw wlan0 set power_save off 2>/dev/null || true

# CPU Governor → performance (consistent performance)
for cpu in /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor; do
  echo performance | sudo tee "$cpu" > /dev/null 2>/dev/null || true
done

# PWM export + permissions (RP1 PWM0: steering ch0 + ESC ch1)
if [ -d /sys/class/pwm/pwmchip0 ]; then
  [ ! -d /sys/class/pwm/pwmchip0/pwm0 ] && echo 0 | sudo tee /sys/class/pwm/pwmchip0/export > /dev/null
  [ ! -d /sys/class/pwm/pwmchip0/pwm1 ] && echo 1 | sudo tee /sys/class/pwm/pwmchip0/export > /dev/null
  sleep 0.1
  sudo chgrp gpio /sys/class/pwm/pwmchip0/export /sys/class/pwm/pwmchip0/unexport \
    /sys/class/pwm/pwmchip0/pwm0/duty_cycle /sys/class/pwm/pwmchip0/pwm0/period \
    /sys/class/pwm/pwmchip0/pwm0/enable /sys/class/pwm/pwmchip0/pwm0/polarity \
    /sys/class/pwm/pwmchip0/pwm1/duty_cycle /sys/class/pwm/pwmchip0/pwm1/period \
    /sys/class/pwm/pwmchip0/pwm1/enable /sys/class/pwm/pwmchip0/pwm1/polarity 2>/dev/null
  sudo chmod g+w /sys/class/pwm/pwmchip0/export /sys/class/pwm/pwmchip0/unexport \
    /sys/class/pwm/pwmchip0/pwm0/duty_cycle /sys/class/pwm/pwmchip0/pwm0/period \
    /sys/class/pwm/pwmchip0/pwm0/enable /sys/class/pwm/pwmchip0/pwm0/polarity \
    /sys/class/pwm/pwmchip0/pwm1/duty_cycle /sys/class/pwm/pwmchip0/pwm1/period \
    /sys/class/pwm/pwmchip0/pwm1/enable /sys/class/pwm/pwmchip0/pwm1/polarity 2>/dev/null
fi

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

# Only update password if changed
_CURRENT_PW_HASH=$(sudo getent shadow physicar 2>/dev/null | cut -d: -f2)
_PW_MATCH=$(_H="$_CURRENT_PW_HASH" _P="$PASSWORD" python3 -c "
import crypt, os
current = os.environ.get('_H', '')
pw = os.environ.get('_P', '')
print('yes' if current and crypt.crypt(pw, current) == current else 'no')
" 2>/dev/null || echo "no")
if [ "$_PW_MATCH" != "yes" ]; then
  echo "physicar:${PASSWORD}" | sudo chpasswd
fi

# Only update hostname if changed
_CURRENT_HOSTNAME=$(hostname)
if [ "$_CURRENT_HOSTNAME" != "$DEVICE_HOSTNAME" ]; then
  sudo hostnamectl set-hostname "$DEVICE_HOSTNAME"
  sudo sed -i '/127.0.1.1/d' /etc/hosts
  echo "127.0.1.1	$DEVICE_HOSTNAME" | sudo tee -a /etc/hosts > /dev/null
fi

# ────────────────── WiFi Hotspot (AP+STA) ──────────────────

# ── Disable WiFi power save & USB autosuspend (prevents latency/disconnects) ──
iw dev wlan0 set power_save off 2>/dev/null || true
for _usbpwr in /sys/bus/usb/devices/*/power/control; do
  [ -f "$_usbpwr" ] && echo "on" > "$_usbpwr" 2>/dev/null || true
done

# Run in background — nothing below depends on hotspot being ready
(
  # ── Purge ghost netplan passthrough files (physicar-hotspot only) ──
  _ghost_files=$(find /etc/netplan -maxdepth 1 -name '90-NM-*-physicar-*.yaml' 2>/dev/null)
  if [ -n "$_ghost_files" ]; then
    _ghost_count=$(echo "$_ghost_files" | wc -l)
    echo "[physicar] Cleaning ${_ghost_count} ghost netplan hotspot files"
    echo "$_ghost_files" | xargs sudo rm -f 2>/dev/null
    sudo rm -f /run/NetworkManager/system-connections/netplan-NM-*-physicar-*.nmconnection 2>/dev/null
    sudo nmcli connection reload 2>/dev/null || true
    sleep 1
  fi

  # ── 1. Detect AP interface: USB WiFi preferred, else virtual ap0 ──
  _AP_IFACE=""
  _AP_IP="10.42.0.1"
  _AP_INDEPENDENT=0
  for _net in /sys/class/net/wlx*; do
    [ -e "$_net" ] || continue
    _candidate=$(basename "$_net")
    # Verify it's a real WiFi device (has wireless/phy80211)
    [ -d "/sys/class/net/$_candidate/wireless" ] || continue
    _AP_IFACE="$_candidate"
    _AP_INDEPENDENT=1
    break
  done

  if [ -z "$_AP_IFACE" ]; then
    _AP_IFACE="ap0"
    if ! iw dev ap0 info &>/dev/null; then
      sudo iw dev wlan0 interface add ap0 type __ap 2>/dev/null || true
    fi
    echo "[physicar] AP interface: ap0 (internal, shared phy with STA)"
  else
    echo "[physicar] AP interface: $_AP_IFACE (USB WiFi, independent phy)"
  fi

  # ── 2. STA autoconnect check ──
  _sta_connected=0
  _sta_freq=0
  if sudo nmcli -t -f NAME,TYPE connection show 2>/dev/null | grep -q '802-11-wireless'; then
    sudo nmcli device wifi rescan ifname wlan0 &>/dev/null || true
    sleep 2
    for _try in 1 2 3; do
      _sta_freq=$(iw dev wlan0 link 2>/dev/null | awk '/freq:/{printf "%d",$2}')
      if [ -n "$_sta_freq" ] && [ "$_sta_freq" != "0" ]; then
        _sta_connected=1
        break
      fi
      sleep 2
    done
  fi

  # ── 3. Country estimation from nearby AP beacons ──
  _country=$(sudo iw dev wlan0 scan 2>/dev/null \
    | grep -oP 'Country: \K[A-Z]{2}' \
    | sort | uniq -c | sort -rn | awk 'NR==1{print $2}')
  : "${_country:=00}"
  echo "[physicar] Estimated country: $_country"

  # ── 4. Channel selection ──
  _ap_band_kf=""
  _ap_channel_kf=""

  if [ "$_AP_INDEPENDENT" = "0" ] && [ "$_sta_connected" = "1" ]; then
    # Internal AP (ap0) + STA connected → must follow STA channel (same phy)
    echo "[physicar] STA on ${_sta_freq} MHz; hotspot follows STA channel"
  else
    # Independent USB WiFi, or internal with no STA → pick least congested channel
    # Priority: 5GHz > 2.4GHz.  Within 5GHz: non-DFS channels only.

    # Determine which phy the AP interface uses
    _ap_phy=""
    if [ "$_AP_IFACE" != "ap0" ]; then
      _ap_phy=$(cat "/sys/class/net/${_AP_IFACE}/phy80211/name" 2>/dev/null)
    fi
    : "${_ap_phy:=phy0}"

    # Query AP phy's supported non-DFS frequencies → build candidate list
    # Only include channels that are: enabled, no-radar, and in our allowed set
    _supported_5g=""
    _supported_24g=""
    while IFS= read -r _line; do
      # Parse lines like: "* 5180.0 MHz [36] (23.0 dBm)"
      # Skip disabled or radar lines
      echo "$_line" | grep -qE 'disabled|radar' && continue
      _freq=$(echo "$_line" | grep -oP '\d{4,5}(?=\.0 MHz)')
      [ -z "$_freq" ] && continue
      case "$_freq" in
        5180|5200|5220|5240)               _supported_5g="$_supported_5g ${_freq}" ;;  # UNII-1
        5745|5765|5785|5805|5825)           _supported_5g="$_supported_5g ${_freq}" ;;  # UNII-3
        2412|2437|2462)                     _supported_24g="$_supported_24g ${_freq}" ;; # ch 1,6,11
      esac
    done < <(sudo iw phy "$_ap_phy" info 2>/dev/null | grep "MHz")

    # Map frequencies to channels
    _freq_to_ch() {
      case "$1" in
        5180) echo 36;; 5200) echo 40;; 5220) echo 44;; 5240) echo 48;;
        5745) echo 149;; 5765) echo 153;; 5785) echo 157;; 5805) echo 161;; 5825) echo 165;;
        2412) echo 1;; 2437) echo 6;; 2462) echo 11;;
      esac
    }

    # Filter by country (UNII-3 restricted in JP/unknown)
    _cand_5g=""
    for _f in $_supported_5g; do
      _ch=$(_freq_to_ch "$_f")
      case "$_country" in
        JP|00) [ "$_ch" -le 48 ] && _cand_5g="$_cand_5g $_ch" ;;  # UNII-1 only
        *)     _cand_5g="$_cand_5g $_ch" ;;
      esac
    done
    _cand_24g=""
    for _f in $_supported_24g; do
      _cand_24g="$_cand_24g $(_freq_to_ch "$_f")"
    done
    _cand_5g=$(echo $_cand_5g)    # trim
    _cand_24g=$(echo $_cand_24g)  # trim

    # Scan interference and pick least congested channel (5GHz preferred)
    # 2.4GHz gets a penalty (+0.01 ≈ -20dBm equivalent) to prefer 5GHz
    _best_channel=$(sudo iw dev wlan0 scan 2>/dev/null | awk -v cands5="$_cand_5g" -v cands24="$_cand_24g" '
      BEGIN {
        n5=split(cands5,c5); for(i=1;i<=n5;i++) energy[c5[i]]=0
        n24=split(cands24,c24); for(i=1;i<=n24;i++) energy[c24[i]]=0.01
      }
      /freq:/ { f=int($2) }
      /signal:/ {
        s=$2
        if      (f==5180) energy[36]  += 10^(s/10)
        else if (f==5200) energy[40]  += 10^(s/10)
        else if (f==5220) energy[44]  += 10^(s/10)
        else if (f==5240) energy[48]  += 10^(s/10)
        else if (f==5745) energy[149] += 10^(s/10)
        else if (f==5765) energy[153] += 10^(s/10)
        else if (f==5785) energy[157] += 10^(s/10)
        else if (f==5805) energy[161] += 10^(s/10)
        else if (f==5825) energy[165] += 10^(s/10)
        else if (f>=2402 && f<=2422) energy[1]  += 10^(s/10)
        else if (f>=2427 && f<=2447) energy[6]  += 10^(s/10)
        else if (f>=2452 && f<=2472) energy[11] += 10^(s/10)
      }
      END {
        ntot=n5+n24
        min_e=-1; min_ch=36
        for(i=1;i<=n5;i++){c=c5[i]; if(min_e<0||energy[c]<min_e){min_e=energy[c];min_ch=c}}
        for(i=1;i<=n24;i++){c=c24[i]; if(min_e<0||energy[c]<min_e){min_e=energy[c];min_ch=c}}
        print min_ch
      }')
    : "${_best_channel:=36}"

    # Determine band from selected channel
    if [ "$_best_channel" -ge 36 ]; then
      _ap_band_kf="band=a"
      echo "[physicar] Hotspot channel: ${_best_channel} (5 GHz, country=${_country})"
    else
      _ap_band_kf="band=bg"
      echo "[physicar] Hotspot channel: ${_best_channel} (2.4 GHz, country=${_country})"
    fi
    _ap_channel_kf="channel=${_best_channel}"
  fi

  # ── 5. dnsmasq captive-portal DNS ──
  sudo mkdir -p /etc/NetworkManager/dnsmasq-shared.d
  {
    echo "address=/$DEVICE_HOSTNAME.local/${_AP_IP}"
    [ "$DEVICE_HOSTNAME" != "physicar" ] && echo "address=/physicar.local/${_AP_IP}"
    echo "address=/device.physicar.ai/${_AP_IP}"
    echo "address=/preview.physicar.ai/${_AP_IP}"
    echo ""
    echo "address=/www.msftconnecttest.com/${_AP_IP}"
    echo "address=/msftconnecttest.com/${_AP_IP}"
    # Windows NCSI also does a DNS probe: dns.msftncsi.com must resolve to the
    # exact IP 131.107.255.255, plus an http://www.msftncsi.com/ncsi.txt fetch.
    # Spoof these self-contained (not via upstream) so the hotspot still looks
    # "online" with no real internet — otherwise Windows flags "No Internet" and
    # the laptop roams to another SSID.
    echo "address=/dns.msftncsi.com/131.107.255.255"
    echo "address=/www.msftncsi.com/${_AP_IP}"
    echo "address=/msftncsi.com/${_AP_IP}"
    echo "address=/captive.apple.com/${_AP_IP}"
    echo "address=/connectivitycheck.gstatic.com/${_AP_IP}"
    echo "address=/clients3.google.com/${_AP_IP}"
    echo "address=/detectportal.firefox.com/${_AP_IP}"
    echo "address=/connectivity-check.ubuntu.com/${_AP_IP}"
    echo ""
    echo "no-resolv"
    echo "server=1.1.1.1"
    echo "server=8.8.8.8"
    echo "server=8.8.4.4"
  } | sudo tee /etc/NetworkManager/dnsmasq-shared.d/physicar.conf > /dev/null

  # ── 6. Hotspot NM keyfile (bypasses netplan passthrough) ──
  _HOTSPOT_FILE="/etc/NetworkManager/system-connections/physicar-hotspot.nmconnection"
  _need_write=1

  if [ -f "$_HOTSPOT_FILE" ]; then
    _file_ssid=$(sudo grep -m1 '^ssid=' "$_HOTSPOT_FILE" 2>/dev/null | cut -d= -f2)
    _file_psk=$(sudo grep -m1 '^psk=' "$_HOTSPOT_FILE" 2>/dev/null | cut -d= -f2)
    _file_iface=$(sudo grep -m1 '^interface-name=' "$_HOTSPOT_FILE" 2>/dev/null | cut -d= -f2)
    if [ "$_file_ssid" = "$DEVICE_HOSTNAME" ] && [ "$_file_psk" = "$PASSWORD" ] \
       && [ "$_file_iface" = "$_AP_IFACE" ] \
       && sudo grep -q '^wps-method=1' "$_HOTSPOT_FILE" 2>/dev/null; then
      _need_write=0
    fi
  fi

  if [ "$_need_write" = "1" ]; then
    # Verify AP interface exists
    if [ "$_AP_IFACE" = "ap0" ] && ! iw dev ap0 info &>/dev/null; then
      echo "[physicar] WARNING: Could not create AP interface" >&2
      exit 1
    fi
    sudo tee "$_HOTSPOT_FILE" > /dev/null <<HOTSPOT_KF
[connection]
id=physicar-hotspot
uuid=$(cat /proc/sys/kernel/random/uuid)
type=wifi
interface-name=${_AP_IFACE}
autoconnect=false

[wifi]
ssid=${DEVICE_HOSTNAME}
mode=ap
${_ap_band_kf}
${_ap_channel_kf}

[wifi-security]
key-mgmt=wpa-psk
proto=rsn
pairwise=ccmp
group=ccmp
pmf=1
# Disable WPS advertisement — Windows otherwise prompts for a router PIN
# instead of the passphrase when connecting to the hotspot.
wps-method=1
psk=${PASSWORD}

[ipv4]
method=shared
address1=${_AP_IP}/24

[ipv6]
method=auto
addr-gen-mode=default

[proxy]
HOTSPOT_KF
    sudo chmod 600 "$_HOTSPOT_FILE"
    sudo nmcli connection reload 2>/dev/null
    sleep 1
    echo "[physicar] Hotspot keyfile written (iface=${_AP_IFACE})"
  fi

  # ── 7. Start hotspot ──
  if ! sudo nmcli connection show --active 2>/dev/null | grep -q physicar-hotspot; then
    sudo nmcli connection up physicar-hotspot &>/dev/null && \
      echo "[physicar] Hotspot: $DEVICE_HOSTNAME (${_AP_IFACE}, ${_AP_IP})" || \
      echo "[physicar] WARNING: Hotspot failed to start" >&2
  else
    echo "[physicar] Hotspot already running: $DEVICE_HOSTNAME"
  fi

  # ── 8. Update avahi/mDNS to use correct AP interface ──
  sudo mkdir -p /etc/avahi
  if [ -f /etc/avahi/avahi-daemon.conf ]; then
    _avahi_iface=$(grep -m1 '^allow-interfaces=' /etc/avahi/avahi-daemon.conf 2>/dev/null | cut -d= -f2)
    if [ "$_avahi_iface" != "$_AP_IFACE" ]; then
      sudo sed -i \
        -e "s/^#*allow-interfaces=.*/allow-interfaces=${_AP_IFACE}/" \
        -e 's/^#*deny-interfaces=.*/deny-interfaces=wlan0,eth0/' \
        /etc/avahi/avahi-daemon.conf
      grep -q '^allow-interfaces=' /etc/avahi/avahi-daemon.conf || \
        sudo sed -i "/^\[server\]/a allow-interfaces=${_AP_IFACE}" /etc/avahi/avahi-daemon.conf
      grep -q '^deny-interfaces=' /etc/avahi/avahi-daemon.conf || \
        sudo sed -i '/^\[server\]/a deny-interfaces=wlan0,eth0' /etc/avahi/avahi-daemon.conf
      sudo systemctl restart avahi-daemon &>/dev/null || true
      echo "[physicar] Avahi updated: allow-interfaces=${_AP_IFACE}"
    fi
  fi
) &

# ────────────────── X11 Display ──────────────────

# Xorg + splash in background — nothing below depends on :0 being ready
(
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
  if [ -f "$SPLASH_IMG" ]; then
    feh --bg-fill --no-fehbg "$SPLASH_IMG" &>/dev/null || true
    feh --fullscreen --hide-pointer --no-menus --image-bg black --no-fehbg \
        "$SPLASH_IMG" &>/dev/null &
    echo $! > /run/physicar/splash.pid
  else
    xsetroot -solid '#0b0b0b' &>/dev/null || true
  fi
) &

# ────────────────── Virtual Display + VNC ──────────────────

# VirtualGL: redirect OpenGL from Xvfb(:1) to GPU on Xorg(:0)
export VGL_DISPLAY=:0
export LD_PRELOAD=/usr/lib/libvglfaker.so
export XDG_RUNTIME_DIR=/tmp/runtime-$(id -u)
mkdir -p "$XDG_RUNTIME_DIR"

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

sudo xhost +SI:localuser:physicar &>/dev/null || true

if ! pgrep -x x11vnc > /dev/null 2>&1; then
  x11vnc -display :1 -forever -shared -nopw -rfbport 5901 -bg -o /dev/null
fi
if ! pgrep -f novnc_proxy > /dev/null 2>&1; then
  /usr/share/novnc/utils/novnc_proxy --vnc localhost:5901 --listen 6080 &>/dev/null &
fi

# Bluetooth auto-pair agent
if command -v bt-agent >/dev/null 2>&1 && ! pgrep -x bt-agent >/dev/null; then
  sudo bt-agent --capability=NoInputNoOutput &>/dev/null &
fi

# ────────────────── Nginx Auth Maps ──────────────────

# Preserve BOOT_TOKEN across restarts (only regenerate on reboot)
# /run/ is tmpfs — cleared on reboot, so token file absence = fresh boot
BOOT_TOKEN_FILE="/run/physicar/boot_token"
if [ -f "$BOOT_TOKEN_FILE" ]; then
  BOOT_TOKEN=$(cat "$BOOT_TOKEN_FILE")
else
  BOOT_TOKEN=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 24)
  echo "$BOOT_TOKEN" > "$BOOT_TOKEN_FILE"
fi
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

APP_FILE="$HOME/physicar_ws/app.physicar"
sudo chattr -i "$APP_FILE" 2>/dev/null || true
echo "https://device.physicar.ai/app" > "$APP_FILE"
chmod 444 "$APP_FILE"
sudo chattr +i "$APP_FILE"

CODE_USER_DIR="$HOME/.local/share/code-server/User"
mkdir -p "$CODE_USER_DIR"
if [ ! -f "$CODE_USER_DIR/settings.json" ]; then
cat > "$CODE_USER_DIR/settings.json" <<'CODE_SETTINGS'
{
  "chat.disableAIFeatures": true,
  "chat.sendElementsToChat.enabled": false,
  "workbench.startupEditor": "none",
  "workbench.welcomePage.walkthroughs.openOnInstall": false,
  "editor.fontSize": 14,
  "editor.tabSize": 4,
  "files.autoSave": "afterDelay",
  "python.defaultInterpreterPath": "/usr/bin/python3",
  "simpleBrowser.focusLockIndicator.enabled": false,
  "telemetry.telemetryLevel": "off"
}
CODE_SETTINGS
fi

# code-server is managed by physicar-code.service

# ── Webview microphone/camera patch (idempotent, every boot) ──
# VS Code's webview iframes don't delegate mic/cam permission, blocking
# getUserMedia in every webview below them (extension panels, app.physicar).
# The install script patches once, but a code-server UPDATE restores the
# bundle — so re-apply here on every boot (no-op when already patched).
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

  # Pattern C edits the inline bootstrap script of the webview pre/index.html,
  # whose sha256 is pinned in the same file's CSP meta tag. Recompute the hash
  # or the browser blocks the script and every webview renders blank
  # (extension panels, app.physicar viewer).
  while IFS= read -r f; do
    python3 - "$f" <<'CSPFIX' || true
import sys, re, hashlib, base64
p = sys.argv[1]
t = open(p, encoding='utf-8').read()
m = re.search(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", t, re.S)
if m:
    h = base64.b64encode(hashlib.sha256(m.group(1).encode()).digest()).decode()
    new, n = re.subn(r"'sha256-[A-Za-z0-9+/=]+'", "'sha256-%s'" % h, t)
    if n and new != t:
        open(p, "w", encoding="utf-8").write(new)
        print("[media-patch] CSP hash updated in " + p)
CSPFIX
  done < <(find "$cs_vscode/out" -path '*webview*/pre/index.html' 2>/dev/null)

  # Silent-failure guard: after patching, at least one file must carry one of
  # the patched allow-lists. If none do, a code-server update changed the
  # pattern shape (it happened at 4.12x already) — warn loudly so it shows up
  # in the boot log instead of mic/cam just silently breaking.
  if ! grep -rqF -e "$A_NEW" -e "$B_NEW" -e "$C_NEW" "$cs_vscode/out" 2>/dev/null; then
    echo "[media-patch] WARNING: no known allow-list pattern found in this code-server version — webview mic/cam will stay blocked until the patterns in this function are updated"
  fi
}
patch_codeserver_webview_media || true

# Pre-install extensions on first boot
EXT_MARKER="$HOME/.local/share/code-server/.physicar-ext-installed"
if [ ! -f "$EXT_MARKER" ]; then
  (
    for i in $(seq 1 60); do
      ss -tlnp 2>/dev/null | grep -q ':8080 ' && break
      sleep 2
    done
    # Old bundled browser extension -> replaced by the Open VSX build
    /usr/local/bin/code-server --uninstall-extension undefined_publisher.physicar-browser-ext &>/dev/null || true
    for EXT_ID in physicar.physicar-ext ms-python.python ms-python.debugpy redhat.vscode-xml redhat.vscode-yaml; do
      /usr/local/bin/code-server --install-extension "$EXT_ID" &>/dev/null || true
    done
    # Marker only on success — installs need internet (Open VSX), which may
    # not be up yet on first boot. No marker -> retried on the next boot.
    if /usr/local/bin/code-server --list-extensions 2>/dev/null | grep -q '^physicar.physicar-ext$'; then
      touch "$EXT_MARKER"
    fi
  ) &
fi


# ────────────────── ROS2 Launch ──────────────────

export DISPLAY=:1
source /opt/ros/jazzy/setup.bash
export ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST

# CycloneDDS pinned to loopback (cyclonedds.xml): all topics are machine-
# local; binding lo with unicast-only discovery keeps hotspot/wifi/eth
# interface changes invisible to DDS. Replaces Fast DDS, whose reliable-
# channel state wedged long-running participants into announce-only mode
# (camera/driver stop delivering while the processes look alive).
export RMW_IMPLEMENTATION=rmw_cyclonedds_cpp
export CYCLONEDDS_URI="file://$PHYSICAR_ROS_DIR/deploy/cyclonedds.xml"
rm -f /dev/shm/fastrtps_* 2>/dev/null

# Absorb the boot-time discovery burst: 13+ DDS participants exchange SEDP
# on 127.0.0.1 at once and the 208KB kernel default drops datagrams
# (UdpRcvbufErrors), leaving endpoints unmatched.
sudo sysctl -qw net.core.rmem_max=16777216 net.core.rmem_default=4194304 net.core.wmem_max=16777216 2>/dev/null || true

UPDATE_SIGNAL="/tmp/.physicar-update-ready"

git config --global --add safe.directory "$PHYSICAR_ROS_DIR" 2>/dev/null || true

clean_build() {
    echo "[physicar] Running clean build..."
    rm -rf "$PHYSICAR_WS/build" "$PHYSICAR_WS/install" "$PHYSICAR_WS/log"
    cd "$PHYSICAR_WS" && colcon build --symlink-install 2>&1
}

do_build() {
    rm -f "$PHYSICAR_ROS_DIR/physicar_camera/COLCON_IGNORE" 2>/dev/null
    rm -f "$PHYSICAR_ROS_DIR/physicar_lidar/COLCON_IGNORE" 2>/dev/null

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

rm -f "$UPDATE_SIGNAL"

# Build only once on first boot (or if install/ is missing)
if [ ! -d "$PHYSICAR_WS/install" ]; then
    do_build
else
    echo "[physicar] install/ exists, skipping build."
    source "$PHYSICAR_WS/install/setup.bash"
fi

while true; do
    echo "[physicar] Launching..."
    ros2 launch physicar_bringup device.launch.py &
    LAUNCH_PID=$!
    wait $LAUNCH_PID 2>/dev/null

    if [ -f "$UPDATE_SIGNAL" ]; then
        rm -f "$UPDATE_SIGNAL"
        echo "[physicar] Update detected → rebuilding..."
        do_build
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
      # Wait for Xorg :0 (started in background)
      until xdpyinfo -display :0 &>/dev/null; do
        sleep 0.5
      done

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

      # XFixes-based global cursor hiding: starts hidden and stays hidden on
      # touch input (even over select dropdowns, which grab the pointer),
      # but real mouse movement shows the cursor — so a plugged-in USB
      # mouse works normally.
      pgrep -f unclutter-xfixes >/dev/null || \
        DISPLAY=:0 unclutter-xfixes --timeout 2 --start-hidden --hide-on-touch &>/dev/null &

      if [ "$FIRST" = "1" ]; then
        SPLASH_PID=$(cat /run/physicar/splash.pid 2>/dev/null)
        if [ -n "$SPLASH_PID" ]; then
          for _ in {1..120}; do
            if DISPLAY=:0 xwininfo -root -tree 2>/dev/null | grep -qi 'chromium'; then
              break
            fi
            sleep 0.25
          done
          sleep 4
          kill "$SPLASH_PID" 2>/dev/null || true
          rm -f /run/physicar/splash.pid
        fi
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
  done
) &

# Keep service alive
wait
