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
Tutorial router — /tutorial page and the APIs behind it.

Everything happens web-side (no terminal): the ▶ buttons run the
tutorial_assets/ scripts IN PLACE via the job runner — no working copies —
and outputs (photos, models) go to /opt/physicar/userdata/tutorial/ —
the scripts use relative paths and the runner supplies that cwd.

    GET  /tutorial                    the page
    GET  /tutorial/api/code/{id}      "write it yourself" sample (live values)
    GET  /tutorial/api/sl/meta        ACTIONS + photo counts
    GET/POST /tutorial/api/sl/settings   gear settings (userdata settings.json)
    POST /tutorial/api/sl/camera      point the camera at the model viewpoint
    POST /tutorial/api/sl/label       button labeling: capture -> save -> drive
    GET  /tutorial/api/sl/photos_all  photo lists (plus /photos, /photo)
    POST /tutorial/api/sl/photo/delete, /sl/clear   dataset curation
    GET  /tutorial/api/sl/events      SSE: photo/settings sync across windows
    POST /tutorial/api/run, /run/stop; GET /run/events   web job runner
    POST /tutorial/api/sl/infer       inference.py live view (per frame)
    GET/POST /tutorial/api/sl/model   download / import model.onnx

Training progress travels as a FILE (train_progress.json, written by
train.py each epoch, carried on /run/events) — not as an API.

