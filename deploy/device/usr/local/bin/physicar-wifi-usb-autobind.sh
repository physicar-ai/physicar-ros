#!/bin/sh
# Called by udev when a USB interface with class ff:ff:ff appears.
# Tries to register the device with Realtek WiFi drivers.
# Usage: physicar-wifi-usb-autobind.sh <devpath>
DEVPATH="$1"
[ -n "$DEVPATH" ] || exit 0

PARENT="/sys$(dirname "$DEVPATH")"
[ -f "$PARENT/idVendor" ] || exit 0

VID=$(cat "$PARENT/idVendor")
PID=$(cat "$PARENT/idProduct")

# Skip USB modeswitch intermediate device
[ "$VID:$PID" = "0bda:1a2b" ] && exit 0

for drv in rtl8852au rtl8852bu; do
  echo "$VID $PID" > "/sys/bus/usb/drivers/$drv/new_id" 2>/dev/null || true
done
