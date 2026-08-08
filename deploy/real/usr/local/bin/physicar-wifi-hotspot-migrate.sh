#!/bin/sh
# Migrate physicar-hotspot to USB WiFi if available, or back to ap0 if not.
# Called by udev (autobind on plug, remove on unplug) and at boot.
# Safe to call multiple times — only rewrites keyfile if interface changed.
#
# Robustness guarantees:
#   - flock prevents concurrent runs (rapid plug/unplug)
#   - USB interface existence verified before activation
#   - Scan has 10s timeout + fallback to cached data
#   - If USB activation fails, automatic fallback to ap0
#   - Never explicitly brings down ap0 hotspot (avoids phy0/wlan0 disruption)
#   - Regulatory domain set from estimated country

HOTSPOT_FILE="/etc/NetworkManager/system-connections/physicar-hotspot.nmconnection"
LOCK="/run/physicar/hotspot-migrate.lock"

mkdir -p /run/physicar

# Avoid concurrent runs (udev can fire multiple events)
exec 9>"$LOCK"
flock -n 9 || exit 0

log() { logger -t physicar-hotspot-migrate "$*"; }

# ── Determine target AP interface ──
_new_iface=""
for _net in /sys/class/net/wlx*; do
  [ -e "$_net" ] || continue
  _candidate=$(basename "$_net")
  [ -d "/sys/class/net/$_candidate/wireless" ] || continue
  _new_iface="$_candidate"
  break
done

if [ -z "$_new_iface" ]; then
  _new_iface="ap0"
  if ! iw dev ap0 info >/dev/null 2>&1; then
    iw dev wlan0 interface add ap0 type __ap 2>/dev/null || true
    sleep 1
  fi
fi

# ── Check if migration is needed ──
[ -f "$HOTSPOT_FILE" ] || exit 0

_current_iface=$(grep -m1 '^interface-name=' "$HOTSPOT_FILE" 2>/dev/null | cut -d= -f2)
[ "$_current_iface" = "$_new_iface" ] && exit 0

log "Migrating hotspot: $_current_iface → $_new_iface"

# ── Read existing config values ──
_ssid=$(grep -m1 '^ssid=' "$HOTSPOT_FILE" | cut -d= -f2)
_psk=$(grep -m1 '^psk=' "$HOTSPOT_FILE" | cut -d= -f2)
_uuid=$(grep -m1 '^uuid=' "$HOTSPOT_FILE" | cut -d= -f2)
: "${_uuid:=$(cat /proc/sys/kernel/random/uuid)}"

if [ -z "$_ssid" ] || [ -z "$_psk" ]; then
  log "ERROR: missing ssid/psk in keyfile"
  exit 1
fi

# ── Channel selection ──
_band=""
_channel=""

if [ "$_new_iface" != "ap0" ]; then
  # USB WiFi — independent phy, pick best channel
  _ap_phy=$(cat "/sys/class/net/${_new_iface}/phy80211/name" 2>/dev/null)
  : "${_ap_phy:=phy0}"

  # Scan — try USB phy (10s timeout), fallback to wlan0 cached scan
  _scan=$(timeout 10 iw dev "${_new_iface}" scan 2>/dev/null) \
    || _scan=$(iw dev wlan0 scan dump 2>/dev/null) \
    || _scan=""

  # Country estimation + regulatory domain
  _country=$(printf '%s' "$_scan" | grep -oP 'Country: \K[A-Z]{2}' \
    | sort | uniq -c | sort -rn | awk 'NR==1{print $2}')
  : "${_country:=00}"

  # Candidate channels from USB phy capabilities
  _cand_5g=""
  _cand_24g=""
  while IFS= read -r _line; do
    echo "$_line" | grep -qE 'disabled|radar' && continue
    _freq=$(echo "$_line" | grep -oP '\d{4,5}(?=\.0 MHz)')
    [ -z "$_freq" ] && continue
    case "$_freq" in
      5180) _ch=36;; 5200) _ch=40;; 5220) _ch=44;; 5240) _ch=48;;
      5745) _ch=149;; 5765) _ch=153;; 5785) _ch=157;; 5805) _ch=161;; 5825) _ch=165;;
      2412) _ch=1;; 2437) _ch=6;; 2462) _ch=11;;
      *) continue;;
    esac
    if [ "$_freq" -ge 5000 ]; then
      case "$_country" in
        JP|00) [ "$_ch" -le 48 ] && _cand_5g="$_cand_5g $_ch" ;;
        *) _cand_5g="$_cand_5g $_ch" ;;
      esac
    else
      _cand_24g="$_cand_24g $_ch"
    fi
  done <<EOF
