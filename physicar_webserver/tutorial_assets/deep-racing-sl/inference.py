import json
import math
import os
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
SETTINGS = "settings.json"
settings_mtime = None


def camera(width, height):
    """Latest camera frame as a BGR image, resized by the server."""
    jpg = requests.get(f"{BASE_URL}/camera",
                       params={"width": width, "height": height},
                       timeout=2).content
    return cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)


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


if not os.path.exists("model.onnx"):
    raise SystemExit("model.onnx not found — run Train first")

sess = ort.InferenceSession("model.onnx", providers=["CPUExecutionProvider"])
labels = list(ACTIONS)
speeds = np.array([ACTIONS[k]["speed"] for k in labels])
steers = np.array([ACTIONS[k]["steering"] for k in labels])
look(CAMERA_PAN, CAMERA_TILT)   # the model's fixed viewpoint

# The tutorial page's MYAPP view: while driving, the same live view is also
# served on :5000 so it shows up in the app's MYAPP tab. The page feeds off
# the tutorial event stream — this server only hands out static HTML.
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_PAGE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui.html")


class _View(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        with open(_PAGE_PATH, "rb") as f:
            self.wfile.write(f.read())

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

        # The tutorial page's settings (gear) apply LIVE, even mid-run —
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

        # feed the live view on the Inference page
        try:
            requests.post(f"{BASE_URL}/tutorial/api/sl/infer",
                          json={"probs": probs.tolist(), "action": labels.index(label),
                                "speed": speed, "steering": steering}, timeout=0.5)
        except requests.RequestException:
            pass

        print(f"action [{label}]  conf {probs.max():.2f}")
        time.sleep(max(0.0, 1 / 15 - (time.time() - t0)))
except KeyboardInterrupt:
    pass
finally:
    drive(0, 0)
    print("stopped")
