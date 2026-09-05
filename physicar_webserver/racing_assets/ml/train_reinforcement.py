import json
import math
import os
import time

import cv2
import gymnasium as gym
import numpy as np
import requests
import torch
import torch.nn as nn
from gymnasium import spaces
from shapely.geometry import Point, Polygon
from shapely.geometry.polygon import LinearRing

# The action table: every step the agent picks one of these.
ACTIONS = {
    "left": {"speed": 0.5, "steering": 20.0},
    "straight": {"speed": 0.5, "steering": 0.0},
    "right": {"speed": 0.5, "steering": -20.0},
}
CAMERA_W, CAMERA_H = 160, 120   # model input resolution
CAMERA_PAN, CAMERA_TILT = 0.0, -15.0   # camera angle (deg)

TOTAL_STEPS = 20000     # total experience to train on
N_STEPS = 1000          # experience collected per policy update
BATCH_SIZE = 64
LEARNING_RATE = 0.0003
GAMMA = 0.99            # discount factor: weight of future rewards
STEP_DT = 1 / 15        # one action per camera frame
MAX_STEPS = 100         # episode length limit (~7 s at 15 Hz)
# Continue training from this model — rides on the run request (empty = new)
BASE_MODEL = os.environ.get("RACING_BASE", "")
WORLDS = []             # tracks to rotate through (empty: the current world)
WHEELBASE = 0.18        # robot dimensions, for the four wheel positions
TRACK_OF_CAR = 0.16

BASE_URL = "http://localhost"   # robot web API; simulator lives under /sim/api

SETTINGS = "ml/settings.json"   # the profile SHARED by the ML courses
settings_mtime = None

# The Racing panel's settings (gear) are saved per machine — apply overrides.
try:
    settings_mtime = os.path.getmtime(SETTINGS)
    with open(SETTINGS) as _f:
        _cfg = json.load(_f)
    for _a in ACTIONS.values():
        _a["speed"] = float(_cfg.get("speed", _a["speed"]))
    ACTIONS["left"]["steering"] = abs(float(_cfg.get("left", 20.0)))
    ACTIONS["right"]["steering"] = -abs(float(_cfg.get("right", 20.0)))
    CAMERA_PAN = float(_cfg.get("pan", CAMERA_PAN))
    CAMERA_TILT = float(_cfg.get("tilt", CAMERA_TILT))
except (OSError, ValueError):
    pass
if BASE_MODEL and not os.path.exists(f"ml/models/{BASE_MODEL}.pt"):
    print(f"base model {BASE_MODEL} has no checkpoint — starting from scratch")
    BASE_MODEL = ""

# The Train step picks this run's length and tracks (runner arguments).
import sys
if len(sys.argv) > 1:
    TOTAL_STEPS = int(sys.argv[1])
if len(sys.argv) > 2:
    WORLDS = [w for w in sys.argv[2].split(",") if w]

# The Racing panel's dashboard reads this file — rewritten as training runs.
progress = {"total_steps": TOTAL_STEPS, "steps": 0, "status": "starting",
            "episodes": []}

def report(ep_reward=None, ep_length=None, **kv):
    if ep_reward is not None:
        progress["episodes"].append({"reward": round(ep_reward, 2),
                                     "length": ep_length})
    progress.update(kv)
    with open("ml/train_progress.json.tmp", "w") as f:
        json.dump(progress, f)
    os.replace("ml/train_progress.json.tmp", "ml/train_progress.json")


# ── web API helpers ─────────────────────────────────────────────────────────

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


def sim_pose(retries=20):
    """Vehicle pose {x, y, yaw(rad)} — retries the brief windows where the
    simulator has no pose yet (right after a teleport)."""
    for _ in range(retries):
        d = requests.get(f"{BASE_URL}/sim/api/pose", timeout=5).json()
        if "x" in d:
            return d
        time.sleep(0.15)
    raise SystemExit("simulator pose unavailable — is the simulator running?")


def overlay(text, ttl=10):
    """Status line on the /sim screen (empty text clears it)."""
    requests.post(f"{BASE_URL}/sim/api/overlay",
                  json={"text": str(text), "ttl": ttl}, timeout=2)


def sim_status():
    """Simulator status: current world, running/switching flags."""
    return requests.get(f"{BASE_URL}/sim/api/status", timeout=5).json()


def sim_objects():
    """Objects placed in the world, with their live poses."""
    return requests.get(f"{BASE_URL}/sim/api/objects",
                        timeout=5).json().get("objects", [])


