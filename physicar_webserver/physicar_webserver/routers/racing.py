"""Racing courses — a small runner API, no UI.

The course scripts are fully standalone (they even serve their own live view
and event stream on :5000); this router only runs them and owns the shared
state under /opt/physicar/userdata/racing so every client — the PhysiCar
panel, the racing_* chat tools, a student's curl — sees one truth.

    POST /racing/run              {"job", "steps"?, "worlds"?, "base"?,
                                   "model"?} — model/base/worlds ride on the
                                   request; nothing about a run is stored
    POST /racing/stop
    GET  /racing/status           lock, gates, log tail, training progress
    GET  /racing/code/{job}       the executed script, settings substituted
    GET/POST /racing/{course}/settings    course: rb | ml (sl and rl are
                                          aliases of ml: ONE shared profile)
    Everything a course OWNS lives under /racing/ml/ — the ML courses share
    one dataset, one model store and one reward function:
    POST /racing/ml/labeling/camera    point the camera at the label viewpoint
    POST /racing/ml/labeling/capture   {"action"}: photo -> save -> drive
    GET  /racing/ml/labeling/photos    ?action=&limit=&offset=  (newest first)
    GET  /racing/ml/labeling/photo     ?action=&name=&w=  (w>0: thumbnail)
    POST /racing/ml/labeling/photo/delete, .../photos/clear   curation
    GET  /racing/ml/models        the model store: hash-named, never overwritten
    POST /racing/ml/models/delete {"id"}
    POST /racing/ml/models/import raw .pt body -> validated, ONNX derived
    GET  /racing/ml/models/{id}   download one model checkpoint (.pt)
    GET/POST /racing/ml/reward    the RL reward function (GET seeds reward.py,
                                  {"reset": true} restores the default)

Jobs: rb-run | sl-train | rl-train | ml-run — one at a time,
machine-wide. Runs are DETACHED (their own process group): they survive this
server restarting, and any client can stop them via the pid in the lock file.
"""
import base64
import glob
import hashlib
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
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from physicar_webserver.sim import is_sim_mode

router = APIRouter()

_SELF = "http://127.0.0.1:8000"
_HOME = "/opt/physicar/userdata/racing"
_LOCK = os.path.join(_HOME, "runner.json")
_LOG = os.path.join(_HOME, "runner.log")
_ACTIONS = ["left", "straight", "right"]
_COURSES = ["rb", "sl", "rl"]
_PHOTO_NAME = re.compile(r"^[A-Za-z0-9._-]+\.jpg$")
# models are named by the first 8 hex chars of their content hash: identical
# uploads dedupe to the same id, and no training run ever overwrites another
_MODELS = os.path.join(_HOME, "ml", "models")
_MODEL_ID = re.compile(r"^[0-9a-f]{8}$")


def _find_assets():
    """Source tree first (symlink installs), ament share second (isolated
    installs) — the missing-module incident taught us not to assume one."""
    cands = [os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
        "racing_assets")]
    try:
        from ament_index_python.packages import get_package_share_directory
        cands.append(os.path.join(get_package_share_directory("physicar_webserver"),
                                  "racing_assets"))
    except Exception:
        pass
    for c in cands:
        if os.path.isdir(c):
            return c
    return cands[0]


_ASSETS = _find_assets()

_JOBS = {
    "rb-run": {"script": "rb/follow_line.py", "home": "rb", "port5000": True},
    # ONE Train step with two teachers, ONE shared Inference — the jobs
    # mirror that: two trainers, one runner
    "sl-train": {"script": "ml/train_supervised.py", "home": "ml"},
    "rl-train": {"script": "ml/train_reinforcement.py", "home": "ml",
                 "sim_only": True, "grace": 30.0},
    "ml-run": {"script": "ml/inference.py", "home": "ml", "port5000": True},
}

# shown by View Code, not startable by the runner — the labeling sample is
# interactive (the keyboard IS the labeler), so it needs a real terminal
_CODE_ONLY = {"sl-label": {"script": "ml/labeling.py", "home": "ml"}}

