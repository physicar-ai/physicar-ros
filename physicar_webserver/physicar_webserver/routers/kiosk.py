#!/usr/bin/env python3
"""
Kiosk router - Web UI for PhysiCar control.
"""

import re
import json
import asyncio as _asyncio
from typing import Optional
from pathlib import Path
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from physicar_webserver.ros_bridge import get_ros_bridge

router = APIRouter()


def _is_sim() -> bool:
    """Check if running in SIM mode (set by sim.launch.py)."""
    try:
        bridge = get_ros_bridge()
        node = bridge._node
        if node:
            if not node.has_parameter('sim_mode'):
                node.declare_parameter('sim_mode', False)
            return node.get_parameter('sim_mode').value
    except Exception:
        pass
    return False


def _get_static_dir() -> Path:
    """Get static directory path - works with both dev and installed package."""
    # Try ament_index first (installed package)
    try:
        from ament_index_python.packages import get_package_share_directory
        share_dir = get_package_share_directory('physicar_webserver')
        static_dir = Path(share_dir) / "static"
        if static_dir.exists():
            return static_dir
    except Exception:
        pass
    
    # Fallback: relative to source (dev mode with symlink-install)
    # __file__ = routers/kiosk.py -> parent.parent.parent = package root
    return Path(__file__).resolve().parent.parent.parent / "static"

_STATIC_DIR = _get_static_dir()


def _load_html(filename: str) -> str:
    """Load HTML file from static directory, injecting cache-busting ?v=<mtime>
    query strings into local /static/*.css and /static/*.js references so that
    browsers automatically pick up changes without a hard reload.
    """
    html_path = _STATIC_DIR / filename
    if not html_path.exists():
        raise HTTPException(status_code=500, detail=f"HTML file not found: {filename} (searched: {_STATIC_DIR})")
    html = html_path.read_text(encoding="utf-8")

    def _bust(match: re.Match) -> str:
        prefix, path, suffix = match.group(1), match.group(2), match.group(3)
        asset = _STATIC_DIR / path.lstrip("/").removeprefix("static/")
        try:
            v = int(asset.stat().st_mtime)
        except OSError:
            return match.group(0)
        sep = "&" if "?" in path else "?"
        return f"{prefix}{path}{sep}v={v}{suffix}"

    # Match href="/static/..." and src="/static/..." for .css and .js files
    html = re.sub(
        r'(href=["\'])(/static/[^"\']+\.css)(["\'])',
        _bust, html
    )
    html = re.sub(
        r'(src=["\'])(/static/[^"\']+\.js)(["\'])',
        _bust, html
    )
    return html


@router.get("/kiosk", response_class=HTMLResponse)
async def kiosk():
    """
    Kiosk settings page - WiFi, Network info, Password display.
    Optimized for 4.3" touchscreen (800x480).
    
    Authentication is handled by AuthMiddleware:
    - Browser: redirects to /login if not authenticated
    - API: returns 401
    - Local (port 8000): no auth required
    """
    return _load_html("kiosk.html")


# ─────────────────────────────────────────────────────────────────────────────
# Calibration API
# ─────────────────────────────────────────────────────────────────────────────

class CalibrationCenterRequest(BaseModel):
    channel: str  # 'steering', 'pan', 'tilt'
    center_value: float  # -15 ~ 15


class CalibrationReverseRequest(BaseModel):
    reverse_direction: bool


class CalibrationEmergencyRequest(BaseModel):
    emergency_enabled: bool


