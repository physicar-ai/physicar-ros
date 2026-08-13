"""PhysiCar SIM tools (bundled with the extension, read-only).

First-class chat tools over the simulator's API at http://localhost/sim/api
— the same URLs the notebooks use. Same format as the user's custom
tools: top-level functions are tools, "_" names are helpers, the docstring
is the description, parameters use Annotated[type, Field(description=...)],
and tools return a LIST of contents ({"type": "text"/"image", ...}).

Admin-ish endpoints (world install/delete, simulator stop, evaluation
run/stop) are intentionally left out — a chat has no business calling them.

Units contract: yaw INPUT parameters take degrees for convenience (converted
with math.radians before POSTing), but every yaw the API RETURNS in JSON is
radians (-pi..pi).
"""
import json
import math
import time

from typing import Annotated
from urllib.parse import quote

import requests
from pydantic import Field


def _wait_ready(timeout):
    """POST /switch and /respawn return 200 immediately while the reload runs
    in a background thread — poll /status until the world is usable again so
    the tools can keep convenient blocking semantics."""
    end = time.monotonic() + timeout
    time.sleep(1.0)
    while time.monotonic() < end:
        try:
            s = requests.get("http://localhost/sim/api/status", timeout=10).json()
            if s.get("running") and not s.get("switching"):
                return "world ready"
        except requests.RequestException:
            pass   # API busy restarting — keep polling
        time.sleep(1.0)
    return "still loading after {:g}s — poll sim_status before using other tools".format(timeout)


def state():
    """One-call live snapshot as JSON: world, running/switching, sim
    time/paused/RTF, vehicle pose, every object's current pose, traffic
    lights, overlay, brightness, and the evaluation-run state. Yaw values in
    the JSON are RADIANS. Prefer this over combining sim_status + sim_objects
    + sim_traffic_lights when you need the current situation.
    """
    r = requests.get("http://localhost/sim/api/state", timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text or "ok"},
    ]
    return tool_call_output_contents


def world():
    """The current world's immutable definition as JSON: identity
    (world_id/rev/display), track geometry (route centerline + boundaries,
    bounds), object catalog (type/static/movable/origin/size), and whether it
    carries an evaluation. Load once per world — it only changes on world
    switch.
    """
    r = requests.get("http://localhost/sim/api/world", timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text or "ok"},
    ]
    return tool_call_output_contents


def evaluation():
    """The current world's evaluation document (config + scoring script published
    from the World Builder) as JSON. 404 when the world has none. Read-only —
    evaluations are run from the /sim page's ▶ button, not from here.
    """
    r = requests.get("http://localhost/sim/api/evaluation", timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text or "ok"},
    ]
    return tool_call_output_contents


def status():
    """Simulator snapshot as JSON: runtime status (current world,
    running/switching state), sim clock (sim/real time, RTF, paused), and
    vehicle pose in world coordinates (yaw in RADIANS).
    """
    parts = {"status": requests.get("http://localhost/sim/api/status", timeout=10).json()}
    try:
        parts["clock"] = requests.get("http://localhost/sim/api/clock", timeout=10).json()
    except Exception:
        pass   # clock not available yet (starting/switching) or sim not running
    try:
        parts["pose"] = requests.get("http://localhost/sim/api/pose", timeout=10).json()
    except Exception:
        pass   # no vehicle
    tool_call_output_contents = [
        {"type": "text", "text": json.dumps(parts, indent=1, ensure_ascii=False)},
    ]
    return tool_call_output_contents


def pose(
    x: Annotated[float, Field(description="World x in meters.")] = None,
    y: Annotated[float, Field(description="World y in meters.")] = None,
    yaw: Annotated[float, Field(description="Heading in degrees (0=+x, +=counter-clockwise).")] = None,
):
    """Teleport the vehicle and/or right a flipped car. x/y: meters in world
    coordinates. yaw INPUT: degrees (0=+x, +=counter-clockwise) — but yaw
    values READ back from the API (sim_state/sim_status) are radians. Omitted
    fields keep their current value, and the pose is normalized upright at
    ground level — so calling with NO arguments rights a flipped car in place.
    Odometry does not know about teleports; use sim_respawn for a full clean
    reset.
    """
    body = {}
    if x is not None:
        body["x"] = float(x)
    if y is not None:
        body["y"] = float(y)
    if yaw is not None:
        body["yaw"] = math.radians(float(yaw))
    r = requests.post("http://localhost/sim/api/pose", json=body, timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text or "ok"},
    ]
    return tool_call_output_contents


def objects(
    name: Annotated[str, Field(description="Model name (from the list). Omit to just list.")] = None,
    x: Annotated[float, Field(description="World x in meters.")] = None,
    y: Annotated[float, Field(description="World y in meters.")] = None,
    z: Annotated[float, Field(description="Height in meters.")] = None,
    yaw: Annotated[float, Field(description="Rotation in degrees (yaw only).")] = None,
):
    """Without arguments: list the world's models as JSON (name, type
    object/wall/light/model, static/movable, origin/current pose — yaw in
    RADIANS, size). With name + any of x/y/z/yaw: move/rotate that object
    (meters; yaw INPUT in degrees; omitted fields keep their value; omitted z
    settles the object on the ground). Traffic lights can be repositioned too.
    Rejected: walls, the track, the sun, and the vehicle — move the vehicle
    with sim_pose.
    """
    if name:
        body = {}
        if x is not None:
            body["x"] = float(x)
        if y is not None:
            body["y"] = float(y)
        if z is not None:
            body["z"] = float(z)
        if yaw is not None:
            body["yaw"] = math.radians(float(yaw))
        if not body:
            raise RuntimeError("provide x/y/z/yaw to move, or omit name to list")
        r = requests.post("http://localhost/sim/api/models/{}/pose".format(quote(name, safe="")), json=body, timeout=10)
    else:
        r = requests.get("http://localhost/sim/api/objects", timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text or "ok"},
    ]
    return tool_call_output_contents


