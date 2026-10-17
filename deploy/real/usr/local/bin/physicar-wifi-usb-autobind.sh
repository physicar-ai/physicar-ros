#!/bin/sh
# Called by udev when a USB interface with class ff:ff:ff appears (a USB WiFi adapter).
# The in-kernel rtw89 driver binds the adapter by USB id on its own; this script only
# triggers the hotspot migration once the wireless interface shows up.
# Usage: physicar-wifi-usb-autobind.sh <devpath>
DEVPATH="$1"
[ -n "$DEVPATH" ] || exit 0

PARENT="/sys$(dirname "$DEVPATH")"
[ -f "$PARENT/idVendor" ] || exit 0

VID=$(cat "$PARENT/idVendor")
PID=$(cat "$PARENT/idProduct")

# Skip if this device needs modeswitch
[ -f "/etc/usb_modeswitch.d/$VID:$PID" ] && exit 0

# Skip devices with a mass storage interface (needs modeswitch first)
for _sib in "$PARENT"/*/bInterfaceClass; do
  [ -f "$_sib" ] || continue
  [ "$(cat "$_sib")" = "08" ] && exit 0
done

# Trigger hotspot migration after driver has time to create the wlx* interface.
# Uses systemd-run to escape udev's process-kill timeout.
# The migrate script has its own flock, so rapid plug/unplug is safe.
/bin/systemd-run --no-block /bin/sh -c '
  for i in 1 2 3 4 5 6; do
    sleep 1
    for n in /sys/class/net/wlx*; do
      [ -d "$n/wireless" ] && exec /opt/physicar/src/physicar-ros/deploy/real/usr/local/bin/physicar-wifi-hotspot-migrate.sh
    done
  done
'
