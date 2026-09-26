#!/usr/bin/env python3
"""PhysiCar tool-call server (FastAPI, 127.0.0.1:9004).

Serves the AI chat's Python tools the same way physicar_webserver (:8000) and
sim_api (:9003) serve the robot/sim: one machine-resident HTTP service. The
VSCode extension is a thin client: GET /tools for the list, POST /tools/<name>
to run one, POST /reload to restart the interpreter.

NEVER-DIE DESIGN — the server core must survive anything user code does:
  - the FastAPI app has ZERO import-time dependency on tool scripts; a broken
    custom_tools.py cannot prevent startup
  - tool scripts are (re)imported inside try/except with LAST-GOOD semantics:
    a failed reimport keeps serving the previous working module and only
    records the error (shown red in the extension's TOOLS panel)
  - every tool call runs on a worker thread with a timeout; any exception is
    returned as data, never raised through the server
  - /reload exits the process AFTER responding — the launch supervisor
    (respawn=True / systemd Restart) brings it back with a fresh interpreter,
    which is also how replaced model weights and newly pip-installed libraries
    are picked up
  - process crash for any other reason: same supervisor restarts it

Modules served (script = section = tool-name namespace):
  robot.py                          -> robot_*   (this directory; read-only)
  sim.py                            -> sim_*     (this directory; only when the sim stack exists)
  /opt/physicar/userdata/custom_tools.py -> custom_*  (user-owned; created on demand)

A tool call may carry a `session` id (the chat window that called it). Tool
code can obtain a wake handle bound to that session (see pcwake.py) to start
an automatic chat turn later — long jobs, watchers, timers.
"""
import concurrent.futures
import importlib.util
import inspect
import json
import os
import sys
import threading
import time
import traceback
import typing

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import uvicorn

HERE = os.path.dirname(os.path.abspath(__file__))
USERDATA = os.environ.get("PHYSICAR_USERDATA", "/opt/physicar/userdata")
CUSTOM_PATH = os.path.join(USERDATA, "custom_tools.py")
PORT = int(os.environ.get("PHYSICAR_TOOLS_PORT", "9004"))
CALL_TIMEOUT = 660          # > robot_speed's 600s server-side cap
MAX_WORKERS = 16
OUTPUT_MAX_CHARS = 40000    # same cap as the extension's truncate()

sys.path.insert(0, HERE)    # tool scripts may import pcwake / sibling helpers


# Which machine this is — set explicitly by the bringup launch ("sim"/"real").
# The only per-profile difference: a real car has no simulator, so the sim
# section is not served there.
PROFILE = os.environ.get("PHYSICAR_PROFILE", "sim")

SECTIONS = {
    "robot": {"path": os.path.join(HERE, "robot.py"), "enabled": lambda: True},
    "sim": {"path": os.path.join(HERE, "sim.py"), "enabled": lambda: PROFILE != "real"},
    "utils": {"path": os.path.join(HERE, "utils.py"), "enabled": lambda: True},
    "racing": {"path": os.path.join(HERE, "racing.py"), "enabled": lambda: True},
    "custom": {"path": CUSTOM_PATH, "enabled": lambda: True},
}

# section -> {"module": module|None, "error": str|None, "mtime": float|None}
# LAST-GOOD: "module" only ever moves forward to a successfully imported one.
_state = {name: {"module": None, "error": None, "mtime": None} for name in SECTIONS}
_state_lock = threading.Lock()
_pool = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS)
_busy = set()               # api names currently executing (for /health)


def _brief(tb):
    lines = [l for l in str(tb).strip().splitlines() if l.strip()]
    return lines[-1] if lines else "load failed"


