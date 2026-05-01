#!/usr/bin/env python3
"""
Generic Teleop Router.

Source-agnostic teleop status reporting and web teleop commanding.

Read endpoints:
    GET  /teleop/status                   - merged TeleopStatus snapshot
                                             {source, drive_engaged,
                                              camera_engaged, estop_latched,
                                              timeout_sec, fresh, received}
    GET  /teleop/web                      - what *this* webserver is currently
                                             publishing as the 'web' source

Write endpoints (publish on the same topics joy_teleop uses, so they go
through the driver / cmd_vel_adapter priority gate):

    POST /teleop/speed         {value: float}    -> /teleop/speed (m/s)
    POST /teleop/steering      {value: float}    -> /teleop/steering (rad)
    POST /teleop/camera/pan    {value: float}    -> /teleop/camera/pan (rad)
    POST /teleop/camera/tilt   {value: float}    -> /teleop/camera/tilt (rad)

    POST /teleop/engage        {drive?: bool, camera?: bool, estop?: bool}
        Manually claim individual locks without sending a value.

    POST /teleop/release       {}
        Release all web locks immediately (one final TeleopStatus frame
        with drive_engaged=false / camera_engaged=false is sent so the
        driver / cmd_vel_adapter exits the gate without waiting for the
        deadman timeout).

Behaviour:
    Each speed/steering call auto-engages drive_engaged.  Each pan/tilt
    call auto-engages camera_engaged.  If no command lands within ~0.5 s
    the engagement is auto-released (deadman semantic, identical to
    holding the joystick LB / RB button).  Send commands at >=10 Hz to
    keep continuous control.

``fresh`` on /teleop/status is false once the cached status is older
than the publisher-declared ``timeout`` window — consumers (driver,
cmd_vel_adapter) treat that as "all locks released" so a teleop
publisher that crashes while holding a lock can't permanently gate the
robot.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..ros_bridge import get_ros_bridge


router = APIRouter(prefix="/teleop", tags=["teleop"])


# =============================================================================
# Request models
# =============================================================================

class TeleopValueRequest(BaseModel):
    value: float = Field(..., description="Command value in SI units (m/s for speed, rad for the rest)")


class TeleopEngageRequest(BaseModel):
    drive: Optional[bool] = Field(default=None, description="Claim or release drive_engaged")
    camera: Optional[bool] = Field(default=None, description="Claim or release camera_engaged")
    estop: Optional[bool] = Field(default=None, description="Latch or clear emergency stop")


# =============================================================================
# Read
# =============================================================================

@router.get("/status")
async def get_teleop_status():
    """Return the latest source-agnostic teleop status."""
    bridge = get_ros_bridge()
    if not bridge.is_ready:
        raise HTTPException(status_code=503, detail="ROS bridge not ready")
    return bridge.get_teleop_status()


@router.get("/web")
async def get_web_teleop():
    """Return *this* webserver's local web teleop state (what we publish)."""
    bridge = get_ros_bridge()
    if not bridge.is_ready:
        raise HTTPException(status_code=503, detail="ROS bridge not ready")
    return bridge.get_web_teleop_state()


# =============================================================================
# Write
# =============================================================================

def _ensure_ready():
    bridge = get_ros_bridge()
    if not bridge.is_ready:
        raise HTTPException(status_code=503, detail="ROS bridge not ready")
    return bridge


@router.post("/speed")
async def teleop_speed(req: TeleopValueRequest):
    """Publish to /teleop/speed (m/s).  Auto-engages drive."""
    bridge = _ensure_ready()
    if not bridge.publish_teleop_speed(req.value):
        raise HTTPException(status_code=500, detail="publish failed")
    return {"success": True, "value": req.value, "engaged": bridge.get_web_teleop_state()}


@router.post("/steering")
async def teleop_steering(req: TeleopValueRequest):
    """Publish to /teleop/steering (rad).  Auto-engages drive."""
    bridge = _ensure_ready()
    if not bridge.publish_teleop_steering(req.value):
        raise HTTPException(status_code=500, detail="publish failed")
    return {"success": True, "value": req.value, "engaged": bridge.get_web_teleop_state()}


@router.post("/camera/pan")
async def teleop_pan(req: TeleopValueRequest):
    """Publish to /teleop/camera/pan (rad).  Auto-engages camera."""
    bridge = _ensure_ready()
    if not bridge.publish_teleop_pan(req.value):
        raise HTTPException(status_code=500, detail="publish failed")
    return {"success": True, "value": req.value, "engaged": bridge.get_web_teleop_state()}


@router.post("/camera/tilt")
async def teleop_tilt(req: TeleopValueRequest):
    """Publish to /teleop/camera/tilt (rad).  Auto-engages camera."""
    bridge = _ensure_ready()
    if not bridge.publish_teleop_tilt(req.value):
        raise HTTPException(status_code=500, detail="publish failed")
    return {"success": True, "value": req.value, "engaged": bridge.get_web_teleop_state()}


@router.post("/engage")
async def teleop_engage(req: TeleopEngageRequest):
    """Manually claim or release individual web teleop locks."""
    bridge = _ensure_ready()
    state = bridge.engage_web_teleop(drive=req.drive, camera=req.camera, estop=req.estop)
    return {"success": True, "engaged": state}


@router.post("/release")
async def teleop_release():
    """Release all web teleop locks immediately."""
    bridge = _ensure_ready()
    state = bridge.release_web_teleop()
    return {"success": True, "engaged": state}

