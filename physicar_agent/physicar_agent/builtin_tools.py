"""PhysiCar Agent — builtin tools.

Each public function (no leading underscore) is registered as a tool.
The function name becomes the tool name.
The docstring becomes the tool description.
Parameters use Annotated[type, Field(description=...)] for metadata.
"""

import base64
import json
import math
import threading
import time
from typing import Annotated, Optional

from pydantic import Field

from physicar_agent import api, text, image


# =====================================================================
# battery
# =====================================================================

def battery() -> list:
    """Read the robot's battery level.

    Returns percentage (0–100%) and voltage.
    Only available on real robot hardware.

    Examples:
        battery()
    """
    try:
        b = api.get('/state/battery')
    except Exception:
        return [text("battery: unavailable")]

    parts = []
    if 'percentage' in b:
        try:
            parts.append(f"{round(float(b['percentage']), 1)}%")
        except Exception:
            pass
    if 'voltage' in b:
        try:
            parts.append(f"{round(float(b['voltage']), 2)}V")
        except Exception:
            pass

    if not parts:
        return [text("battery: unavailable")]

    return [text("battery: " + " / ".join(parts))]


# =====================================================================
# camera
# =====================================================================

def camera() -> list:
    """Capture the latest camera frame as a JPEG image.

    Resolution: 480x360. Returns a single JPEG image.
    The camera is mounted on a pan/tilt gimbal controlled via
    the control tool (pan: ±30°, tilt: ±30°).

    Examples:
        camera()
    """
    try:
        jpeg = api.get('/state/camera')
    except Exception:
        return [text("camera: unavailable")]

    if not jpeg or not isinstance(jpeg, bytes):
        return [text("camera: unavailable")]

    b64 = base64.b64encode(jpeg).decode('ascii')
    return [image(b64)]


# =====================================================================
# control
# =====================================================================

def control(
    speed: Annotated[Optional[float], Field(description="Speed in m/s (-2.0..2.0, positive=forward)")] = None,
    steering: Annotated[Optional[float], Field(description="Steering in degrees (-26..26, positive=left)")] = None,
    pan: Annotated[Optional[float], Field(description="Camera pan in degrees (-30..30, positive=left)")] = None,
    tilt: Annotated[Optional[float], Field(description="Camera tilt in degrees (-30..30, positive=up)")] = None,
    duration: Annotated[float, Field(description="Duration in seconds")] = 3.0,
) -> dict:
    """Drive the robot or move the camera.

    Examples:
        control(speed=0.3, duration=2)              # forward 2s
        control(speed=0.3, steering=20, duration=1)  # forward + turn left
        control(pan=30)                               # look left
        control(tilt=20)                              # look up
        control(speed=0, steering=0)                  # stop
    """
    if pan is not None:
        api.post('/control/camera/pan', value=math.radians(float(pan)))
    if tilt is not None:
        api.post('/control/camera/tilt', value=math.radians(float(tilt)))

    if speed is None and steering is None:
        time.sleep(duration if duration > 0 else 0.3)
        return text("done")

    spd = float(speed or 0.0)
    steer = float(steering or 0.0)
    duration = duration if duration > 0 else 3.0

    api.post('/control/speed', value=spd)
    api.post('/control/steering', value=math.radians(steer))
    time.sleep(duration)
    api.post('/control/speed', value=0.0)

    return text("done")


# =====================================================================
# deepracer
# =====================================================================

def deepracer(
    action: Annotated[str, Field(description="One of: status, load, unload, start, stop")],
    model_name: Annotated[Optional[str], Field(description="Model name (for action=load or unload)")] = None,
) -> dict:
    """Manage DeepRacer reinforcement-learning models and autonomous driving.

    Models are stored in /home/physicar/physicar_ws/userdata/deepracer/models/<model_name>/.

    Examples:
        deepracer("status")
        deepracer("load", model_name="my-model")
        deepracer("start")
        deepracer("stop")
        deepracer("unload", model_name="my-model")
    """
    if action == "status":
        resp = api.get('/deepracer/status')
        return {
            "model_loaded": resp.get("model_loaded", False),
            "inference_running": resp.get("inference_running", False),
            "model_name": resp.get("model_name", ""),
            "action_count": resp.get("action_count", 0),
            "inference_rate_hz": resp.get("inference_rate", 0.0),
            "inference_count": resp.get("inference_count", 0),
            "speed_percent": resp.get("speed_percent", 1.0),
        }

    if action == "load":
        if not model_name:
            return {"success": False, "message": "model_name required"}
        resp = api.post('/deepracer/load_model', model_name=model_name)
        result = {"success": resp.get("success", False), "message": resp.get("message", "")}
        if result["success"] and resp.get("action_space_json"):
            result["action_space"] = json.loads(resp["action_space_json"])
        return result

    if action == "unload":
        resp = api.post('/deepracer/unload_model', model_name=model_name or "")
        return {"success": resp.get("success", False), "message": resp.get("message", "")}

    if action == "start":
        resp = api.post('/deepracer/control', start=True)
        return {"success": resp.get("success", False), "message": resp.get("message", "")}

    if action == "stop":
        resp = api.post('/deepracer/control', start=False)
        return {"success": resp.get("success", False), "message": resp.get("message", "")}

    return {"success": False, "message": f"Unknown action: {action}. Use: status, load, unload, start, stop"}


