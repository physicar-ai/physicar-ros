#!/usr/bin/env python3
#
# Copyright 2026 AICASTLE Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
PhysiCar Web Server - FastAPI Application
REST API for robot control and monitoring
"""

from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
# GZipMiddleware removed — causes buffering of SSE/MJPEG streams
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

# Authentication is handled by nginx (see deploy/).
# FastAPI listens on 127.0.0.1:8000 and trusts every request that reaches it.
from physicar_webserver.routers import health, kiosk, info, auth, deepracer
from physicar_webserver.routers import hw, calibration, network, bluetooth, myapp
from physicar_webserver.routers import pages
from physicar_webserver.routers import audio as audio_router
from physicar_webserver.routers import joy
from physicar_webserver.ros_bridge import get_ros_bridge
from physicar_webserver.state_manager import state_manager


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
    
    # Fallback: relative to source (dev mode)
    # __file__ = .../physicar_webserver/main.py -> parent.parent = package root
    return Path(__file__).resolve().parent.parent / "static"


# Static files directory
_STATIC_DIR = _get_static_dir()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan - initialize state manager with ROS bridge."""
    # Startup - Initialize StateManager with ROS node (if bridge is ready)
    bridge = get_ros_bridge()
    if bridge.is_ready:
        # Bridge already initialized by webserver_node.py
        state_manager.init(bridge._node)
        print("[PhysiCar API] State Manager initialized")
    else:
        # Standalone mode (not launched via ros2 launch)
        if bridge.init():
            print("[PhysiCar API] ROS Bridge initialized")
            state_manager.init(bridge._node)
            print("[PhysiCar API] State Manager initialized")
        else:
            print("[PhysiCar API] WARNING: ROS Bridge failed to initialize")
    
    yield
    
    # Shutdown - Cleanup ROS bridge (only if we created it)
    if not getattr(bridge, '_external_node', False):
        bridge.shutdown()
        print("[PhysiCar API] ROS Bridge shutdown")


# Create FastAPI application
app = FastAPI(
    title="PhysiCar API",
    description="REST API for PhysiCar Robot Control",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

# ── Middleware (order: last added = outermost = runs first) ──────────────

# CORS - FastAPI handles CORS independently (works with or without nginx)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["Content-Type", "Authorization", "X-Password"],
    allow_credentials=True,
    max_age=86400,
)


# Private Network Access (Chrome 92+: HTTPS pages → local HTTP device)
class PrivateNetworkAccessMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if (request.method == "OPTIONS"
                and request.headers.get("Access-Control-Request-Private-Network") == "true"):
            from starlette.responses import Response as StarletteResponse
            return StarletteResponse(status_code=204, headers={
                "Access-Control-Allow-Origin": request.headers.get("Origin", "*"),
                "Access-Control-Allow-Methods": "GET, POST, PUT, DELETE, OPTIONS",
                "Access-Control-Allow-Headers": "Content-Type, Authorization, X-Password",
                "Access-Control-Allow-Credentials": "true",
                "Access-Control-Allow-Private-Network": "true",
                "Access-Control-Max-Age": "86400",
            })
        response = await call_next(request)
        response.headers["Access-Control-Allow-Private-Network"] = "true"
        return response

app.add_middleware(PrivateNetworkAccessMiddleware)

# Block mutating Network/Settings endpoints in SIM mode (no real hardware/wifi).
# Read-only endpoints still work and return placeholder data.
_SIM_BLOCKED_PREFIXES = (
    "/network/wifi/connect",
    "/network/wifi/saved",  # DELETE on saved connections
    "/network/bluetooth/",  # All bluetooth mutating endpoints
    "/auth/password",       # POST: change device password (reboots host)
    "/kiosk/calibration",   # POST endpoints (center/reverse)
)


class SimBlockMiddleware:
    """Block mutating requests in SIM mode.

    Implemented as a pure ASGI middleware instead of BaseHTTPMiddleware to
    avoid 'No response returned' errors on long-lived StreamingResponse
    connections (SSE, MJPEG).
    """

    _MUTATING = {b"POST", b"DELETE", b"PUT", b"PATCH"}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("method", "").encode() in self._MUTATING:
            path = scope.get("path", "")
            if any(path.startswith(p) for p in _SIM_BLOCKED_PREFIXES):
                try:
                    from physicar_webserver.routers.info import _get_mode
                    if _get_mode() == "sim":
                        from starlette.responses import JSONResponse
                        resp = JSONResponse(
                            status_code=403,
                            content={"detail": "Not supported in simulation mode."},
                        )
                        await resp(scope, receive, send)
                        return
                except Exception:
                    pass
        await self.app(scope, receive, send)

app.add_middleware(SimBlockMiddleware)

# GZipMiddleware REMOVED — it buffers StreamingResponse chunks internally,
# causing massive latency for real-time SSE and MJPEG streams.
# nginx handles gzip compression for static/JSON responses instead.

# Include routers
app.include_router(health.router, tags=["Health"])
app.include_router(auth.router, tags=["Auth"])
app.include_router(info.router, tags=["Info"])

# Kiosk router (HTML page + calibration endpoints) is hardware-specific.
# In SIM mode there is no touchscreen, no servo calibration, and no host to
# manage, so we skip mounting it entirely → /kiosk and /kiosk/calibration/*
# return 404. The /info endpoint exposes "mode": "sim" so the / page can hide
# Settings → Calibration accordingly.
from physicar_webserver.sim import is_sim_mode as _is_sim_mode
if not _is_sim_mode():
    app.include_router(kiosk.router, tags=["Kiosk"])

# New API structure
app.include_router(hw.router)          # GET /states, /speed, /camera, ... + POST /speed, /audio, ...
if not _is_sim_mode():
    app.include_router(calibration.router) # /calibration (no servo in SIM)
app.include_router(network.router)     # /network
app.include_router(bluetooth.router)   # /network/bluetooth
app.include_router(myapp.router)       # /settings/myapp (host-side :5000 student web app)
if not _is_sim_mode():
    app.include_router(joy.router)     # /teleop/joy — gamepad (sim has no joy node and no kiosk UI)
app.include_router(pages.router)       # /deepracer (standalone UI page)
app.include_router(audio_router.router)  # /audio (command-based playback + WS PCM16 stream)

# DeepRacer
app.include_router(deepracer.router)

# Mount static files (for kiosk assets like JS/CSS libraries)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR), follow_symlink=True), name="static")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return FileResponse(str(_STATIC_DIR / "favicon.ico"), media_type="image/x-icon")


# NOTE: `/` is no longer served here. nginx routes `/` to code-server on
# device and non-Codespaces sim (404 in Codespaces); only the explicit
# page/API locations are proxied to this app.
