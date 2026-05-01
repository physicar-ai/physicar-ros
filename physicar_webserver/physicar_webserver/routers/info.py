#!/usr/bin/env python3
"""
Info router - Kit information endpoint.
"""

import hashlib
import os

from fastapi import APIRouter

from physicar_webserver.ros_bridge import get_ros_bridge

router = APIRouter()


def _get_serial() -> str:
    """Compute device serial number: SHA-256(rpi-serial)[:16].

    Mirrors host-side logic in physicar.sh — same value used for hostname
    suffix (first 8 chars) and default password (last 8 chars).
    Returns empty string if unavailable (e.g. SIM mode on non-RPi host).
    """
    serial_file = '/sys/firmware/devicetree/base/serial-number'
    try:
        if os.path.exists(serial_file):
            with open(serial_file, 'rb') as f:
                raw = f.read().rstrip(b'\x00').decode('utf-8', errors='ignore').strip()
                if raw:
                    return hashlib.sha256(raw.encode()).hexdigest()[:16]
    except Exception:
        pass
    return ''


def _get_mode() -> str:
    """Determine running mode: 'sim' or 'real'.
    
    sim.launch.py sets sim_mode=True on the webserver node.
    robot.launch.py does not set it, so it defaults to False.
    """
    try:
        bridge = get_ros_bridge()
        node = bridge._node
        if node:
            if not node.has_parameter('sim_mode'):
                node.declare_parameter('sim_mode', False)
            if node.get_parameter('sim_mode').value:
                return 'sim'
    except Exception:
        pass
    return 'real'


# Kit Information
KIT_INFO = {
    "name": "PhysiCar",
    "generation": 1,
    "version": "1.0.0",
    "hardware": {
        "board": "Yahboom Robot Expansion Board",
        "mcu": "STM32",
        "sbc": "Raspberry Pi 5",
        "lidar": "RPLidar C1",
        "camera": "Raspberry Pi Camera Module 3",
        "imu": "MPU6050",
    },
    "software": {
        "ros_distro": "jazzy",
        "os": "Ubuntu 24.04",
        "api_version": "2.0.0",
    },
    "physical": {
        "wheel_radius_m": 0.0375,
        "wheelbase_m": 0.18,
        "track_width_m": 0.16,
        "max_speed_ms": 3.0,
        "max_steering_deg": 25.0,
    },
    "endpoints": {
        "health": "/health",
        "info": "/info",
        "kiosk": "/kiosk",
        "control": "/control",
        "ros": "/ros/*",
        "docs": "/docs",
    },
}


@router.get("/info")
async def get_info():
    """
    Get kit information.
    
    Returns hardware specs, software versions, physical parameters, and mode.
    """
    mode = _get_mode()
    info = {**KIT_INFO, "mode": mode, "serial": _get_serial()}
    if mode == "sim":
        info["hardware"] = {
            **KIT_INFO["hardware"],
            "sbc": "Simulation",
            "lidar": "Gazebo LiDAR (simulated)",
            "camera": "Gazebo Camera (simulated)",
            "imu": "Gazebo IMU (simulated)",
        }
    return info
