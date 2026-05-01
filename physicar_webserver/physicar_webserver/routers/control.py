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
Control Router - Low-level hardware control endpoints.

Maps directly to ROS2 topics:
  POST /control/speed        → /speed (Float64, m/s)
  POST /control/steering     → /steering (Float64, radians)
  POST /control/camera/pan   → /camera/pan (Float64, radians)
  POST /control/camera/tilt  → /camera/tilt (Float64, radians)
  POST /control/audio        → /audio (Audio msg)

All units match ROS2 topic units (no conversion).
"""

import base64
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from physicar_webserver.ros_bridge import get_ros_bridge
from physicar_webserver.state_manager import get_state_manager

router = APIRouter(prefix="/control", tags=["control"])


# =============================================================================
# Request Models
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
    # Channel control
    channel: str = Field(default="default", description="Channel name (same=replace, different=mix)")
    
    # Audio data (base64 encoded)
    data: Optional[str] = Field(default=None, description="Base64 encoded audio data")
    
    # Format: "" or "pcm" = raw PCM, "mp3", "wav", "ogg", etc.
    format: str = Field(default="", description="Audio format")
    
    # PCM parameters (used when format="" or "pcm")
    sample_rate: int = Field(default=16000, description="Sample rate (16000, 44100, 48000)")
    audio_channels: int = Field(default=1, ge=1, le=2, description="1=mono, 2=stereo")
    bits_per_sample: int = Field(default=16, description="Bits per sample (8, 16, 24, 32)")
    
    # Volume
    volume: float = Field(default=0.5, ge=0.0, le=1.0, description="Volume 0.0 ~ 1.0")
    
    # Control flags
    stop: bool = Field(default=False, description="Stop playback on this channel")
    stop_all: bool = Field(default=False, description="Stop all channels")


# =============================================================================
# Response Models
# =============================================================================

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
# Speed Control
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
# Steering Control
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
# Camera Pan Control
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
# Camera Tilt Control
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
# Audio Control
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
        # Decode base64 data if provided
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
