# /// script
# dependencies = ["av"]
# ///
"""Search and play iTunes 30-second song previews (Apple iTunes Search API).

Apple's iTunes Search API terms require that any UI showing previews must
(a) be placed next to a link that goes directly to the iTunes Store page for
the track, and (b) include the "provided courtesy of iTunes" attribution.
Search/play results therefore include `view_url` and `attribution` fields,
and the assistant must surface them to the user whenever it presents preview
results.
"""

from typing import Annotated, Optional
from pydantic import Field

from physicar_agent import topic

from physicar_interfaces.msg import Audio
import threading
import time

_stop_event = threading.Event()
_CHANNEL = "music"
_ATTRIBUTION = "Preview provided courtesy of iTunes."


def tool(
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
        tool("search", query="BTS SWIM")
        tool("play", query="BTS SWIM")
        tool("play", url="https://audio-ssl.itunes.apple.com/...")
        tool("stop")
        tool("volume", volume=0.8)
    """

    if action == "stop":
        _stop_event.set()
        msg = Audio()
        msg.channel = _CHANNEL
        msg.stop = True
        topic.pub('/audio', msg)
        return {"success": True, "message": "Preview stopped"}

    if action == "volume":
        vol = max(0.0, min(1.0, volume))
        msg = Audio()
        msg.channel = _CHANNEL
        msg.volume = vol
        topic.pub('/audio', msg)
        return {"success": True, "message": f"Volume: {vol:.0%}"}

    if action == "search":
        if not query:
            return {"success": False, "message": "query required"}
        return _search(query)

    if action == "play":
        track_info = None
        if not url and query:
            result = _search(query)
            if result.get("results"):
                track_info = result["results"][0]
                url = track_info["preview_url"]
            else:
                return {"success": False, "message": "No results found"}

        if not url:
            return {"success": False, "message": "url or query required"}

        # Stop previous playback
        _stop_event.set()
        stop_msg = Audio()
        stop_msg.channel = _CHANNEL
        stop_msg.stop = True
        topic.pub('/audio', stop_msg)
        time.sleep(0.3)

        _stop_event.clear()

        # Set volume
        vol = max(0.0, min(1.0, volume))
        vol_msg = Audio()
        vol_msg.channel = _CHANNEL
        vol_msg.volume = vol
        topic.pub('/audio', vol_msg)

        # Stream audio
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

                        msg = Audio()
                        msg.channel = _CHANNEL
                        msg.data = list(r.to_ndarray().tobytes())
                        msg.format = "pcm"
                        msg.sample_rate = 44100
                        msg.audio_channels = 2
                        msg.bits_per_sample = 16
                        topic.pub('/audio', msg)
                        time.sleep(0.001)

                container.close()
            except Exception:
                pass

        threading.Thread(target=stream_audio, daemon=True).start()

        # Build a response that includes the iTunes Store link + attribution.
        if track_info is None and query:
            sr = _search(query)
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


def _search(query: str, limit: int = 5) -> dict:
    """Search iTunes for music tracks. Result rows include `view_url` (link
    back to the iTunes Store) and a top-level `attribution` field per the
    iTunes Search API terms of use.
    """
    import urllib.request
    import urllib.parse
    import json

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
        # `trackViewUrl` is the canonical iTunes Store page for this track.
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
