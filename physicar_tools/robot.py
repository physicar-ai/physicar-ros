"""Robot API tools (bundled with the extension, read-only).

A 1:1 mirror of the physicar-ros Web API at http://localhost — the same
URLs the notebooks use: every tool is named after its endpoint and keeps
the endpoint's exact contract (angles in RADIANS, speeds in m/s), so using
these tools is an indirect tour of the same Web API the notebooks teach.
Written in the same format as the user's custom tools, so this file
doubles as a reference:

  - every top-level function is a tool, named after the function; the
    extension prepends this script's namespace, so `def camera` is exposed
    to the AI as `robot_camera`
  - names starting with "_" are helpers, never exposed
  - the docstring is the tool description the AI reads
  - parameters are documented with Annotated[type, Field(description=...)]
  - return a LIST of contents — any mix of
      {"type": "text", "text": ...}
      {"type": "image", "mime": ..., "base64": ...}

Admin-ish endpoints are intentionally left out — a chat has no business
calling them. Stopping the car is robot_speed(value=0) (or the duration
auto-stop).
"""
import base64
import json

from typing import Annotated

import requests
from pydantic import Field


def states(
    include: Annotated[str, Field(description="Comma-separated top-level keys to keep, e.g. 'odom,battery,imu'. Omit for the full snapshot.")] = None,
):
    """GET /states — full robot state snapshot as JSON: command speed/steering,
    odometry, battery, IMU, lidar, joints, camera pan/tilt. The full response
    is large (it includes the whole lidar scan) — pass include to keep only
    the keys you need.
    """
    r = requests.get("http://localhost/states", timeout=10)
    r.raise_for_status()
    text = r.text or "ok"
    if include:
        # the API's ?include only applies to streaming, so filter here
        keys = [k.strip() for k in include.split(",") if k.strip()]
        text = json.dumps({k: v for k, v in r.json().items() if k in keys}, ensure_ascii=False)
    tool_call_output_contents = [
        {"type": "text", "text": text},
    ]
    return tool_call_output_contents


def odom():
    """GET /odom — odometry as JSON (pose + twist). Teleports in the simulator do
    not reset it.
    """
    r = requests.get("http://localhost/odom", timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text or "ok"},
    ]
    return tool_call_output_contents


def battery():
    """GET /battery — battery state as JSON."""
    r = requests.get("http://localhost/battery", timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text or "ok"},
    ]
    return tool_call_output_contents


def imu():
    """GET /imu — IMU reading as JSON (6-axis accelerometer + gyroscope)."""
    r = requests.get("http://localhost/imu", timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text or "ok"},
    ]
    return tool_call_output_contents


def lidar(
    step: Annotated[float, Field(description="Angular step in degrees, 0.5~30 (default 5 → 72 points; use 1 for the full 360-point scan).")] = 5.0,
):
    """GET /lidar — 360° LiDAR distance scan. Returns angle→distance(m).
    0°=front, +90°=left, -90°=right, 180°=rear. Range 0.15m~16m.
    """
    r = requests.get("http://localhost/lidar", params={"step": step}, timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text or "ok"},
    ]
    return tool_call_output_contents


def camera(
    width: Annotated[int, Field(description="Resize width in pixels, 16..1920 (native 480; out of range → HTTP 422).")] = None,
    height: Annotated[int, Field(description="Resize height in pixels, 16..1080 (native 360; out of range → HTTP 422).")] = None,
):
    """GET /camera — capture a JPEG from the front camera so you can SEE it. Aim
    first with robot_camera_pan/robot_camera_tilt if needed.
    """
    r = requests.get("http://localhost/camera", params={"width": width, "height": height}, timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "image", "mime": "image/jpeg", "base64": base64.b64encode(r.content).decode()},
    ]
    return tool_call_output_contents


def speed(
    value: Annotated[float, Field(description="Speed in m/s (-3..3, +=forward). 0 stops the robot. Omit to read the current speed.")] = None,
    duration: Annotated[float, Field(description="Seconds to keep the command alive, up to 600 (with value only; above 600 → HTTP 422). The server publishes 0 at the end and this call BLOCKS until the drive finishes.")] = None,
):
    """Speed in m/s. Without value: GET /speed reads the current speed. With
    value: POST /speed {value, duration?} — WITHOUT duration the command
    expires after ~1s (safety watchdog) unless renewed; WITH duration the
    server keeps it alive, stops at the end, and the call returns after the
    drive completes, so you can chain the next action directly. e.g. forward
    2s then look: robot_speed(value=1, duration=2) → robot_camera() ·
    emergency stop: robot_speed(value=0).
    """
    if value is None:
        if duration is not None:
            raise RuntimeError("duration requires value")
        r = requests.get("http://localhost/speed", timeout=10)
    else:
        body = {"value": float(value)}
        if duration is not None and float(value) != 0:
            body["duration"] = float(duration)
        # with duration the server holds the response until the drive ends (blocking)
        r = requests.post("http://localhost/speed", json=body, timeout=body.get("duration", 0) + 15)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text or "ok"},
    ]
    return tool_call_output_contents


