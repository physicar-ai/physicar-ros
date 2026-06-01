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
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Body
from pydantic import BaseModel, Field

from physicar_webserver.ros_bridge import get_ros_bridge

router = APIRouter(prefix="/agent", tags=["agent"])


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
        "tool_count": response.tool_count,
    }


@router.post("/tool/init")
async def init_tools():
    """Init tools to builtin defaults."""
    response = await _call_tool_service('/agent/tool/init', {})

    return {
        "success": response.success,
        "tool_count": response.tool_count,
    }
