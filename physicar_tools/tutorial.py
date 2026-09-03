"""Tutorial tools (bundled with the extension, read-only).

Drives the /tutorial page's courses — the SAME web runner and settings the
page uses, so everything stays in sync with every open browser window:

  - run/stop start and stop a course script (one job runs at a time,
    machine-wide; starting a new one replaces the old)
  - settings reads/writes a course's settings.json; a RUNNING script
    re-reads it live, so changes apply mid-drive within a frame
  - status is the eyes: what is running, how it ended, recent output,
    the run gates (photo count, model presence) and the training curve

Jobs / courses:
  sl-train, sl-run  -> course "sl" (Racing - Supervised Learning)
  rb-run            -> course "rb" (Racing - Rule-Based line following)
  rl-train, rl-run  -> course "rl" (Racing - Reinforcement Learning;
                       rl-train runs in the simulator only and takes
                       optional steps / worlds arguments)
"""
import json

from typing import Annotated

import requests
from pydantic import Field

_API = "http://localhost/tutorial/api"


def _snapshot():
    """First event of the run stream — a complete state snapshot."""
    with requests.get(f"{_API}/run/events", stream=True, timeout=10) as r:
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):
            if line and line.startswith("data:"):
                return json.loads(line[5:])
    raise RuntimeError("no event received from the run stream")


def status():
    """Current tutorial state: which job is running (or the last one's exit
    code), its most recent output lines, the run gates (photo count, whether
    model.onnx exists, MYAPP viewer up) and — after a training run — the
    training curve (SL: accuracy per epoch, RL: reward per episode). Call
    this after run() to watch progress and after a failure to read the
    reason.
    """
    m = _snapshot()
    out = {
        "job": m.get("job"),
        "running": bool(m.get("running")),
        "exit_code": m.get("exit"),
        "log_tail": [ln for ln in (m.get("lines") or []) if ln.strip()][-15:],
        "gates": m.get("gates"),
    }
    if m.get("train"):
        out["training"] = m["train"]
    if m.get("rl_train"):
        out["rl_training"] = m["rl_train"]
    tool_call_output_contents = [
        {"type": "text", "text": json.dumps(out, ensure_ascii=False)},
    ]
    return tool_call_output_contents


def run(
    job: Annotated[str, Field(description="Which course script to start: 'sl-train' (train the supervised model), 'sl-run' (drive with the trained model), 'rb-run' (rule-based line following), 'rl-train' (train the reinforcement model; simulator only), 'rl-run' (drive with the reinforcement model).")],
    steps: Annotated[int, Field(description="rl-train only: total training steps (1000-300000, default 10000).")] = None,
    worlds: Annotated[str, Field(description="rl-train only: comma-separated simulator world names to rotate through (default: the current world).")] = None,
):
    """Start a tutorial course script via the web runner (exactly the page's
    Run button — every open tutorial window sees it). Only one job runs at a
    time; starting a new one stops the previous. May be refused by a gate
    (too few photos, missing model) — status() explains why.
    """
    body = {"job": job}
    if steps is not None:
        body["steps"] = steps
    if worlds:
        body["worlds"] = [w.strip() for w in worlds.split(",") if w.strip()]
    r = requests.post(f"{_API}/run", json=body, timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text + " — call status() to watch progress"},
    ]
    return tool_call_output_contents


def stop():
    """Stop the currently running tutorial job (SIGINT, like the page's Stop
    button; the script stops the car on its way out).
    """
    r = requests.post(f"{_API}/run/stop", timeout=10)
    r.raise_for_status()
    tool_call_output_contents = [
        {"type": "text", "text": r.text},
    ]
    return tool_call_output_contents


def settings(
    course: Annotated[str, Field(description="Which course: 'sl' (Supervised Learning), 'rb' (Rule-Based) or 'rl' (Reinforcement Learning).")],
    values: Annotated[str, Field(description="JSON object of settings to change, e.g. '{\"speed\": 0.8}' or '{\"hue\": 100, \"hue_tol\": 12}'. Pass '{\"reset\": true}' for defaults. Omit to just read. sl/rl keys: speed, left, right, pan, tilt, mode(greedy|stochastic|mean). rb keys: speed, gain, hue, sat, val, hue_tol, sat_tol, val_tol, crop, pan, tilt.")] = None,
):
    """Read or change a course's settings (the page's right-hand panel). The
    server clamps values to the hardware's real limits and returns the full
    effective settings. A RUNNING script applies changes live — speed, color
    range, camera angle all take effect within a frame, no restart.
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