def route():
    """Track geometry as JSON: centerline waypoints, inner/outer boundary lines
    when available, and the track bounding box. Useful when writing driving
    code.
    """
    parts = {"route": requests.get("http://localhost/sim/api/route", timeout=10).json()}
    try:
        parts["bounds"] = requests.get("http://localhost/sim/api/bounds", timeout=10).json()
    except Exception:
        pass
    tool_call_output_contents = [
        {"type": "text", "text": json.dumps(parts, ensure_ascii=False)},
    ]
    return tool_call_output_contents


def overlay(
    text: Annotated[str, Field(description="Text to show (≤300 chars).")] = None,
    ttl: Annotated[float, Field(description="Seconds until it disappears (1~3600, default 10).")] = None,
):
    """Status text on the simulator screen. With text: show it (ttl seconds
    1~3600, default 10; expires by itself — good for progress updates).
    Without arguments: read the current overlay text.
    """
    if text is not None:
        ttl = max(1.0, min(3600.0, float(10 if ttl is None else ttl)))
        r = requests.post("http://localhost/sim/api/overlay", json={"text": str(text)[:300], "ttl": ttl}, timeout=10)
    else:
        r = requests.get("http://localhost/sim/api/overlay", timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text or "ok"},
    ]
    return tool_call_output_contents


def worlds(
    world: Annotated[str, Field(description="Official world file name to switch to (e.g. 'physicar_track.world'). Omit to just list.")] = None,
    world_id: Annotated[str, Field(description="Installed custom world id (32 hex chars) to switch to. Mutually exclusive with world.")] = None,
):
    """Without arguments: list the available simulator worlds (the current one is
    marked). With world or world_id: switch to that world — POST /switch
    returns immediately, so this tool then polls the status until the new
    world is running (up to ~3 min) before returning.
    """
    if world or world_id:
        body = {"world": world} if world else {"world_id": world_id}
        r = requests.post("http://localhost/sim/api/switch", json=body, timeout=10)
        r.raise_for_status()
        text = (r.text or "ok") + " | " + _wait_ready(180.0)
    else:
        r = requests.get("http://localhost/sim/api/worlds", timeout=10)
        r.raise_for_status()
        text = r.text or "ok"
    tool_call_output_contents = [
        {"type": "text", "text": text},
    ]
    return tool_call_output_contents


def respawn():
    """Reload the current world: the vehicle and every object return to their
    start state and odometry restarts cleanly (traffic-light states are
    preserved). POST /respawn returns immediately, so this tool polls the
    status until the world is running again (~10 s) before returning. For an
    instant position-only reset use sim_reset.
    """
    r = requests.post("http://localhost/sim/api/respawn", timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": (r.text or "ok") + " | " + _wait_ready(60.0)},
    ]
    return tool_call_output_contents


def reset():
    """Instant light reset — every movable object, light and the vehicle go back
    to their start poses WITHOUT reloading the world (the response returning
    means the poses are applied). Traffic-light states are preserved and
    odometry is NOT reset — use sim_respawn when odometry must restart too.
    """
    r = requests.post("http://localhost/sim/api/reset", timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text or "ok"},
    ]
    return tool_call_output_contents


def brightness(
    value: Annotated[float, Field(description="Brightness factor 0.2–2.0 (1.0 = default). Omit to read the current value.")] = None,
):
    """Get or set the simulator scene brightness. Without value: returns the
    current factor. With value (0.2–2.0, 1.0 = default): applies instantly —
    the robot camera image and the 3D viewer darken/brighten identically (no
    restart). Persists across world switches. SIM only.
    """
    if value is not None:
        r = requests.post("http://localhost/sim/api/brightness", json={"value": float(value)}, timeout=10)
    else:
        r = requests.get("http://localhost/sim/api/brightness", timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text or "ok"},
    ]
    return tool_call_output_contents


def traffic_lights(
    name: Annotated[str, Field(description="Traffic light name (from the list).")] = None,
    state: Annotated[str, Field(description="'red' | 'green'")] = None,
):
    """Without arguments: list the world's traffic lights and their states. With
    name and state ('red' | 'green'): set that light. green→red passes through
    3 s of yellow, during which further commands are rejected (HTTP 409).
    """
    if name and state:
        r = requests.post("http://localhost/sim/api/traffic_lights/{}".format(quote(name, safe="")), json={"state": state}, timeout=10)
    elif name or state:
        raise RuntimeError("provide both name and state to set a light, or neither to list")
    else:
        r = requests.get("http://localhost/sim/api/traffic_lights", timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text or "ok"},
    ]
    return tool_call_output_contents
