import glob
import json
import math
import os
import sys
import time

import cv2
import numpy as np
import onnxruntime as ort
import requests

# The action table — must match what the model was trained with.
ACTIONS = {
    "left": {"speed": 0.5, "steering": 20.0},
    "straight": {"speed": 0.5, "steering": 0.0},
    "right": {"speed": 0.5, "steering": -20.0},
}
CAMERA_W, CAMERA_H = 160, 120   # model input resolution
CAMERA_PAN, CAMERA_TILT = 0.0, -15.0   # camera angle (deg) — same as labeling

# How the action is picked from the network's probabilities:
#   greedy     - always the most probable action
#   stochastic - sample by probability (explores, less repetitive)
#   mean       - probability-weighted speed/steering (smooth, continuous)
MODE = "greedy"

BASE_URL = "http://localhost"
SETTINGS = "ml/settings.json"   # the profile SHARED by the ML courses
settings_mtime = None
LOCK = "runner.json"    # this run's lock — its "model" can change mid-drive
lock_mtime = None


def camera(width, height):
    """Latest camera frame as a BGR image, resized by the server.
    Retries the brief windows without a valid frame (camera warming up,
    server restarting) — one dropped frame must not kill the drive."""
    for _ in range(30):
        try:
            jpg = requests.get(f"{BASE_URL}/camera",
                               params={"width": width, "height": height},
                               timeout=2).content
        except requests.RequestException:
            time.sleep(0.2)     # a busy machine can stall the API briefly
            continue
        img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
        if img is not None:
            return img
        time.sleep(0.1)
    raise SystemExit("camera unavailable — is the robot stack running?")


def drive(speed, steering):
    """speed in m/s, steering in degrees (+ = left). The API wants radians.
    A transient API stall must not kill the loop — skip the beat instead."""
    try:
        requests.post(f"{BASE_URL}/speed", json={"value": float(speed)}, timeout=2)
        requests.post(f"{BASE_URL}/steering",
                      json={"value": math.radians(steering)}, timeout=2)
    except requests.RequestException:
        pass


def look(pan, tilt):
    """Point the camera (degrees)."""
    try:
        requests.post(f"{BASE_URL}/camera/pan",
                      json={"value": math.radians(pan)}, timeout=2)
        requests.post(f"{BASE_URL}/camera/tilt",
                      json={"value": math.radians(tilt)}, timeout=2)
    except requests.RequestException:
        pass


MODELS_DIR = "ml/models"    # every trained model, named by its content hash
MODEL_ID = sys.argv[1] if len(sys.argv) > 1 else ""   # picked at run time


def load_model(mid=""):
    """The requested model — this run's argv by default, newest in the
    store as the last resort. A checkpoint-only model (a fresh .pt from
    another machine) gets its deployable ONNX derived here first."""
    mid = mid or MODEL_ID

    def have(m, ext):
        return os.path.exists(os.path.join(MODELS_DIR, m + ext))
    if not (mid and (have(mid, ".onnx") or have(mid, ".pt"))):
        got = sorted(glob.glob(os.path.join(MODELS_DIR, "*.onnx"))
                     + glob.glob(os.path.join(MODELS_DIR, "*.pt")),
                     key=os.path.getmtime)
        if not got:
            raise SystemExit("no model in models/ — run Train first")
        mid = os.path.basename(got[-1]).rsplit(".", 1)[0]
    path = os.path.join(MODELS_DIR, mid + ".onnx")
    if not os.path.exists(path):
        import subprocess
        import sys
        conv = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "..", "convert.py")
        subprocess.run([sys.executable, conv,
                        os.path.join(MODELS_DIR, mid + ".pt"), path],
                       check=True)
    s = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
    print(f"model {mid} loaded")
    return mid, s


model_id, sess = load_model()
labels = list(ACTIONS)
speeds = np.array([ACTIONS[k]["speed"] for k in labels])
steers = np.array([ACTIONS[k]["steering"] for k in labels])
look(CAMERA_PAN, CAMERA_TILT)   # the model's fixed viewpoint