# =====================================================================
# lidar
# =====================================================================

def lidar(
    step: Annotated[float, Field(
        description="Angular step in degrees (0.5~30). Default 5 gives ~72 points.",
        ge=0.5, le=30,
    )] = 5.0,
) -> list:
    """Read the latest 360 LiDAR scan.

    Orientation: 0=front, +90=left, -90=right, 180=rear.
    Range: 0.15-16 m.

    Examples:
        lidar()              # 5 step, ~72 points
        lidar(step=1)        # 1 step, ~360 points
        lidar(step=10)       # 10 step, ~36 points
    """
    try:
        data = api.get("/state/lidar", step=step)
    except Exception:
        return [text("lidar: unavailable")]

    if not data or "ranges" not in data:
        return [text("lidar: no data")]

    ranges = data["ranges"]
    range_min = data.get("range_min", 0.15)
    range_max = data.get("range_max", 12.0)
    count = data.get("count", len(ranges))

    out = []
    for angle_str, dist in ranges.items():
        if dist is None:
            out.append(f"{angle_str}=null")
        else:
            out.append(f"{angle_str}={round(dist, 3)}m")

    header = f"lidar (step={step}, range={range_min}~{range_max}m, points={count}):"
    return [text(header + "\n  " + ", ".join(out))]


# =====================================================================
# motion
# =====================================================================

def motion() -> list:
    """Read the robot's current motion and IMU state.

    Returns:
        linear    - forward velocity in m/s (from odometry)
        angular   - yaw rate in rad/s (from odometry)
        steering  - front wheel angle in degrees
        heading   - IMU yaw in degrees
        accel     - IMU linear acceleration x/y/z in m/s²
        pan/tilt  - camera gimbal angles in degrees

    Examples:
        motion()
    """
    lines = []

    try:
        odom = api.get('/state/odom')
        vel = odom.get('velocity', {})
        lines.append(f"linear: {vel.get('linear', 0)} m/s")
        lines.append(f"angular: {vel.get('angular', 0)} rad/s")
    except Exception:
        lines.append("linear: unavailable")
        lines.append("angular: unavailable")

    try:
        state = api.get('/state')
        cmd = state.get('cmd', {})
        st_rad = cmd.get('steering', 0)
        lines.append(f"steering: {round(math.degrees(st_rad), 1)}°")
    except Exception:
        lines.append("steering: unavailable")

    try:
        imu = api.get('/state/imu')
        o = imu.get('orientation', {})
        x, y, z, w = o.get('x', 0), o.get('y', 0), o.get('z', 0), o.get('w', 1)
        siny = 2.0 * (w * z + x * y)
        cosy = 1.0 - 2.0 * (y * y + z * z)
        yaw = math.degrees(math.atan2(siny, cosy))
        lines.append(f"heading: {round(yaw, 1)}°")

        la = imu.get('acceleration', {})
        ax = round(float(la.get('x', 0)), 2)
        ay = round(float(la.get('y', 0)), 2)
        az = round(float(la.get('z', 0)), 2)
        lines.append(f"accel: x={ax} y={ay} z={az} m/s²")
    except Exception:
        lines.append("heading: unavailable")
        lines.append("accel: unavailable")

    try:
        pan_data = api.get('/state/camera/pan')
        lines.append(f"pan: {round(math.degrees(pan_data.get('value', 0)), 1)}°")
    except Exception:
        lines.append("pan: unavailable")

    try:
        tilt_data = api.get('/state/camera/tilt')
        lines.append(f"tilt: {round(math.degrees(tilt_data.get('value', 0)), 1)}°")
    except Exception:
        lines.append("tilt: unavailable")

    return [text("\n".join(lines))]


# =====================================================================
# music
# =====================================================================

_stop_event = threading.Event()
_MUSIC_CHANNEL = "music"
_ATTRIBUTION = "Preview provided courtesy of iTunes."


