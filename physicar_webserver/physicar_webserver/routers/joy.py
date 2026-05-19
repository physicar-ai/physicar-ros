#!/usr/bin/env python3
"""
Joy Teleop Mapping Router.

The joystick teleop node (physicar_joy_teleop) exposes two ROS services:

    /physicar_joy_teleop/get_mapping
    /physicar_joy_teleop/set_mapping

This router wraps them as REST endpoints so the kiosk / web UI can read
and update the mapping without giving end users shell access.

GET  /joy/mapping         - return current mapping + source
POST /joy/mapping         - update one or more keys; persist with save=true
POST /joy/mapping/reset   - revert to YAML defaults (deletes saved JSON file)

Mapping keys (see physicar_interfaces/srv/SetJoyMapping.srv):

    int   axis_speed | axis_steering | axis_pan | axis_tilt
    int   deadman_button | estop_button | center_camera_button
    float max_speed | max_steering | max_pan | max_tilt
    float deadzone | rate
    bool  invert_speed | invert_steering | invert_pan | invert_tilt
"""

import json
import os
import subprocess
import asyncio
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import Any, Dict, Optional

from physicar_webserver.ros_bridge import get_ros_bridge
from physicar_webserver.state_manager import get_state_manager

logger = logging.getLogger(__name__)


def _wants_stream(request: Request, stream: Optional[bool]) -> bool:
    if stream is True:
        return True
    accept = request.headers.get("accept", "")
    return "text/event-stream" in accept


router = APIRouter(prefix="/teleop/joy", tags=["teleop-joy"])


# =============================================================================
# Broadcaster — keeps multiple browser windows in sync without polling.
#
# Same shape as kiosk._CalibrationBroadcaster: an in-process queue fan-out
# with an idle poll loop and a `bump()` method that POST handlers call so
# subscribers see updates within ~50 ms of the change instead of waiting for
# the next 5 s poll.
#
# Three streams are exposed:
#   /joy/mapping?stream=true   - mapping JSON + source
#   /joy/enabled?stream=true   - {"enabled": bool, "received": bool}
#   /joy/state?stream=true     - everything: mapping + enabled + connected
# =============================================================================

_JOY_CONNECTED_WINDOW = 1.5  # seconds; /joy SSE silence longer than this -> disconnected


def _read_joy_device_name() -> Optional[str]:
    """Return a human-readable name for the joystick joy_node is using.

    joy_node opens /dev/input/js0 by default, and the kernel exposes
    each joystick's product name at /sys/class/input/jsX/device/name
    (e.g. "Xbox Wireless Controller", "Sony Interactive Entertainment
    Wireless Controller").  We just read js0 — that's the same device
    joy_node is publishing from.

    Returns None if no joystick is plugged in.
    """
    import glob
    paths = sorted(glob.glob("/sys/class/input/js*/device/name"))
    if not paths:
        return None
    try:
        with open(paths[0], "r") as f:
            name = f.read().strip()
        return name or None
    except OSError:
        return None


