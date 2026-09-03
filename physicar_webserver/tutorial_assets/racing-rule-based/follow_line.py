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
LINE_HSV = (27, 170, 170)    # target color of the line (yellow)
LINE_TOL = (8, 90, 90)       # allowed distance per channel (H, S, V)

SPEED = 0.5                  # m/s while the line is visible
STEER_GAIN = 20.0            # steering degrees per unit of line offset
CROP_TOP = 0.5               # ignore this top fraction of the image (too far)
CAMERA_W, CAMERA_H = 160, 120
CAMERA_PAN, CAMERA_TILT = 0.0, -15.0   # camera angle (deg)

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

steering = 0.0
frame = 0
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

        # feed the live view on the tutorial page (mask every 3rd frame)
        try:
            report = {"found": offset is not None,
                      "offset": offset if offset is not None else 0.0,
                      "steering": steering,
                      "speed": SPEED}
            if frame % 3 == 0:
                report["mask"] = base64.b64encode(
                    cv2.imencode(".jpg", mask)[1]).decode()
            requests.post(f"{BASE_URL}/tutorial/api/rb/telemetry",
                          json=report, timeout=0.5)
        except requests.RequestException:
            pass

        frame += 1
        time.sleep(max(0.0, 1 / 15 - (time.time() - t0)))
except KeyboardInterrupt:
    pass
finally:
    drive(0, 0)
    print("stopped")
