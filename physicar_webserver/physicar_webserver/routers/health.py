#!/usr/bin/env python3
"""
Health check router for PhysiCar API.
"""

import subprocess
from datetime import datetime
from fastapi import APIRouter

from physicar_webserver.sim import is_sim_mode

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
    """Restart the physicar service (rebuilds + relaunches ROS)."""
    try:
        if is_sim_mode():
            cmd = ["supervisorctl", "-s", "unix:///tmp/supervisor.sock",
                   "restart", "physicar"]
        else:
            cmd = ["sudo", "systemctl", "restart", "physicar.service"]
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}
