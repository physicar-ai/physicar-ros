#!/usr/bin/env python3
"""
PhysiCar device password helpers.

Authentication itself is handled by the host nginx (see
physicar-setup/host-device/files/etc/nginx/sites-available/physicar).
This module only exposes utilities for code that needs to know the
device password value (e.g. WiFi hotspot SSID setup, the
POST /auth/password endpoint that rewrites the password file).

Password resolution priority (matches host physicar.sh):
  1. /opt/physicar/password file (8-63 ASCII printable)
  2. DEV/SIM mode → "physicar"
  3. SHA-256(serial)[8:16]   (production)
  4. fallback → "physicar"
"""

import hashlib
import os
from functools import lru_cache


def _is_dev_mode() -> bool:
    """True if DEV env is set or sim_mode ROS parameter is true."""
    val = os.environ.get('DEV', '').lower()
    if val in ('true', '1'):
        return True

    env_file = '/opt/physicar/.env'
    if os.path.exists(env_file):
        try:
            with open(env_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('DEV='):
                        value = line.split('=', 1)[1].strip().strip('\'"').lower()
                        if value in ('true', '1'):
                            return True
        except Exception:
            pass

    # SIM mode: check ROS parameter on webserver node
    try:
        from physicar_webserver.ros_bridge import get_ros_bridge
        bridge = get_ros_bridge()
        node = bridge._node
        if node:
            if not node.has_parameter('sim_mode'):
                node.declare_parameter('sim_mode', False)
            if node.get_parameter('sim_mode').value:
                return True
    except Exception:
        pass

    return False


@lru_cache(maxsize=1)
def get_password() -> str:
    """Get the device password (cached)."""
    password_file = '/opt/physicar/password'
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

    if _is_dev_mode():
        return 'physicar'

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