Button labeling writes into the very data folder train.py reads
(userdata tutorial data home), so the friendly UI and the real training
code stay one pipeline.
"""

import asyncio
import base64
import glob
import json
import math
import os
import re
import signal
import socket
import subprocess
import threading
import time
from datetime import datetime

import requests
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel

from physicar_webserver.routers.kiosk import _load_html
from physicar_webserver.sim import is_sim_mode

router = APIRouter(tags=["Tutorial"])

_SELF = "http://127.0.0.1:8000"

# Cell code lives IN the repo and runs in place — no working copies, exactly
# what the page displays. Only the OUTPUTS (photos, models) go to the data
# home below, which the code states openly in its first lines.
# realpath: the install space symlinks individual files back into src, so
# __file__-relative paths must be resolved before walking up the tree.
_ASSETS = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
    "tutorial_assets")
# The scripts use bare relative paths (they read as natural standalone
# samples) — the RUNNER decides where they run: every spawn below passes
# the course data home as cwd, so outputs always land there.
_SL_DIR = "/opt/physicar/userdata/tutorial/deep-racing-sl"   # photos + models
_RB_DIR = "/opt/physicar/userdata/tutorial/racing-rule-based"   # settings only
_RL_DIR = "/opt/physicar/userdata/tutorial/deep-racing-rl"   # model + settings
os.makedirs(_SL_DIR, exist_ok=True)
os.makedirs(_RB_DIR, exist_ok=True)
os.makedirs(_RL_DIR, exist_ok=True)

# code cells — id -> path under tutorial_assets/
_CODE_FILES = {
    "sl-labeling": "deep-racing-sl/labeling.py",
    "sl-train": "deep-racing-sl/train.py",
    "sl-run": "deep-racing-sl/inference.py",
    "rb-run": "racing-rule-based/follow_line.py",
    "rl-train": "deep-racing-rl/train.py",
    "rl-run": "deep-racing-sl/inference.py",
}


@router.get("/tutorial", response_class=HTMLResponse)
async def tutorial_page():
    return _load_html("tutorial.html")


def _fmt_actions(actions):
    body = "\n".join('    "%s": {"speed": %s, "steering": %s},'
                      % (k, v["speed"], v["steering"])
                      for k, v in actions.items())
    return "ACTIONS = {\n%s\n}" % body


@router.get("/tutorial/api/code/{file_id}")
def tutorial_code(file_id: str):
    """Sample code for the "write it yourself" blocks. The samples read as
    standalone scripts: the settings-override plumbing is stripped and the
    ACTIONS literal shows the CURRENT effective values, so changing Speed in
    the UI changes the sample too."""
    name = _CODE_FILES.get(file_id)
    if not name:
        raise HTTPException(status_code=404, detail="unknown file id")
    path = os.path.join(_ASSETS, name)
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        raise HTTPException(status_code=404, detail="file not found: " + name)
    st = _read_settings()
    text = re.sub(
        r"\n# The tutorial page's settings \(gear\).*?except \(OSError, ValueError\):\n    pass\n",
        "\n", text, flags=re.S)
    text = re.sub(
        r"\n        # The tutorial page's settings \(gear\).*?except \(OSError, ValueError\):\n            pass\n",
        "\n", text, flags=re.S)
    text = re.sub(r"(?m)^SETTINGS = .*\n(settings_mtime = None\n)?", "", text)
    # the live-view POSTs are tutorial-page wiring, not part of a standalone
    # script — hide them too
    text = re.sub(
        r"\n        # feed the live view on the [^\n]*\n.*?"
        r"except requests\.RequestException:\n            pass\n",
        "\n", text, flags=re.S)
    text = re.sub(
        r"\n# The tutorial page's MYAPP view.*?"
        r"threading\.Thread\(target=_serve_view, daemon=True\)\.start\(\)\n",
        "\n", text, flags=re.S)
    if file_id.startswith("sl-"):
        meta, error = _sl_actions()
        if not error:
            text = re.sub(r"ACTIONS\s*=\s*\{.*?\n\}", _fmt_actions(meta["actions"]),
                          text, count=1, flags=re.S)
    if file_id == "sl-run":
        text = re.sub(r'(?m)^MODE = .*$', 'MODE = "%s"' % st["mode"], text, count=1)
    if file_id in ("rl-train", "rl-run"):
        rl = _read_rl_settings()
        acts = {"left": {"speed": rl["speed"], "steering": abs(rl["left"])},
                "straight": {"speed": rl["speed"], "steering": 0.0},
                "right": {"speed": rl["speed"], "steering": -abs(rl["right"])}}
        text = re.sub(r"ACTIONS\s*=\s*\{.*?\n\}", _fmt_actions(acts),
                      text, count=1, flags=re.S)
    if file_id == "rl-run":
        rl = _read_rl_settings()
        text = re.sub(r'(?m)^MODE = .*$', 'MODE = "%s"' % rl["mode"], text, count=1)
    if file_id == "rl-train":
        # runner-argument plumbing (steps/worlds) and the dashboard feed are
        # tutorial wiring — hide them like the rest
        text = re.sub(
            r"\n# The Train page picks this run's length.*?"
            r"sys\.argv\[2\]\.split[^\n]*\n",
            "\n", text, flags=re.S)
        text = re.sub(
            r"\n# The tutorial page's dashboard reads this file.*?"
            r"os\.replace\([^\n]*train_progress[^\n]*\n",
            "\n", text, flags=re.S)
        text = re.sub(r"(?m)^\s*report\([^\n]*\n", "", text)
        for mod in ("json", "os"):
            text = re.sub(r"(?m)^import %s\n" % mod, "", text)
    if file_id == "rb-run":
        rb = _read_rb_settings()
        text = re.sub(r"(?m)^(SPEED = )[0-9.]+", r"\g<1>%s" % rb["speed"], text)
        text = re.sub(r"(?m)^(STEER_GAIN = )[0-9.]+", r"\g<1>%s" % rb["gain"], text)
        text = re.sub(r"(?m)^(CROP_TOP = )[0-9.]+", r"\g<1>%s" % rb["crop"], text)
        text = re.sub(r"(?m)^(LINE_HSV = )\([0-9, ]+\)",
                      r"\g<1>(%d, %d, %d)" % (rb["hue"], rb["sat"], rb["val"]), text)
        text = re.sub(r"(?m)^(LINE_TOL = )\([0-9, ]+\)",
                      r"\g<1>(%d, %d, %d)" % (rb["hue_tol"], rb["sat_tol"], rb["val_tol"]),
                      text)
        # image-cadence counter feeds the (stripped) live view — hide it too
        text = re.sub(r"(?m)^frame = 0\n", "", text)
        text = re.sub(r"(?m)^        frame \+= 1\n", "", text)
        for mod in ("base64", "json", "os"):
            text = re.sub(r"(?m)^import %s\n" % mod, "", text)
    text = re.sub(r"\n\n\n+", "\n\n", text)   # stripped blocks leave gaps
    return {"id": file_id, "path": name, "text": text}


# ---- Deep Racing SL: button labeling --------------------------------------
# ACTIONS lives in the student's editable model.py. It is read in a
# subprocess (a broken edit must not take the webserver down) and cached by
# mtime (the import pulls in torch, which costs a few seconds once).

_actions_cache = {"mtime": None, "value": None, "error": None}


def _warm_actions():
    """First ACTIONS read imports torch (~1 min on this class of machine) —
    do it once in the background at startup so the labeling page never
    makes the first visitor sit through it."""
    try:
        _sl_actions()
    except Exception:  # noqa: BLE001 — warming is best-effort
        pass


threading.Timer(3.0, _warm_actions).start()


def _sl_actions():
    path = os.path.join(_ASSETS, "deep-racing-sl", "train.py")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None, "train.py not found in tutorial assets"
    try:
        # settings.json overrides are part of what model.py evaluates to
        mtime = (mtime, os.path.getmtime(os.path.join(_SL_DIR, "settings.json")))
    except OSError:
        mtime = (mtime, 0)
    if _actions_cache["mtime"] == mtime:
        return _actions_cache["value"], _actions_cache["error"]
    code = (
        "import json, sys\n"
        "sys.path.insert(0, {dir!r})\n"
        "import train\n"
        "print(json.dumps({{'actions': train.ACTIONS,\n"
        "  'w': int(getattr(train, 'CAMERA_W', 160)),\n"
        "  'h': int(getattr(train, 'CAMERA_H', 120)),\n"
        "  'pan': float(getattr(train, 'CAMERA_PAN', 0.0)),\n"
        "  'tilt': float(getattr(train, 'CAMERA_TILT', -15.0))}}))\n"
    ).format(dir=os.path.join(_ASSETS, "deep-racing-sl"))
    value, error = None, None
    try:
        out = subprocess.run(
            ["/usr/bin/python3", "-c", code],
            capture_output=True, text=True, timeout=120, cwd=_SL_DIR,
            env={**os.environ, "PYTHONPYCACHEPREFIX": "/opt/physicar/pycache"},
        )
        if out.returncode == 0:
            value = json.loads(out.stdout.strip().splitlines()[-1])
        else:
            tail = (out.stderr or "").strip().splitlines()
            error = "train.py failed to load" + (": " + tail[-1] if tail else "")
    except subprocess.TimeoutExpired:
        error = "train.py took too long to load"
    except Exception as e:  # noqa: BLE001 — any parse failure reads the same to the page
        error = "train.py could not be read: " + str(e)
    if error is None:
        _actions_cache.update({"mtime": mtime, "value": value, "error": None})
    else:
        # NEVER cache a failure under the mtime key — a cold-start torch
        # import that once exceeded the timeout would pin the error until
        # the file changed. Remember nothing so the next call retries.
        _actions_cache.update({"mtime": None, "value": None, "error": error})
    return value, error


def _sl_counts(actions):
    return {k: len(glob.glob(os.path.join(_SL_DIR, "data", k, "*.jpg")))
            for k in actions}


@router.get("/tutorial/api/sl/meta")
def sl_meta():
    meta, error = _sl_actions()
    if error:
        return {"error": error}
    return {"actions": meta["actions"], "pan": meta["pan"], "tilt": meta["tilt"],
            "counts": _sl_counts(meta["actions"]), "settings": _read_settings()}


# One settings.json per course holds every tunable. The gear popovers
# read/write it; the scripts apply it as overrides (live, mtime-gated); the
# change stream pings every open window so settings sync like the photos do.
_SETTINGS_PATH = os.path.join(_SL_DIR, "settings.json")
_SETTINGS_DEFAULTS = {"speed": 0.5, "left": 20.0, "right": 20.0, "pan": 0.0,
                      "tilt": -15.0, "mode": "greedy"}
_MODES = ("greedy", "stochastic", "mean")
_SETTINGS_LIMITS = {"speed": (0.1, 3.0), "left": (0.0, 20.0), "right": (0.0, 20.0),
                    "pan": (-30.0, 30.0), "tilt": (-30.0, 30.0)}

_RB_SETTINGS_PATH = os.path.join(_RB_DIR, "settings.json")
_RB_DEFAULTS = {"speed": 0.5, "gain": 20.0, "hue": 18, "sat": 255, "val": 255,
                "hue_tol": 8, "sat_tol": 90, "val_tol": 90, "crop": 0.5,
                "pan": 0.0, "tilt": -15.0}
_RB_LIMITS = {"speed": (0.1, 3.0), "gain": (0.0, 60.0),
              "hue": (0, 179), "sat": (0, 255), "val": (0, 255),
              "hue_tol": (0, 90), "sat_tol": (0, 255), "val_tol": (0, 255),
              "crop": (0.0, 0.9), "pan": (-30.0, 30.0), "tilt": (-30.0, 30.0)}
_RB_INTS = ("hue", "sat", "val", "hue_tol", "sat_tol", "val_tol")


def _settings_load(path, defaults, limits, enums=(), ints=()):
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        cfg = {}
    out = dict(defaults)
    _settings_apply(out, cfg, defaults, limits, enums, ints)
    return out


def _settings_apply(out, src, defaults, limits, enums, ints):
    for k in defaults:
        if k not in src:
            continue
        if k in enums:
            if str(src[k]) in enums[k]:
                out[k] = str(src[k])
            continue
        try:
            lo, hi = limits[k]
            out[k] = max(lo, min(hi, float(src[k])))
            if k in ints:
                out[k] = int(out[k])
        except (TypeError, ValueError):
            pass


def _settings_store(req, path, defaults, limits, enums=(), ints=()):
    if req.get("reset"):
        cur = dict(defaults)
    else:
        cur = _settings_load(path, defaults, limits, enums, ints)
        _settings_apply(cur, req, defaults, limits, enums, ints)
    with open(path + ".tmp", "w") as f:
        json.dump(cur, f)
    os.replace(path + ".tmp", path)   # never leave a half-written file
    return cur


def _read_settings():
    return _settings_load(_SETTINGS_PATH, _SETTINGS_DEFAULTS, _SETTINGS_LIMITS,
                          enums={"mode": _MODES})


def _read_rb_settings():
    return _settings_load(_RB_SETTINGS_PATH, _RB_DEFAULTS, _RB_LIMITS,
                          ints=_RB_INTS)


_RL_SETTINGS_PATH = os.path.join(_RL_DIR, "settings.json")


def _read_rl_settings():
    return _settings_load(_RL_SETTINGS_PATH, _SETTINGS_DEFAULTS,
                          _SETTINGS_LIMITS, enums={"mode": _MODES})


@router.get("/tutorial/api/sl/settings")
def sl_settings():
    return _read_settings()


@router.post("/tutorial/api/sl/settings")
def sl_settings_write(req: dict):
    return _settings_store(req, _SETTINGS_PATH, _SETTINGS_DEFAULTS,
                           _SETTINGS_LIMITS, enums={"mode": _MODES})


@router.get("/tutorial/api/rl/settings")
def rl_settings():
    return _read_rl_settings()


@router.post("/tutorial/api/rl/settings")
def rl_settings_write(req: dict):
    return _settings_store(req, _RL_SETTINGS_PATH, _SETTINGS_DEFAULTS,
                           _SETTINGS_LIMITS, enums={"mode": _MODES})


@router.get("/tutorial/api/rb/settings")
def rb_settings():
    return _read_rb_settings()


@router.post("/tutorial/api/rb/settings")
def rb_settings_write(req: dict):
    return _settings_store(req, _RB_SETTINGS_PATH, _RB_DEFAULTS, _RB_LIMITS,
                           ints=_RB_INTS)


@router.post("/tutorial/api/sl/camera")
def sl_camera():
    """Point the camera at the model viewpoint (pan/tilt from settings —
    the same values the labeling/inference scripts use). Never goes
    through _sl_actions(): it must answer instantly even right after a
    settings change (whose new mtime would force the torch subprocess)."""
    st = _read_settings()
    requests.post(_SELF + "/camera/pan",
                  json={"value": math.radians(float(st.get("pan", 0.0)))}, timeout=2)
    requests.post(_SELF + "/camera/tilt",
                  json={"value": math.radians(float(st.get("tilt", -15.0)))}, timeout=2)
    return {"ok": True}


class LabelReq(BaseModel):
    action: str


@router.post("/tutorial/api/sl/label")
def sl_label(req: LabelReq):
    """One labeling beat, exactly like a 1_labeling.py key press: the photo at
    this moment is the question (x), the pressed button is the answer (y),
    then the car performs the action (the ~1s command watchdog stops it)."""
    meta, error = _sl_actions()
    if error:
        raise HTTPException(status_code=500, detail=error)
    action = meta["actions"].get(req.action)
    if action is None:
        raise HTTPException(status_code=400, detail="unknown action: " + req.action)
    r = requests.get(_SELF + "/camera",
                     params={"width": meta.get("w", 160), "height": meta.get("h", 120)},
                     timeout=2)
    jpg = r.content
    if r.status_code != 200 or not jpg.startswith(b"\xff\xd8"):
        # not a JPEG (camera still warming up etc.) — never save garbage labels
        raise HTTPException(status_code=503, detail="camera not ready — try again in a moment")
    d = os.path.join(_SL_DIR, "data", req.action)
    os.makedirs(d, exist_ok=True)
    name = datetime.now().strftime("ui_%Y%m%d_%H%M%S_%f") + ".jpg"
    with open(os.path.join(d, name), "wb") as f:
        f.write(jpg)
    _sl_bump()
    requests.post(_SELF + "/speed", json={"value": float(action["speed"])}, timeout=2)
    requests.post(_SELF + "/steering",
                  json={"value": math.radians(float(action["steering"]))}, timeout=2)
    return {"saved": name, "count": len(glob.glob(os.path.join(d, "*.jpg"))),
            "preview": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}


# ---- dataset curation (Teachable-Machine style) ---------------------------
# The page shows every class's photos and lets the student throw bad ones
# out. Only files inside data/<known action>/ are ever touched, and names
# are validated hard — these endpoints delete student data on request.

_PHOTO_NAME = re.compile(r"^[A-Za-z0-9._-]+\.jpg$")


def _photo_dir(action):
    meta, error = _sl_actions()
    if error:
        raise HTTPException(status_code=500, detail=error)
    if action not in meta["actions"]:
        raise HTTPException(status_code=400, detail="unknown action: " + action)
    return os.path.join(_SL_DIR, "data", action)


# dataset change stream: every mutation (any browser's label/delete/clear —
# they all pass through this server) bumps the version, and the data dir
# mtimes catch script-side writes too. SSE pushes a ping whenever the
# combined signature moves; clients refetch the gallery on ping.
_sl_version = 0
_sl_vlock = threading.Lock()


def _sl_bump():
    global _sl_version
    with _sl_vlock:
        _sl_version += 1


def _sl_signature():
    # PURE stat calls — this runs every 0.5s per subscriber and must NEVER
    # go through _sl_actions(): its cache key includes the settings mtime,
    # so a settings change would spawn the torch-importing subprocess and
    # stall every ping for tens of seconds.
    sig = [str(_sl_version)]
    try:
        sig.append("s:%s" % os.path.getmtime(_SETTINGS_PATH))
    except OSError:
        sig.append("s:0")
    try:
        sig.append("r:%s" % os.path.getmtime(_RB_SETTINGS_PATH))
    except OSError:
        sig.append("r:0")
    try:
        sig.append("q:%s" % os.path.getmtime(_RL_SETTINGS_PATH))
    except OSError:
        sig.append("q:0")
    data = os.path.join(_SL_DIR, "data")
    try:
        names = sorted(os.listdir(data))
    except OSError:
        names = []
    for k in names:
        try:
            sig.append("%s:%s" % (k, os.path.getmtime(os.path.join(data, k))))
        except OSError:
            sig.append(k + ":0")
    return "|".join(sig)


@router.get("/tutorial/api/sl/events")
async def sl_events():
    async def gen():
        last = None
        while True:
            sig = await asyncio.to_thread(_sl_signature)
            if sig != last:
                last = sig
                yield "data: changed\n\n"
            await asyncio.sleep(0.5)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@router.get("/tutorial/api/sl/photos_all")
def sl_photos_all(limit: int = 60):
    """Every class's gallery in one call — the page polls this to stay in
    sync with other browsers working on the same machine."""
    meta, error = _sl_actions()
    if error:
        return {"error": error}
    limit = max(1, min(2000, limit))
    out = {}
    for k in meta["actions"]:
        names = sorted((os.path.basename(p)
                        for p in glob.glob(os.path.join(_SL_DIR, "data", k, "*.jpg"))),
                       reverse=True)
        out[k] = {"total": len(names), "photos": names[:limit]}
    return {"classes": out}


@router.get("/tutorial/api/sl/photos")
def sl_photos(action: str, limit: int = 60, offset: int = 0):
    d = _photo_dir(action)
    names = sorted((os.path.basename(p) for p in glob.glob(os.path.join(d, "*.jpg"))),
                   reverse=True)   # newest first (timestamps sort lexicographically)
    limit = max(1, min(2000, limit))
    offset = max(0, offset)
    return {"total": len(names), "photos": names[offset:offset + limit]}


@router.get("/tutorial/api/sl/photo")
def sl_photo(action: str, name: str, w: int = 0):
    """Serve one photo. w > 0 returns a downscaled thumbnail (a 3000-photo
    dataset must not ship megabytes of full frames to the gallery)."""
    d = _photo_dir(action)
    if not _PHOTO_NAME.match(name):
        raise HTTPException(status_code=400, detail="bad name")
    path = os.path.join(d, name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="no such photo")
    if w:
        import cv2
        import numpy as np
        img = cv2.imread(path)
        if img is not None:
            w = max(32, min(480, int(w)))
            h = max(1, round(img.shape[0] * w / img.shape[1]))
            small = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                # timestamped names never change content — let the browser
                # cache thumbnails forever (gallery rebuilds cost 0 requests)
                return Response(content=buf.tobytes(), media_type="image/jpeg",
                                headers={"Cache-Control":
                                         "public, max-age=31536000, immutable"})
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control":
                                 "public, max-age=31536000, immutable"})


class PhotoReq(BaseModel):
    action: str
    name: str


@router.post("/tutorial/api/sl/photo/delete")
def sl_photo_delete(req: PhotoReq):
    d = _photo_dir(req.action)
    if not _PHOTO_NAME.match(req.name):
        raise HTTPException(status_code=400, detail="bad name")
    try:
        os.remove(os.path.join(d, req.name))
    except OSError:
        raise HTTPException(status_code=404, detail="no such photo")
    _sl_bump()
    return {"count": len(glob.glob(os.path.join(d, "*.jpg")))}


class ClearReq(BaseModel):
    action: str


@router.post("/tutorial/api/sl/clear")
def sl_clear(req: ClearReq):
    d = _photo_dir(req.action)
    n = 0
    for p in glob.glob(os.path.join(d, "*.jpg")):
        try:
            os.remove(p)
            n += 1
        except OSError:
            pass
    _sl_bump()
    return {"removed": n, "count": 0}


# ---- trained model: download / import -------------------------------------

def _model_download(course_dir):
    path = os.path.join(course_dir, "model.onnx")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="model.onnx not trained yet")
    return FileResponse(path, media_type="application/octet-stream",
                        filename="model.onnx")


@router.get("/tutorial/api/sl/model")
def sl_model_download():
    return _model_download(_SL_DIR)


@router.get("/tutorial/api/rl/model")
def rl_model_download():
    return _model_download(_RL_DIR)


@router.post("/tutorial/api/rl/model")
async def rl_model_import(request: Request):
    return await _model_import(request, _RL_DIR)


@router.post("/tutorial/api/sl/model")
async def sl_model_import(request: Request):
    return await _model_import(request, _SL_DIR)


async def _model_import(request, course_dir):
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    model = None
    try:
        import onnx
        model = onnx.load_model_from_string(data)
    except ImportError:
        pass                    # validator not installed — accept as-is
    except Exception:
        raise HTTPException(status_code=400, detail="not a valid ONNX model")
    if model is not None:
        # graph contract — exactly what inference.py feeds and reads:
        # one input named "camera" shaped (1, 3, 120, 160), one output with
        # 3 action scores. A valid-but-foreign ONNX must not replace ours.
        def _dims(vi):
            return [d.dim_value if d.HasField("dim_value") else 0
                    for d in vi.type.tensor_type.shape.dim]
        init = {i.name for i in model.graph.initializer}
        ins = [i for i in model.graph.input if i.name not in init]
        if len(ins) != 1 or ins[0].name != "camera" or _dims(ins[0])[-3:] != [3, 120, 160]:
            raise HTTPException(status_code=400,
                                detail="wrong model — input must be camera (1, 3, 120, 160)")
        outs = model.graph.output
        if len(outs) != 1 or _dims(outs[0])[-1] not in (0, 3):
            raise HTTPException(status_code=400,
                                detail="wrong model — output must be 3 action scores")
    tmp = os.path.join(course_dir, ".model.onnx.tmp")
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, os.path.join(course_dir, "model.onnx"))
    return {"ok": True, "bytes": len(data)}


# ---- web runner ------------------------------------------------------------
# Train/Inference run SERVER-SIDE — the whole tutorial works in a plain
# browser. One job at a time: pressing ▶ replaces whatever ran before
# (SIGINT first so run.py's finally can stop the car, SIGKILL after 2 s).
# Logs stream to the page over SSE.

_JOBS = {
    "sl-train": {"argv": ["/usr/bin/python3", "-u",
                          os.path.join(_ASSETS, "deep-racing-sl", "train.py")],
                 "cwd": _SL_DIR},
    "sl-run": {"argv": ["/usr/bin/python3", "-u",
                        os.path.join(_ASSETS, "deep-racing-sl", "inference.py")],
               "cwd": _SL_DIR, "takes_myapp": True},
    "rb-run": {"argv": ["/usr/bin/python3", "-u",
                        os.path.join(_ASSETS, "racing-rule-based", "follow_line.py")],
               "cwd": _RB_DIR, "takes_myapp": True},
    # rl-run drives with the SL inference script — same model contract, the
    # RL data home as cwd makes it pick up THIS course's model and settings
    "rl-train": {"argv": ["/usr/bin/python3", "-u",
                          os.path.join(_ASSETS, "deep-racing-rl", "train.py")],
                 "cwd": _RL_DIR, "sim_only": True, "grace": 30.0},
    "rl-run": {"argv": ["/usr/bin/python3", "-u",
                        os.path.join(_ASSETS, "deep-racing-sl", "inference.py")],
               "cwd": _RL_DIR, "takes_myapp": True},
}


def _free_myapp_port():
    """The MYAPP slot (:5000) yields to a course run: stop the managed
    student app (so it does not respawn) and kill any straggler listener."""
    subprocess.run(["bash", "-c",
                    "supervisorctl -s unix:///tmp/supervisor.sock stop myapp"
                    " >/dev/null 2>&1;"
                    "sudo -n systemctl stop physicar-myapp >/dev/null 2>&1;"
                    "fuser -k 5000/tcp >/dev/null 2>&1; true"],
                   capture_output=True, timeout=15)
_run_lock = threading.Lock()
_run_state = {"job": None, "proc": None, "exit": None, "lines": [], "base": 0, "ver": 0}


def _run_append(proc, line):
    with _run_lock:
        if _run_state["proc"] is not proc:
            return
        _run_state["lines"].append(line)
        if len(_run_state["lines"]) > 1000:
            drop = len(_run_state["lines"]) - 1000
            _run_state["base"] += drop
            del _run_state["lines"][:drop]
        _run_state["ver"] += 1


def _run_reader(proc):
    for raw in proc.stdout:
        line = raw.decode("utf-8", "replace").rstrip("\n")
        if "\r" in line:
            line = line.split("\r")[-1]   # progress lines: keep the final state
        _run_append(proc, line)
    code = proc.wait()
    with _run_lock:
        if _run_state["proc"] is proc:
            _run_state["exit"] = code
            _run_state["proc"] = None
            _run_state["ver"] += 1


def _terminate(proc, grace=2.0):
    try:
        os.killpg(proc.pid, signal.SIGINT)
    except (ProcessLookupError, PermissionError):
        return
    for _ in range(int(grace * 10)):
        if proc.poll() is not None:
            return
        time.sleep(0.1)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# live inference view: inference.py POSTs one snapshot per frame; the run
# event stream carries the latest one to every open page.
_infer_state = {"data": None, "ver": 0}

# live training view: train.py rewrites train_progress.json every epoch and
# the run event stream carries it to every open page. mtime-gated — the
# 0.05s loop only stats the file; it re-parses when it actually changed.
_TRAIN_FILE = os.path.join(_SL_DIR, "train_progress.json")
_RL_TRAIN_FILE = os.path.join(_RL_DIR, "train_progress.json")
_train_cache = {"mtime": None, "data": None}
_rl_train_cache = {"mtime": None, "data": None}


# run-button gates: photo total (Train needs >= _MIN_PHOTOS) and whether a
# trained model exists (Inference needs one; Train warns before overwrite).
# The counting only reruns when a data folder or the model actually changed.
_MIN_PHOTOS = 100    # keep in sync with MIN_PHOTOS in train.py
_gate_cache = {"key": None, "photos": 0}


def _run_gates():
    data = os.path.join(_SL_DIR, "data")
    key = []
    try:
        for k in sorted(os.listdir(data)):
            try:
                key.append((k, os.path.getmtime(os.path.join(data, k))))
            except OSError:
                pass
    except OSError:
        pass
    key = tuple(key)
    if key != _gate_cache["key"]:
        n = 0
        for k, _ in key:
            try:
                n += sum(1 for f in os.listdir(os.path.join(data, k))
                         if f.endswith(".jpg"))
            except OSError:
                pass
        _gate_cache.update({"key": key, "photos": n})
    try:
        # is the MYAPP viewer (:5000) actually up? The pages only attach
        # their iframe once it is — no "not running" fallback flash.
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        probe.settimeout(0.05)
        myapp = probe.connect_ex(("127.0.0.1", 5000)) == 0
        probe.close()
    except OSError:
        myapp = False
    return {"photos": _gate_cache["photos"], "min": _MIN_PHOTOS,
            "model": os.path.exists(os.path.join(_SL_DIR, "model.onnx")),
            "rl_model": os.path.exists(os.path.join(_RL_DIR, "model.onnx")),
            "sim": is_sim_mode(),
            "myapp": myapp}


def _read_progress_file(path, cache):
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        cache["mtime"] = None
        cache["data"] = None
        return None, None
    if mtime != cache["mtime"]:
        try:
            with open(path) as f:
                cache["data"] = json.load(f)
            cache["mtime"] = mtime
        except (OSError, ValueError):
            pass    # mid-write — keep the last good copy, retry next tick
    return cache["data"], cache["mtime"]


def _read_train_progress():
    return _read_progress_file(_TRAIN_FILE, _train_cache)


@router.post("/tutorial/api/sl/infer")
def sl_infer(req: dict):
    _infer_state["data"] = {"probs": [float(x) for x in (req.get("probs") or [])][:16],
                            "action": req.get("action"),
                            "speed": float(req.get("speed") or 0.0),
                            "steering": float(req.get("steering") or 0.0)}
    _infer_state["ver"] += 1
    return {"ok": True}


# Rule-Based live view: follow_line.py POSTs gauges every frame and the mask
# image every 3rd. The image keeps its own version so the event stream sends
# each one to a client ONCE, not repeated on every gauge tick.
_rb_state = {"data": None, "ver": 0, "img": None, "img_ver": 0}


@router.post("/tutorial/api/rb/telemetry")
def rb_telemetry(req: dict):
    _rb_state["data"] = {"found": bool(req.get("found")),
                         "offset": float(req.get("offset") or 0.0),
                         "steering": float(req.get("steering") or 0.0),
                         "speed": float(req.get("speed") or 0.0)}
    if req.get("mask") is not None:
        _rb_state["img"] = {"mask": str(req.get("mask") or "")}
        _rb_state["img_ver"] += 1
    _rb_state["ver"] += 1
    return {"ok": True}


class RunReq(BaseModel):
    job: str
    steps: int | None = None            # rl-train only: this run's length
    worlds: list[str] | None = None     # rl-train only: tracks to rotate


@router.post("/tutorial/api/run")
def run_start(req: RunReq):
    job = _JOBS.get(req.job)
    if not job:
        raise HTTPException(status_code=400, detail="unknown job: " + req.job)
    if job.get("sim_only") and not is_sim_mode():
        raise HTTPException(status_code=400,
                            detail="this course trains in the simulator only")
    argv = list(job["argv"])
    if req.job == "rl-train":
        argv.append(str(max(1000, min(300000, req.steps or 10000))))
        worlds = [w for w in (req.worlds or [])
                  if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", w)]
        if worlds:
            argv.append(",".join(worlds))
    with _run_lock:
        old = _run_state["proc"]
    if old is not None:
        _terminate(old, _JOBS.get(_run_state["job"], {}).get("grace", 2.0))
    if job.get("takes_myapp"):
        _free_myapp_port()
    proc = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        start_new_session=True, cwd=job["cwd"],
        env={**os.environ, "PYTHONUNBUFFERED": "1",
             "PYTHONPYCACHEPREFIX": "/opt/physicar/pycache"})
    with _run_lock:
        _run_state.update({"job": req.job, "proc": proc, "exit": None,
                           "lines": [], "base": 0})
        _run_state["ver"] += 1
    if req.job == "sl-train":
        try:
            os.remove(_TRAIN_FILE)      # fresh dashboard for the new run
        except OSError:
            pass
    if req.job == "rl-train":
        try:
            os.remove(_RL_TRAIN_FILE)
        except OSError:
            pass
    threading.Thread(target=_run_reader, args=(proc,), daemon=True).start()
    return {"job": req.job, "running": True}


@router.post("/tutorial/api/run/stop")
def run_stop():
    with _run_lock:
        proc = _run_state["proc"]
        grace = _JOBS.get(_run_state["job"], {}).get("grace", 2.0)
    if proc is not None:
        threading.Thread(target=_terminate, args=(proc, grace), daemon=True).start()
    return {"ok": True}


@router.get("/tutorial/api/run/events")
async def run_events():
    async def gen():
        cursor = None
        last_ver = -1
        sent_img = -1
        while True:
            def snap():
                with _run_lock:
                    return (_run_state["ver"], _run_state["job"],
                            _run_state["proc"] is not None, _run_state["exit"],
                            _run_state["base"], list(_run_state["lines"]))
            ver, job, running, exit_code, base, lines = await asyncio.to_thread(snap)
            train, train_mtime = _read_train_progress()
            rl_train, rl_mtime = _read_progress_file(_RL_TRAIN_FILE, _rl_train_cache)
            gates = await asyncio.to_thread(_run_gates)
            ver = (ver, _infer_state["ver"], _rb_state["ver"], train_mtime,
                   rl_mtime, gates["photos"], gates["model"], gates["rl_model"],
                   gates["myapp"])
            if ver != last_ver:
                last_ver = ver
                if cursor is None:
                    cursor = base
                start = max(cursor - base, 0)
                new = lines[start:]
                cursor = base + len(lines)
                payload = {"job": job, "running": running,
                           "exit": exit_code, "lines": new,
                           "infer": _infer_state["data"] if running else None,
                           "rb": _rb_state["data"] if running else None,
                           "train": train, "rl_train": rl_train,
                           "gates": gates}
                if running and _rb_state["img"] and _rb_state["img_ver"] != sent_img:
                    sent_img = _rb_state["img_ver"]
                    payload["rb_img"] = _rb_state["img"]
                yield "data: " + json.dumps(payload) + "\n\n"
            await asyncio.sleep(0.05)
    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})
