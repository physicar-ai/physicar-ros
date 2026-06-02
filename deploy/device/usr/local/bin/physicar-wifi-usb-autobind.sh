#!/bin/sh
# Called by udev when a USB interface with class ff:ff:ff appears.
# Tries to register the device with Realtek WiFi drivers.
# Uses ref-ID mechanism so driver_info (chip type) is correctly passed,
# avoiding segfaults from driver_info=0 auto-detection failures.
# Usage: physicar-wifi-usb-autobind.sh <devpath>
DEVPATH="$1"
[ -n "$DEVPATH" ] || exit 0

PARENT="/sys$(dirname "$DEVPATH")"
[ -f "$PARENT/idVendor" ] || exit 0

VID=$(cat "$PARENT/idVendor")
PID=$(cat "$PARENT/idProduct")

# Skip if this device needs another modeswitch (config exists in usb_modeswitch.d)
[ -f "/etc/usb_modeswitch.d/$VID:$PID" ] && exit 0

# Skip devices that still have a mass storage interface (needs modeswitch first)
for _sib in "$PARENT"/*/bInterfaceClass; do
  [ -f "$_sib" ] || continue
  [ "$(cat "$_sib")" = "08" ] && exit 0
done

# Skip if driver already bound
[ -L "$PARENT/$(basename "$DEVPATH")/driver" ] && exit 0

# Register with each Realtek driver using a reference ID so that
# driver_info (chip type) is inherited from a known table entry.
# Format: "VID PID bInterfaceClass refVID refPID"
#   rtl8852au ref: 0bda:8832 (RTL8852A)
#   rtl8852bu ref: 0bda:b832 (RTL8852B)
#   rtl8852cu ref: 0bda:8832 (RTL8852A)
echo "$VID $PID ff 0bda 8832" > /sys/bus/usb/drivers/rtl8852au/new_id 2>/dev/null || true
echo "$VID $PID ff 0bda b832" > /sys/bus/usb/drivers/rtl8852bu/new_id 2>/dev/null || true
echo "$VID $PID ff 0bda 8832" > /sys/bus/usb/drivers/rtl8852cu/new_id 2>/dev/null || true
