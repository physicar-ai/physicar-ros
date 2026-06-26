#!/usr/bin/env python3
#
# Copyright 2026 AICASTLE Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Agent Router - AI agent tool management.

RESTful API mirroring the ROS service paths:

GET  /agent/tool/list           - List tools (/agent/tool/list)
GET  /agent/tool/get/{name}     - Tool details (/agent/tool/get)
POST /agent/tool/call/{name}    - Run a tool (/agent/tool/call)
POST /agent/tool/set            - Save entire tools.py (/agent/tool/set)
POST /agent/tool/load           - Load tools from disk (/agent/tool/load)
POST /agent/tool/init           - Init (/agent/tool/init)
"""

import asyncio
import json
import math
import os
import time
from typing import Any, Dict, List, Optional, Set

from fastapi import APIRouter, HTTPException, Body, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from physicar_webserver.ros_bridge import get_ros_bridge

router = APIRouter(prefix="/agent", tags=["agent"])


# =============================================================================
# Shared Agent Config (global, persisted to disk, broadcast over SSE)
#
# Instructions / model / reasoning / tool selection are shared across every
# browser viewing the Home dashboard: a change made by one client is written
# to disk and pushed to all others (same pattern as cross-browser tab sync).
#
# Chat history, TTS target and volume are intentionally NOT stored here — they
# stay client-side in each browser's localStorage.
# =============================================================================

AGENT_DATA_DIR = "/opt/physicar/userdata/agent"
CONFIG_FILE = os.path.join(AGENT_DATA_DIR, "config.json")

# Default system instructions used when no config has been saved yet.
DEFAULT_INSTRUCTIONS = (
    "You are PhysiCar, a small and cute AI robot. You are an RC car equipped "
    "with a pan-tilt camera and a LiDAR sensor. Always speak in the first "
    'person (say "I", not "the car").\n\n'
    "For a motion command, call every tool the turn needs in one go. Tools run "
    "one at a time, in order. Calling a single tool per turn makes the motion "
    "stutter and feel unnatural because of the LLM response delay.\n\n"
    'e.g. "dance" Tool Calls: [drive(speed=1, steering=20), look(pan=15, '
    "tilt=10), sleep(0.5), drive(speed=1, steering=-20), look(pan=-15, "
    "tilt=-10), sleep(0.5), drive(speed=0), look()]"
)

# System meta-tools let the agent read/overwrite its own tools.py, so they are
# disabled by default. Tool selection is opt-out: every tool is enabled unless
# its name appears in disabled_tools.
DEFAULT_DISABLED_TOOLS = ["tool_code", "tool_set"]

_DEFAULT_CONFIG: Dict[str, Any] = {
    "instructions": DEFAULT_INSTRUCTIONS,
    "model": "",
    "reasoning_effort": "low",
    "disabled_tools": list(DEFAULT_DISABLED_TOOLS),
    "updated_at": 0.0,
}


class _ConfigStore:
    """In-memory shared config backed by a JSON file, with SSE broadcast."""

    def __init__(self):
        self._cfg: Optional[Dict[str, Any]] = None
        self._subscribers: Set[asyncio.Queue] = set()

    @staticmethod
    def _coerce(saved: Any) -> Dict[str, Any]:
        """Build a valid config from arbitrary parsed JSON: keep only known,
        well-typed fields and fall back to defaults for anything missing or
        malformed (wrong type, NaN, bad list elements, ...)."""
        cfg = dict(_DEFAULT_CONFIG)
        if not isinstance(saved, dict):
            return cfg
        v = saved.get("instructions")
        if isinstance(v, str):
            cfg["instructions"] = v
        v = saved.get("model")
        if isinstance(v, str):
            cfg["model"] = v
        v = saved.get("reasoning_effort")
        if isinstance(v, str):
            cfg["reasoning_effort"] = v
        v = saved.get("disabled_tools")
        if isinstance(v, list):
            cfg["disabled_tools"] = [x for x in v if isinstance(x, str)]
        v = saved.get("updated_at")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
            cfg["updated_at"] = float(v)
        return cfg

    def _load(self):
        if self._cfg is not None:
            return
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            self._cfg = self._coerce(saved)
        except Exception:
            # Missing / unreadable / non-UTF8 / corrupt / invalid JSON / etc.
            # Always recover to a usable default config; never raise.
            self._cfg = dict(_DEFAULT_CONFIG)

    def get(self) -> Dict[str, Any]:
        self._load()
        return dict(self._cfg)

    def update(self, patch: Dict[str, Any]) -> Dict[str, Any]:
        self._load()
        for k in _DEFAULT_CONFIG:
            if k == "updated_at":
                continue
            if k in patch and patch[k] is not None:
                self._cfg[k] = patch[k]
        self._cfg["updated_at"] = time.time()
        self._persist()
        self._broadcast()
        return dict(self._cfg)

    def _persist(self):
        try:
            os.makedirs(AGENT_DATA_DIR, exist_ok=True)
            tmp = CONFIG_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_FILE)
        except Exception:
            # Disk full / permission / read-only FS / etc. Keep the in-memory
            # config authoritative; persistence failure must not break the API.
            pass

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=8)
        self._subscribers.add(q)
        try:
            q.put_nowait(json.dumps(self.get()))
        except asyncio.QueueFull:
            pass
        return q

    def unsubscribe(self, q: asyncio.Queue):
        self._subscribers.discard(q)

    def _broadcast(self):
        msg = json.dumps(self._cfg)
        for q in list(self._subscribers):
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                pass


_config_store = _ConfigStore()


class ConfigUpdate(BaseModel):
    """Partial update for the shared agent config (any subset of fields)."""
    instructions: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None
    disabled_tools: Optional[List[str]] = None


async def _config_stream():
    q = _config_store.subscribe()
    try:
        while True:
            msg = await q.get()
            yield f"data: {msg}\n\n"
    except asyncio.CancelledError:
        pass
    finally:
        _config_store.unsubscribe(q)


@router.get("/config")
async def get_config(request: Request, stream: Optional[bool] = Query(None)):
    """Current shared agent config (one-shot JSON, or SSE with ?stream=true)."""
    accept = request.headers.get("accept", "")
    if stream or "text/event-stream" in accept:
        return StreamingResponse(
            _config_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return _config_store.get()


@router.post("/config")
async def update_config(patch: ConfigUpdate):
    """Update shared agent config; persists to disk and broadcasts to all."""
    return _config_store.update(patch.dict(exclude_unset=True))


@router.get("/config/default")
async def get_default_config():
    """Read-only built-in defaults (used by the UI 'Reset to default')."""
    return {
        "instructions": DEFAULT_INSTRUCTIONS,
        "disabled_tools": list(DEFAULT_DISABLED_TOOLS),
    }


# =============================================================================
# Service Client Helpers
# =============================================================================

_tool_services_available = None


def _check_tool_services():
    global _tool_services_available
    if _tool_services_available is None:
        try:
            from physicar_interfaces.srv import (
                ToolList, ToolCall, ToolSet, ToolLoad, ToolInit
            )
            _tool_services_available = True
        except ImportError:
            _tool_services_available = False
    return _tool_services_available


async def _call_tool_service(service_name: str, request_data: dict, timeout: float = 30.0) -> dict:
    bridge = get_ros_bridge()
    if not bridge.is_ready:
        raise HTTPException(503, "ROS bridge not ready")

    if not _check_tool_services():
        raise HTTPException(503, "Tool services not available")

    from physicar_interfaces.srv import ToolList, ToolGet, ToolCall, ToolSet, ToolLoad, ToolInit

    service_types = {
        '/agent/tool/list': ToolList,
        '/agent/tool/get': ToolGet,
        '/agent/tool/call': ToolCall,
        '/agent/tool/set': ToolSet,
        '/agent/tool/load': ToolLoad,
        '/agent/tool/init': ToolInit,
    }

    srv_type = service_types.get(service_name)
    if srv_type is None:
        raise HTTPException(500, f"Unknown service: {service_name}")

    node = bridge._node
    client = node.create_client(srv_type, service_name)

    try:
        if not client.wait_for_service(timeout_sec=2.0):
            raise HTTPException(503, f"Service {service_name} not available")

        request = srv_type.Request()
        for key, value in request_data.items():
            if hasattr(request, key):
                setattr(request, key, value)

        future = client.call_async(request)

        start = asyncio.get_event_loop().time()
        while not future.done():
            await asyncio.sleep(0.05)
            if asyncio.get_event_loop().time() - start > timeout:
                raise HTTPException(504, "Service call timeout")

        return future.result()
    finally:
        node.destroy_client(client)


# =============================================================================
# Request/Response Models
# =============================================================================

class ToolSetRequest(BaseModel):
    """tools.py overwrite request."""
    code: str = Field(..., description="Full Python source code for tools.py")


# =============================================================================
# Endpoints
# =============================================================================

@router.get("/tool/list")
async def list_tools(include_system: bool = False):
    """List all available tools."""
    response = await _call_tool_service('/agent/tool/list', {'include_system': include_system})

    system_tool_names = ["tool_code", "tool_set"]

    try:
        tools = json.loads(response.tools_json)
        return {
            "tools": tools,
            "system_tool_names": system_tool_names,
            "count": len(tools)
        }
    except json.JSONDecodeError:
        return {"tools": [], "system_tool_names": system_tool_names, "count": 0}


@router.get("/tool/get/{name}")
async def get_tool(name: str, include_code: bool = True):
    """Get details of a specific tool."""
    response = await _call_tool_service('/agent/tool/get', {
        'name': name,
        'include_code': include_code,
    })

    if not response.found:
        try:
            error_info = json.loads(response.info_json)
            raise HTTPException(404, error_info.get('error', f"Tool '{name}' not found"))
        except json.JSONDecodeError:
            raise HTTPException(404, f"Tool '{name}' not found")

    try:
        return json.loads(response.info_json)
    except json.JSONDecodeError:
        raise HTTPException(500, "Failed to parse tool info")


@router.post("/tool/call/{name}")
async def call_tool(name: str, arguments: Dict[str, Any] = Body(default={})):
    """Call (execute) a tool."""
    response = await _call_tool_service('/agent/tool/call', {
        'name': name,
        'args_json': json.dumps(arguments, ensure_ascii=False),
    })

    try:
        result = json.loads(response.result_json)
        return {
            "success": response.success,
            "result": result,
        }
    except json.JSONDecodeError:
        return {
            "success": response.success,
            "result": [{"type": "text", "text": response.result_json}],
        }


@router.post("/tool/set")
async def set_tools(request: ToolSetRequest):
    """Save the entire tools.py file and reload.

    The code must contain at least one public function (no leading underscore).
    Each public function becomes a tool.
    """
    response = await _call_tool_service('/agent/tool/set', {
        'code': request.code,
    })

    if not response.success:
        raise HTTPException(400, response.message)

    return {
        "success": True,
        "message": response.message,
        "tool_count": response.tool_count,
    }


TOOLS_FILE = "/opt/physicar/userdata/agent/tools.py"


@router.get("/tool/file")
async def get_tools_file():
    """Get tools source code from last successful load."""
    response = await _call_tool_service('/agent/tool/get', {'name': ''})
    if not response.found:
        raise HTTPException(404, "No tools loaded")
    try:
        return json.loads(response.info_json)
    except json.JSONDecodeError:
        raise HTTPException(500, "Failed to parse response")


@router.post("/tool/load")
async def load_tools():
    """Load tools from disk (reimport tools.py after external edits)."""
    response = await _call_tool_service('/agent/tool/load', {})

    return {
        "success": response.success,
        "message": response.message,
        "tool_count": response.tool_count,
    }


@router.post("/tool/init")
async def init_tools():
    """Init tools to builtin defaults."""
    response = await _call_tool_service('/agent/tool/init', {})

    return {
        "success": response.success,
        "message": response.message,
        "tool_count": response.tool_count,
    }
