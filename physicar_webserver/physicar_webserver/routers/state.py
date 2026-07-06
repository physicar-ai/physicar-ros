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
POST /speed, /steering, /camera/pan, /audio, ... → write control command

All GET endpoints support:
- One-shot reads (default)
- SSE streaming via ?stream=true or Accept: text/event-stream header
"""

import asyncio
import base64
from typing import Optional

from fastapi import APIRouter, Query, Request, HTTPException
from fastapi.responses import StreamingResponse, Response, JSONResponse
from pydantic import BaseModel, Field

from physicar_webserver.ros_bridge import get_ros_bridge
from physicar_webserver.state_manager import get_state_manager

router = APIRouter(tags=["state"])


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


class AudioRequest(BaseModel):
    """
    Audio control request - matches ROS2 Audio.msg structure.
    
    For encoded audio (mp3, wav, etc.): set format and data
    For PCM streaming: set format="pcm" with sample_rate, audio_channels, bits_per_sample
    For stop: set stop=true with channel name
    For stop all: set stop_all=true
    """
    channel: str = Field(default="default", description="Channel name (same=replace, different=mix)")
    data: Optional[str] = Field(default=None, description="Base64 encoded audio data")
    format: str = Field(default="", description="Audio format")
    sample_rate: int = Field(default=16000, description="Sample rate (16000, 44100, 48000)")
    audio_channels: int = Field(default=1, ge=1, le=2, description="1=mono, 2=stereo")
    bits_per_sample: int = Field(default=16, description="Bits per sample (8, 16, 24, 32)")
    volume: float = Field(default=0.5, ge=0.0, le=1.0, description="Volume 0.0 ~ 1.0")
    stop: bool = Field(default=False, description="Stop playback on this channel")
    stop_all: bool = Field(default=False, description="Stop all channels")


class ControlResponse(BaseModel):
    """Standard control response."""
    success: bool
    value: Optional[float] = None
    message: Optional[str] = None


class AudioResponse(BaseModel):
    """Audio control response."""
    success: bool
    message: str


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

@router.get("/state")
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
    """Get current measured speed from odometry (m/s)."""
    sm = get_state_manager()

    if _wants_stream(request, stream):
        return StreamingResponse(
            sm.stream_sse("odom"),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    data = sm.get_once("odom")
    if data is None:
        return {"value": 0.0}
    return {"value": data.get("velocity", {}).get("linear", 0.0)}


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

    return {"value": sm.get_cmd_state()["steering"]}


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


# =============================================================================
# POST — Audio Control
# =============================================================================

@router.post("/audio", response_model=AudioResponse)
async def control_audio(request: AudioRequest):
    """
    Audio control - play, stream, or stop audio.
    
    Publishes to /audio topic (Audio.msg).
    
    **Play encoded audio (mp3, wav, ogg, etc.):**
    ```json
    {"data": "<base64>", "format": "mp3", "volume": 0.8}
    ```
    
    **Stream PCM audio:**
    ```json
    {"data": "<base64>", "format": "pcm", "sample_rate": 44100, "audio_channels": 2}
    ```
    
    **Stop specific channel:**
    ```json
    {"channel": "bgm", "stop": true}
    ```
    
    **Stop all channels:**
    ```json
    {"stop_all": true}
    ```
    
    **Channel behavior:**
    - Same channel name = replaces existing playback
    - Different channel name = plays simultaneously (mixing)
    """
    bridge = get_ros_bridge()
    if not bridge.is_ready:
        raise HTTPException(503, "ROS bridge not ready")
    
    try:
        audio_data = b''
        if request.data:
            audio_data = base64.b64decode(request.data)
        
        result = bridge.publish_audio(
            data=audio_data,
            channel=request.channel,
            format=request.format,
            sample_rate=request.sample_rate,
            audio_channels=request.audio_channels,
            bits_per_sample=request.bits_per_sample,
            volume=request.volume,
            stop=request.stop,
            stop_all=request.stop_all,
        )
        return AudioResponse(**result)
    except Exception as e:
        raise HTTPException(500, str(e))
