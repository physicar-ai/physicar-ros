"""Utility tools (bundled with the extension's tool server, read-only).

Tools that belong to no API in particular: timing helpers, music search, and
example runners that exercise what the student built in the examples/
notebooks. Same format as every other tool script — top-level function =
tool (exposed as utils_<name>), "_" names are helpers.
"""
import json
import math
import os
import time

from typing import Annotated

import requests
from pydantic import Field

_ROS_API = "http://127.0.0.1:8000"
_EXAMPLES = os.path.expanduser("~/physicar_ws/examples")


def sleep(
    seconds: Annotated[float, Field(description="Seconds to wait (0.1~60).")],
):
    """Wait for a given duration. Use between tool calls to create timed sequences. e.g. look left, wait 1s, look right: robot_camera_pan(value=0.5) → utils_sleep(1) → robot_camera_pan(value=-0.5). For driving, prefer robot_speed's duration parameter (auto-stop)."""
    sec = max(0.1, min(60.0, float(seconds or 1)))
    time.sleep(sec)
    return "waited {:g}s".format(sec)


def music_search(
    query: Annotated[str, Field(description="Search query.")],
):
    """Search iTunes for music tracks. Returns title, artist, a 30s preview mp3 URL (play it with robot_audio_play) and a view URL."""
    r = requests.get(
        "https://itunes.apple.com/search",
        params={"term": str(query or ""), "media": "music", "limit": "5"},
        headers={"User-Agent": "PhysiCar/1.0"},
        timeout=10,
    )
    if not r.ok:
        raise RuntimeError("iTunes HTTP {}".format(r.status_code))
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
    return json.dumps({"success": True, "results": results}, indent=1, ensure_ascii=False)


def wake_reserve(
    message: Annotated[str, Field(description="What the AI is told when this wake fires — state WHY it was reserved, e.g. 'the training job finished'.")],
):
    """Reserve a ONE-SHOT wake ticket bound to this chat. When anything later POSTs http://localhost/physicar-ext/wake with {"wake_id": "<id>"} (curl, python requests, a web page), the AI wakes up in this chat with the reserved message; an optional "note" in the body is appended (e.g. the job's result). One-shot: a second trigger does nothing, so a retrying script cannot spam the chat. Ticket state can be checked at POST /physicar-ext/wake/status {"wake_id"}. Typical use: reserve → hand the wake_id to a long-running job → the job POSTs it on completion."""
    from pcwake import reserve
    wid = reserve(message)
    if not wid:
        raise RuntimeError("no chat session is attached to this call — wake_reserve only works from the chat")
    return json.dumps({
        "wake_id": wid,
        "how": "when the event happens: curl -X POST http://localhost/physicar-ext/wake -H 'Content-Type: application/json' -d '{\"wake_id\": \"" + wid + "\", \"note\": \"optional result\"}'",
    })


def wake_status(
    wake_id: Annotated[str, Field(description="Ticket to check. Omit to list this chat's outstanding tickets.")] = None,
):
    """Check wake tickets. With wake_id: is it still pending, already redeemed (fired), or unknown (expired / never existed / server restarted)? Without arguments: list this chat's outstanding (un-redeemed) tickets with their messages and ages."""
    import pcwake
    if wake_id:
        return json.dumps(pcwake.status(wake_id), ensure_ascii=False)
    return json.dumps({"outstanding": pcwake.tickets()}, ensure_ascii=False)


def run_example_racing(
    duration: Annotated[float, Field(description="Seconds to drive autonomously (3~60). The car is stopped afterwards no matter what.")] = 15.0,
):
    """Drive autonomously with the model trained in examples/racing-deeplearning.ipynb. Fully self-contained from the assets: models/model.onnx (architecture + weights) and models/model.json (the action table and camera config the notebook saved next to the weights) — so it stays correct even after the student edits ACTIONS or the network and retrains. Blocks until done, then stops the car. Fails with a clear message when no trained model exists yet."""
    # Heavy imports stay inside the function: only sessions that actually race
    # pay the onnxruntime/cv2 load, and the server stays light otherwise.
    import cv2
    import numpy as np
    import onnxruntime as ort

    models = os.path.join(_EXAMPLES, "assets", "racing-deeplearning", "models")
    onnx_path = os.path.join(models, "model.onnx")
    meta_path = os.path.join(models, "model.json")
    if not os.path.isfile(onnx_path):
        raise RuntimeError("no trained model at {} — train one first in examples/racing-deeplearning.ipynb (section 1.2 or 2.2)".format(onnx_path))
    if not os.path.isfile(meta_path):
        raise RuntimeError("missing {} (action table) — re-run the notebook's training/export cell once; current notebooks save it next to the weights".format(meta_path))

    with open(meta_path) as f:
        meta = json.load(f)
    actions = meta["actions"]   # [{"speed": m/s, "steering": deg}, ...]
    cam = meta["camera"]        # {"width", "height", "pan", "tilt"}
    sess = ort.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])
    input_name = sess.get_inputs()[0].name

    def post(path, value):
        requests.post(_ROS_API + path, json={"value": value}, timeout=2)

    sec = max(3.0, min(60.0, float(duration or 15)))
    post("/camera/pan", math.radians(float(cam.get("pan", 0.0))))
    post("/camera/tilt", math.radians(float(cam.get("tilt", -15.0))))
    counts = [0] * len(actions)
    steps = 0
    deadline = time.time() + sec
    try:
        while time.time() < deadline:
            jpg = requests.get(_ROS_API + "/camera", params={"width": cam["width"], "height": cam["height"]}, timeout=2).content
            img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
            if img is None:
                continue
            x = img.transpose(2, 0, 1).astype(np.float32)[None]      # 1x3xHxW, raw —
            a = int(np.argmax(sess.run(None, {input_name: x})[0]))   # norm is in the net
            post("/speed", float(actions[a]["speed"]))
            post("/steering", math.radians(float(actions[a]["steering"])))
            counts[a] += 1
            steps += 1
    finally:
        # The stop must survive any error above — a crashed loop must not
        # leave the car driving on its last command.
        try:
            post("/speed", 0.0)
            post("/steering", 0.0)
        except requests.RequestException:
            pass
    return json.dumps({"drove_seconds": round(sec, 1), "steps": steps,
                       "actions": {str(i): c for i, c in enumerate(counts)}})
