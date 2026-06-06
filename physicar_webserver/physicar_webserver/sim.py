"""SIM-mode helpers shared across routers.

In simulation, hardware-tied operations (host network, password change/reboot,
servo calibration) make no sense, so we either reject them at the API level or
hide them in the UI.

`is_sim_mode()` reads the `sim_mode` ROS param set by `sim.launch.py`.
"""

import os

from fastapi import HTTPException


def is_sim_mode() -> bool:
    """True when running under sim.launch.py (Gazebo simulation)."""
    # Environment variable works at module-load time (before ROS node init)
    if os.environ.get('PHYSICAR_SIM') == '1':
        return True
    try:
        from physicar_webserver.ros_bridge import get_ros_bridge
        bridge = get_ros_bridge()
        node = bridge._node
        if node:
            if not node.has_parameter('sim_mode'):
                node.declare_parameter('sim_mode', False)
            return bool(node.get_parameter('sim_mode').value)
    except Exception:
        pass
    return False


def reject_in_sim(operation: str = "this operation") -> None:
    """Raise 503 if running in SIM mode. Use as the first line of mutating
    endpoints that touch host hardware (network, calibration, host password)."""
    if is_sim_mode():
        raise HTTPException(
            status_code=503,
            detail=f"Not supported in simulation mode: {operation}",
        )