_DEFAULTS = {
    "rb": {"speed": 0.5, "gain": 20.0, "hue": 18, "sat": 255, "val": 255,
           "hue_tol": 8, "sat_tol": 90, "val_tol": 90, "crop": 0.5,
           "pan": 0.0, "tilt": -15.0},
    # sl and rl SHARE one profile (and one model store): same net, same
    # drive. Which model runs / trains-from is a RUN parameter, not a setting.
    "ml": {"speed": 0.5, "left": 20.0, "right": 20.0, "pan": 0.0,
           "tilt": -15.0, "mode": "greedy"},
}
_LIMITS = {"speed": (0.1, 3.0), "gain": (0.0, 60.0), "left": (0.0, 20.0),
           "right": (0.0, 20.0), "hue": (0, 179), "sat": (0, 255),
           "val": (0, 255), "hue_tol": (0, 179), "sat_tol": (0, 255),
           "val_tol": (0, 255), "crop": (0.0, 0.9), "pan": (-30.0, 30.0),
           "tilt": (-30.0, 30.0)}
_MODES = ["greedy", "stochastic", "mean"]


def _settings_path(course):
    d = os.path.join(_HOME, course)                 # rb/ | ml/
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "settings.json")


def _data_dir(action):
    d = os.path.join(_HOME, "ml", "labeling_data", action)
    os.makedirs(d, exist_ok=True)
    return d


def _scourse(course):
    """Settings/model course key: sl and rl share the single 'ml' profile."""
    return "ml" if course in ("sl", "rl", "ml") else course


def _check_course(course):
    if course not in _COURSES and course != "ml":
        raise HTTPException(status_code=404, detail="unknown course: " + course)
    return course


def _clamp(course, vals):
    out = dict(_DEFAULTS[_scourse(course)])
    for k, v in (vals or {}).items():
        if k not in out:
            continue
        if k == "mode":
            if v in _MODES:
                out[k] = v
            continue
        try:
            n = float(v)
        except (TypeError, ValueError):
            continue
        lo, hi = _LIMITS[k]
        out[k] = min(hi, max(lo, n))
    return out


def _read_settings(course):
    course = _scourse(course)
    try:
        with open(_settings_path(course)) as f:
            return _clamp(course, json.load(f))
    except (OSError, ValueError):
        return dict(_DEFAULTS[course])


@router.get("/racing/{course}/settings")
def settings_read(course: str):
    return _read_settings(_check_course(course))


@router.post("/racing/{course}/settings")
def settings_write(course: str, req: dict):
    course = _scourse(_check_course(course))
    if req.get("reset"):
        cur = dict(_DEFAULTS[course])
    else:
        cur = _clamp(course, {**_read_settings(course), **req})
    os.makedirs(_HOME, exist_ok=True)
    path = _settings_path(course)
    with open(path + ".tmp", "w") as f:
        json.dump(cur, f)
    os.replace(path + ".tmp", path)
    return cur


# ---- runner ----------------------------------------------------------------

def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _lock_read():
    try:
        with open(_LOCK) as f:
            l = json.load(f)
        if l.get("pid") and _alive(l["pid"]):
            return l
    except (OSError, ValueError):
        pass
    return None


def _stop_job(grace=None):
    l = _lock_read()
    if not l:
        return False
    g = grace if grace is not None else _JOBS.get(l.get("job"), {}).get("grace", 2.0)
    # SIGINT the whole group: KeyboardInterrupt lets the script stop the car
    # and export what it trained; escalate only after the grace window
    try:
        os.killpg(l["pid"], signal.SIGINT)
    except OSError:
        try:
            os.kill(l["pid"], signal.SIGINT)
        except OSError:
            pass
    t0 = time.time()
    while _alive(l["pid"]) and time.time() - t0 < g:
        time.sleep(0.2)
    if _alive(l["pid"]):
        try:
            os.killpg(l["pid"], signal.SIGKILL)
        except OSError:
            pass
    try:
        cur = json.load(open(_LOCK))
        if cur.get("pid") == l["pid"]:
            os.unlink(_LOCK)
    except (OSError, ValueError):
        pass
    return True


def _free_myapp_port():
    """The MYAPP slot (:5000) yields to a course run: stop the managed
    student app (so it does not respawn) and kill any straggler listener."""
    subprocess.run(["bash", "-c",
                    "supervisorctl -s unix:///tmp/supervisor.sock stop myapp"
                    " >/dev/null 2>&1;"
                    "sudo -n systemctl stop physicar-myapp >/dev/null 2>&1;"
                    "fuser -k 5000/tcp >/dev/null 2>&1; true"],
                   capture_output=True, timeout=15)


