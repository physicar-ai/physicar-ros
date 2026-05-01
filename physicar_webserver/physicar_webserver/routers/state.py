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
State Router - Read-only endpoints for robot state.

All endpoints support:
- One-shot reads (default)
- SSE streaming via ?stream=true or Accept: text/event-stream header
"""

import asyncio
from typing import Optional

from fastapi import APIRouter, Query, Request, HTTPException
from fastapi.responses import StreamingResponse, Response, JSONResponse

from physicar_webserver.state_manager import get_state_manager

router = APIRouter(prefix="/state", tags=["state"])


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

@router.get("")
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
    - Note: lidar excluded by default (heavy), use /state/lidar?stream=true instead
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
    
    return {"value": sm.get_cmd_state()["pan"]}


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
    
    return {"value": sm.get_cmd_state()["tilt"]}


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
# Audio Stream (SIM mode — streams /audio topic to browser for playback)
# =============================================================================

@router.get("/audio")
async def get_audio_stream(request: Request):
    """
    Audio stream (SSE only).

    Streams /audio ROS2 topic to browser for playback.
    Used by gzweb in SIM mode to play audio in the browser
    since Gazebo has no audio hardware.

    Each SSE event is a JSON object matching the Audio.msg structure:
    - channel, format, sample_rate, channels, bits, volume
    - data: base64-encoded audio bytes
    - stop / stop_all: control flags
    """
    sm = get_state_manager()
    return StreamingResponse(
        sm.stream_audio_sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
