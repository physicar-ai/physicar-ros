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
POST /agent/tool/set/{name}     - Register a tool (/agent/tool/set)
POST /agent/tool/delete/{name}  - Delete a tool (/agent/tool/delete)
POST /agent/tool/reset          - Reset (/agent/tool/reset)
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

# Import tool service types (lazy import to avoid startup issues)
_tool_services_available = None


def _check_tool_services():
    """Check if tool services are available."""
    global _tool_services_available
    if _tool_services_available is None:
        try:
            from physicar_interfaces.srv import (
                ToolList, ToolCall, ToolSet, ToolDelete, ToolReset
            )
            _tool_services_available = True
        except ImportError:
            _tool_services_available = False
    return _tool_services_available


async def _call_tool_service(service_name: str, request_data: dict, timeout: float = 30.0) -> dict:
    """
    Call a tool service via ROS2.
    
    This creates a temporary service client, calls the service, and returns the result.
    """
    bridge = get_ros_bridge()
    if not bridge.is_ready:
        raise HTTPException(503, "ROS bridge not ready")
    
    if not _check_tool_services():
        raise HTTPException(503, "Tool services not available")
    
    from physicar_interfaces.srv import ToolList, ToolGet, ToolCall, ToolSet, ToolDelete, ToolReset
    
    service_types = {
        '/agent/tool/list': ToolList,
        '/agent/tool/get': ToolGet,
        '/agent/tool/call': ToolCall,
        '/agent/tool/set': ToolSet,
        '/agent/tool/delete': ToolDelete,
        '/agent/tool/reset': ToolReset,
    }
    
    srv_type = service_types.get(service_name)
    if srv_type is None:
        raise HTTPException(500, f"Unknown service: {service_name}")
    
    # Create client
    node = bridge._node
    client = node.create_client(srv_type, service_name)
    
    try:
        # Wait for service
        if not client.wait_for_service(timeout_sec=2.0):
            raise HTTPException(503, f"Service {service_name} not available")
        
        # Create request
        request = srv_type.Request()
        for key, value in request_data.items():
            if hasattr(request, key):
                setattr(request, key, value)
        
        # Call service
        loop = asyncio.get_event_loop()
        future = client.call_async(request)
        
        # Wait for result with timeout
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
    """Tool create/update request."""
    code: str = Field(..., description="Python code with def tool(...) and optional PEP 723 metadata")


# =============================================================================
# Endpoints - mirror ROS service paths: /agent/tool/{action}
# =============================================================================

@router.get("/tool/list")
async def list_tools(include_system: bool = False):
    """
    List all available tools.
    
    Args:
        include_system: Include system tools in the list. Default: False
    
    Returns:
        - tools: All tools (user + system if include_system=True)
        - system_tool_names: Names of system tools (always included for reference)
        - count: Total tool count
    """
    response = await _call_tool_service('/agent/tool/list', {'include_system': include_system})
    
    # System tool name list
    system_tool_names = ["tool_get", "tool_set", "tool_delete"]
    
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
    """
    Get details of a specific tool.
    
    Args:
        name: Tool name
        include_code: Include source code for non-system tools. Default: True
    
    Returns the tool schema including name, description, input schema, and optionally code.
    """
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
    """
    Call (execute) a tool.
    
    Example:
    ```json
    POST /agent/tool/call/itunes_preview
    {
      "action": "play",
      "query": "lofi hip hop"
    }
    ```
    
    Returns the tool execution result as an array of content blocks.
    """
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


@router.post("/tool/set/{name}")
async def set_tool(name: str, request: ToolSetRequest):
    """
    Create or update a tool.
    
    The code must contain a function named `tool` with a docstring.
    Optional: Include PEP 723 metadata for dependencies.
    
    Example:
    ```python
    # /// script
    # dependencies = ["requests"]
    # ///
    
    def tool(url: str) -> str:
        '''
        Fetch a URL.
        
        Args:
            url: The URL to fetch
            
        Returns:
            The response text
        '''
        import requests
        return requests.get(url).text
    ```
    """
    response = await _call_tool_service('/agent/tool/set', {
        'name': name,
        'code': request.code,
    })
    
    if not response.success:
        raise HTTPException(400, response.message)
    
    return {
        "success": True,
        "message": response.message,
        "name": name,
    }


@router.post("/tool/delete/{name}")
async def delete_tool(name: str):
    """
    Delete a tool.
    
    Builtin tools cannot be deleted.
    """
    response = await _call_tool_service('/agent/tool/delete', {
        'name': name,
    })
    
    if not response.success:
        raise HTTPException(404, response.message)
    
    return {
        "success": True,
        "message": response.message,
    }


@router.post("/tool/reset")
async def reset_tools():
    """
    Reset tools to builtin defaults.
    
    Deletes all custom tools and restores builtin tools.
    """
    response = await _call_tool_service('/agent/tool/reset', {})
    
    return {
        "success": response.success,
        "tool_count": response.tool_count,
    }
