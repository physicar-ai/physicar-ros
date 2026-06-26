import base64
import json
import math
import threading
import time
from typing import Annotated, Optional

from pydantic import Field

from physicar.robot import api
from physicar.chat.types import TextContent, ImageContent


def camera():
    """Capture an image from the front camera."""
    jpeg = api.get('/camera')
    tool_call_output_contents = [
        ImageContent(
            mime="image/jpeg",
            base64=base64.b64encode(jpeg).decode(),
        ),
    ]
    return tool_call_output_contents

def lidar(
    step: Annotated[float, Field(description="angular step in degrees (0.5~30)", ge=0.5, le=30)] = 5.0,
):
    """Read 360° LiDAR distance scan.

    Returns angle→distance(m) map. 0°=front, +90°=left, -90°=right, 180°=rear.
    Smaller step = more points (e.g. step=5 → 72 points, step=30 → 12 points).
    Range: 0.15m ~ 16m.
    """
    tool_call_output_contents = [
        TextContent(
            text=json.dumps(api.get("/lidar", step=step), ensure_ascii=False),
        ),
    ]
    return tool_call_output_contents

def states():
    """Read robot states: speed, steering, camera pan/tilt, and battery."""
    tool_call_output_contents = [
        TextContent(text=json.dumps({"speed": api.get('/speed')})),
        TextContent(text=json.dumps({"steering_deg": round(math.degrees(api.get('/steering')), 1)})),
        TextContent(text=json.dumps({"pan_deg": round(math.degrees(api.get('/camera/pan')), 1)})),
        TextContent(text=json.dumps({"tilt_deg": round(math.degrees(api.get('/camera/tilt')), 1)})),
        TextContent(text=json.dumps(api.get('/battery'), ensure_ascii=False)),
    ]
    return tool_call_output_contents

def look(
    pan: Annotated[float, Field(description="camera pan degrees (-30..30, +=left)")] = 0.0,
    tilt: Annotated[float, Field(description="camera tilt degrees (-30..30, +=up)")] = 0.0,
):
    """Rotate the camera. pan=left/right, tilt=up/down."""
    api.post('/camera/pan', value=math.radians(float(pan)))
    api.post('/camera/tilt', value=math.radians(float(tilt)))
    tool_call_output_contents = [
        TextContent(text="done"),
    ]
    return tool_call_output_contents

def drive(
    speed: Annotated[float, Field(description="m/s (-3..3, +=forward)")] = 0.0,
    steering: Annotated[float, Field(description="degrees (-20..20, +=left)")] = 0.0,
):
    """Set speed and steering.

    e.g. turn right: drive(speed=1, steering=-10)
    e.g. steer left only: drive(steering=10)
    e.g. stop: drive(speed=0)
    """
    api.post('/speed', value=float(speed))
    api.post('/steering', value=math.radians(float(steering)))
    tool_call_output_contents = [
        TextContent(text="done"),
    ]
    return tool_call_output_contents

def sleep(
    seconds: Annotated[float, Field(description="seconds to wait (0.1~60)")] = 1.0,
):
    """Wait for a given duration. Use between tool calls to create timed sequences.

    e.g. drive forward 2s then stop: drive(speed=1) → sleep(2) → drive(speed=0)
    """
    time.sleep(max(0.1, min(60.0, seconds)))
    tool_call_output_contents = [
        TextContent(text="done"),
    ]
    return tool_call_output_contents

def deepracer(
    action: Annotated[str, Field(description="load / unload / start / stop")],
    model_name: Annotated[Optional[str], Field(description="model name (for load/unload)")] = None,
):
    """Control DeepRacer autonomous driving.

    - load: load a trained model into memory (use deepracer_models to list available ones)
    - unload: remove model from memory
    - start: begin autonomous driving with the loaded model
    - stop: stop autonomous driving
    """
    if action == "load":
        resp = api.post('/deepracer/load_model', model_name=model_name)
    elif action == "unload":
        resp = api.post('/deepracer/unload_model', model_name=model_name or "")
    elif action == "start":
        resp = api.post('/deepracer/control', start=True)
    elif action == "stop":
        resp = api.post('/deepracer/control', start=False)
    else:
        resp = {"success": False, "message": f"Unknown action: {action}. Use: load, unload, start, stop"}

    tool_call_output_contents = [
        TextContent(text=json.dumps(resp, ensure_ascii=False)),
    ]
    return tool_call_output_contents

def deepracer_models():
    """List available DeepRacer models on disk."""
    resp = api.get('/deepracer/models')
    models = resp.get('models', [])
    summary = {
        "models": [
            {
                "name": m["name"],
                "sensors": m.get("sensors", []),
                "valid": m.get("is_valid", False),
            }
            for m in models
        ]
    }
    tool_call_output_contents = [
        TextContent(text=json.dumps(summary)),
    ]
    return tool_call_output_contents

def deepracer_status():
    """Check DeepRacer state: loaded model name, whether inference is running, action selection mode, and speed scale."""
    tool_call_output_contents = [
        TextContent(
            text=json.dumps(api.get('/deepracer/status'), ensure_ascii=False),
        ),
    ]
    return tool_call_output_contents

