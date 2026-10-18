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
import os

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
    not reset it — robot_odom_reset does.
    """
    r = requests.get("http://localhost/odom", timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text or "ok"},
    ]
    return tool_call_output_contents


def odom_reset():
    """POST /odom/reset — zero the odometry now (laser odometry + EKF → (0, 0, 0)
    in `odom`). Call it after the car was teleported or placed by hand
    (sim_pose, Move Objects in the 3D view, lifting the real robot): odometry
    does not notice that by itself. The next /odom reflects it within ~0.1 s.
    """
    r = requests.post("http://localhost/odom/reset", timeout=15)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text or "ok"},
    ]
    return tool_call_output_contents


# The robot software checkout is named after the kit's generation key — /opt/physicar/src/<key>-ros
# (today physicar-ros; a gen-2 kit ships physicar2-ros); its VERSION file is the software version.
# Same rule as the extension's rosSrcDir(): the key from PHYSICAR_GENERATION (the cloud simulator
# sets it), else derived from /info's generation number; without that checkout, any *-ros there.
_SRC_DIR = "/opt/physicar/src"


def _generation_key(generation):
    key = os.environ.get("PHYSICAR_GENERATION", "").strip()
    if key:
        return key
    try:
        n = int(generation)
    except (TypeError, ValueError):
        n = 1
    return "physicar" if n <= 1 else "physicar{}".format(n)


def _software_version(key):
    """(checkout name, version): <key>-ros first, then any other *-ros dir; 'unknown' without a VERSION file."""
    names = [key + "-ros"]
    try:
        names += sorted(n for n in os.listdir(_SRC_DIR) if n.endswith("-ros") and n != names[0])
    except OSError:
        pass
    for name in names:
        try:
            with open(os.path.join(_SRC_DIR, name, "VERSION"), encoding="utf-8") as f:
                version = f.read().strip()
        except OSError:
            continue
        if version:
            return name, version
    return names[0], "unknown"


def _kv(d):
    return ", ".join("{}={}".format(k, v) for k, v in (d or {}).items()) or "-"


def info():
    """GET /info — what this machine is, as short text: mode (sim | real), the kit
    generation (number + key, e.g. 1 / physicar), the robot software version (the
    VERSION file of /opt/physicar/src/<key>-ros), the hardware, software and
    physical summary, and the serial. Answer "what generation / version / mode is
    this?" from it — the System tab shows the same values. Not /info's own
    `version`/`api_version` fields: those are static API labels.
    """
    r = requests.get("http://localhost/info", timeout=10)
    r.raise_for_status()
    d = r.json()
    generation = d.get("generation", 1)
    key = _generation_key(generation)
    repo, version = _software_version(key)
    lines = [
        "mode: {}".format(d.get("mode", "unknown")),
        "generation: {} (key {})".format(generation, key),
        "robot software: {} {}".format(repo, version),
        "hardware: " + _kv(d.get("hardware")),
        "software: " + _kv(d.get("software")),
        "physical: " + _kv(d.get("physical")),
        "serial: " + (d.get("serial") or "-"),
    ]
    tool_call_output_contents = [
        {"type": "text", "text": "\n".join(lines)},
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
    volume: Annotated[float, Field(description="Volume 0..1.")],
    id: Annotated[str, Field(description="Playing item id (from robot_audio_play/robot_audio); omit for the master volume, which scales every sound and is kept across reboots.")] = None,
):
    """POST /audio/volume — the volume of one playing item, or the master volume without id."""
    body = {"volume": float(volume)}
    if id:
        body["id"] = id
    r = requests.post("http://localhost/audio/volume", json=body, timeout=10)
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


_RESTART_PLAN = (
    "POST /api/restart restarts the robot software — the whole ROS stack and the web server "
    "(sim: supervisorctl restart physicar; real robot: systemctl restart physicar.service). "
    "The car stops; every running program loses the robot (student scripts, Racing courses, "
    "SLAM/Nav2); the Web API and the robot_* tools are away until the stack is back up "
    "(longer when a pending update rebuilds). Check afterwards with robot_info or "
    "GET http://localhost/health."
)


def restart(
    confirm: Annotated[bool, Field(description="true = restart now. false (default) = do nothing, only describe what the restart would do.")] = False,
):
    """POST /api/restart — restart the robot software (the ROS stack + the web
    server; same on both machines). It ends every running program that talks to
    the robot and takes the Web API away until the stack is back up, so it
    requires confirm=true — without it nothing happens and the plan is returned;
    ask the user first when something is running. This tool server restarts
    with the stack, so the answer may be cut off: that is the restart, not a
    failure. Not for the startup script (supervisorctl/systemctl restart it) and
    not for the simulator itself (physicar.ai).
    """
    if confirm not in (True, 1, "true", "True", "1"):
        tool_call_output_contents = [
            {"type": "text", "text": "Not restarted (confirm is not true). robot_restart(confirm=true) would: " + _RESTART_PLAN},
        ]
        return tool_call_output_contents
    r = requests.post("http://localhost/api/restart", timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": "restart requested: " + (r.text or "ok") + " — " + _RESTART_PLAN},
    ]
    return tool_call_output_contents
