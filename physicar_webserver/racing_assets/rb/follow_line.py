import base64
import json
import math
import os
import time

import cv2
import numpy as np
import requests

# The rule: pixels near this HSV color ARE the line (each channel may be
# off by its tolerance). Everything else is road.
LINE_HSV = (18, 255, 255)    # target color of the line (yellow)
LINE_TOL = (8, 90, 90)       # allowed distance per channel (H, S, V)

SPEED = 0.5                  # m/s while the line is visible
STEER_GAIN = 20.0            # steering degrees per unit of line offset
CROP_TOP = 0.5               # ignore this top fraction of the image (too far)
CAMERA_W, CAMERA_H = 160, 120
CAMERA_PAN, CAMERA_TILT = 0.0, -15.0   # camera angle (deg)

BASE_URL = "http://localhost"
SETTINGS = "rb/settings.json"
settings_mtime = None


def camera(width, height):
    """Latest camera frame as a BGR image, resized by the server.
    Retries the brief windows without a valid frame (camera warming up,
    server restarting) — one dropped frame must not kill the drive."""
    for _ in range(30):
        jpg = requests.get(f"{BASE_URL}/camera",
                           params={"width": width, "height": height},
                           timeout=2).content
        img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
        if img is not None:
            return img
        time.sleep(0.1)
    raise SystemExit("camera unavailable — is the robot stack running?")


def drive(speed, steering):
    """speed in m/s, steering in degrees (+ = left). The API wants radians."""
    requests.post(f"{BASE_URL}/speed", json={"value": float(speed)}, timeout=2)
    requests.post(f"{BASE_URL}/steering",
                  json={"value": math.radians(steering)}, timeout=2)


def look(pan, tilt):
    """Point the camera (degrees)."""
    requests.post(f"{BASE_URL}/camera/pan",
                  json={"value": math.radians(pan)}, timeout=2)
    requests.post(f"{BASE_URL}/camera/tilt",
                  json={"value": math.radians(tilt)}, timeout=2)


def find_line(img):
    """Where is the line? Horizontal offset from image center, normalized
    to [-1, 1] (None if not visible), plus the mask the rule 'sees'."""
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    lo = np.clip(np.array(LINE_HSV) - LINE_TOL, 0, 255)
    hi = np.clip(np.array(LINE_HSV) + LINE_TOL, 0, 255)
    mask = cv2.inRange(hsv, lo, hi)
    mask[:int(img.shape[0] * CROP_TOP)] = 0   # keep only the near road
    m = cv2.moments(mask)
    if m["m00"] < 500:                      # too few matching pixels
        return None, mask
    cx = m["m10"] / m["m00"]
    return (cx - img.shape[1] / 2) / (img.shape[1] / 2), mask


look(CAMERA_PAN, CAMERA_TILT)
print("following the line — press Stop to end")

# The MYAPP live view: while driving, this script serves its own page (the
# _PAGE string below) AND its own event stream on :5000 — one file, no deps.
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_PAGE = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Rule-Based</title>
<style>
  html, body { margin: 0; height: 100%; overflow: hidden; background: #0d0d10;
               font-family: system-ui, sans-serif; }
  img { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: cover; }
  canvas { position: absolute; inset: 0; width: 100%; height: 100%; pointer-events: none; }
  #crop { position: absolute; top: 0; left: 0; right: 0; height: 0; pointer-events: none;
          background: rgba(255,255,255,.06);
          border-bottom: 1.5px dashed rgba(255,255,255,.55); }
  #marker { position: absolute; top: 0; height: 100%; width: 2px; display: none;
            background: rgba(239,68,68,.55); pointer-events: none; }
  #status { position: absolute; left: 0; right: 0; top: 6px; text-align: center;
            font-size: clamp(8px, 5vw, 13.5px); color: #b9b9cc; white-space: nowrap;
            font-variant-numeric: tabular-nums; text-shadow: 0 1px 3px rgba(0,0,0,.9); }
  #status.lost { color: #f87171; font-weight: 600; }
</style>
</head>
<body>
  <img id="mask" alt="">
  <i id="crop"></i><i id="marker"></i>
  <canvas id="cv"></canvas>
  <div id="status">not running</div>
