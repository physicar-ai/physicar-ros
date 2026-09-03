import math
import select
import sys
import termios
import time
import tty
from datetime import datetime
from pathlib import Path

import requests

# The action table: the key you press is the answer (y); the photo at that
# moment is the question (x). Photos land in data/<key>/.
ACTIONS = {
    "left": {"speed": 0.5, "steering": 20.0},
    "straight": {"speed": 0.5, "steering": 0.0},
    "right": {"speed": 0.5, "steering": -20.0},
}
CAMERA_W, CAMERA_H = 160, 120   # saved photo resolution = model input
CAMERA_PAN, CAMERA_TILT = 0.0, -15.0   # camera angle (deg)

# The tutorial page's settings (gear) are saved per machine — apply overrides.
try:
    import json
    with open("settings.json") as _f:
        _cfg = json.load(_f)
    for _a in ACTIONS.values():
        _a["speed"] = float(_cfg.get("speed", _a["speed"]))
    ACTIONS["left"]["steering"] = abs(float(_cfg.get("left", 20.0)))
    ACTIONS["right"]["steering"] = -abs(float(_cfg.get("right", 20.0)))
    CAMERA_PAN = float(_cfg.get("pan", CAMERA_PAN))
    CAMERA_TILT = float(_cfg.get("tilt", CAMERA_TILT))
except (OSError, ValueError):
    pass

# one folder per action — the photos land here
for key in ACTIONS:
    Path("data", key).mkdir(parents=True, exist_ok=True)

BASE_URL = "http://localhost"
KEYS = {"1": "left", "2": "straight", "3": "right"}   # keyboard -> action


def photo():
    """Latest camera frame as JPEG bytes, already at model resolution."""
    return requests.get(f"{BASE_URL}/camera",
                        params={"width": CAMERA_W, "height": CAMERA_H},
                        timeout=2).content


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


def read_key(timeout=0.0):
    r, _, _ = select.select([sys.stdin], [], [], timeout)
    return sys.stdin.read(1) if r else None


look(CAMERA_PAN, CAMERA_TILT)   # the model's fixed viewpoint

for k, name in KEYS.items():
    print(f"  [{k}] {name}  ->  data/{name}/")
print("press keys to drive + save photos, q to quit")

old = termios.tcgetattr(sys.stdin)
tty.setcbreak(sys.stdin.fileno())
n = 0
try:
    while True:
        key = read_key(0.05)
        if key == "q":
            break
        name = KEYS.get(key)
        if name:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            Path("data", name, f"{stamp}.jpg").write_bytes(photo())
            n += 1
            drive(ACTIONS[name]["speed"], ACTIONS[name]["steering"])
            print(f"\r{n} photos saved   ", end="")
finally:
    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
    drive(0, 0)
    print("\ndone")