def _load(section):
    """(Re)import one tool script. Never raises; failure keeps the old module."""
    spec = SECTIONS[section]
    path = spec["path"]
    st = _state[section]
    if not os.path.isfile(path):
        st["module"], st["error"], st["mtime"] = None, None, None
        return
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return
    if st["mtime"] == mtime and (st["module"] is not None or st["error"] is not None):
        return   # unchanged since the last (successful OR failed) attempt
    try:
        mod_spec = importlib.util.spec_from_file_location("pctool_" + section, path)
        mod = importlib.util.module_from_spec(mod_spec)
        mod_spec.loader.exec_module(mod)
        st["module"], st["error"], st["mtime"] = mod, None, mtime
        # The replaced module (old weights, buffers) just lost its last strong
        # reference — collect NOW so RSS follows edits instead of creeping.
        # (Swapped LIBRARY versions still need /reload: sys.modules caches by
        # name and only a fresh interpreter truly forgets them.)
        import gc
        gc.collect()
    except Exception:
        # keep st["module"] (last good) — only record what went wrong
        st["error"], st["mtime"] = "import error: " + _brief(traceback.format_exc()), mtime


def _refresh():
    """mtime-driven reload sweep — cheap (one stat per script per request)."""
    with _state_lock:
        for name, spec in SECTIONS.items():
            if spec["enabled"]():
                _load(name)


def _param_info(annotation):
    """(json_type, description) from a plain or Annotated[...] annotation."""
    type_map = {int: "number", float: "number", str: "string", bool: "boolean",
                list: "array", dict: "object"}
    desc = None
    if typing.get_origin(annotation) is typing.Annotated:
        args = typing.get_args(annotation)
        annotation = args[0]
        for extra in args[1:]:
            d = getattr(extra, "description", None)
            if d:
                desc = d
    return type_map.get(annotation, "string"), desc


def _tools_of(section):
    """Public functions of a loaded module -> tool descriptors."""
    mod = _state[section]["module"]
    if mod is None:
        return []
    out = []
    for name, fn in vars(mod).items():
        if name.startswith("_") or not inspect.isfunction(fn):
            continue
        if getattr(fn, "__module__", None) != mod.__name__:
            continue   # imported helpers (e.g. requests.get) are not tools
        props = []
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            continue
        for p in sig.parameters.values():
            if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                continue
            jt, desc = _param_info(p.annotation) if p.annotation is not p.empty else ("string", None)
            prop = {"name": p.name, "type": jt, "required": p.default is p.empty}
            if desc:
                prop["description"] = desc
            props.append(prop)
        out.append({
            "name": name,
            "api": section + "_" + name,
            "section": section,
            "description": (inspect.getdoc(fn) or "Custom tool {}.".format(name)).strip(),
            "properties": props,
        })
    return out


def _serialize(result):
    """Content list. The documented contract is a list of content dicts —
    {"type": "text", "text": ...} / {"type": "image", "mime": ..., "base64": ...} —
    but plain str/dict/list returns and .text / .mime+.base64 objects are
    accepted too and converted."""
    items = result if isinstance(result, (list, tuple)) else [result]
    out = []
    for it in items:
        if it is None:
            continue
        if isinstance(it, dict) and it.get("base64") and it.get("mime"):
            out.append({"type": "image", "mime": it["mime"], "base64": it["base64"]})
            continue
        if isinstance(it, dict) and it.get("type") == "text" and "text" in it:
            out.append({"type": "text", "text": str(it["text"])[:OUTPUT_MAX_CHARS]})
            continue
        b64 = getattr(it, "base64", None)
        mime = getattr(it, "mime", None)
        txt = getattr(it, "text", None)
        if b64 and mime:
            out.append({"type": "image", "mime": mime, "base64": b64})
        elif txt is not None:
            out.append({"type": "text", "text": str(txt)[:OUTPUT_MAX_CHARS]})
        elif isinstance(it, (dict, list)):
            out.append({"type": "text", "text": json.dumps(it, ensure_ascii=False)[:OUTPUT_MAX_CHARS]})
        else:
            out.append({"type": "text", "text": str(it)[:OUTPUT_MAX_CHARS]})
    return out or [{"type": "text", "text": "done"}]


app = FastAPI(title="physicar_tools", docs_url=None, redoc_url=None)


@app.get("/tools")
def list_tools():
    _refresh()
    tools, errors, sections = [], {}, []
    with _state_lock:
        for name, spec in SECTIONS.items():
            if not spec["enabled"]():
                continue
            sections.append(name)
            tools.extend(_tools_of(name))
            if _state[name]["error"]:
                errors[name] = _state[name]["error"]
    # "sections" is the authority the client renders from — section names,
    # order, and which ones exist on this machine all come from here.
    return {"tools": tools, "errors": errors, "sections": sections, "custom_path": CUSTOM_PATH}


