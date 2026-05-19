"""DeepRacer model management and autonomous driving control (service API example)."""

import json
from typing import Annotated, Optional
from pydantic import Field

from physicar_agent import topic, service, action, text, image

from physicar_interfaces.srv import (
    DeepracerStatus,
    DeepracerLoadModel,
    DeepracerUnloadModel,
    DeepracerControl,
)


def tool(
    action: Annotated[str, Field(description="One of: status, load, unload, start, stop")],
    model_name: Annotated[Optional[str], Field(description="Model name (for action=load or unload)")] = None,
) -> dict:
    """Manage DeepRacer reinforcement-learning models and autonomous driving.

    Models are stored in /home/physicar/physicar_ws/userdata/deepracer/models/<model_name>/.

    Examples:
        tool("status")
        tool("load", model_name="my-model")
        tool("start")
        tool("stop")
        tool("unload", model_name="my-model")
    """

    if action == "status":
        resp = service('/deepracer/status', DeepracerStatus.Request())
        return {
            "model_loaded": resp.model_loaded,
            "inference_running": resp.inference_running,
            "model_name": resp.model_name,
            "action_count": resp.action_count,
            "inference_rate_hz": resp.inference_rate,
            "inference_count": resp.inference_count,
            "speed_percent": resp.speed_percent,
        }

    if action == "load":
        if not model_name:
            return {"success": False, "message": "model_name required"}
        req = DeepracerLoadModel.Request()
        req.model_name = model_name
        resp = service('/deepracer/load_model', req)
        result = {"success": resp.success, "message": resp.message}
        if resp.success and resp.action_space_json:
            result["action_space"] = json.loads(resp.action_space_json)
        return result

    if action == "unload":
        req = DeepracerUnloadModel.Request()
        req.model_name = model_name or ""
        resp = service('/deepracer/unload_model', req)
        return {"success": resp.success, "message": resp.message}

    if action == "start":
        req = DeepracerControl.Request()
        req.start = True
        resp = service('/deepracer/control', req)
        return {"success": resp.success, "message": resp.message}

    if action == "stop":
        req = DeepracerControl.Request()
        req.start = False
        resp = service('/deepracer/control', req)
        return {"success": resp.success, "message": resp.message}

    return {"success": False, "message": f"Unknown action: {action}. Use: status, load, unload, start, stop"}