def music(
    action: Annotated[str, Field(description="One of: search, play, stop, volume")],
    query: Annotated[Optional[str], Field(description="Search query (for action=search or play)")] = None,
    url: Annotated[Optional[str], Field(description="Preview URL (for action=play)")] = None,
    volume: Annotated[float, Field(description="Volume from 0.0 to 1.0")] = 0.5
) -> dict:
    """Search for songs and play their 30-second iTunes previews.

    Uses the Apple iTunes Search API. When presenting preview results to the
    user, you MUST also surface the iTunes Store link (`view_url`) and the
    `attribution` text so the user can purchase or listen to the full song
    on iTunes (required by Apple's terms of use).

    Examples:
        music("search", query="BTS SWIM")
        music("play", query="BTS SWIM")
        music("play", url="https://audio-ssl.itunes.apple.com/...")
        music("stop")
        music("volume", volume=0.8)
    """
    if action == "stop":
        _stop_event.set()
        api.post('/control/audio', channel=_MUSIC_CHANNEL, stop=True)
        return {"success": True, "message": "Preview stopped"}

    if action == "volume":
        vol = max(0.0, min(1.0, volume))
        api.post('/control/audio', channel=_MUSIC_CHANNEL, volume=vol)
        return {"success": True, "message": f"Volume: {vol:.0%}"}

    if action == "search":
        if not query:
            return {"success": False, "message": "query required"}
        return _music_search(query)

    if action == "play":
        track_info = None
        if not url and query:
            result = _music_search(query)
            if result.get("results"):
                track_info = result["results"][0]
                url = track_info["preview_url"]
            else:
                return {"success": False, "message": "No results found"}

        if not url:
            return {"success": False, "message": "url or query required"}

        _stop_event.set()
        api.post('/control/audio', channel=_MUSIC_CHANNEL, stop=True)
        time.sleep(0.3)
        _stop_event.clear()

        vol = max(0.0, min(1.0, volume))
        api.post('/control/audio', channel=_MUSIC_CHANNEL, volume=vol)

        import av

        stream_url = url

        def stream_audio():
            try:
                container = av.open(stream_url)
                audio_stream = next(s for s in container.streams if s.type == 'audio')
                resampler = av.AudioResampler(format='s16', layout='stereo', rate=44100)

                for frame in container.decode(audio_stream):
                    if _stop_event.is_set():
                        break
                    for r in resampler.resample(frame):
                        if _stop_event.is_set():
                            break
                        pcm_b64 = base64.b64encode(r.to_ndarray().tobytes()).decode()
                        api.post('/control/audio',
                                 channel=_MUSIC_CHANNEL,
                                 data=pcm_b64,
                                 format='pcm',
                                 sample_rate=44100,
                                 audio_channels=2,
                                 bits_per_sample=16)
                        time.sleep(0.001)
                container.close()
            except Exception:
                pass

        threading.Thread(target=stream_audio, daemon=True).start()

        if track_info is None and query:
            sr = _music_search(query)
            if sr.get("results"):
                track_info = sr["results"][0]

        title = "Unknown"
        view_url = None
        artwork = None
        if track_info:
            title = f"{track_info.get('artist', '')} - {track_info.get('title', '')}".strip(" -")
            view_url = track_info.get('view_url')
            artwork = track_info.get('artwork')
        return {
            "success": True,
            "message": f"Playing 30s preview: {title}",
            "title": title,
            "view_url": view_url,
            "artwork": artwork,
            "attribution": _ATTRIBUTION,
            "notice": "Tell the user the title and include the iTunes link (view_url) so they can purchase or listen in full.",
        }

    return {"success": False, "message": f"Unknown action: {action}"}


def _music_search(query: str, limit: int = 5) -> dict:
    """Search iTunes for music tracks."""
    import urllib.request
    import urllib.parse

    params = urllib.parse.urlencode({
        'term': query,
        'media': 'music',
        'limit': limit
    })
    api_url = f"https://itunes.apple.com/search?{params}"

    try:
        req = urllib.request.Request(api_url, headers={'User-Agent': 'PhysiCar/1.0'})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        return {"success": False, "results": [], "message": str(e)}

    results = []
    for t in data.get('results', []):
        preview = t.get('previewUrl')
        if not preview:
            continue
        view_url = t.get('trackViewUrl') or t.get('collectionViewUrl') or t.get('artistViewUrl')
        results.append({
            "title": t.get('trackName', ''),
            "artist": t.get('artistName', ''),
            "album": t.get('collectionName', ''),
            "duration_ms": t.get('trackTimeMillis', 0),
            "genre": t.get('primaryGenreName', ''),
            "preview_url": preview,
            "artwork": t.get('artworkUrl100', ''),
            "view_url": view_url,
        })

    return {
        "success": True,
        "results": results,
        "attribution": _ATTRIBUTION,
        "notice": "When showing these preview results to the user, always include the track title and the iTunes Store link (view_url) so they can purchase or listen to the full song.",
    }
