#!/bin/bash
# ============================================
# USB Audio Setup (auto-detect with dmix for multi-channel mixing)
# ============================================

USB_LINE=$(grep "USB-Audio" /proc/asound/cards 2>/dev/null | head -1)
if [ -n "$USB_LINE" ]; then
  USB_CARD_NAME=$(echo "$USB_LINE" | sed 's/.*\[\(.*\)\].*/\1/' | tr -d ' ')
  cat > /etc/asound.conf << ASOUND
# USB Speaker hardware device
pcm.usb_hw {
    type hw
    card $USB_CARD_NAME
    device 0
}

# dmix for multi-channel audio mixing (allows concurrent playback)
pcm.dmixer {
    type dmix
    ipc_key 1024
    slave {
        pcm "usb_hw"
        period_time 0
        period_size 1024
        buffer_size 4096
        rate 48000
    }
}

# Default uses dmix via plug for format conversion
pcm.!default {
    type plug
    slave.pcm "dmixer"
}

ctl.!default {
    type hw
    card $USB_CARD_NAME
}
ASOUND
  # Set volume to 100%
  amixer -c 0 sset PCM 100% >/dev/null 2>&1 || true
  echo "[setup_audio] USB Audio: $USB_CARD_NAME (100%) [dmix enabled]"
else
  echo "[setup_audio] WARNING: USB audio not found" >&2
fi