class _JoyBroadcaster:
    """Polls the joy node + bridge every POLL_INTERVAL seconds and fans the
    payload out to all subscribed SSE queues.  Only emits when the payload
    has actually changed."""

    POLL_INTERVAL = 5.0

    def __init__(self):
        self._last_payload: Optional[str] = None
        self._subscribers: set = set()
        self._task: Optional[asyncio.Task] = None
        self._wake = asyncio.Event()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        self._subscribers.add(q)
        if self._last_payload is not None:
            try:
                q.put_nowait(self._last_payload)
            except asyncio.QueueFull:
                pass
        self._ensure_running()
        # Schedule an immediate refresh so a brand-new subscriber gets the
        # latest state even if `_last_payload` was None.
        self.bump()
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def bump(self) -> None:
        """Wake the poll loop so the next tick happens immediately."""
        try:
            self._wake.set()
        except RuntimeError:
            pass

    def _ensure_running(self) -> None:
        if self._task is None or self._task.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._task = loop.create_task(self._run())

    async def _build_payload(self) -> Dict[str, Any]:
        """Collect mapping + enabled + connected into one snapshot."""
        bridge = get_ros_bridge()
        sm = get_state_manager()

        mapping_data: Dict[str, Any] = {}
        source = ""
        if bridge and bridge.is_ready:
            try:
                resp = await bridge.get_joy_mapping()
                mapping_data = resp.get('mapping', {}) or {}
                source = resp.get('source', '') or ''
            except Exception as exc:  # noqa: BLE001
                logger.debug("get_joy_mapping failed in broadcaster: %s", exc)

        enabled = False
        received = False
        if bridge and bridge.is_ready:
            try:
                status = bridge.get_joy_status() or {}
                enabled = bool(status.get('enabled'))
                received = bool(status.get('received'))
            except Exception as exc:  # noqa: BLE001
                logger.debug("get_joy_status failed in broadcaster: %s", exc)

        # Connected = a /joy message arrived recently.
        age = sm.buffer_age("joy") if sm else None
        connected = bool(age is not None and age < _JOY_CONNECTED_WINDOW)
        # Surface the kernel's device name (e.g. "Xbox Wireless Controller")
        # so the UI can show *which* gamepad is bound rather than just Yes/No.
        device = _read_joy_device_name() if connected else None

        return {
            "mapping": mapping_data,
            "source": source,
            "enabled": enabled,
            "received": received,
            "connected": connected,
            "device": device,
        }

    async def _emit(self, payload: Dict[str, Any]) -> None:
        # Drop the connected/age fields when comparing so we don't fan out
        # 1 Hz updates just because the freshness counter ticked.  Connected
        # transitions still get pushed because the boolean flips.
        text = json.dumps(payload, sort_keys=True)
        if text == self._last_payload:
            return
        self._last_payload = text
        dead = []
        for q in list(self._subscribers):
            try:
                q.put_nowait(text)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)

    async def _run(self) -> None:
        try:
            while True:
                try:
                    payload = await self._build_payload()
                    await self._emit(payload)
                except Exception as exc:  # noqa: BLE001
                    logger.debug("joy broadcaster tick failed: %s", exc)

                self._wake.clear()
                if not self._subscribers:
                    # No-one listening: idle until next bump().
                    await self._wake.wait()
                    continue
                try:
                    await asyncio.wait_for(self._wake.wait(), timeout=self.POLL_INTERVAL)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            pass


_broadcaster: Optional[_JoyBroadcaster] = None


def _get_joy_broadcaster() -> _JoyBroadcaster:
    global _broadcaster
    if _broadcaster is None:
        _broadcaster = _JoyBroadcaster()
    return _broadcaster


def _bump_joy() -> None:
    """Trigger an immediate re-broadcast.  Called from POST handlers so
    other browser windows see the change without waiting for the 5 s
    poll."""
    try:
        _get_joy_broadcaster().bump()
    except Exception as exc:  # noqa: BLE001
        logger.debug("_bump_joy failed: %s", exc)


async def _stream_joy(filter_keys: Optional[set] = None):
    """SSE generator.  If filter_keys is given, only those fields are
    forwarded (and dedup re-applied client-side)."""
    bcaster = _get_joy_broadcaster()
    q = bcaster.subscribe()
    last_filtered: Optional[str] = None
    try:
        while True:
            text = await q.get()
            if filter_keys is None:
                yield f"data: {text}\n\n"
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                continue
            sub = {k: obj.get(k) for k in filter_keys}
            sub_text = json.dumps(sub, sort_keys=True)
            if sub_text == last_filtered:
                continue
            last_filtered = sub_text
            yield f"data: {sub_text}\n\n"
    except asyncio.CancelledError:
        pass
    finally:
        bcaster.unsubscribe(q)


# Background ticker so the `connected` flag stays current even when no
# POST happens (i.e. the joystick is unplugged silently).  We only need
# to bump once a second; the broadcaster will swallow it as a no-op
# unless `connected` actually flipped.
_ticker_task: Optional[asyncio.Task] = None


async def _connected_ticker() -> None:
    while True:
        await asyncio.sleep(1.0)
        try:
            _get_joy_broadcaster().bump()
        except Exception:  # noqa: BLE001
            pass


def _ensure_ticker() -> None:
    global _ticker_task
    if _ticker_task is None or _ticker_task.done():
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        _ticker_task = loop.create_task(_connected_ticker())


# Same key/type registry as the ROS node; kept in sync manually because we
# can't import from the rclpy package at request time without pulling rclpy
# into a thread.  Update both in lock-step.
_INT_KEYS = {
    'axis_speed', 'axis_steering', 'axis_pan', 'axis_tilt',
    'deadman_button', 'estop_button', 'center_camera_button',
    'camera_assist_button',
}
_FLOAT_KEYS = {
    'max_speed', 'max_steering', 'max_pan', 'max_tilt', 'deadzone', 'rate',
}
_BOOL_KEYS = {
    'invert_speed', 'invert_steering', 'invert_pan', 'invert_tilt',
}
_ALL_KEYS = _INT_KEYS | _FLOAT_KEYS | _BOOL_KEYS

MAPPING_FILE = '/home/physicar/physicar_ws/userdata/joy_mapping.json'


