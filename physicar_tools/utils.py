"""Utility tools (bundled with the extension's tool server, read-only).

Tools that belong to no API in particular: timing helpers, music search,
wake tickets. Same format as every other tool script — top-level function =
tool (exposed as utils_<name>), "_" names are helpers, and tools return a
LIST of contents ({"type": "text"/"image", ...}).
"""
import json
import time

from typing import Annotated

import requests
from pydantic import Field


def sleep(
    seconds: Annotated[float, Field(description="Seconds to wait (0.1~60).")],
):
    """Wait for a given duration. Use between tool calls to create timed
    sequences. e.g. look left, wait 1s, look right:
    robot_camera_pan(value=0.5) → utils_sleep(1) →
    robot_camera_pan(value=-0.5). For driving, prefer robot_speed's duration
    parameter (auto-stop).
    """
    sec = max(0.1, min(60.0, float(seconds or 1)))
    time.sleep(sec)
    tool_call_output_contents = [
        {"type": "text", "text": "waited {:g}s".format(sec)},
    ]
    return tool_call_output_contents


def music_search(
    query: Annotated[str, Field(description="Search query.")],
):
    """Search iTunes for music tracks. Returns title, artist, a 30s preview mp3
    URL (play it with robot_audio_play) and a view URL.
    """
    r = requests.get(
        "https://itunes.apple.com/search",
        params={"term": str(query or ""), "media": "music", "limit": "5"},
        headers={"User-Agent": "PhysiCar/1.0"},
        timeout=10,
    )
    r.raise_for_status()
    results = []
    for t in r.json().get("results", []):
        if not t.get("previewUrl"):
            continue
        results.append({
            "title": t.get("trackName", ""),
            "artist": t.get("artistName", ""),
            "preview_url": t["previewUrl"],
            "view_url": t.get("trackViewUrl") or t.get("collectionViewUrl") or t.get("artistViewUrl") or "",
        })
    tool_call_output_contents = [
        {"type": "text", "text": json.dumps({"success": True, "results": results}, indent=1, ensure_ascii=False)},
    ]
    return tool_call_output_contents


def wake_reserve(
    message: Annotated[str, Field(description="What the AI is told when this wake fires — state WHY it was reserved, e.g. 'the training job finished'.")],
):
    """Reserve a ONE-SHOT wake ticket bound to this chat. When anything later
    POSTs http://localhost/physicar-ext/wake with {"wake_id": "<id>"} (curl,
    python requests, a web page), the AI wakes up in this chat with the
    reserved message; an optional "note" in the body is appended (e.g. the
    job's result). One-shot: a second trigger does nothing, so a retrying
    script cannot spam the chat. Ticket state can be checked at POST
    /physicar-ext/wake/status {"wake_id"}. Typical use: reserve → hand the
    wake_id to a long-running job → the job POSTs it on completion.
    """
    from pcwake import reserve
    wid = reserve(message)
    if not wid:
        raise RuntimeError("no chat session is attached to this call — wake_reserve only works from the chat")
    tool_call_output_contents = [
        {"type": "text", "text": json.dumps({
            "wake_id": wid,
            "how": "when the event happens: curl -X POST http://localhost/physicar-ext/wake -H 'Content-Type: application/json' -d '{\"wake_id\": \"" + wid + "\", \"note\": \"optional result\"}'",
        })},
    ]
    return tool_call_output_contents


def wake_status(
    wake_id: Annotated[str, Field(description="Ticket to check. Omit to list this chat's outstanding tickets.")] = None,
):
    """Check wake tickets. With wake_id: is it still pending, already redeemed
    (fired), or unknown (expired / never existed / server restarted)? Without
    arguments: list this chat's outstanding (un-redeemed) tickets with their
    messages and ages.
    """
    import pcwake
    if wake_id:
        text = json.dumps(pcwake.status(wake_id), ensure_ascii=False)
    else:
        text = json.dumps({"outstanding": pcwake.tickets()}, ensure_ascii=False)
    tool_call_output_contents = [
        {"type": "text", "text": text},
    ]
    return tool_call_output_contents