def _finalize_checkpoint():
    """File a training run's checkpoint.pt into the model store — the id is
    the checkpoint's content hash — and make it the active model. Called when
    a training process ends, HOWEVER it ended, so a Stop or a crash keeps
    everything up to the last checkpoint."""
    src = os.path.join(_HOME, "ml", "checkpoint.pt")
    try:
        with open(src, "rb") as f:
            mid = hashlib.sha256(f.read()).hexdigest()[:8]
    except OSError:
        return None
    os.makedirs(_MODELS, exist_ok=True)
    dst = os.path.join(_MODELS, mid + ".pt")
    try:
        if os.path.exists(dst):
            os.unlink(src)              # identical model already filed
        else:
            os.replace(src, dst)
    except OSError:
        return None                     # a concurrent finalize won the race
    return mid


class RunReq(BaseModel):
    job: str
    steps: int | None = None
    worlds: list[str] | None = None
    base: str | None = None     # train jobs: model id to continue from
    model: str | None = None    # run jobs: model id to drive with (default: newest)


@router.post("/racing/run")
def run_start(req: RunReq):
    spec = _JOBS.get(req.job)
    if not spec:
        raise HTTPException(status_code=400,
                            detail="unknown job: %s (use one of %s)"
                                   % (req.job, ", ".join(_JOBS)))
    if spec.get("sim_only") and not is_sim_mode():
        raise HTTPException(status_code=400,
                            detail="this course trains in the simulator only")
    script = os.path.join(_ASSETS, spec["script"])
    if not os.path.isfile(script):
        raise HTTPException(status_code=500, detail="course script missing: " + script)
    if req.job in ("sl-train", "rl-train") and req.base:
        if not _MODEL_ID.match(req.base) or not os.path.exists(
                os.path.join(_MODELS, req.base + ".pt")):
            raise HTTPException(status_code=400,
                                detail="base model has no checkpoint to continue from")
    model = ""
    if req.job == "ml-run":
        # the model to drive with rides ON THE RUN REQUEST — no stored pick
        if req.model:
            if not _MODEL_ID.match(req.model) or not _model_exists(req.model):
                raise HTTPException(status_code=400,
                                    detail="no such model: " + req.model)
            model = req.model
        else:
            ms = models_list()["models"]
            if not ms:
                raise HTTPException(status_code=400,
                                    detail="no model yet — train one first")
            model = ms[0]["id"]      # newest
        cur = _read_json(_LOCK)
        if cur and cur.get("job") == "ml-run" and _alive(int(cur.get("pid") or -1)):
            # LIVE SWAP — the drive keeps running: rewrite the lock's model
            # and inference.py follows the lock between frames (no restart)
            if cur.get("model") != model:
                cur["model"] = model
                with open(_LOCK + ".tmp", "w") as f:
                    json.dump(cur, f)
                os.replace(_LOCK + ".tmp", _LOCK)
            return {"job": "ml-run", "running": True, "model": model,
                    "swapped": True}
    _stop_job()   # one job at a time, machine-wide
    os.makedirs(_HOME, exist_ok=True)
    cwd = _HOME
    if spec.get("port5000"):
        _free_myapp_port()
    if req.job in ("sl-train", "rl-train"):
        _finalize_checkpoint()      # a leftover from a crashed run, if any
        try:
            os.unlink(os.path.join(_HOME, "ml", "train_progress.json"))
        except OSError:
            pass                # fresh dashboard for the new run
    if req.job == "rl-train":
        _reward_read()          # make sure reward.py exists (seeds the default)
    argv = ["/usr/bin/python3", "-u", script]
    if req.job == "rl-train":
        argv.append(str(max(1000, min(300000, req.steps or 20000))))
        worlds = [w for w in (req.worlds or [])
                  if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", str(w))]
        if worlds:
            argv.append(",".join(worlds))
    if model:
        argv.append(model)          # inference.py loads exactly this one
    logf = open(_LOG, "wb")     # fresh console for the new run
    env = {**os.environ, "PYTHONPYCACHEPREFIX": "/opt/physicar/pycache"}
    if req.base:
        env["RACING_BASE"] = req.base
    proc = subprocess.Popen(argv, cwd=cwd, stdout=logf, stderr=logf,
                            stdin=subprocess.DEVNULL, start_new_session=True,
                            env=env)
    logf.close()
    with open(_LOCK, "w") as f:
        json.dump({"pid": proc.pid, "job": req.job, "model": model or None,
                   "started": int(time.time() * 1000)}, f)
    with open(os.path.join(_HOME, "last_job.json"), "w") as f:
        json.dump({"job": req.job}, f)

    def _reap():
        code = proc.wait()
        mid = _finalize_checkpoint() if req.job in ("sl-train", "rl-train") \
            else None
        if mid:
            with open(os.path.join(_HOME, "last_job.json"), "w") as f:
                json.dump({"job": req.job, "model": mid}, f)
            if not os.path.exists(os.path.join(_MODELS, mid + ".onnx")):
                try:    # derive the deployable now (inference could, later)
                    subprocess.run(
                        ["nice", "-n", "15", "/usr/bin/python3",
                         os.path.join(_ASSETS, "convert.py"),
                         os.path.join(_MODELS, mid + ".pt"),
                         os.path.join(_MODELS, mid + ".onnx")],
                        capture_output=True, timeout=300,
                        env={**os.environ,
                             "PYTHONPYCACHEPREFIX": "/opt/physicar/pycache"})
                except Exception:
                    pass
        try:
            with open(_LOG, "ab") as lf:
                if mid:
                    lf.write(("\nsaved -> models/%s.pt (active)\n" % mid).encode())
                lf.write(("\n[exit %s]\n" % code).encode())
            cur = json.load(open(_LOCK))
            if cur.get("pid") == proc.pid:
                os.unlink(_LOCK)
        except (OSError, ValueError):
            pass
    threading.Thread(target=_reap, daemon=True).start()
    return {"job": req.job, "running": True, "pid": proc.pid}


@router.post("/racing/stop")
def run_stop():
    return {"ok": True, "stopped": _stop_job()}


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


@router.get("/racing/status")
def status():
    l = _lock_read()
    tail = ""
    try:
        size = os.path.getsize(_LOG)
        with open(_LOG, "rb") as f:
            f.seek(max(0, size - 4096))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        pass
    counts = {}
    for a in _ACTIONS:
        try:
            counts[a] = len([x for x in os.listdir(os.path.join(_HOME, "ml", "labeling_data", a))
                             if x.endswith(".jpg")])
        except OSError:
            counts[a] = 0
    probe = socket.socket()
    probe.settimeout(0.4)
    try:
        probe.connect(("127.0.0.1", 5000))
        myapp = True
    except OSError:
        myapp = False
    finally:
        probe.close()
    if not l:
        # a run that outlived its reaper (server restarted mid-run): file
        # its leftover checkpoint the moment anyone looks
        mid = _finalize_checkpoint()
        if mid:
            was = _read_json(os.path.join(_HOME, "last_job.json")) or {}
            with open(os.path.join(_HOME, "last_job.json"), "w") as f:
                json.dump({"job": was.get("job"), "model": mid}, f)
    last = _read_json(os.path.join(_HOME, "last_job.json"))
    prog = _read_json(os.path.join(_HOME, "ml", "train_progress.json"))
    ml = _read_settings("ml")
    return {
        "job": l["job"] if l else None,
        "model": l.get("model") if l else None,
        "running": bool(l),
        "lastJob": last["job"] if last else None,
        "lastModel": (last or {}).get("model"),
        "logTail": tail,
        "settings": {"rb": _read_settings("rb"), "ml": ml, "sl": ml, "rl": ml},
        "rewardMtime": (os.path.getmtime(_reward_path())
                        if os.path.exists(os.path.join(_HOME, "ml", "reward.py")) else 0),
        "slProgress": prog if prog and "epochs" in prog else None,
        "rlProgress": prog if prog and "total_steps" in prog else None,
        "gates": {
            "photos": sum(counts.values()), "min": 100, "counts": counts,
            "models": len(_model_ids()),
            "sim": is_sim_mode(), "myapp": myapp,
        },
    }


# ---- SL labeling -----------------------------------------------------------

@router.post("/racing/ml/labeling/camera")
def sl_camera():
    """Point the camera at the SL model's fixed viewpoint (pan/tilt from the
    course settings) — call on entering Labeling and after settings change."""
    st = _read_settings("sl")
    for path, deg in (("/camera/pan", st["pan"]), ("/camera/tilt", st["tilt"])):
        try:
            requests.post(_SELF + path, json={"value": math.radians(deg)}, timeout=2)
        except requests.RequestException:
            pass
    return {"ok": True, "pan": st["pan"], "tilt": st["tilt"]}


class LabelReq(BaseModel):
    action: str


@router.post("/racing/ml/labeling/capture")
def sl_label(req: LabelReq):
    """Button labeling: capture a photo at the MODEL's resolution, save it
    under the action, then the car performs the action (the ~1s command
    watchdog stops it once the button is released)."""
    if req.action not in _ACTIONS:
        raise HTTPException(status_code=400, detail="unknown action: " + req.action)
    st = _read_settings("sl")
    r = requests.get(_SELF + "/camera", params={"width": 160, "height": 120},
                     timeout=2)
    jpg = r.content
    if r.status_code != 200 or not jpg.startswith(b"\xff\xd8"):
        # not a JPEG (camera still warming up etc.) — never save garbage labels
        raise HTTPException(status_code=503,
                            detail="camera not ready — try again in a moment")
    d = _data_dir(req.action)
    name = datetime.now().strftime("ui_%Y%m%d_%H%M%S_%f") + ".jpg"
    with open(os.path.join(d, name), "wb") as f:
        f.write(jpg)
    steering = abs(st["left"]) if req.action == "left" \
        else -abs(st["right"]) if req.action == "right" else 0.0
    for path, val in (("/speed", st["speed"]), ("/steering", math.radians(steering))):
        try:
            requests.post(_SELF + path, json={"value": float(val)}, timeout=2)
        except requests.RequestException:
            pass
    return {"saved": name, "count": len(glob.glob(os.path.join(d, "*.jpg"))),
            "preview": "data:image/jpeg;base64," + base64.b64encode(jpg).decode()}


def _photo_dir(action):
    if action not in _ACTIONS:
        raise HTTPException(status_code=400, detail="unknown action: " + action)
    return _data_dir(action)


@router.get("/racing/ml/labeling/photos")
def sl_photos(action: str, limit: int = 60, offset: int = 0):
    d = _photo_dir(action)
    entries = []
    for p in glob.glob(os.path.join(d, "*.jpg")):
        try:
            entries.append((os.path.getmtime(p), os.path.basename(p)))
        except OSError:
            pass
    # newest first by mtime — photo names span two eras, so a lexicographic
    # sort would bury fresh captures mid-list
    entries.sort(reverse=True)
    names = [n for _, n in entries]
    limit = max(1, min(2000, limit))
    offset = max(0, offset)
    return {"total": len(names), "photos": names[offset:offset + limit]}


@router.get("/racing/ml/labeling/thumbs")
def sl_thumbs(action: str, offset: int = 0, limit: int = 60):
    """One page of gallery thumbnails INLINE as data URIs. One localhost
    request serves a whole column and the panel relays it over its existing
    websocket — hosted sims sit behind a proxy, so per-file HTTP fetches
    from the browser are exactly what we avoid."""
    import cv2
    d = _photo_dir(action)
    entries = []
    for pth in glob.glob(os.path.join(d, "*.jpg")):
        try:
            entries.append((os.path.getmtime(pth), os.path.basename(pth)))
        except OSError:
            pass
    entries.sort(reverse=True)      # newest first by mtime, like /photos
    names = [n for _, n in entries]
    offset = max(0, offset)
    limit = max(1, min(200, limit))
    items = []
    for n in names[offset:offset + limit]:
        src = ""
        img = cv2.imread(os.path.join(d, n))
        if img is not None:
            h = max(1, round(img.shape[0] * 160 / img.shape[1]))
            small = cv2.resize(img, (160, h), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                src = "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()
        items.append({"name": n, "src": src})
    return {"total": len(names), "offset": offset, "items": items}


@router.get("/racing/ml/labeling/photo")
def sl_photo(action: str, name: str, w: int = 0):
    """Serve one photo. w > 0 returns a downscaled thumbnail."""
    d = _photo_dir(action)
    if not _PHOTO_NAME.match(name):
        raise HTTPException(status_code=400, detail="bad name")
    path = os.path.join(d, name)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="no such photo")
    if w:
        import cv2
        img = cv2.imread(path)
        if img is not None:
            w = max(32, min(480, int(w)))
            h = max(1, round(img.shape[0] * w / img.shape[1]))
            small = cv2.resize(img, (w, h), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
            if ok:
                # timestamped names never change content — cache hard
                return Response(content=buf.tobytes(), media_type="image/jpeg",
                                headers={"Cache-Control": "public, max-age=31536000, immutable"})
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})


