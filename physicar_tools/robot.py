"""Robot API tools (bundled with the extension, read-only).

A 1:1 mirror of the physicar-ros Web API (physicar_webserver on
127.0.0.1:8000 — trusts local requests): every tool is named after its
endpoint and keeps the endpoint's exact contract (angles in RADIANS, speeds
in m/s), so using these tools is an indirect tour of the same Web API the
notebooks teach. Written in the same format as the user's custom tools, so
this file doubles as a reference:

  - every top-level function is a tool, named after the function; the
    extension prepends this script's namespace, so `def camera` is exposed
    to the AI as `robot_camera`
  - names starting with "_" are helpers, never exposed
  - the docstring is the tool description the AI reads
  - parameters are documented with Annotated[type, Field(description=...)]
  - return a str/dict/list, or a list of objects with .text or .mime/.base64

Admin-ish endpoints are intentionally left out — a chat has no business
calling them. Stopping the car is robot_speed(value=0) (or the duration
auto-stop).
"""
import base64
import json

from typing import Annotated

import requests
from pydantic import Field

_API = "http://127.0.0.1:8000"
_MAX = 40000   # same output cap as the extension host's truncate()


def _truncate(s):
    s = str(s)
    if len(s) > _MAX:
        return s[:_MAX] + "\n...[truncated {} chars]".format(len(s) - _MAX)
    return s


def _request(method, path, timeout=10, **kw):
    try:
        r = requests.request(method, _API + path, timeout=timeout, **kw)
    except requests.exceptions.Timeout:
        raise RuntimeError("timed out: " + _API)
    except requests.exceptions.ConnectionError:
        raise RuntimeError("unreachable (is the stack running?): " + _API)
    if not r.ok:
        raise RuntimeError("HTTP {}: {}".format(r.status_code, r.text[:500]))
    return r


def _get(path, timeout=10, **params):
    params = {k: v for k, v in params.items() if v is not None}
    return _request("GET", path, timeout=timeout, params=params or None)


def _post(path, timeout=10, **body):
    body = {k: v for k, v in body.items() if v is not None}
    return _request("POST", path, timeout=timeout, json=body)


class _Image:
    """Duck-typed image content: the bridge serializes .mime/.base64."""

    def __init__(self, mime, b64):
        self.mime = mime
        self.base64 = b64


# ---- Sensor queries (GET) -----------------------------------------------------------------

def states(
    include: Annotated[str, Field(description="Comma-separated top-level keys to keep, e.g. 'odom,battery,imu'. Omit for the full snapshot.")] = None,
):
    """GET /states — full robot state snapshot as JSON: command speed/steering, odometry, battery, IMU, lidar, joints, camera pan/tilt. The full response is large (it includes the whole lidar scan) — pass include to keep only the keys you need."""
    # The API's ?include only applies to streaming; a one-shot GET always
    # returns everything, so the selection is done here.
    text = _get("/states").text or "ok"
    if include:
        try:
            data = json.loads(text)
            keys = [k.strip() for k in include.split(",") if k.strip()]
            text = json.dumps({k: v for k, v in data.items() if k in keys}, ensure_ascii=False)
        except ValueError:
            pass
    return _truncate(text)


def odom():
    """GET /odom — odometry as JSON (pose + twist). Teleports in the simulator do not reset it."""
    return _truncate(_get("/odom").text or "ok")


def battery():
    """GET /battery — battery state as JSON."""
    return _truncate(_get("/battery").text or "ok")


def imu():
    """GET /imu — IMU reading as JSON (6-axis accelerometer + gyroscope)."""
    return _truncate(_get("/imu").text or "ok")


def lidar(
    step: Annotated[float, Field(description="Angular step in degrees, 0.5~30 (default 5 → 72 points; use 1 for the full 360-point scan).")] = 5.0,
):
    """GET /lidar — 360° LiDAR distance scan. Returns angle→distance(m). 0°=front, +90°=left, -90°=right, 180°=rear. Range 0.15m~16m."""
    return _truncate(_get("/lidar", step=step).text or "ok")


def camera(
    width: Annotated[int, Field(description="Resize width in pixels, 16..1920 (native 480; out of range → HTTP 422).")] = None,
    height: Annotated[int, Field(description="Resize height in pixels, 16..1080 (native 360; out of range → HTTP 422).")] = None,
):
    """GET /camera — capture a JPEG from the front camera so you can SEE it. Aim first with robot_camera_pan/robot_camera_tilt if needed."""
    r = _get("/camera", width=width, height=height)
    return [_Image("image/jpeg", base64.b64encode(r.content).decode())]


