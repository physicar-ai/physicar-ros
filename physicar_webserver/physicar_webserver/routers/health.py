#!/usr/bin/env python3
"""
Health check router for PhysiCar API.
"""

import subprocess
from datetime import datetime
from fastapi import APIRouter

router = APIRouter()


@router.get("/health")
async def health_check():
    """
    Health check endpoint.
    
    Returns basic service status information.
    """
    return {
        "status": "ok",
        "service": "physicar-api",
        "timestamp": datetime.now().isoformat(),
    }


@router.post("/api/restart")
async def restart_ros():
    """Restart the physicar systemd service (rebuilds + relaunches ROS)."""
    try:
        subprocess.Popen(
            ["sudo", "systemctl", "restart", "physicar.service"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
