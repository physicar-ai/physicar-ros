# PhysiCar custom tools — every top-level function in this script becomes a chat tool.
#
# Format (identical to the bundled Robot API / Sim API scripts — open them with View):
#   - function name            -> tool name, namespaced as custom_<name> for the AI
#                                 (def hello  ->  the AI calls custom_hello)
#   - names starting with "_"  -> ignored (private helpers)
#   - docstring                -> tool description the AI reads
#   - parameters               -> tool schema; describe them with
#       Annotated[float, Field(description="...")]  (typing.Annotated + pydantic.Field)
#   - return value             -> a LIST of contents (several allowed):
#       tool_call_output_contents = [
#           {"type": "text", "text": "done"},
#           {"type": "image", "mime": "image/jpeg", "base64": "..."},
#       ]
#       return tool_call_output_contents
#
# This script is imported by the machine's tool server and stays loaded, so
# module state (models, connections, counters) survives across calls — from
# every window. Heavy imports (cv2, torch, ...) load once per machine. Saving
# reloads it; the Reload button restarts the interpreter (fresh model weights,
# newly pip-installed libraries).
#
# Waking the AI later (long jobs, watchers) — one-shot wake tickets:
#   from pcwake import reserve, redeem
#   wid = reserve("lidar: obstacle within 0.5 m")  # inside a tool call: binds to this chat
#   # ... in a background thread, when the event fires:  redeem(wid)
#   # (or from anywhere: POST http://localhost/physicar-ext/wake with {"wake_id": wid})

# from typing import Annotated
# from pydantic import Field
# import requests
#
#
# def drive(
#     speed: Annotated[float, Field(description="m/s (-3..3, + = forward)")] = 0.0,
# ):
#     """Set the robot speed. It expires after ~1s (safety watchdog)."""
#     requests.post("http://localhost/speed", json={"value": float(speed)}, timeout=5).raise_for_status()
#     tool_call_output_contents = [
#         {"type": "text", "text": "done"},
#     ]
#     return tool_call_output_contents
#
#
# def snapshot():
#     """Front camera photo + the current speed in ONE result —
#     a tool can return SEVERAL contents (texts and images mixed)."""
#     import base64
#     jpg = requests.get("http://localhost/camera", timeout=10).content
#     spd = requests.get("http://localhost/speed", timeout=10).text
#     tool_call_output_contents = [
#         {"type": "image", "mime": "image/jpeg", "base64": base64.b64encode(jpg).decode()},
#         {"type": "text", "text": "current speed: " + spd + " m/s"},
#     ]
#     return tool_call_output_contents
