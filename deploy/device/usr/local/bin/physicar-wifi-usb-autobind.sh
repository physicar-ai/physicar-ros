#!/bin/sh
# Called by udev when a USB interface with class ff:ff:ff appears.
# Tries to register the device with Realtek WiFi drivers via ref-ID mechanism.
# Then triggers hotspot migration if a wireless interface appears.
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

# Register with drivers only if not already bound
if ! [ -L "$PARENT/$(basename "$DEVPATH")/driver" ]; then
  echo "$VID $PID ff 0bda 8832" > /sys/bus/usb/drivers/rtl8852au/new_id 2>/dev/null || true
  echo "$VID $PID ff 0bda b832" > /sys/bus/usb/drivers/rtl8852bu/new_id 2>/dev/null || true
  echo "$VID $PID ff 0bda 8832" > /sys/bus/usb/drivers/rtl8852cu/new_id 2>/dev/null || true
fi

# Trigger hotspot migration after driver has time to create the wlx* interface.
# Uses systemd-run to escape udev's process-kill timeout.
# The migrate script has its own flock, so rapid plug/unplug is safe.
/bin/systemd-run --no-block /bin/sh -c '
  for i in 1 2 3 4 5 6; do
    sleep 1
    for n in /sys/class/net/wlx*; do
      [ -d "$n/wireless" ] && exec /usr/local/bin/physicar-wifi-hotspot-migrate.sh
    done
  done
'