class PhotoDelReq(BaseModel):
    action: str
    name: str


@router.post("/racing/ml/labeling/photo/delete")
def sl_photo_delete(req: PhotoDelReq):
    d = _photo_dir(req.action)
    if not _PHOTO_NAME.match(req.name):
        raise HTTPException(status_code=400, detail="bad name")
    try:
        os.unlink(os.path.join(d, req.name))
    except OSError:
        pass
    return {"ok": True, "count": len(glob.glob(os.path.join(d, "*.jpg")))}


class ClearReq(BaseModel):
    action: str


@router.post("/racing/ml/labeling/photos/clear")
def sl_clear(req: ClearReq):
    d = _photo_dir(req.action)
    n = 0
    for p in glob.glob(os.path.join(d, "*.jpg")):
        try:
            os.unlink(p)
            n += 1
        except OSError:
            pass
    return {"ok": True, "deleted": n}


# ---- RL reward function ------------------------------------------------------
# The student edits ONE file: reward.py in the RL home. The Train step's
# script loads it and patches PhysicarEnv.reward with it (no subclassing).

_REWARD_DEFAULT = '''import numpy as np
from shapely.geometry import Point
from shapely.geometry.polygon import LinearRing


def reward(self):
    x, y = self.state["x"], self.state["y"]
    center = self.state["waypoints_center"]
    d = Point(x, y).distance(LinearRing(center))
    i = int(np.argmin(np.hypot(center[:, 0] - x, center[:, 1] - y)))
    half_width = np.hypot(*(self.state["waypoints_outer"][i]
                            - self.state["waypoints_inner"][i])) / 2
    return float(1.0 - d / half_width)
'''