class JoyMappingUpdateRequest(BaseModel):
    """Request body for POST /joy/mapping.

    Either ``key`` + ``value`` (single update) or ``mapping`` (bulk) must
    be supplied.  Setting ``save=true`` persists the result to
    /home/physicar/physicar_ws/userdata/joy_mapping.json so it survives reboots.
    """
    key: Optional[str] = Field(default=None, description="Mapping key to update")
    value: Optional[Any] = Field(
        default=None, description="New value for the key"
    )
    mapping: Optional[Dict[str, Any]] = Field(
        default=None, description="Bulk update; merged with current mapping"
    )
    save: bool = Field(default=False, description="Persist to /home/physicar/physicar_ws/userdata/joy_mapping.json")


@router.get("/mapping")
async def get_joy_mapping(
    request: Request,
    stream: Optional[bool] = Query(None, description="Enable SSE streaming"),
):
    """Return the live joystick teleop mapping.

    Pass ``?stream=true`` (or ``Accept: text/event-stream``) to receive
    real-time updates whenever any browser window changes the mapping —
    used by the kiosk / settings Joystick panel so multiple open windows
    stay in sync.
    """
    bridge = get_ros_bridge()
    if not bridge.is_ready:
        raise HTTPException(status_code=503, detail="ROS bridge not ready")

    if _wants_stream(request, stream):
        _ensure_ticker()
        return StreamingResponse(
            _stream_joy(filter_keys={"mapping", "source"}),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        return await bridge.get_joy_mapping()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/mapping")
async def set_joy_mapping(req: JoyMappingUpdateRequest):
    """Update one or more mapping keys.

    Two call shapes are accepted:

    1. Single key  -> ``{"key": "deadman_button", "value": 5, "save": true}``
    2. Bulk update -> ``{"mapping": {"axis_speed": 1, "invert_speed": true}, "save": false}``
    """
    bridge = get_ros_bridge()
    if not bridge.is_ready:
        raise HTTPException(status_code=503, detail="ROS bridge not ready")

    try:
        if req.mapping is not None:
            # Bulk update path — validate keys client-side for a friendlier 400
            unknown = set(req.mapping) - _ALL_KEYS
            if unknown:
                raise HTTPException(
                    status_code=400,
                    detail=f"Unknown mapping keys: {sorted(unknown)}",
                )
            result = await bridge.set_joy_mapping(
                mapping_json=json.dumps(req.mapping),
                save=req.save,
            )
            _bump_joy()
            return result

        if not req.key:
            raise HTTPException(
                status_code=400,
                detail="Either 'key'+'value' or 'mapping' must be provided",
            )
        if req.key not in _ALL_KEYS:
            raise HTTPException(status_code=400, detail=f"Unknown key: {req.key}")
        if req.value is None:
            raise HTTPException(status_code=400, detail="Missing 'value'")

        kwargs: Dict[str, Any] = {'key': req.key, 'save': req.save}
        if req.key in _INT_KEYS:
            kwargs['int_value'] = int(req.value)
        elif req.key in _FLOAT_KEYS:
            kwargs['float_value'] = float(req.value)
        else:  # bool
            kwargs['bool_value'] = bool(req.value)

        result = await bridge.set_joy_mapping(**kwargs)
        _bump_joy()
        return result
    except HTTPException:
        raise
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))


@router.post("/mapping/reset")
async def reset_joy_mapping():
    """Delete the persisted JSON file and restart the joy node so the
    yaml defaults take effect immediately.

    The teleop node loads /home/physicar/physicar_ws/userdata/joy_mapping.json at startup; if
    we just delete the file the running node keeps its in-memory mapping
    until the next reboot.  To make the reset take effect immediately we
    also send SIGTERM to physicar_joy_teleop — robot.launch.py /
    sim.launch.py both run it with respawn=True, respawn_delay=2.0, so
    it comes back ~2s later loading the yaml defaults (no file present).
    """
    import signal

    removed = False
    try:
        if os.path.isfile(MAPPING_FILE):
            os.remove(MAPPING_FILE)
            removed = True
    except OSError as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    # Kill the joy node so launch's respawn picks up yaml defaults.
    # Best-effort: even if we can't find the process the file deletion
    # alone still resets behaviour on the next boot.
    restarted = False
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", "joy_teleop_node"], text=True
        ).strip().split()
        for pid in out:
            try:
                os.kill(int(pid), signal.SIGTERM)
                restarted = True
            except (ValueError, ProcessLookupError, PermissionError):
                pass
    except (subprocess.CalledProcessError, FileNotFoundError):
        # pgrep returns 1 when nothing matches; nothing to do.
        pass

    parts = []
    parts.append(f"Removed {MAPPING_FILE}" if removed else f"{MAPPING_FILE} did not exist")
    if restarted:
        parts.append("restarting joy node (defaults apply in ~2s)")
    _bump_joy()
    return {"success": True, "restarted": restarted, "message": "; ".join(parts)}


