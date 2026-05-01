#!/usr/bin/env python3
#
# Copyright 2026 AICASTLE Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Calibration Router - Robot calibration endpoints.

GET  /calibration - Get all calibration values
POST /calibration/{channel} - Set calibration value
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from typing import Optional

from physicar_webserver.ros_bridge import get_ros_bridge
from physicar_webserver.sim import reject_in_sim

router = APIRouter(prefix="/calibration", tags=["calibration"])


# =============================================================================
# Request Models
# =============================================================================

class CalibrationValueRequest(BaseModel):
    """Calibration value request for center offset."""
    value: float = Field(..., description="Center offset value (degrees)")
    save: bool = Field(default=True, description="Save to calibration file")


class CalibrationBoolRequest(BaseModel):
    """Calibration boolean request."""
    enabled: bool = Field(..., description="Enable/disable")
    save: bool = Field(default=True, description="Save to calibration file")


# =============================================================================
# Get Calibration
# =============================================================================

@router.get("")
async def get_calibration():
    """
    Get all current calibration values.
    
    Returns:
    - steering_center: Steering servo center offset
    - pan_center: Camera pan servo center offset
    - tilt_center: Camera tilt servo center offset
    - reverse_direction: ESC reverse direction enabled
    - emergency_enabled: Emergency stop enabled
    - max_steering: Maximum steering angle
    - max_speed: Maximum speed
    - max_pan: Maximum pan angle
    - max_tilt: Maximum tilt angle
    - source: Where calibration was loaded from
    """
    bridge = get_ros_bridge()
    if not bridge.is_ready:
        raise HTTPException(503, "ROS bridge not ready")
    
    try:
        return await bridge.get_calibration()
    except Exception as e:
        raise HTTPException(500, str(e))


# =============================================================================
# Set Calibration - Center offsets
# =============================================================================

@router.post("/steering")
async def set_steering_center(request: CalibrationValueRequest):
    """
    Set steering servo center offset.
    
    Adjusts the neutral position of the steering servo.
    Positive values turn wheels left, negative turn right.
    """
    reject_in_sim("calibration")
    bridge = get_ros_bridge()
    if not bridge.is_ready:
        raise HTTPException(503, "ROS bridge not ready")
    
    try:
        return await bridge.set_calibration(
            channel="steering_center",
            center_value=request.value,
            save=request.save
        )
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/pan")
async def set_pan_center(request: CalibrationValueRequest):
    """
    Set camera pan servo center offset.
    
    Adjusts the neutral position of the camera pan servo.
    """
    reject_in_sim("calibration")
    bridge = get_ros_bridge()
    if not bridge.is_ready:
        raise HTTPException(503, "ROS bridge not ready")
    
    try:
        return await bridge.set_calibration(
            channel="pan_center",
            center_value=request.value,
            save=request.save
        )
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/tilt")
async def set_tilt_center(request: CalibrationValueRequest):
    """
    Set camera tilt servo center offset.
    
    Adjusts the neutral position of the camera tilt servo.
    """
    reject_in_sim("calibration")
    bridge = get_ros_bridge()
    if not bridge.is_ready:
        raise HTTPException(503, "ROS bridge not ready")
    
    try:
        return await bridge.set_calibration(
            channel="tilt_center",
            center_value=request.value,
            save=request.save
        )
    except Exception as e:
        raise HTTPException(500, str(e))


# =============================================================================
# Set Calibration - Boolean settings
# =============================================================================

@router.post("/reverse")
async def set_reverse_direction(request: CalibrationBoolRequest):
    """
    Set ESC reverse direction.
    
    Some ESCs have reverse direction. Enable this if the robot
    moves backward when commanded to go forward.
    """
    reject_in_sim("calibration")
    bridge = get_ros_bridge()
    if not bridge.is_ready:
        raise HTTPException(503, "ROS bridge not ready")
    
    try:
        return await bridge.set_calibration(
            channel="reverse",
            bool_value=request.enabled,
            save=request.save
        )
    except Exception as e:
        raise HTTPException(500, str(e))


@router.post("/emergency")
async def set_emergency_enabled(request: CalibrationBoolRequest):
    """
    Enable/disable emergency stop feature.
    
    When enabled, the robot will stop if obstacles are detected
    within the safety threshold distance.
    """
    reject_in_sim("calibration")
    bridge = get_ros_bridge()
    if not bridge.is_ready:
        raise HTTPException(503, "ROS bridge not ready")
    
    try:
        return await bridge.set_calibration(
            channel="emergency",
            bool_value=request.enabled,
            save=request.save
        )
    except Exception as e:
        raise HTTPException(500, str(e))