@app.post("/tools/{api_name}")
async def call_tool(api_name: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    args = body.get("args") or {}
    session = str(body.get("session") or "")
    _refresh()
    section, _, func = api_name.partition("_")
    with _state_lock:
        mod = _state.get(section, {}).get("module")
    fn = getattr(mod, func, None) if mod else None
    if not (SECTIONS.get(section) and fn and not func.startswith("_") and inspect.isfunction(fn)):
        return JSONResponse({"ok": False, "error": "unknown tool: " + api_name}, status_code=404)

    def run():
        import pcwake
        pcwake._session.value = session   # wake handles default to the calling chat
        _busy.add(api_name)
        try:
            return {"ok": True, "contents": _serialize(fn(**args))}
        except TypeError as e:
            # bad/missing arguments — tell the model what the function accepts
            params = []
            for p in inspect.signature(fn).parameters.values():
                params.append(p.name if p.default is p.empty else "{}={!r}".format(p.name, p.default))
            return {"ok": False, "error": "{}\nsignature: {}({})".format(e, func, ", ".join(params))}
        except Exception:
            return {"ok": False, "error": traceback.format_exc(limit=8)[-2000:]}
        finally:
            _busy.discard(api_name)

    loop = __import__("asyncio").get_event_loop()
    try:
        return await __import__("asyncio").wait_for(loop.run_in_executor(_pool, run), timeout=CALL_TIMEOUT)
    except __import__("asyncio").TimeoutError:
        return {"ok": False, "error": "tool timed out after {}s (the call may still be running — POST /reload to clear a wedged interpreter)".format(CALL_TIMEOUT)}


@app.post("/wake")
async def redeem_wake(request: Request):
    """Redeem a one-shot wake ticket (reserved by the utils_wake_reserve tool).
    POST http://localhost/wake with {"wake_id": "...", "note": "optional"} —
    the id rides in the body, never in the URL (URLs land in access logs)."""
    import pcwake
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    wake_id = str(body.get("wake_id") or "")
    note = str(body.get("note") or "")[:500]
    if not pcwake.redeem(wake_id, note):
        return JSONResponse({"ok": False, "error": "unknown, expired or already-used wake_id"}, status_code=404)
    return {"ok": True}


@app.post("/wake/status")
async def wake_status(request: Request):
    """Ticket state over HTTP (id in the body, like /wake — never in URLs/logs).
    {"wake_id": "..."}  -> {"state": "pending" | "redeemed" | "unknown", ...}
    {"session": "..."}  -> {"outstanding": [...]} for that chat session."""
    import pcwake
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    if body.get("wake_id"):
        return pcwake.status(str(body["wake_id"]))
    if body.get("session"):
        return {"outstanding": pcwake.tickets(str(body["session"]))}
    return JSONResponse({"ok": False, "error": "pass wake_id or session"}, status_code=400)


@app.get("/wakes/{session}")
async def poll_wakes(session: str, wait: float = 0.0):
    """Long-poll pending wake messages for one chat session (in-memory only).
    Blocks up to `wait` seconds (max 30) for the first message, then drains."""
    import pcwake
    loop = __import__("asyncio").get_event_loop()
    wakes = await loop.run_in_executor(None, pcwake.drain, session, min(max(wait, 0.0), 30.0))
    return {"wakes": wakes}


@app.post("/reload")
def reload_server():
    """Fresh interpreter: picks up new libraries and replaced model weights and
    frees everything the old modules held. The supervisor respawns us."""
    threading.Timer(0.3, lambda: os._exit(0)).start()
    return {"ok": True, "restarting": True}


def _rss_mb():
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024)
    except OSError:
        pass
    return None


@app.get("/health")
def health():
    _refresh()
    with _state_lock:
        mods = {n: {"loaded": _state[n]["module"] is not None, "error": _state[n]["error"]}
                for n, s in SECTIONS.items() if s["enabled"]()}
    return {"ok": True, "pid": os.getpid(), "rss_mb": _rss_mb(), "busy": sorted(_busy), "modules": mods}


def main():
    _refresh()   # warm-up: failures land in _state, never abort startup
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