def respawn(world=None, start_m=0.0):
    """Put the car on the track's center line, facing along the track.
    world:   switch to this track first (only when different — a switch takes
             seconds, so rotate worlds every N episodes, not every one)
    start_m: how far along the lap, in meters (wraps around at the end)."""
    if world is not None:
        while True:
            st = sim_status()
            if (st.get("running") and st.get("current") == world
                    and not st.get("switching")):
                break
            if not st.get("switching"):
                requests.post(f"{BASE_URL}/sim/api/switch",
                              json={"world": f"{world}.world"}, timeout=5)
            time.sleep(2)
    wp = requests.get(f"{BASE_URL}/sim/api/route", timeout=5).json()["waypoints"]
    pts = np.asarray(wp, float)
    dist = np.concatenate([[0.0], np.cumsum(np.hypot(*np.diff(pts, axis=0).T))])
    i = min(int(np.searchsorted(dist, start_m % dist[-1])), len(wp) - 2)
    yaw = math.atan2(wp[i + 1][1] - wp[i][1], wp[i + 1][0] - wp[i][0])
    requests.post(f"{BASE_URL}/sim/api/pose",
                  json={"x": wp[i][0], "y": wp[i][1], "yaw": yaw}, timeout=5)


# ── the model (identical to the supervised course — one CNN, two teachers) ──

class PhysicarNet(nn.Module):
    """Small CNN: camera image in -> action scores out.
    Normalization lives inside the network, so a raw image goes in."""

    def __init__(self):
        super().__init__()
        self.cnn = nn.Sequential(
            nn.Conv2d(3, 32, 8, 4), nn.ReLU(),
            nn.Conv2d(32, 64, 4, 2), nn.ReLU(),
            nn.Conv2d(64, 64, 3, 1), nn.ReLU(), nn.Flatten())
        with torch.no_grad():
            n = self.cnn(torch.zeros(1, 3, CAMERA_H, CAMERA_W)).shape[1]
        self.head = nn.Sequential(
            nn.Linear(n, 256), nn.ReLU(),
            nn.Linear(256, len(ACTIONS)))

    def forward(self, camera):
        x = camera / 255.0 * 2.0 - 1.0                    # 0-255 -> -1..1
        return torch.softmax(self.head(self.cnn(x)), dim=1)


# ── the environment: the track IS the teacher ───────────────────────────────