def steering(
    value: Annotated[float, Field(description="Steering angle in RADIANS (-0.349..0.349 ≈ ±20°, +=left). Omit to read the current angle.")] = None,
):
    """Steering angle in RADIANS (the Web API contract — NOT degrees). Without
    value: GET /steering. With value: POST /steering {value}; the angle
    persists until changed. e.g. 10° left: robot_steering(value=0.175) ·
    center: robot_steering(value=0).
    """
    if value is None:
        r = requests.get("http://localhost/steering", timeout=10)
    else:
        r = requests.post("http://localhost/steering", json={"value": float(value)}, timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text or "ok"},
    ]
    return tool_call_output_contents


def camera_pan(
    value: Annotated[float, Field(description="Camera pan in RADIANS (-0.524..0.524 ≈ ±30°, +=left). Omit to read the current angle.")] = None,
):
    """Camera pan angle in RADIANS (the Web API contract — NOT degrees). Without
    value: GET /camera/pan. With value: POST /camera/pan {value}. Use
    robot_camera() to see the image.
    """
    if value is None:
        r = requests.get("http://localhost/camera/pan", timeout=10)
    else:
        r = requests.post("http://localhost/camera/pan", json={"value": float(value)}, timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text or "ok"},
    ]
    return tool_call_output_contents


def camera_tilt(
    value: Annotated[float, Field(description="Camera tilt in RADIANS (-0.524..0.524 ≈ ±30°, +=up). Omit to read the current angle.")] = None,
):
    """Camera tilt angle in RADIANS (the Web API contract — NOT degrees). Without
    value: GET /camera/tilt. With value: POST /camera/tilt {value}. Use
    robot_camera() to see the image.
    """
    if value is None:
        r = requests.get("http://localhost/camera/tilt", timeout=10)
    else:
        r = requests.post("http://localhost/camera/tilt", json={"value": float(value)}, timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text or "ok"},
    ]
    return tool_call_output_contents


def audio_play(
    url: Annotated[str, Field(description="Audio file URL.")] = None,
    path: Annotated[str, Field(description="Audio file path on this machine.")] = None,
    data: Annotated[str, Field(description="Base64-encoded audio file.")] = None,
    format: Annotated[str, Field(description="Container hint for data, e.g. 'mp3', 'wav', 'ogg' (default mp3; only used with data).")] = None,
    volume: Annotated[float, Field(description="Volume 0..1.")] = None,
    loop: Annotated[bool, Field(description="Loop playback.")] = False,
    replace: Annotated[bool, Field(description="Replace whatever is currently playing.")] = False,
):
    """POST /audio/play — play one of url / path / data on the robot speaker
    (browser viewer in SIM). Find music preview URLs with utils_music_search.
    Returns the playing item id.
    """
    body = {"url": url, "path": path, "data": data, "format": format,
            "volume": volume, "loop": loop or None, "replace": replace or None}
    body = {k: v for k, v in body.items() if v is not None}   # the API rejects explicit nulls
    r = requests.post("http://localhost/audio/play", json=body, timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text or "ok"},
    ]
    return tool_call_output_contents


def audio_stop(
    id: Annotated[str, Field(description="Playing item id (from robot_audio_play/robot_audio).")] = None,
    all: Annotated[bool, Field(description="Stop everything.")] = False,
):
    """POST /audio/stop — stop playback by id, or everything with all=true.
    """
    body = {"all": True} if all else ({"id": id} if id is not None else {})
    r = requests.post("http://localhost/audio/stop", json=body, timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text or "ok"},
    ]
    return tool_call_output_contents


def audio_volume(
    id: Annotated[str, Field(description="Playing item id.")],
    volume: Annotated[float, Field(description="Volume 0..1.")],
):
    """POST /audio/volume — change the volume of a playing item."""
    r = requests.post("http://localhost/audio/volume", json={"id": id, "volume": float(volume)}, timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text or "ok"},
    ]
    return tool_call_output_contents


def audio():
    """GET /audio — list the currently playing items."""
    r = requests.get("http://localhost/audio", timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text or "ok"},
    ]
    return tool_call_output_contents
