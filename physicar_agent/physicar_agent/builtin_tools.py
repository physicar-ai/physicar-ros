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


def camera() -> list:
    """Capture a camera frame (JPEG)."""
    return image(base64.b64encode(api.get('/state/camera')).decode())


def battery() -> dict:
    """Read battery percentage and voltage."""
    b = api.get('/state/battery')
    return {"percentage": b.get("percentage"), "voltage": b.get("voltage")}


def control(
    speed: Annotated[Optional[float], Field(description="m/s (-2..2, +=forward)")] = None,
    steering: Annotated[Optional[float], Field(description="degrees (-26..26, +=left)")] = None,
    pan: Annotated[Optional[float], Field(description="camera pan degrees (-30..30, +=left)")] = None,
    tilt: Annotated[Optional[float], Field(description="camera tilt degrees (-30..30, +=up)")] = None,
    duration: Annotated[float, Field(description="seconds")] = 3.0,
) -> str:
    """Drive the robot or move the camera."""
    if pan is not None:
        api.post('/control/camera/pan', value=math.radians(float(pan)))
    if tilt is not None:
        api.post('/control/camera/tilt', value=math.radians(float(tilt)))

    if speed is None and steering is None:
        time.sleep(duration if duration > 0 else 0.3)
        return "done"

    spd = float(speed or 0.0)
    steer = float(steering or 0.0)
    duration = duration if duration > 0 else 3.0

    api.post('/control/speed', value=spd)
    api.post('/control/steering', value=math.radians(steer))
    time.sleep(duration)
    api.post('/control/speed', value=0.0)
    return "done"

def deepracer(
    action: Annotated[str, Field(description="status / load / unload / start / stop")],
    model_name: Annotated[Optional[str], Field(description="model name (for load/unload)")] = None,
) -> dict:
    """Manage DeepRacer models and autonomous driving."""
    if action == "status":
        return api.get('/deepracer/status')

    if action == "load":
        if not model_name:
            return {"success": False, "message": "model_name required"}
        resp = api.post('/deepracer/load_model', model_name=model_name)
        result = {"success": resp.get("success", False), "message": resp.get("message", "")}
        if result["success"] and resp.get("action_space_json"):
            result["action_space"] = json.loads(resp["action_space_json"])
        return result

    if action == "unload":
        return api.post('/deepracer/unload_model', model_name=model_name or "")

    if action == "start":
        return api.post('/deepracer/control', start=True)

    if action == "stop":
        return api.post('/deepracer/control', start=False)

    return {"success": False, "message": f"Unknown action: {action}. Use: status, load, unload, start, stop"}

def lidar(
    step: Annotated[float, Field(description="angular step in degrees (0.5~30)", ge=0.5, le=30)] = 5.0,
) -> dict:
    """Read 360° LiDAR scan. 0°=front, +90°=left, -90°=right, 180°=rear."""
    return api.get("/state/lidar", step=step)

def motion() -> dict:
    """Read current velocity, steering, heading, acceleration, and camera angles."""
    odom = api.get('/state/odom')
    state = api.get('/state')
    imu = api.get('/state/imu')
    pan_data = api.get('/state/camera/pan')
    tilt_data = api.get('/state/camera/tilt')

    vel = odom.get('velocity', {})
    cmd = state.get('cmd', {})
    o = imu.get('orientation', {})
    x, y, z, w = o.get('x', 0), o.get('y', 0), o.get('z', 0), o.get('w', 1)
    yaw = math.degrees(math.atan2(2.0 * (w * z + x * y), 1.0 - 2.0 * (y * y + z * z)))
    la = imu.get('acceleration', {})

    return {
        "linear_m_s": vel.get('linear', 0),
        "angular_rad_s": vel.get('angular', 0),
        "steering_deg": round(math.degrees(cmd.get('steering', 0)), 1),
        "heading_deg": round(yaw, 1),
        "accel": {
            "x": round(float(la.get('x', 0)), 2),
            "y": round(float(la.get('y', 0)), 2),
            "z": round(float(la.get('z', 0)), 2),
        },
        "pan_deg": round(math.degrees(pan_data.get('value', 0)), 1),
        "tilt_deg": round(math.degrees(tilt_data.get('value', 0)), 1),
    }

_stop_event = threading.Event()
_MUSIC_CHANNEL = "music"
_ATTRIBUTION = "Preview provided courtesy of iTunes."
def music(
    action: Annotated[str, Field(description="search / play / stop / volume")],
    query: Annotated[Optional[str], Field(description="search query (for search/play)")] = None,
    url: Annotated[Optional[str], Field(description="preview URL (for play)")] = None,
    volume: Annotated[float, Field(description="0.0 to 1.0")] = 0.5
) -> dict:
    """Search and play 30s iTunes previews. Include view_url in responses."""
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