@router.get("/kiosk/calibration")
async def get_calibration(
    request: Request,
    stream: Optional[bool] = Query(None, description="Enable SSE streaming"),
):
    """
    Get current calibration values via ROS service.

    Default: one-shot JSON.
    With ?stream=true or Accept: text/event-stream — SSE stream that pushes
    on change. POSTs to calibration endpoints trigger an immediate broadcast
    so other tabs/clients update in <100ms.
    """
    accept = request.headers.get("accept", "")
    wants_stream = bool(stream) or ("text/event-stream" in accept)

    if wants_stream:
        return StreamingResponse(
            _stream_calibration(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        bridge = get_ros_bridge()
        if not bridge.is_ready:
            raise HTTPException(status_code=503, detail="ROS bridge not ready")
        
        result = await bridge.get_calibration()
        if result.get('success'):
            return {
                "success": True,
                "steering_center": result.get('steering_center', 0.0),
                "pan_center": result.get('pan_center', 0.0),
                "tilt_center": result.get('tilt_center', 0.0),
                "reverse_direction": result.get('reverse_direction', False),
                "emergency_enabled": result.get('emergency_enabled', True),
            }
        else:
            raise HTTPException(status_code=500, detail=result.get('message', 'Failed to get calibration'))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ROS service error: {str(e)}")


@router.post("/kiosk/calibration/center")
async def set_calibration_center(request: CalibrationCenterRequest):
    """
    Set center offset for steering, pan, or tilt.
    """
    if request.channel not in ['steering', 'pan', 'tilt']:
        raise HTTPException(status_code=400, detail="Invalid channel. Use 'steering', 'pan', or 'tilt'")
    
    if not -15.0 <= request.center_value <= 15.0:
        raise HTTPException(status_code=400, detail="center_value must be between -15 and 15")
    
    try:
        bridge = get_ros_bridge()
        if not bridge.is_ready:
            raise HTTPException(status_code=503, detail="ROS bridge not ready")
        
        # Use channel_center format (e.g., 'steering_center') for center-only updates
        result = await bridge.set_calibration(
            channel=f"{request.channel}_center",
            center_value=request.center_value,
            save=True
        )
        if result.get('success'):
            _bump_calibration()
            return {"success": True, "message": result.get('message', 'OK')}
        else:
            raise HTTPException(status_code=500, detail=result.get('message', 'Failed to set calibration'))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ROS service error: {str(e)}")


@router.post("/kiosk/calibration/reverse")
async def set_calibration_reverse(request: CalibrationReverseRequest):
    """
    Set ESC reverse direction.
    """
    try:
        bridge = get_ros_bridge()
        if not bridge.is_ready:
            raise HTTPException(status_code=503, detail="ROS bridge not ready")
        
        result = await bridge.set_calibration(
            channel="reverse",
            bool_value=request.reverse_direction,
            save=True
        )
        if result.get('success'):
            _bump_calibration()
            return {"success": True, "message": result.get('message', 'OK')}
        else:
            raise HTTPException(status_code=500, detail=result.get('message', 'Failed to set reverse'))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ROS service error: {str(e)}")


@router.post("/kiosk/calibration/emergency")
async def set_calibration_emergency(request: CalibrationEmergencyRequest):
    """
    Set emergency stop enabled.
    """
    try:
        bridge = get_ros_bridge()
        if not bridge.is_ready:
            raise HTTPException(status_code=503, detail="ROS bridge not ready")
        
        result = await bridge.set_calibration(
            channel="emergency",
            bool_value=request.emergency_enabled,
            save=True
        )
        if result.get('success'):
            _bump_calibration()
            return {"success": True, "message": result.get('message', 'OK')}
        else:
            raise HTTPException(status_code=500, detail=result.get('message', 'Failed to set emergency'))
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ROS service error: {str(e)}")


# ─────────────────────────────────────────────────────────────────────────────
# Calibration SSE Broadcaster
# ─────────────────────────────────────────────────────────────────────────────

class _CalibrationBroadcaster:
    """
    Single shared poller for calibration. Many SSE clients can subscribe;
    only one ROS service call per poll. `bump()` forces an immediate poll.
    """
    POLL_INTERVAL = 5.0  # seconds (relaxed; bump() handles instant updates)

    def __init__(self):
        self._last_payload: Optional[str] = None
        self._subscribers: set = set()
        self._task: Optional[_asyncio.Task] = None
        self._wake = _asyncio.Event()

    def subscribe(self) -> "_asyncio.Queue":
        q: _asyncio.Queue = _asyncio.Queue(maxsize=8)
        self._subscribers.add(q)
        if self._last_payload is not None:
            try:
                q.put_nowait(self._last_payload)
            except _asyncio.QueueFull:
                pass
        self._ensure_running()
        return q

    def unsubscribe(self, q):
        self._subscribers.discard(q)

    def bump(self):
        """Force immediate re-poll (call after state-changing operations)."""
        self._wake.set()

    def _ensure_running(self):
        if self._task is None or self._task.done():
            self._task = _asyncio.create_task(self._run())

    def _emit(self, payload: str):
        for q in list(self._subscribers):
            try:
                q.put_nowait(payload)
            except _asyncio.QueueFull:
                pass

    async def _run(self):
        bridge = get_ros_bridge()
        try:
            while self._subscribers:
                try:
                    if bridge.is_ready:
                        result = await bridge.get_calibration()
                        if result.get('success'):
                            data = {
                                "success": True,
                                "steering_center": result.get('steering_center', 0.0),
                                "pan_center": result.get('pan_center', 0.0),
                                "tilt_center": result.get('tilt_center', 0.0),
                                "reverse_direction": result.get('reverse_direction', False),
                                "emergency_enabled": result.get('emergency_enabled', True),
                            }
                            payload = json.dumps(data, sort_keys=True)
                            if payload != self._last_payload:
                                self._last_payload = payload
                                self._emit(payload)
                except Exception as e:
                    self._emit(json.dumps({"success": False, "error": str(e)}))
                try:
                    await _asyncio.wait_for(self._wake.wait(), timeout=self.POLL_INTERVAL)
                except _asyncio.TimeoutError:
                    pass
                self._wake.clear()
        except _asyncio.CancelledError:
            pass


_calibration_broadcaster: Optional[_CalibrationBroadcaster] = None


def _get_calibration_broadcaster() -> _CalibrationBroadcaster:
    global _calibration_broadcaster
    if _calibration_broadcaster is None:
        _calibration_broadcaster = _CalibrationBroadcaster()
    return _calibration_broadcaster


def _bump_calibration():
    """Trigger an immediate calibration re-poll for all SSE subscribers."""
    try:
        _get_calibration_broadcaster().bump()
    except Exception:
        pass


async def _stream_calibration():
    """SSE generator: subscribe to shared broadcaster."""
    bcaster = _get_calibration_broadcaster()
    q = bcaster.subscribe()
    try:
        while True:
            payload = await q.get()
            yield f"data: {payload}\n\n"
    except _asyncio.CancelledError:
        pass
    finally:
        bcaster.unsubscribe(q)
