#!/usr/bin/env python3
#
# Copyright 2026 AICASTLE Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
State & Control Router - Unified read/write endpoints for robot hardware.

GET  /speed, /steering, /camera, /lidar, ...  → read sensor/state
POST /speed, /steering, /camera/pan, ... → write control command

All GET endpoints support:
- One-shot reads (default)
- SSE streaming via ?stream=true or Accept: text/event-stream header
"""

import asyncio
import base64
import json
from typing import Optional

from fastapi import APIRouter, Query, Request, HTTPException, WebSocket
from fastapi.responses import StreamingResponse, Response, JSONResponse
from pydantic import BaseModel, Field

from physicar_webserver.ros_bridge import get_ros_bridge
from physicar_webserver.state_manager import get_state_manager

router = APIRouter(tags=["hw"])


# =============================================================================
# Request / Response Models (from control)
# =============================================================================

class SpeedRequest(BaseModel):
    """Speed control request."""
    value: float = Field(..., description="Speed in m/s. Positive=forward, Negative=backward")


class SteeringRequest(BaseModel):
    """Steering control request."""
    value: float = Field(..., description="Steering angle in radians. Positive=left, Negative=right")


class PanRequest(BaseModel):
    """Camera pan control request."""
    value: float = Field(..., description="Pan angle in radians. 0=center, Positive=left, Negative=right")


class TiltRequest(BaseModel):
    """Camera tilt control request."""
    value: float = Field(..., description="Tilt angle in radians. 0=level, Positive=up, Negative=down")


class ControlResponse(BaseModel):
    """Standard control response."""
    success: bool
    value: Optional[float] = None
    message: Optional[str] = None


# =============================================================================
# Helper: Check if SSE stream requested
# =============================================================================

def _wants_stream(request: Request, stream: Optional[bool]) -> bool:
    """Check if client wants SSE stream."""
    if stream:
        return True
    accept = request.headers.get("accept", "")
    return "text/event-stream" in accept


# =============================================================================
# Summary Endpoint
# =============================================================================

@router.get("/states")
async def get_state_summary(
    request: Request,
    stream: Optional[bool] = Query(None, description="Enable SSE streaming"),
    include: Optional[str] = Query(None, description="Comma-separated keys to include in stream (e.g., 'odom,battery,imu'). Default: cmd,odom,battery"),
):
    """
    Get all robot states combined.
    
    One-shot: Returns all available states (cmd, odom, battery, imu, lidar, joints, camera info).
    
    Streaming (?stream=true): Returns selected states continuously.
    - Default: cmd, odom, battery (lightweight)
    - Use ?include=odom,battery,imu to customize
    - Available: cmd, odom, battery, imu, joints, camera_pan, camera_tilt
    - Note: lidar excluded by default (heavy), use /lidar?stream=true instead
    """
    sm = get_state_manager()
    
    if _wants_stream(request, stream):
        include_list = None
        if include:
            include_list = [k.strip() for k in include.split(",") if k.strip()]
        
        return StreamingResponse(
            sm.stream_all_sse(include_list),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    
    return sm.get_all_states()


# =============================================================================
# Speed / Steering
# =============================================================================

@router.get("/speed")
async def get_speed(
    request: Request,
    stream: Optional[bool] = Query(None, description="Enable SSE streaming"),
):
    """Get current speed (m/s)."""
    sm = get_state_manager()

    if _wants_stream(request, stream):
        return StreamingResponse(
            sm.stream_cmd_state_sse("speed"),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return sm.get_cmd_state()["speed"]


@router.get("/steering")
async def get_steering(
    request: Request,
    stream: Optional[bool] = Query(None, description="Enable SSE streaming"),
):
    """Get current steering angle (radians)."""
    sm = get_state_manager()

    if _wants_stream(request, stream):
        return StreamingResponse(
            sm.stream_cmd_state_sse("steering"),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return sm.get_cmd_state()["steering"]


# =============================================================================
# Odometry
# =============================================================================

@router.get("/odom")
async def get_odom(
    request: Request,
    stream: Optional[bool] = Query(None, description="Enable SSE streaming"),
):
    """
    Get odometry data (position, orientation, velocity).
    
    Use ?stream=true for continuous updates.
    """
    sm = get_state_manager()
    
    if _wants_stream(request, stream):
        return StreamingResponse(
            sm.stream_sse("odom"),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    
    data = sm.get_once("odom")
    if data is None:
        return JSONResponse({"error": "No odometry data", "status": "waiting"}, status_code=503)
    return data


# =============================================================================
# Battery
# =============================================================================

@router.get("/battery")
async def get_battery(
    request: Request,
    stream: Optional[bool] = Query(None, description="Enable SSE streaming"),
):
    """
    Get battery state (voltage, percentage, charging).
    
    Use ?stream=true for continuous updates.
    """
    sm = get_state_manager()
    
    if _wants_stream(request, stream):
        return StreamingResponse(
            sm.stream_sse("battery"),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    
    data = sm.get_once("battery")
    if data is None:
        return JSONResponse({"error": "No battery data", "status": "waiting"}, status_code=503)
    return data


# =============================================================================
# IMU
# =============================================================================

@router.get("/imu")
async def get_imu(
    request: Request,
    stream: Optional[bool] = Query(None, description="Enable SSE streaming"),
):
    """
    Get IMU data (acceleration, gyro, orientation).
    
    Use ?stream=true for continuous updates.
    """
    sm = get_state_manager()
    
    if _wants_stream(request, stream):
        return StreamingResponse(
            sm.stream_sse("imu"),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    
    data = sm.get_once("imu")
    if data is None:
        return JSONResponse({"error": "No IMU data", "status": "waiting"}, status_code=503)
    return data


# =============================================================================
# Camera
# =============================================================================

@router.get("/camera")
async def get_camera(
    request: Request,
    stream: Optional[bool] = Query(None, description="Enable MJPEG streaming"),
    width: Optional[int] = Query(None, ge=16, le=1920, description="Image width"),
    height: Optional[int] = Query(None, ge=16, le=1080, description="Image height"),
):
    """
    Get camera image.
    
    - Default: Single JPEG image
    - ?stream=true: MJPEG stream (continuous frames)
    
    Optionally resize with width/height parameters.
    """
    sm = get_state_manager()
    
    if _wants_stream(request, stream):
        return StreamingResponse(
            sm.stream_camera_mjpeg(width, height),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    
    loop = asyncio.get_event_loop()
    frame = await loop.run_in_executor(None, sm.get_camera_image, width, height)
    
    if frame is None:
        return Response(content="Camera not available", status_code=503, media_type="text/plain")
    
    return Response(
        content=frame,
        media_type="image/jpeg",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@router.get("/camera/pan")
async def get_camera_pan(
    request: Request,
    stream: Optional[bool] = Query(None, description="Enable SSE streaming"),
):
    """
    Get current camera pan angle (radians).
    
    Use ?stream=true for continuous updates.
    """
    sm = get_state_manager()
    
    if _wants_stream(request, stream):
        return StreamingResponse(
            sm.stream_cmd_state_sse("pan"),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    
    return sm.get_cmd_state()["pan"]


@router.get("/camera/tilt")
async def get_camera_tilt(
    request: Request,
    stream: Optional[bool] = Query(None, description="Enable SSE streaming"),
):
    """
    Get current camera tilt angle (radians).
    
    Use ?stream=true for continuous updates.
    """
    sm = get_state_manager()
    
    if _wants_stream(request, stream):
        return StreamingResponse(
            sm.stream_cmd_state_sse("tilt"),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    
    return sm.get_cmd_state()["tilt"]


# =============================================================================
# Lidar
# =============================================================================

@router.get("/lidar")
async def get_lidar(
    request: Request,
    stream: Optional[bool] = Query(None, description="Enable SSE streaming"),
    step: float = Query(1.0, ge=0.5, le=30, description="Angle step (degrees)"),
):
    """
    Get lidar scan data.
    
    - step=1: 1° intervals (~360 points)
    - step=10: 10° intervals (~36 points)
    
    Use ?stream=true for continuous updates.
    
    Orientation: 0°=front, +90°=left, -90°=right, ±180°=rear
    """
    sm = get_state_manager()
    
    if _wants_stream(request, stream):
        return StreamingResponse(
            sm.stream_sse("lidar", step=step),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    
    data = sm.get_once("lidar", step=step)
    if data is None:
        return JSONResponse({"error": "No scan data", "status": "waiting"}, status_code=503)
    return data


# =============================================================================
# POST — Speed Control
# =============================================================================

@router.post("/speed", response_model=ControlResponse)
async def set_speed(request: SpeedRequest):
    """
    Set robot speed (m/s).
    
    Publishes directly to /speed topic.
    No Ackermann conversion - raw speed value.
    
    - Positive = forward
    - Negative = backward
    - 0 = stop
    """
    bridge = get_ros_bridge()
    if not bridge.is_ready:
        raise HTTPException(503, "ROS bridge not ready")
    
    success = bridge.publish_speed(request.value)
    
    sm = get_state_manager()
    sm.update_cmd_state(speed=request.value)
    
    return ControlResponse(success=success, value=request.value)


# =============================================================================
# WS — Streaming Control (per-topic write streams)
# =============================================================================
# Streaming counterpart of the control endpoints — the write-side twin of the
# sensors' ?stream=true read streams. Each frame carries the same
# {"value": <float>} body as the matching POST.
# Used by the App page control UI: a whole driving session costs ONE request
# through tunnels/proxies instead of 10/s.
#
# Dead-man switch: when a stream closes, its value is published as 0 — a dead
# browser can never leave the robot driving. Only the drive axes
# (speed/steering) get streams; camera pan/tilt is occasional input and the
# plain POST is enough.

def _make_control_stream(path: str, field: str, publish_name: str, zero_on_close: bool):
    async def _stream(ws: WebSocket):
        await ws.accept()
        bridge = get_ros_bridge()
        sm = get_state_manager()
        publish = getattr(bridge, publish_name)
        sent = False
        try:
            while True:
                msg = await ws.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                text = msg.get("text")
                if not text or not bridge.is_ready:
                    continue
                try:
                    value = float(json.loads(text)["value"])
                except (ValueError, KeyError, TypeError):
                    continue
                publish(value)
                sm.update_cmd_state(**{field: value})
                sent = True
        except Exception:
            pass
        finally:
            if zero_on_close and sent and bridge.is_ready:
                publish(0.0)
                sm.update_cmd_state(**{field: 0.0})
    router.add_api_websocket_route(path, _stream)


_make_control_stream("/speed/stream", "speed", "publish_speed", zero_on_close=True)
_make_control_stream("/steering/stream", "steering", "publish_steering", zero_on_close=True)


# =============================================================================
# POST — Steering Control
# =============================================================================

@router.post("/steering", response_model=ControlResponse)
async def set_steering(request: SteeringRequest):
    """
    Set steering angle (radians).
    
    Publishes directly to /steering topic.
    
    - Positive = left
    - Negative = right
    - 0 = center
    """
    bridge = get_ros_bridge()
    if not bridge.is_ready:
        raise HTTPException(503, "ROS bridge not ready")
    
    success = bridge.publish_steering(request.value)
    
    sm = get_state_manager()
    sm.update_cmd_state(steering=request.value)
    
    return ControlResponse(success=success, value=request.value)


# =============================================================================
# POST — Camera Pan Control
# =============================================================================

@router.post("/camera/pan", response_model=ControlResponse)
async def set_pan(request: PanRequest):
    """
    Set camera pan angle (radians).
    
    Publishes directly to /camera/pan topic.
    
    - Range: typically -π/2 to +π/2 radians
    - 0 = center
    - Positive = left
    - Negative = right
    """
    bridge = get_ros_bridge()
    if not bridge.is_ready:
        raise HTTPException(503, "ROS bridge not ready")
    
    success = bridge.publish_pan(request.value)
    
    sm = get_state_manager()
    sm.update_cmd_state(pan=request.value)
    
    return ControlResponse(success=success, value=request.value)


# =============================================================================
# POST — Camera Tilt Control
# =============================================================================

@router.post("/camera/tilt", response_model=ControlResponse)
async def set_tilt(request: TiltRequest):
    """
    Set camera tilt angle (radians).
    
    Publishes directly to /camera/tilt topic.
    
    - Range: typically -π/6 to +π/6 radians
    - 0 = level
    - Positive = up
    - Negative = down
    """
    bridge = get_ros_bridge()
    if not bridge.is_ready:
        raise HTTPException(503, "ROS bridge not ready")
    
    success = bridge.publish_tilt(request.value)
    
    sm = get_state_manager()
    sm.update_cmd_state(tilt=request.value)
    
    return ControlResponse(success=success, value=request.value)