$(iw phy "$_ap_phy" info 2>/dev/null | grep "MHz")
EOF
  _cand_5g=$(echo $_cand_5g)
  _cand_24g=$(echo $_cand_24g)

  _best_channel=$(printf '%s' "$_scan" | awk -v c5="$_cand_5g" -v c24="$_cand_24g" '
    BEGIN {
      n5=split(c5,a5); for(i=1;i<=n5;i++) e[a5[i]]=0
      n24=split(c24,a24); for(i=1;i<=n24;i++) e[a24[i]]=0.01
    }
    /freq:/{f=int($2)}
    /signal:/{s=$2
      if(f==5180)e[36]+=10^(s/10); else if(f==5200)e[40]+=10^(s/10)
      else if(f==5220)e[44]+=10^(s/10); else if(f==5240)e[48]+=10^(s/10)
      else if(f==5745)e[149]+=10^(s/10); else if(f==5765)e[153]+=10^(s/10)
      else if(f==5785)e[157]+=10^(s/10); else if(f==5805)e[161]+=10^(s/10)
      else if(f==5825)e[165]+=10^(s/10)
      else if(f>=2402&&f<=2422)e[1]+=10^(s/10)
      else if(f>=2427&&f<=2447)e[6]+=10^(s/10)
      else if(f>=2452&&f<=2472)e[11]+=10^(s/10)
    }
    END {
      min=-1;ch=36
      for(i=1;i<=n5;i++){c=a5[i];if(min<0||e[c]<min){min=e[c];ch=c}}
      for(i=1;i<=n24;i++){c=a24[i];if(min<0||e[c]<min){min=e[c];ch=c}}
      print ch
    }')
  : "${_best_channel:=36}"

  if [ "$_best_channel" -ge 36 ]; then
    _band="a"
  else
    _band="bg"
  fi
  _channel="$_best_channel"
  log "USB phy=$_ap_phy country=$_country → ch $_channel"
else
  # ap0 — follow STA channel (same phy)
  _sta_freq=$(iw dev wlan0 link 2>/dev/null | awk '/freq:/{printf "%d",$2}')
  if [ -n "$_sta_freq" ] && [ "$_sta_freq" != "0" ]; then
    log "ap0 follows STA channel (${_sta_freq} MHz)"
  fi
fi

# ── Verify target interface still exists (USB could have been yanked) ──
if [ "$_new_iface" != "ap0" ] && [ ! -d "/sys/class/net/${_new_iface}" ]; then
  log "WARN: ${_new_iface} disappeared during migration, falling back to ap0"
  _new_iface="ap0"
  _band=""
  _channel=""
  if ! iw dev ap0 info >/dev/null 2>&1; then
    iw dev wlan0 interface add ap0 type __ap 2>/dev/null || true
    sleep 1
  fi
  # Re-check if already on ap0
  [ "$_current_iface" = "ap0" ] && exit 0
fi

# ── Rewrite keyfile (never explicitly bring down ap0 to protect wlan0 STA) ──
_band_line=""
_channel_line=""
[ -n "$_band" ] && _band_line="band=$_band"
[ -n "$_channel" ] && _channel_line="channel=$_channel"

cat > "$HOTSPOT_FILE" <<HOTSPOT_KF
[connection]
id=physicar-hotspot
uuid=${_uuid}
type=wifi
interface-name=${_new_iface}
autoconnect=false

[wifi]
ssid=${_ssid}
mode=ap
${_band_line}
${_channel_line}

[wifi-security]
key-mgmt=wpa-psk
proto=rsn
pairwise=ccmp
group=ccmp
pmf=1
psk=${_psk}

[ipv4]
method=shared
address1=10.42.0.1/24

[ipv6]
method=auto
addr-gen-mode=default

[proxy]
HOTSPOT_KF
chmod 600 "$HOTSPOT_FILE"
nmcli connection load "$HOTSPOT_FILE" 2>/dev/null
sleep 1

# ── Start hotspot on new interface ──
if nmcli connection up physicar-hotspot 2>/dev/null; then
  log "Hotspot started on $_new_iface"
  # Disable power save on the AP interface
  iw dev "$_new_iface" set power_save off 2>/dev/null || true
else
  log "ERROR: Hotspot failed on $_new_iface"
  if [ "$_new_iface" != "ap0" ]; then
    log "Falling back to ap0"
    sed -i "s/^interface-name=.*/interface-name=ap0/" "$HOTSPOT_FILE"
    # Remove band/channel so ap0 follows STA
    sed -i '/^band=/d; /^channel=/d' "$HOTSPOT_FILE"
    nmcli connection load "$HOTSPOT_FILE" 2>/dev/null
    sleep 1
    nmcli connection up physicar-hotspot 2>/dev/null || log "ERROR: ap0 fallback also failed"
  fi
fi

# ── Update avahi ──
if [ -f /etc/avahi/avahi-daemon.conf ]; then
  _active_iface=$(grep -m1 '^interface-name=' "$HOTSPOT_FILE" | cut -d= -f2)
  _cur_avahi=$(grep -m1 '^allow-interfaces=' /etc/avahi/avahi-daemon.conf 2>/dev/null | cut -d= -f2)
  if [ "$_cur_avahi" != "$_active_iface" ]; then
    sed -i "s/^#*allow-interfaces=.*/allow-interfaces=${_active_iface}/" /etc/avahi/avahi-daemon.conf
    systemctl restart avahi-daemon 2>/dev/null || true
  fi
fi