def deepracer_action_mode(
    mode: Annotated[str, Field(description="greedy / stochastic / mean")],
):
    """Set how DeepRacer picks actions.

    Modes:
    - greedy: always pick the highest-probability action
    - stochastic: randomly sample from the probability distribution
    - mean: weighted average of all actions by their probabilities
    """
    resp = api.post(
        '/deepracer/set_config',
        key='action_selection',
        string_value=mode,
        float_value=0.0,
    )
    tool_call_output_contents = [
        TextContent(text=json.dumps(resp, ensure_ascii=False)),
    ]
    return tool_call_output_contents

def deepracer_speed_percent(
    percent: Annotated[float, Field(description="speed scale 0.0~1.0")],
):
    """Scale DeepRacer driving speed.

    1.0 = full speed, 0.5 = half, 0.0 = stopped.
    """
    resp = api.post(
        '/deepracer/set_config',
        key='speed_percent',
        string_value='',
        float_value=float(percent),
    )
    tool_call_output_contents = [
        TextContent(text=json.dumps(resp, ensure_ascii=False)),
    ]
    return tool_call_output_contents

def music_search(
    query: Annotated[str, Field(description="search query")],
):
    """Search iTunes for music tracks."""
    import urllib.request
    import urllib.parse

    params = urllib.parse.urlencode({'term': query, 'media': 'music', 'limit': 5})
    try:
        req = urllib.request.Request(
            f"https://itunes.apple.com/search?{params}",
            headers={'User-Agent': 'PhysiCar/1.0'},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        tool_call_output_contents = [
            TextContent(
                text=json.dumps({"success": False, "results": [], "message": str(e)}),
            ),
        ]
        return tool_call_output_contents

    results = []
    for t in data.get('results', []):
        preview = t.get('previewUrl')
        if not preview:
            continue
        view_url = (
            t.get('trackViewUrl')
            or t.get('collectionViewUrl')
            or t.get('artistViewUrl')
        )
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

    tool_call_output_contents = [
        TextContent(
            text=json.dumps({
                "success": True,
                "results": results,
                "attribution": "Preview provided courtesy of iTunes.",
                "notice": "When showing these preview results to the user, always include the track title and the iTunes Store link (view_url) so they can purchase or listen to the full song.",
            }, ensure_ascii=False),
        ),
    ]
    return tool_call_output_contents

def music_player(
    action: Annotated[str, Field(description="play / stop / volume / status")],
    url: Annotated[Optional[str], Field(description="preview URL from music_search")] = None,
    volume: Annotated[float, Field(description="0.0~1.0")] = 0.5,
):
    """Control music playback. Use music_search first to get the preview URL."""
    if not hasattr(music_player, '_stop_event'):
        music_player._stop_event = threading.Event()
        music_player._thread = None
        music_player._current_url = None

    if action == "status":
        playing = music_player._thread is not None and music_player._thread.is_alive()
        tool_call_output_contents = [
            TextContent(
                text=json.dumps({
                    "playing": playing,
                    "url": music_player._current_url if playing else None,
                }),
            ),
        ]
        return tool_call_output_contents

    if action == "stop":
        music_player._stop_event.set()
        music_player._current_url = None
        api.post('/audio', channel="music", stop=True)
        tool_call_output_contents = [
            TextContent(text="done"),
        ]
        return tool_call_output_contents

    if action == "volume":
        api.post('/audio', channel="music", volume=max(0.0, min(1.0, volume)))
        tool_call_output_contents = [
            TextContent(text="done"),
        ]
        return tool_call_output_contents

    if action == "play":
        if not url:
            tool_call_output_contents = [
                TextContent(
                    text=json.dumps({"success": False, "message": "url required"}),
                ),
            ]
            return tool_call_output_contents

        music_player._stop_event.set()
        api.post('/audio', channel="music", stop=True)
        time.sleep(0.3)
        music_player._stop_event.clear()
        music_player._current_url = url

        vol = max(0.0, min(1.0, volume))
        api.post('/audio', channel="music", volume=vol)

        import av

        stop_event = music_player._stop_event

        def stream_audio():
            try:
                container = av.open(url)
                audio_stream = next(s for s in container.streams if s.type == 'audio')
                resampler = av.AudioResampler(format='s16', layout='stereo', rate=44100)
                for frame in container.decode(audio_stream):
                    if stop_event.is_set():
                        break
                    for r in resampler.resample(frame):
                        if stop_event.is_set():
                            break
                        pcm_b64 = base64.b64encode(r.to_ndarray().tobytes()).decode()
                        api.post('/audio',
                                 channel="music",
                                 data=pcm_b64,
                                 format='pcm',
                                 sample_rate=44100,
                                 audio_channels=2,
                                 bits_per_sample=16)
                        time.sleep(0.001)
                container.close()
            except Exception:
                pass

        t = threading.Thread(target=stream_audio, daemon=True)
        t.start()
        music_player._thread = t
        tool_call_output_contents = [
            TextContent(text="done"),
        ]
        return tool_call_output_contents

    tool_call_output_contents = [
        TextContent(
            text=json.dumps({
                "success": False,
                "message": f"Unknown action: {action}. Use: play, stop, volume, status",
            }),
        ),
    ]
    return tool_call_output_contents
