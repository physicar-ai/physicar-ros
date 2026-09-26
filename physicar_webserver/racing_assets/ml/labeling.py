"""Collect labeled driving photos from the terminal.

Every key press is a LABEL: the camera frame at that moment is saved under
ml/labeling_data/<action>/ and the car performs that action — exactly what the
Train page's labeling buttons do, in script form. Train with sl-train next.
"""
import json
import math
import select
import sys
import termios
import time
import tty
from datetime import datetime
from pathlib import Path

import requests

# The action table: the folders the photos land in AND how the car moves.
ACTIONS = {
    "left": {"speed": 0.5, "steering": 20.0},
    "straight": {"speed": 0.5, "steering": 0.0},
    "right": {"speed": 0.5, "steering": -20.0},
}
CAMERA_W, CAMERA_H = 160, 120   # model input resolution
CAMERA_PAN, CAMERA_TILT = 0.0, -15.0   # camera angle (deg)

KEYS = {"a": "left", "w": "straight", "d": "right"}
ACTION_TIMEOUT = 0.5    # seconds a key press keeps the robot moving

BASE_URL = "http://localhost"

# The Racing panel's settings (gear) are saved per machine and SHARED by the
# supervised and reinforcement courses — apply the overrides.
try:
    with open("ml/settings.json") as _f:
        _cfg = json.load(_f)
    for _a in ACTIONS.values():
        _a["speed"] = float(_cfg.get("speed", _a["speed"]))
    ACTIONS["left"]["steering"] = abs(float(_cfg.get("left", 20.0)))
    ACTIONS["right"]["steering"] = -abs(float(_cfg.get("right", 20.0)))
    CAMERA_PAN = float(_cfg.get("pan", CAMERA_PAN))
    CAMERA_TILT = float(_cfg.get("tilt", CAMERA_TILT))
except (OSError, ValueError):
    pass


def camera(width, height):
    """Latest camera frame as JPEG bytes, resized by the server. Retries the
    brief windows without a valid frame — never save garbage labels."""
    for _ in range(30):
        jpg = requests.get(f"{BASE_URL}/camera",
                           params={"width": width, "height": height},
                           timeout=2).content
        if jpg.startswith(b"\xff\xd8"):
            return jpg
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


def read_key(timeout=0.0):
    r, _, _ = select.select([sys.stdin], [], [], timeout)
    return sys.stdin.read(1) if r else None


def main():
    if not sys.stdin.isatty():
        raise SystemExit("run this from a terminal — the keyboard is the labeler")
    for action in ACTIONS:
        Path("ml/labeling_data", action).mkdir(parents=True, exist_ok=True)
    counts = {a: len(list(Path("ml/labeling_data", a).glob("*.jpg"))) for a in ACTIONS}
    look(CAMERA_PAN, CAMERA_TILT)   # the model's fixed viewpoint

    print("The key you press is the answer (y); "
          "the photo at that moment is the question (x).")
    for key, action in KEYS.items():
        print(f"  [{key}] {action:9s}->  ml/labeling_data/{action}/")
    print("  [space] stop now   [q] quit\n")

    old = termios.tcgetattr(sys.stdin)
    tty.setcbreak(sys.stdin.fileno())
    moving_until = 0.0
    try:
        while True:
            key = read_key(0.05)
            if key == "q":
                break
            if key == " ":
                drive(0, 0)
                moving_until = 0.0
            elif key in KEYS:
                action = KEYS[key]
                jpg = camera(CAMERA_W, CAMERA_H)
                name = datetime.now().strftime("cli_%Y%m%d_%H%M%S_%f") + ".jpg"
                (Path("ml/labeling_data", action) / name).write_bytes(jpg)
                counts[action] += 1
                drive(ACTIONS[action]["speed"], ACTIONS[action]["steering"])
                moving_until = time.time() + ACTION_TIMEOUT

            # a pressed key only drives for a moment — labeling is tap, look,
            # tap again, so the car never runs away between labels
            if moving_until and time.time() >= moving_until:
                drive(0, 0)
                moving_until = 0.0

            state = "moving " if moving_until else "stopped"
            print(f"\r  {state}  photos {sum(counts.values()):,} "
                  f"(L {counts['left']:,} / S {counts['straight']:,} "
                  f"/ R {counts['right']:,})   ", end="")
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
        drive(0, 0)
        print(f"\nsaved under ml/labeling_data/ — {sum(counts.values()):,} photos total")


if __name__ == "__main__":
    main()