<script>
(function () {
  "use strict";
  var $ = function (id) { return document.getElementById(id); };
  var ctx = $("cv").getContext("2d"), target = null, disp = null;

  // dashed guide arcs + the steering arrow (one path, one stroke)
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
    if (!d) { return; }
    var a = -Math.PI / 2 - d.steering * 2 * Math.PI / 180;
    var ex = cx + Math.cos(a) * R * 0.92, ey = cy + Math.sin(a) * R * 0.92;
    ctx.beginPath();
    ctx.moveTo(cx, cy); ctx.lineTo(ex, ey);
    ctx.moveTo(ex, ey); ctx.lineTo(ex - 15 * Math.cos(a - 0.45), ey - 15 * Math.sin(a - 0.45));
    ctx.moveTo(ex, ey); ctx.lineTo(ex - 15 * Math.cos(a + 0.45), ey - 15 * Math.sin(a + 0.45));
    ctx.strokeStyle = "rgba(239,68,68,.55)"; ctx.lineWidth = 6; ctx.lineCap = "round";
    ctx.stroke();
  }

  // snapshots arrive ~15 Hz; the canvas eases toward the latest at display rate
  (function tick() {
    if (target) {
      disp = disp || { steering: target.steering };
      disp.steering += (target.steering - disp.steering) * 0.3;
      draw(disp);
    } else if (disp) { disp = null; draw(null); }
    requestAnimationFrame(tick);
  }());

  var es = new EventSource("events");   // served by this same script
  es.onmessage = function (ev) {
    var m; try { m = JSON.parse(ev.data); } catch (e) { return; }
    target = m.found !== undefined ? m : null;
    if (m.crop !== undefined) { $("crop").style.height = m.crop * 100 + "%"; }
    if (m.mask) { $("mask").src = "data:image/jpeg;base64," + m.mask; }
    $("marker").style.display = target && m.found ? "block" : "none";
    if (target && m.found) { $("marker").style.left = (m.offset + 1) / 2 * 100 + "%"; }
    var deg = target ? (m.steering >= 0 ? "+" : "") + m.steering.toFixed(1) + "\u00b0" : "";
    $("status").textContent = !target ? "" : m.found ? "steering " + deg : "line lost \u00b7 " + deg;
    $("status").className = target && !m.found ? "lost" : "";
  };
  es.onerror = function () { $("status").textContent = "not running"; $("status").className = ""; };
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

steering = 0.0
frame = 0
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
                SPEED = float(cfg.get("speed", SPEED))
                STEER_GAIN = float(cfg.get("gain", STEER_GAIN))
                CROP_TOP = float(cfg.get("crop", CROP_TOP))
                LINE_HSV = (int(cfg.get("hue", LINE_HSV[0])),
                            int(cfg.get("sat", LINE_HSV[1])),
                            int(cfg.get("val", LINE_HSV[2])))
                LINE_TOL = (int(cfg.get("hue_tol", LINE_TOL[0])),
                            int(cfg.get("sat_tol", LINE_TOL[1])),
                            int(cfg.get("val_tol", LINE_TOL[2])))
                new_pan = float(cfg.get("pan", CAMERA_PAN))
                new_tilt = float(cfg.get("tilt", CAMERA_TILT))
                if (new_pan, new_tilt) != (CAMERA_PAN, CAMERA_TILT):
                    CAMERA_PAN, CAMERA_TILT = new_pan, new_tilt
                    look(CAMERA_PAN, CAMERA_TILT)
                print(f"settings applied: speed {SPEED}, gain {STEER_GAIN}, "
                      f"hsv {LINE_HSV} tol {LINE_TOL}")
        except (OSError, ValueError):
            pass

        img = camera(CAMERA_W, CAMERA_H)
        offset, mask = find_line(img)

        if offset is None:
            # line lost: turn hard toward the side it was last seen on
            steering = 20.0 if steering >= 0 else -20.0
            drive(SPEED, steering)
            print("line lost — searching")
        else:
            steering = max(-20.0, min(20.0, -offset * STEER_GAIN))
            drive(SPEED, steering)
            print(f"offset {offset:+.2f}  steering {steering:+.1f}")

        # feed the live view (the mask image every 3rd frame)
        report = {"found": offset is not None,
                  "offset": offset if offset is not None else 0.0,
                  "steering": steering,
                  "speed": SPEED,
                  "crop": CROP_TOP}
        if frame % 3 == 0:
            report["mask"] = base64.b64encode(
                cv2.imencode(".jpg", mask)[1]).decode()
        report_view(**report)

        frame += 1
        time.sleep(max(0.0, 1 / 15 - (time.time() - t0)))
except KeyboardInterrupt:
    pass
finally:
    drive(0, 0)
    print("stopped")
