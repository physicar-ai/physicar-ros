#!/usr/bin/env python3
"""
PhysiCar device password helpers.

Authentication itself is handled by the host nginx (see
physicar-setup/host-device/files/etc/nginx/sites-available/physicar).
This module only exposes utilities for code that needs to know the
device password value (e.g. WiFi hotspot SSID setup, the
POST /auth/password endpoint that rewrites the password file).

Password resolution priority (matches host physicar.sh):
  1. /home/physicar/physicar_ws/userdata/password file (8-63 ASCII printable)
  2. SHA-256(serial)[8:16]
  3. fallback → "physicar"
"""

import hashlib
import os
from functools import lru_cache


@lru_cache(maxsize=1)
def get_password() -> str:
    """Get the device password (cached)."""
    password_file = '/home/physicar/physicar_ws/userdata/password'
    if os.path.exists(password_file):
        try:
            with open(password_file, 'r') as f:
                password = f.read().strip()
                if (password
                        and 8 <= len(password) <= 63
                        and all(0x20 <= ord(c) <= 0x7E for c in password)):
                    return password
        except Exception:
            pass

    serial_file = '/sys/firmware/devicetree/base/serial-number'
    if os.path.exists(serial_file):
        try:
            with open(serial_file, 'rb') as f:
                serial = f.read().rstrip(b'\x00').decode('utf-8', errors='ignore')
                if serial:
                    serial_hash = hashlib.sha256(serial.encode()).hexdigest()[:16]
                    return serial_hash[8:16]
        except Exception:
            pass

    return 'physicar'


def clear_password_cache():
    """Drop the cached password (call after rewriting the password file)."""
    get_password.cache_clear()