class PhysicarEnv(gym.Env):
    # reward() is not defined here on purpose — it lives in reward.py (the
    # Reward step's file) and is attached to this class right below.

    def on_reset(self):
        """Start the next episode on the center line, 2 m further along the
        track each episode (wrapping past the finish line, so every part of
        the lap gets its turn). The WORLDS tracks rotate every 10 episodes."""
        world = WORLDS[(self.episode // 10) % len(WORLDS)] if WORLDS else None
        respawn(world=world, start_m=self.episode * 2.0)

    # ══════════════════ machinery below ══════════════════

    def __init__(self):
        super().__init__()
        self.observation_space = spaces.Box(
            0, 255, (3, CAMERA_H, CAMERA_W), np.float32)
        self.action_space = spaces.Discrete(len(ACTIONS))
        self.actions = list(ACTIONS.values())
        self.episode = -1               # becomes 0 on the first reset()
        self._track_world = None

    def _load_track(self):
        current = sim_status().get("current")
        if current == self._track_world:
            return
        r = requests.get(f"{BASE_URL}/sim/api/route", timeout=5).json()
        if "waypoints" not in r:
            raise SystemExit("this world has no track route — switch to a "
                             "racing world first")
        center = np.asarray(r["waypoints"], float)
        inner = np.asarray(r["inner"], float)
        outer = np.asarray(r["outer"], float)
        if np.allclose(center[0], center[-1]):    # closed loop: drop dup row
            center, inner, outer = center[:-1], inner[:-1], outer[:-1]
        self._waypoints = center
        self._inner_pts = inner
        self._outer_pts = outer
        self._road = Polygon(r["outer"], [r["inner"]])
        self._bounds = requests.get(f"{BASE_URL}/sim/api/bounds", timeout=5).json()
        self._track_world = current

    def _wheel_points(self, x, y, yaw_rad):
        c, s = math.cos(yaw_rad), math.sin(yaw_rad)
        return [Point(x + dx * c - dy * s, y + dx * s + dy * c)
                for dx, dy in ((WHEELBASE / 2, TRACK_OF_CAR / 2),
                               (WHEELBASE / 2, -TRACK_OF_CAR / 2),
                               (-WHEELBASE / 2, TRACK_OF_CAR / 2),
                               (-WHEELBASE / 2, -TRACK_OF_CAR / 2))]

    def _refresh(self):
        self.obs = camera(CAMERA_W, CAMERA_H).transpose(2, 0, 1).astype(np.float32)
        # a busy machine (e.g. a model import converting on another core)
        # can stall the API past the timeout — retry before giving up
        for attempt in range(3):
            try:
                pose = sim_pose()
                odom = requests.get(f"{BASE_URL}/odom", timeout=2).json()
                objects = sim_objects()
                lights = requests.get(f"{BASE_URL}/sim/api/traffic_lights",
                                      timeout=2).json().get("lights", [])
                break
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(0.5)
        wheels = self._wheel_points(pose["x"], pose["y"], pose["yaw"])
        self._is_offtrack = not any(self._road.contains(w) for w in wheels)
        # crashed = a movable object moved since the episode started — only
        # the car can push one (reference poses are snapshotted in reset())
        self._is_crashed = any(
            o["movable"] and math.hypot(
                o["current"]["x"] - self._obj_ref.get(o["name"], o["current"])["x"],
                o["current"]["y"] - self._obj_ref.get(o["name"], o["current"])["y"]) > 0.03
            for o in objects)
        self.state = {
            "x": pose["x"], "y": pose["y"],
            "heading": math.degrees(pose["yaw"]),
            "linear_velocity": odom["velocity"]["linear"],
            "angular_velocity": odom["velocity"]["angular"],
            "waypoints_center": self._waypoints,
            "waypoints_inner": self._inner_pts,
            "waypoints_outer": self._outer_pts,
            "objects": objects,
            "traffic_lights": lights,
            "bounds": self._bounds,
            "steps": self._steps,
            "episode": self.episode,
        }

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        drive(0, 0)
        self.episode += 1
        self._steps = 0
        self.on_reset()
        look(CAMERA_PAN, CAMERA_TILT)   # the model's fixed viewpoint
        time.sleep(0.3)                 # let physics settle
        self._load_track()
        # crash detection baseline: where the objects are right now
        self._obj_ref = {o["name"]: o["current"] for o in sim_objects()}
        self._refresh()
        self._t_step = time.time()
        return self.obs, {}

    def step(self, action):
        # The Racing panel's settings (gear) apply LIVE, even mid-run —
        # one stat per step, re-read only when the file actually changed
        global settings_mtime, CAMERA_PAN, CAMERA_TILT
        try:
            m = os.path.getmtime(SETTINGS)
            if m != settings_mtime:
                settings_mtime = m
                with open(SETTINGS) as f:
                    cfg = json.load(f)
                for a in ACTIONS.values():
                    a["speed"] = float(cfg.get("speed", a["speed"]))
                ACTIONS["left"]["steering"] = abs(float(cfg.get("left", 20.0)))
                ACTIONS["right"]["steering"] = -abs(float(cfg.get("right", 20.0)))
                new_pan = float(cfg.get("pan", CAMERA_PAN))
                new_tilt = float(cfg.get("tilt", CAMERA_TILT))
                if (new_pan, new_tilt) != (CAMERA_PAN, CAMERA_TILT):
                    CAMERA_PAN, CAMERA_TILT = new_pan, new_tilt
                    look(CAMERA_PAN, CAMERA_TILT)
                print(f"settings applied: speed {ACTIONS['straight']['speed']}")
        except (OSError, ValueError):
            pass

        a = self.actions[int(action)]
        drive(a["speed"], a["steering"])
        # steady step period: sleep whatever remains of STEP_DT after the
        # time already spent since the last step
        time.sleep(max(0.0, STEP_DT - (time.time() - self._t_step)))
        self._t_step = time.time()
        self._steps += 1
        self._refresh()
        reward = self.reward()
        # all four wheels out, or an object was hit
        terminated = self._is_offtrack or self._is_crashed
        truncated = self._steps >= MAX_STEPS    # episode length limit
        overlay(f"reward {reward:+.2f}")
        return self.obs, reward, terminated, truncated, {"action": int(action)}

    def close(self):
        drive(0, 0)
        overlay("")


# reward.py (kept next to this run's data) IS the reward function — the
# Reward step seeds and edits it. A broken file fails the run loudly.
import importlib.util
try:
    _spec = importlib.util.spec_from_file_location("user_reward", "ml/reward.py")
    _user = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(_user)
except FileNotFoundError:
    raise SystemExit("ml/reward.py not found — open the Train page once "
                     "(it seeds the default reward function)")
PhysicarEnv.reward = _user.reward
print("reward.py loaded")


class Extractor:
    """Let PPO train our PhysicarNet directly, so what we deploy is exactly
    what was trained. Runs everything except the final action layer
    (mirrors the normalization inside PhysicarNet.forward)."""
    def __new__(cls, *a, **kw):
        from stable_baselines3.common.torch_layers import BaseFeaturesExtractor

        class _E(BaseFeaturesExtractor):
            def __init__(self, observation_space):
                super().__init__(observation_space, features_dim=256)
                self.net = PhysicarNet()

            def forward(self, obs):
                x = obs / 255.0 * 2.0 - 1.0
                return torch.relu(self.net.head[0](self.net.cnn(x)))
        return _E(*a, **kw)


def main():
    from stable_baselines3 import PPO
    from stable_baselines3.common.callbacks import BaseCallback
    from stable_baselines3.common.monitor import Monitor

    class Console(BaseCallback):
        """Progress per finished episode, and stop the car while the policy
        updates (it would drive blind otherwise)."""
        episodes = 0

        def _on_training_start(self):
            # PPO steps the optimizer once per minibatch — count those calls
            # for a real progress percentage while the network updates
            # (nothing on screen moves otherwise).
            opt = self.model.policy.optimizer
            total = self.model.n_epochs * math.ceil(N_STEPS / BATCH_SIZE)
            orig_step = opt.step
            counter = {"n": 0}
            self._new_update = lambda: counter.update(n=0)

            def step(*args, **kwargs):
                counter["n"] += 1
                pct = min(100, round(100 * counter["n"] / total))
                if counter["n"] % 12 == 0 or counter["n"] == total:
                    overlay(f"updating the model... {pct}%", ttl=30)
                    report(status="updating", update_pct=pct)
                return orig_step(*args, **kwargs)
            opt.step = step

        def _on_rollout_start(self):
            overlay("")     # clear the update banner — the car drives again
            save_checkpoint()   # post-update weights: never lose an update

        def _on_step(self):
            for info in self.locals["infos"]:
                if "episode" in info:   # Monitor adds this when one ends
                    self.episodes += 1
                    r, ln = info["episode"]["r"], info["episode"]["l"]
                    print(f"episode {self.episodes}  reward {r:.1f}  ({ln} steps)")
                    report(ep_reward=float(r), ep_length=int(ln), steps=self.num_timesteps, status="collecting")
            return True

        def _on_rollout_end(self):
            drive(0, 0)
            print(f"updating the model... "
                  f"{self.num_timesteps:,}/{TOTAL_STEPS:,} steps done")
            self._new_update()
            overlay("updating the model... 0%", ttl=30)
            report(steps=self.num_timesteps, status="updating", update_pct=0)

    env = Monitor(PhysicarEnv())
    report(status="collecting")
    print(f"training for {TOTAL_STEPS:,} steps — press Stop to end early "
          f"(the model trained so far is still saved)")

    model = PPO("CnnPolicy", env,
                n_steps=N_STEPS, batch_size=BATCH_SIZE,
                learning_rate=LEARNING_RATE, gamma=GAMMA,
                policy_kwargs={"features_extractor_class": Extractor,
                               "normalize_images": False,
                               "net_arch": []},
                verbose=0)
    if BASE_MODEL:
        # warm start: the base model's weights fill the policy backbone AND
        # its action layer — an SL-trained model works too (same network)
        base = PhysicarNet()
        base.load_state_dict(torch.load(f"ml/models/{BASE_MODEL}.pt",
                                        map_location="cpu", weights_only=True))
        model.policy.features_extractor.net.load_state_dict(base.state_dict())
        with torch.no_grad():
            model.policy.action_net.weight.copy_(base.head[2].weight)
            model.policy.action_net.bias.copy_(base.head[2].bias)
        print(f"continuing from model {BASE_MODEL}")

    def save_checkpoint():
        """The current policy as a plain PhysicarNet checkpoint — written
        atomically, so the runner can always file the LAST one into the
        model store, no matter how this run ends."""
        net = PhysicarNet()
        net.load_state_dict(model.policy.features_extractor.net.state_dict())
        with torch.no_grad():   # PPO's action layer becomes the final layer
            net.head[2].weight.copy_(model.policy.action_net.weight)
            net.head[2].bias.copy_(model.policy.action_net.bias)
        torch.save(net.state_dict(), "ml/checkpoint.pt.tmp")
        os.replace("ml/checkpoint.pt.tmp", "ml/checkpoint.pt")

    try:
        model.learn(total_timesteps=TOTAL_STEPS, callback=Console())
    except KeyboardInterrupt:
        pass                # Stop pressed: the checkpoint still counts
    env.close()
    save_checkpoint()       # the final weights
    report(status="done")
    print("\ntraining ended — the runner files the checkpoint as a model")


if __name__ == "__main__":
    main()