def _reward_path():
    d = os.path.join(_HOME, "ml")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "reward.py")


def _reward_read():
    p = _reward_path()
    if not os.path.exists(p):
        with open(p, "w") as f:
            f.write(_REWARD_DEFAULT)   # first visit seeds the default
    with open(p) as f:
        return f.read()


@router.get("/racing/ml/reward")
def reward_read():
    return {"path": _reward_path(), "text": _reward_read()}


@router.post("/racing/ml/reward")
def reward_reset(req: dict):
    if req.get("reset"):
        with open(_reward_path(), "w") as f:
            f.write(_REWARD_DEFAULT)
    return {"path": _reward_path(), "text": _reward_read()}


# ---- the model store --------------------------------------------------------
# models/<id>.pt (the canonical checkpoint) + models/<id>.onnx (the derived
# deployable), <id> = sha256(pt bytes)[:8]. Training ADDS models; nothing is
# overwritten. The files ARE the metadata: mtime = created, .pt = can
# continue/share. A legacy onnx-only model still drives, nothing more.

def _model_ids():
    ids = set()
    for p in glob.glob(os.path.join(_MODELS, "*.onnx")) \
            + glob.glob(os.path.join(_MODELS, "*.pt")):
        mid = os.path.basename(p).rsplit(".", 1)[0]
        if _MODEL_ID.match(mid):
            ids.add(mid)
    return ids