# The MYAPP live view: while driving, this script serves its own page (the
# _PAGE string below) AND its own event stream on :5000 — one file, no deps.
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_PAGE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Inference</title>
<style>
  html, body { margin: 0; height: 100%; overflow: hidden; background: #0d0d10;
               font-family: system-ui, sans-serif; }
  canvas { position: absolute; inset: 0; width: 100%; height: 100%; z-index: 1;
           pointer-events: none; }
  #probs { position: absolute; inset: 0; display: flex; gap: 8%; padding: 6px 12% 0; }
  .col { flex: 1; display: flex; flex-direction: column; align-items: center; gap: 4px; }
  /* the label sits ON TOP of its own bar (the space above the arcs),
     like the rule-based status line: "left 32%" */
  .lbl { font-weight: 600; font-size: clamp(9px, 3.2vw, 13.5px); color: #b9b9cc;
         white-space: nowrap; font-variant-numeric: tabular-nums;
         text-shadow: 0 1px 3px rgba(0,0,0,.9); }
  .lbl.sel { color: #cfc0f5; }
  .wrap { width: 100%; flex: 1; display: flex; align-items: flex-end; }
  .bar { width: 100%; height: 0%; background: rgba(182,167,216,.35);
         border-radius: 10px 10px 0 0; }
  .bar.sel { background: rgba(167,139,250,.5); }
</style>
</head>
<body>
  <div id="probs"></div>
  <canvas id="cv"></canvas>
<script>
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var ctx = $("cv").getContext("2d");
  var labels = [], bars = [], lbls = [], target = null, disp = null;

  // action names ride on the event stream — one column per action
  function build(nm) {
    if (nm.length === labels.length) { return; }
    labels = nm.slice(); bars = []; lbls = [];
    $("probs").innerHTML = "";
    nm.forEach(function (n) {
      var col = document.createElement("div"); col.className = "col";
      var lbl = document.createElement("span"); lbl.className = "lbl"; lbl.textContent = n;
      var wrap = document.createElement("div"); wrap.className = "wrap";
      var bar = document.createElement("div"); bar.className = "bar";
      wrap.appendChild(bar);
      col.appendChild(lbl); col.appendChild(wrap);
      $("probs").appendChild(col);
      lbls.push(lbl); bars.push(bar);
    });
    draw(null);
  }

  // dashed guide arcs + the steering arrow (one path, one stroke), then bars
  function draw(d) {
    var cv = $("cv"), r = cv.getBoundingClientRect();
    var w = Math.round(r.width), h = Math.round(r.height);
    if (w < 10 || h < 10) { return; }
    if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; }
    var cx = w / 2, cy = h - 2, R = Math.min(w / 2 - 2, h - 8);
    ctx.clearRect(0, 0, w, h);
    ctx.strokeStyle = "rgba(255,255,255,.4)"; ctx.lineWidth = 1; ctx.setLineDash([3, 3]);
    ctx.beginPath(); ctx.arc(cx, cy, R, Math.PI, 0); ctx.stroke();
    ctx.beginPath(); ctx.arc(cx, cy, R / 2, Math.PI, 0); ctx.stroke();
    ctx.setLineDash([]);
    if (d) {
      var a = -Math.PI / 2 - d.steering * 2 * Math.PI / 180;
      var ex = cx + Math.cos(a) * R * 0.92, ey = cy + Math.sin(a) * R * 0.92;
      ctx.beginPath();
      ctx.moveTo(cx, cy); ctx.lineTo(ex, ey);
      ctx.moveTo(ex, ey); ctx.lineTo(ex - 15 * Math.cos(a - 0.45), ey - 15 * Math.sin(a - 0.45));
      ctx.moveTo(ex, ey); ctx.lineTo(ex - 15 * Math.cos(a + 0.45), ey - 15 * Math.sin(a + 0.45));
      ctx.strokeStyle = "rgba(239,68,68,.55)"; ctx.lineWidth = 6; ctx.lineCap = "round";
      ctx.stroke();
    }
    bars.forEach(function (bar, i) {
      var p = d && d.probs ? d.probs[i] || 0 : 0;
      bar.style.height = p * 100 + "%";
      bar.className = "bar" + (d && i === d.action ? " sel" : "");
      lbls[i].textContent = labels[i] + (d ? " " + (p * 100).toFixed(0) + "%" : "");
      lbls[i].className = "lbl" + (d && i === d.action ? " sel" : "");
    });
  }

  // snapshots arrive ~15 Hz; the view eases toward the latest at display rate
  (function tick() {
    if (target && target.probs) {
      if (!disp) {
        disp = { probs: target.probs.slice(), steering: target.steering, action: target.action };
      } else {
        disp.probs = disp.probs.map(function (v, i) { return v + ((target.probs[i] || 0) - v) * 0.3; });
        disp.steering += (target.steering - disp.steering) * 0.3;
        disp.action = target.action;
      }
      draw(disp);
    } else if (disp) { disp = null; draw(null); }
    requestAnimationFrame(tick);
  }());

  var es = new EventSource("events");   // served by this same script
  es.onmessage = function (ev) {
    var m; try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (m.labels && m.labels.length) { build(m.labels); }
    target = m.probs && m.probs.length ? m : null;
  };
  es.onerror = function () { target = null; };
}());
</script>
</body>
</html>
"""
_live = {"seq": 0, "data": {}}


def report_view(**kv):
    _live["data"] = kv
    _live["seq"] += 1


class _View(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/events"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            last = -1
            try:
                while True:
                    if _live["seq"] != last:
                        last = _live["seq"]
                        self.wfile.write(b"data: "
                                         + json.dumps(_live["data"]).encode()
                                         + b"\n\n")
                        self.wfile.flush()
                    time.sleep(1 / 30)
            except (BrokenPipeError, ConnectionResetError):
                return
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(_PAGE.encode())

    def log_message(self, *args):
        pass


def _serve_view():
    try:
        ThreadingHTTPServer(("127.0.0.1", 5000), _View).serve_forever()
    except OSError:
        print("port 5000 busy — MYAPP view off")


threading.Thread(target=_serve_view, daemon=True).start()

print(f"driving ({MODE}) — press Stop to end")
try:
    while True:
        t0 = time.time()

        # The Racing panel's settings (gear) apply LIVE, even mid-run —
        # one stat per frame, re-read only when the file actually changed
        try:
            m = os.path.getmtime(SETTINGS)
            if m != settings_mtime:
                settings_mtime = m
                with open(SETTINGS) as f:
                    cfg = json.load(f)
                for k in labels:
                    ACTIONS[k]["speed"] = float(cfg.get("speed", ACTIONS[k]["speed"]))
                ACTIONS["left"]["steering"] = abs(float(cfg.get("left", 20.0)))
                ACTIONS["right"]["steering"] = -abs(float(cfg.get("right", 20.0)))
                MODE = str(cfg.get("mode", MODE))
                new_pan = float(cfg.get("pan", CAMERA_PAN))
                new_tilt = float(cfg.get("tilt", CAMERA_TILT))
                if (new_pan, new_tilt) != (CAMERA_PAN, CAMERA_TILT):
                    CAMERA_PAN, CAMERA_TILT = new_pan, new_tilt
                    look(CAMERA_PAN, CAMERA_TILT)
                speeds = np.array([ACTIONS[k]["speed"] for k in labels])
                steers = np.array([ACTIONS[k]["steering"] for k in labels])
                print(f"settings applied: {MODE}, speed {ACTIONS[labels[0]]['speed']}")
        except (OSError, ValueError):
            pass

        # Live model swap: the panel (or a curl to /racing/run while this
        # drive runs) rewrites the lock's "model" — follow it between
        # frames. A failed load keeps the current model driving.
        try:
            m = os.path.getmtime(LOCK)
        except OSError:
            m = None
        if m is not None and m != lock_mtime:
            lock_mtime = m
            try:
                with open(LOCK) as f:
                    want = json.load(f).get("model") or ""
                if want and want != model_id:
                    model_id, sess = load_model(want)
            except Exception as e:   # keep driving the model we have
                print(f"model swap failed: {e}")

        img = camera(CAMERA_W, CAMERA_H)
        x = img.transpose(2, 0, 1)[None].astype(np.float32)

        probs = sess.run(None, {"camera": x})[0][0]
        if MODE == "mean":
            # probability-weighted average -> smooth continuous control
            speed, steering = float(probs @ speeds), float(probs @ steers)
            label = labels[int(probs.argmax())]
        elif MODE == "stochastic":
            label = labels[int(np.random.choice(len(labels), p=probs / probs.sum()))]
            speed, steering = ACTIONS[label]["speed"], ACTIONS[label]["steering"]
        else:   # greedy
            label = labels[int(probs.argmax())]
            speed, steering = ACTIONS[label]["speed"], ACTIONS[label]["steering"]
        drive(speed, steering)

        # feed the live view on :5000
        report_view(probs=probs.tolist(), action=labels.index(label),
                    labels=labels, speed=speed, steering=steering)

        print(f"action [{label}]  conf {probs.max():.2f}")
        time.sleep(max(0.0, 1 / 15 - (time.time() - t0)))
except KeyboardInterrupt:
    pass
finally:
    drive(0, 0)
    print("stopped")