# =============================================================================
# /joy/enabled  - master toggle
# /joy/status   - joy-specific status (currently just `enabled`)
# =============================================================================

class JoyEnabledRequest(BaseModel):
    """Request body for POST /joy/enabled."""
    enabled: bool = Field(..., description="True to engage joy_teleop, false to disable")


@router.get("/status")
async def get_joy_status():
    """Return joy-specific status (currently just the `enabled` flag).

    Generic teleop lock state (drive_engaged, camera_engaged, ...) is now served
    by ``GET /teleop/status`` regardless of which teleop source (joy,
    keyboard, web, ...) is currently asserting it.
    """
    bridge = get_ros_bridge()
    if not bridge.is_ready:
        raise HTTPException(status_code=503, detail="ROS bridge not ready")
    return bridge.get_joy_status()


@router.get("/enabled")
async def get_joy_enabled(
    request: Request,
    stream: Optional[bool] = Query(None, description="Enable SSE streaming"),
):
    """Convenience accessor for the `enabled` flag from joy status.

    Pass ``?stream=true`` to receive real-time updates whenever any
    window toggles the joystick on/off, plus a `connected` flag derived
    from /joy publish freshness.
    """
    bridge = get_ros_bridge()
    if not bridge.is_ready:
        raise HTTPException(status_code=503, detail="ROS bridge not ready")

    if _wants_stream(request, stream):
        _ensure_ticker()
        return StreamingResponse(
            _stream_joy(filter_keys={"enabled", "received", "connected", "device"}),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    status = bridge.get_joy_status()
    sm = get_state_manager()
    age = sm.buffer_age("joy") if sm else None
    connected = bool(age is not None and age < _JOY_CONNECTED_WINDOW)
    return {
        "enabled": bool(status.get('enabled')),
        "received": bool(status.get('received')),
        "connected": connected,
        "device": _read_joy_device_name() if connected else None,
    }


@router.post("/enabled")
async def set_joy_enabled(req: JoyEnabledRequest):
    """Engage or disengage the joy_teleop node at runtime.

    When disabled the node publishes nothing on `/teleop/speed`,
    `/teleop/steering`, `/teleop/camera/pan`, `/teleop/camera/tilt` and
    reports drive_engaged/camera_engaged=false in TeleopStatus.  Other publishers
    (REST /control, deepracer) regain full control immediately without
    needing to be restarted."""
    bridge = get_ros_bridge()
    if not bridge.is_ready:
        raise HTTPException(status_code=503, detail="ROS bridge not ready")
    try:
        result = await bridge.set_joy_enabled(req.enabled)
        _bump_joy()
        return result
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=str(exc))


# =============================================================================
# /joy/state - combined snapshot (mapping + enabled + connected) over a single
# SSE so the kiosk/settings panel only needs one EventSource for everything.
# =============================================================================

@router.get("/state")
async def get_joy_state(
    request: Request,
    stream: Optional[bool] = Query(None, description="Enable SSE streaming"),
):
    """Combined joystick state snapshot.

    Returns ``{mapping, source, enabled, received, connected}``.  With
    ``?stream=true`` (or ``Accept: text/event-stream``) the same payload
    is pushed whenever any field changes — used by the joy panel UI so
    it only opens a single SSE connection per window.
    """
    bridge = get_ros_bridge()
    if not bridge.is_ready:
        raise HTTPException(status_code=503, detail="ROS bridge not ready")

    if _wants_stream(request, stream):
        _ensure_ticker()
        return StreamingResponse(
            _stream_joy(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return await _get_joy_broadcaster()._build_payload()


# =============================================================================
# /joy/raw - live Joy message (axes + buttons), useful for showing the user
# which axis/button index moves when they touch the controller, so they can
# wire up the mapping UI without guessing SDL indexes.
# =============================================================================

@router.get("/raw")
async def get_joy_raw(
    request: Request,
    stream: Optional[bool] = Query(None, description="Enable SSE streaming"),
):
    """Return the latest /joy message (axes + buttons).

    Use ``?stream=true`` (or ``Accept: text/event-stream``) for continuous
    updates — the joy mapping UI uses this to highlight whichever axis or
    button is moving in real time.
    """
    sm = get_state_manager()

    if _wants_stream(request, stream):
        return StreamingResponse(
            sm.stream_sse("joy"),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    data = sm.get_once("joy")
    if data is None:
        return JSONResponse(
            {"error": "No joy data", "status": "waiting"}, status_code=503
        )
    return data