def _model_exists(mid):
    return any(os.path.exists(os.path.join(_MODELS, mid + e))
               for e in (".onnx", ".pt"))


def _check_model_id(mid):
    if not _MODEL_ID.match(mid or ""):
        raise HTTPException(status_code=400, detail="bad model id")
    if not _model_exists(mid):
        raise HTTPException(status_code=404, detail="no such model: " + mid)
    return mid


@router.get("/racing/ml/models")
def models_list():
    os.makedirs(_MODELS, exist_ok=True)
    out = []
    for mid in _model_ids():
        pt = os.path.join(_MODELS, mid + ".pt")
        ref = pt if os.path.exists(pt) else os.path.join(_MODELS, mid + ".onnx")
        out.append({"id": mid,
                    "created": int(os.path.getmtime(ref)),
                    "size": os.path.getsize(ref),
                    "cont": os.path.exists(pt)})
    out.sort(key=lambda m: m["created"], reverse=True)
    return {"models": out}


class ModelReq(BaseModel):
    id: str


@router.post("/racing/ml/models/delete")
def models_delete(req: ModelReq):
    mid = _check_model_id(req.id)
    l = _lock_read()
    if l and l.get("model") == mid:
        raise HTTPException(status_code=409,
                            detail="this model is driving right now — stop "
                                   "the run first")
    for ext in (".onnx", ".pt"):
        try:
            os.unlink(os.path.join(_MODELS, mid + ext))
        except OSError:
            pass
    return {"ok": True}


