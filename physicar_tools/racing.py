"""Racing tools (bundled with the extension, read-only).

Drives the Racing courses through the robot's own web API (/racing) —
the SAME runner the PhysiCar panel uses, so tool calls, panel windows and a
student's curl all see one machine-wide truth:

  - run/stop start and stop a course script (one job at a time,
    machine-wide; starting a new one replaces the old)
  - settings reads/writes a course's settings; a RUNNING script re-reads
    them live, so changes apply mid-drive within a frame. The supervised
    and reinforcement courses SHARE one profile (course "ml") and one
    model store: every training run adds a NEW model named by its content
    hash (8 hex chars) — nothing is overwritten. Which model a run drives
    with (or trains from) rides on the run() call itself; models travel
    as .pt checkpoints and the deployable ONNX is derived automatically
  - status is the eyes: what is running, recent output, the run gates
    (photo count, model presence, simulator) and the training progress

Jobs:
  rb-run    -> Rule-Based line following (course "rb")
  sl-train  -> train the shared model by Supervised Learning (labeled photos)
  rl-train  -> train it by Reinforcement Learning (the student's reward.py;
               simulator only, optional steps / worlds arguments)
  ml-run    -> drive with a trained model — the SAME job after either training
"""
import json

from typing import Annotated

import requests
from pydantic import Field

_API = "http://localhost/racing"


def status():
    """Current Racing state: which job is running, its most recent output
    lines, the run gates (photo count, model presence, simulator) and the
    training progress (SL: accuracy per epoch, RL: reward per episode).
    Call this after run() to watch progress and after a failure to read
    the reason.
    """
    r = requests.get(f"{_API}/status", timeout=10)
    r.raise_for_status()
    d = r.json()
    out = {
        "job": d.get("job"),
        "running": bool(d.get("running")),
        "log_tail": [ln for ln in (d.get("logTail") or "").split("\n") if ln.strip()][-15:],
        "gates": d.get("gates"),
    }
    if d.get("slProgress"):
        out["sl_training"] = d["slProgress"]
    if d.get("rlProgress"):
        out["rl_training"] = d["rlProgress"]
    tool_call_output_contents = [
        {"type": "text", "text": json.dumps(out, ensure_ascii=False)},
    ]
    return tool_call_output_contents


def run(
    job: Annotated[str, Field(description="Which job to start: 'rb-run' (rule-based line following), 'sl-train' (train the shared model on labeled photos), 'rl-train' (train it by reinforcement; simulator only), 'ml-run' (drive with a trained model).")],
    steps: Annotated[int, Field(description="rl-train only: total training steps (1000-300000, default 20000).")] = None,
    worlds: Annotated[str, Field(description="rl-train only: comma-separated simulator world names to rotate through (default: the current world).")] = None,
    base: Annotated[str, Field(description="sl-train / rl-train only: id of an existing model to CONTINUE training from (8-hex hash). Any model with a checkpoint works. Rides on this run only — nothing is stored.")] = None,
    model: Annotated[str, Field(description="ml-run only: id of the model to drive with (default: the newest in the store). Calling run again with a different model while ml-run is already driving swaps it LIVE — the drive keeps running, no restart.")] = None,
):
    """Start a Racing course script (exactly the panel's Run button — every
    open window sees it). Only one job runs at a time machine-wide; starting
    a new one stops the previous — EXCEPT ml-run over a running ml-run,
    which swaps the model live without restarting the drive. The run is
    detached, so Stop works from any window or tool call.
    """
    body = {"job": job}
    if steps is not None:
        body["steps"] = steps
    if worlds:
        body["worlds"] = [w.strip() for w in worlds.split(",") if w.strip()]
    if base:
        body["base"] = base
    if model:
        body["model"] = model
    r = requests.post(f"{_API}/run", json=body, timeout=30)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text + " — call status() to watch progress"},
    ]
    return tool_call_output_contents


def models():
    """The model store, newest first: id (8-hex — pass it to run() as
    model= to drive with it, or base= to continue training from it),
    created (unix seconds), size (bytes) and cont (True = has a .pt
    checkpoint so training can continue from it; False = drive-only).
    ONE store shared by both trainings. Call this before run() when the
    user asks for a specific/older model.
    """
    r = requests.get(f"{_API}/ml/models", timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text},
    ]
    return tool_call_output_contents


def stop():
    """Stop the running Racing job (SIGINT, like the panel's Stop button;
    the script stops the car on its way out). Training checkpoints
    continuously, and the runner files the LAST checkpoint into the model
    store however the run ends — a stopped (or even crashed) training still
    yields a model, named in status as lastModel.
    """
    r = requests.post(f"{_API}/stop", timeout=60)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text},
    ]
    return tool_call_output_contents


def settings(
    course: Annotated[str, Field(description="Which course: 'rb' (Rule-Based) or 'ml' (the ONE profile shared by the Supervised and Reinforcement courses; 'sl' and 'rl' are accepted as aliases of it).")],
    values: Annotated[str, Field(description="JSON object of settings to change, e.g. '{\"speed\": 0.8}' or '{\"hue\": 100}'. Pass '{\"reset\": true}' for defaults. Omit to just read. ml keys: speed, left, right, pan, tilt, mode(greedy|stochastic|mean). rb keys: speed, gain, hue, sat, val, hue_tol, sat_tol, val_tol, crop, pan, tilt.")] = None,
):
    """Read or change a course's settings (the panel's gear). The server
    clamps values to the hardware's real limits and returns the full
    effective settings. A RUNNING script applies changes live — speed,
    color range, camera angle all take effect within a frame, no restart.
    """
    if values:
        r = requests.post(f"{_API}/{course}/settings", json=json.loads(values), timeout=10)
    else:
        r = requests.get(f"{_API}/{course}/settings", timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text},
    ]
    return tool_call_output_contents