# ---- Control (GET without value, POST with value) -------------------------------------------

def speed(
    value: Annotated[float, Field(description="Speed in m/s (-3..3, +=forward). 0 stops the robot. Omit to read the current speed.")] = None,
    duration: Annotated[float, Field(description="Seconds to keep the command alive, up to 600 (with value only; above 600 → HTTP 422). The server publishes 0 at the end and this call BLOCKS until the drive finishes.")] = None,
):
    """Speed in m/s. Without value: GET /speed reads the current speed. With value: POST /speed {value, duration?} — WITHOUT duration the command expires after ~1s (safety watchdog) unless renewed; WITH duration the server keeps it alive, stops at the end, and the call returns after the drive completes, so you can chain the next action directly. e.g. forward 2s then look: robot_speed(value=1, duration=2) → robot_camera() · emergency stop: robot_speed(value=0)."""
    if value is None:
        if duration is not None:
            raise RuntimeError("duration requires value")
        return _get("/speed").text or "ok"
    body = {"value": float(value)}
    if duration is not None and float(value) != 0:
        body["duration"] = float(duration)
    # with duration the server holds the response until the drive ends (blocking)
    timeout = body["duration"] + 15 if "duration" in body else 10
    return _post("/speed", timeout=timeout, **body).text or "ok"


def steering(
    value: Annotated[float, Field(description="Steering angle in RADIANS (-0.349..0.349 ≈ ±20°, +=left). Omit to read the current angle.")] = None,
):
    """Steering angle in RADIANS (the Web API contract — NOT degrees). Without value: GET /steering. With value: POST /steering {value}; the angle persists until changed. e.g. 10° left: robot_steering(value=0.175) · center: robot_steering(value=0)."""
    if value is None:
        return _get("/steering").text or "ok"
    return _post("/steering", value=float(value)).text or "ok"


def camera_pan(
    value: Annotated[float, Field(description="Camera pan in RADIANS (-0.524..0.524 ≈ ±30°, +=left). Omit to read the current angle.")] = None,
):
    """Camera pan angle in RADIANS (the Web API contract — NOT degrees). Without value: GET /camera/pan. With value: POST /camera/pan {value}. Use robot_camera() to see the image."""
    if value is None:
        return _get("/camera/pan").text or "ok"
    return _post("/camera/pan", value=float(value)).text or "ok"


def camera_tilt(
    value: Annotated[float, Field(description="Camera tilt in RADIANS (-0.524..0.524 ≈ ±30°, +=up). Omit to read the current angle.")] = None,
):
    """Camera tilt angle in RADIANS (the Web API contract — NOT degrees). Without value: GET /camera/tilt. With value: POST /camera/tilt {value}. Use robot_camera() to see the image."""
    if value is None:
        return _get("/camera/tilt").text or "ok"
    return _post("/camera/tilt", value=float(value)).text or "ok"


# ---- Audio ----------------------------------------------------------------------------------

def audio_play(
    url: Annotated[str, Field(description="Audio file URL.")] = None,
    path: Annotated[str, Field(description="Audio file path on this machine.")] = None,
    data: Annotated[str, Field(description="Base64-encoded audio file.")] = None,
    format: Annotated[str, Field(description="Container hint for data, e.g. 'mp3', 'wav', 'ogg' (default mp3; only used with data).")] = None,
    volume: Annotated[float, Field(description="Volume 0..1.")] = None,
    loop: Annotated[bool, Field(description="Loop playback.")] = False,
    replace: Annotated[bool, Field(description="Replace whatever is currently playing.")] = False,
):
    """POST /audio/play — play one of url / path / data on the robot speaker (browser viewer in SIM). Find music preview URLs with utils_music_search. Returns the playing item id."""
    return _post("/audio/play", url=url, path=path, data=data, format=format,
                 volume=volume, loop=loop or None, replace=replace or None).text or "ok"


def audio_stop(
    id: Annotated[str, Field(description="Playing item id (from robot_audio_play/robot_audio).")] = None,
    all: Annotated[bool, Field(description="Stop everything.")] = False,
):
    """POST /audio/stop — stop playback by id, or everything with all=true."""
    return _post("/audio/stop", **({"all": True} if all else {"id": id})).text or "ok"


def audio_volume(
    id: Annotated[str, Field(description="Playing item id.")],
    volume: Annotated[float, Field(description="Volume 0..1.")],
):
    """POST /audio/volume — change the volume of a playing item."""
    return _post("/audio/volume", id=id, volume=float(volume)).text or "ok"


def audio():
    """GET /audio — list the currently playing items."""
    return _truncate(_get("/audio").text or "ok")