@router.get("/racing/ml/models/{id}")
def model_download(id: str):
    mid = _check_model_id(id)
    pt = os.path.join(_MODELS, mid + ".pt")
    if not os.path.exists(pt):
        raise HTTPException(status_code=404,
                            detail="this model has no checkpoint to share")
    return FileResponse(pt, media_type="application/octet-stream",
                        filename="physicar_%s.pt" % mid)


@router.post("/racing/ml/models/import")
async def model_import(request: Request):
    data = await request.body()
    if not data:
        raise HTTPException(status_code=400, detail="empty upload")
    mid = hashlib.sha256(data).hexdigest()[:8]
    os.makedirs(_MODELS, exist_ok=True)
    pt = os.path.join(_MODELS, mid + ".pt")
    onnx_path = os.path.join(_MODELS, mid + ".onnx")
    if not (os.path.exists(pt) and os.path.exists(onnx_path)):
        tmp = pt + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        # loading the checkpoint into PhysicarNet (strict) IS the validation,
        # and the deployable ONNX falls out of it — one subprocess, no torch
        # kept resident in this server
        # low priority: a conversion must not starve a RUNNING course (a
        # 2s-timeout script call losing the CPU race kills the run)
        r = subprocess.run(
            ["nice", "-n", "15", "/usr/bin/python3",
             os.path.join(_ASSETS, "convert.py"), tmp, onnx_path],
            capture_output=True, timeout=180,
            env={**os.environ, "PYTHONPYCACHEPREFIX": "/opt/physicar/pycache"})
        if r.returncode != 0:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise HTTPException(status_code=400,
                                detail="not a PhysiCar model checkpoint (.pt)")
        os.replace(tmp, pt)               # same bytes = same id: dedupes
    return {"ok": True, "id": mid, "bytes": len(data)}


# ---- "write it yourself" samples --------------------------------------------

def _fmt_actions(s):
    def f(x):
        return "%.1f" % x if float(x) == int(x) else str(x)
    return ('ACTIONS = {\n    "left": {"speed": %s, "steering": %s},\n'
            '    "straight": {"speed": %s, "steering": 0.0},\n'
            '    "right": {"speed": %s, "steering": -%s},\n}'
            % (f(s["speed"]), f(abs(s["left"])), f(s["speed"]),
               f(s["speed"]), f(abs(s["right"]))))


@router.get("/racing/code/{job}")
def sample_code(job: str):
    """The executed script, VERBATIM — the scripts are fully standalone, so
    the real thing is the sample. Only the tunable constants are substituted
    with the CURRENT settings, so the gear and the code always agree."""
    spec = _JOBS.get(job) or _CODE_ONLY.get(job)
    if not spec:
        raise HTTPException(status_code=404, detail="unknown job: " + job)
    path = os.path.join(_ASSETS, spec["script"])
    try:
        with open(path) as f:
            text = f.read()
    except OSError:
        raise HTTPException(status_code=404, detail="file not found: " + path)
    if job == "rb-run":
        s = _read_settings("rb")
        text = re.sub(r"(?m)^(SPEED = )[0-9.]+", r"\g<1>%s" % s["speed"], text)
        text = re.sub(r"(?m)^(STEER_GAIN = )[0-9.]+", r"\g<1>%s" % s["gain"], text)
        text = re.sub(r"(?m)^(CROP_TOP = )[0-9.]+", r"\g<1>%s" % s["crop"], text)
        text = re.sub(r"(?m)^(LINE_HSV = )\([0-9, ]+\)",
                      r"\g<1>(%d, %d, %d)" % (s["hue"], s["sat"], s["val"]), text)
        text = re.sub(r"(?m)^(LINE_TOL = )\([0-9, ]+\)",
                      r"\g<1>(%d, %d, %d)" % (s["hue_tol"], s["sat_tol"], s["val_tol"]),
                      text)
    else:
        s = _read_settings(spec["home"])
        text = re.sub(r"ACTIONS\s*=\s*\{.*?\n\}", _fmt_actions(s), text,
                      count=1, flags=re.S)
        text = re.sub(r"(?m)^MODE = .*$", 'MODE = "%s"' % s["mode"], text, count=1)
    return {"job": job, "text": text}
