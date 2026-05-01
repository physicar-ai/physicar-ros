# PhysiCar ROS 2

[![ROS 2 Jazzy](https://img.shields.io/badge/ROS%202-Jazzy-blue)](https://docs.ros.org/en/jazzy/)
[![Ubuntu 24.04](https://img.shields.io/badge/Ubuntu-24.04-E95420)](https://releases.ubuntu.com/24.04/)
[![License](https://img.shields.io/badge/License-Apache%202.0-green)](LICENSE)

[한국어](README.ko.md) | English

ROS 2 Jazzy stack for the **PhysiCar** Ackermann-steered RC robot.
Runs unchanged on real hardware (Raspberry Pi 5) and inside a Gazebo
Harmonic simulation (GitHub Codespaces / desktop). Same launch file, same
topics, same web API — only `SIM=true` flips the mode.

---

## Run modes at a glance

| Mode | How it starts | Hardware | Where Gazebo runs |
|---|---|---|---|
| **Real robot** | `entrypoint.sh` (no `SIM`) → `robot.launch.py` | RPi 5 + Yahboom board + RPLidar + Pi Camera | n/a |
| **Simulation** | `entrypoint.sh` with `SIM=true` → `sim.launch.py` | none | host (gz sim, container uses `--network host`) |

In sim mode the container ships only the upper-layer ROS nodes; the world
runs in a separate Gazebo Harmonic process on the host and is bridged in
via `ros_gz_bridge`. The web UI, REST API, agent, DeepRacer inference
and gamepad teleop are identical to the real robot.

---

## Repository layout

```
physicar-ros/
├── entrypoint.sh                 # Container entrypoint (build → launch)
├── physicar_bringup/             # System launch + drivers + utilities
│   ├── launch/
│   │   ├── robot.launch.py         # Real-robot launch
│   │   └── sim.launch.py           # Gazebo (sim) launch
│   ├── config/driver_params.yaml   # All node parameters
│   ├── physicar_bringup/
│   │   ├── yahboom_board.py        # Serial protocol for Yahboom expansion board
│   │   └── servo_controller.py     # Servo angle clipping / mapping
│   ├── scripts/
│   │   ├── physicar_driver_node.py   # ESC, servos, IMU, battery, Ackermann
│   │   ├── audio_node.py             # /audio playback + mixing
│   │   ├── camera_proc_node.py       # Camera post-processing helper
│   │   ├── cmd_vel_adapter_node.py   # /speed+/steering ↔ /cmd_vel (sim only)
│   │   ├── scan_filter_node.py       # /scan → /scan_filtered (clean inf/nan)
│   │   ├── topic_watchdog_node.py    # Restart trigger on stale sensor topics
│   │   ├── setup_audio.sh            # USB audio + dmix init
│   │   └── calibration/              # measure_min_speed.py, pulse_coasting_test.py
│   └── sounds/intro.mp3              # Boot chime
├── physicar_description/         # URDF/xacro (single source of truth for geometry)
├── physicar_interfaces/          # Custom msg / srv definitions
├── physicar_teleop/              # Gamepad teleop node (joy_teleop_node)
├── physicar_agent/               # AI-agent runtime + tool registry
│   └── physicar_agent/
│       ├── agent_node.py           # rclpy spinner; owns the shared core
│       ├── core.py                 # Auto-discovery topic/service/action proxies
│       ├── registry.py             # Tool loader + isolated venv + PEP 723 deps
│       └── builtin/{control,state,music}.py
├── physicar_deepracer/           # DeepRacer ONNX inference pipeline
├── physicar_webserver/           # FastAPI web server (port 8000)
│   └── physicar_webserver/
│       ├── main.py                 # FastAPI app
│       ├── ros_bridge.py           # rclpy ↔ FastAPI bridge
│       ├── state_manager.py        # Topic snapshot cache + SSE fan-out
│       ├── routers/                # /state /control /agent /calibration ...
│       └── static/                 # Kiosk + studio web UI
├── camera_ros/                   # [submodule] libcamera-based camera driver
├── rplidar_ros/                  # [submodule] RPLidar SDK driver
└── rf2o_laser_odometry/          # [submodule] Laser-only odometry
```

---

## Boot sequence — `entrypoint.sh`

1. Read `/opt/physicar/.env` (e.g. `SIM=true`, `DEV=true`).
2. Source `/opt/ros/jazzy/setup.bash`.
3. Toggle `COLCON_IGNORE` on `camera_ros` / `rplidar_ros` when `SIM=true`
   (their drivers depend on real hardware).
4. `colcon build --symlink-install` (with one clean-build retry on failure).
5. Source `install/setup.bash`.
6. **DDS isolation** — `ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET` plus the
   loopback-only Fast DDS XML at `/opt/physicar/fastdds-lo.xml`. SHM is
   disabled (UDP only) so the container's root nodes don't trip on UID
   boundaries with the host `physicar` user, and so other PhysiCars on
   the same WiFi can't see our topics.
7. Launch:
   - `SIM=true` → `ros2 launch physicar_bringup sim.launch.py`
   - else → `ros2 launch physicar_bringup robot.launch.py`
8. `exec sleep infinity` keeps the container alive after a launch crash so
   logs remain inspectable.

Every ROS node has `respawn=True, respawn_delay≈2s`. Killing a node
(`pkill -f node_name`) is the supported way to pick up new YAML / Python.

---

## Real-robot launch (`robot.launch.py`)

Subset enabled when `SIM` is unset.

| Stage | Component | Notes |
|---|---|---|
| t=0  | `setup_audio.sh` | One-shot ALSA `dmix` setup for the USB sound card |
| t=0  | `robot_state_publisher` | Publishes TF from the URDF |
| t=0  | `physicar_driver_node` | ESC + servos + IMU + battery; consumes `/speed`, `/steering`, `/cmd_vel`, `/camera/pan`, `/camera/tilt` |
| t=0  | `rplidar_node` (RPLidar C1) | `/scan` |
| t=0  | `scan_filter_node` | `/scan` → `/scan_filtered` (drops NaN / out-of-range) |
| t=0  | `camera_node` (camera_ros) | libcamera capture + undistort + resize → `/camera/image_raw` and `.../compressed` |
| t=0  | `rf2o_laser_odometry_node` | `/scan_filtered` → `/odom` + TF |
| t=0  | `audio_node` | Subscribes `/audio` (mixes channels, MP3/PCM/WAV/OGG) |
| t=0  | `deepracer_node` | Always on; idle until a model is loaded |
| t=0  | `joy_node` (SDL2) | Reads `/dev/input/jsX`; `SDL_JOYSTICK_HIDAPI=0` to keep xpadneo BLE pads working |
| t≈2s | `joy_teleop_node` | Maps `/joy` → `/teleop/{speed,steering,camera/pan,camera/tilt}` and `/teleop/status` |
| t≈3s | `agent_node` | Auto-discovers topics/services/actions; loads custom tools |
| t≈3s | `play_intro` | Publishes `intro.mp3` to `/audio` (TRANSIENT_LOCAL, waits for subscriber) |
| t≈4s | `webserver_node` | FastAPI / uvicorn on `127.0.0.1:8000` |
| t≈10s| `topic_watchdog_node` | After a 30 s grace, SIGTERM nodes whose sensor topic is stale → respawn restarts them |

External processes (nginx, hotspot, etc.) are managed **outside** this
package — by the host's `physicar.sh` orchestrator on the device side and
by `supervisord` on the Codespaces side (see *Running in Codespaces*).

---

## Simulation launch (`sim.launch.py`)

Activated by `SIM=true`. Hardware drivers are skipped (`COLCON_IGNORE`
markers also keep `camera_ros` / `rplidar_ros` from being rebuilt).

Nodes that run:

- `robot_state_publisher` (with `use_sim_time: True`)
- `cmd_vel_adapter_node` — replaces the real driver. Inverse Ackermann
  conversion `/speed + /steering → /cmd_vel`; also publishes a constant
  full `/battery_state`, default `/physicar_driver/calibration_status`
  (TRANSIENT_LOCAL) and a dummy `/servo/commands` subscriber so every
  topic that exists on the real robot also exists in sim.
- `scan_filter_node`, `deepracer_node`, `joy_node`, `joy_teleop_node`,
  `agent_node`, `webserver_node` — bit-for-bit the same as on hardware.
- `ros_gz_bridge` (`parameter_bridge`) — bridges:
  - **Gz → ROS 2:** `/imu`, `/scan`, `/odom`, `/joint_states`, `/camera/image_raw`, `/clock`
  - **ROS 2 → Gz:** `/cmd_vel`, `/camera/pan`, `/camera/tilt`
  - `GZ_PARTITION=physicar` so it only sees our world.
- `image_transport republish raw → compressed` — provides `/camera/image_raw/compressed`.

Host-side requirements (handled by `physicar-sim` / Codespaces):

- Gazebo Harmonic + the PhysiCar SDF world/model
  ([`physicar-sim`](https://github.com/physicar-ai/physicar-sim))
- `gz launch websocket.gzlaunch` (port 9002) — gzweb 3D viewer
- `sim_api.py` (port 9003) — track switching, world status

Topic / service / message inventory is **identical** between modes —
the same agent tools and HTTP routes work in both.

---

## ROS 2 interface

### Topics

#### Low-level control (consumed by the driver / cmd_vel_adapter)

| Topic | Type | Units | Notes |
|---|---|---|---|
| `/speed` | `std_msgs/Float64` | m/s | Direct throttle |
| `/steering` | `std_msgs/Float64` | rad | Wheel angle (sine-model → servo) |
| `/camera/pan` | `std_msgs/Float64` | rad | Pan servo |
| `/camera/tilt` | `std_msgs/Float64` | rad | Tilt servo |
| `/audio` | `physicar_interfaces/Audio` | — | PCM/MP3/WAV/OGG, multi-channel |

#### High-level control

| Topic | Type | Notes |
|---|---|---|
| `/cmd_vel` | `geometry_msgs/Twist` | `linear.x` (m/s) + `angular.z` (rad/s); driver runs Ackermann `δ = atan(ω·L / v)` |
| `/teleop/speed`, `/teleop/steering`, `/teleop/camera/pan`, `/teleop/camera/tilt` | `Float64` | Same units as the un-prefixed topics |
| `/teleop/status` | `physicar_interfaces/TeleopStatus` | While `drive_engaged=true` (and fresh) the driver ignores `/speed`, `/steering`, `/cmd_vel` and follows the teleop topics instead. Same idea for `/camera/*`. |

#### Sensors / state

| Topic | Type | Notes |
|---|---|---|
| `/scan`, `/scan_filtered` | `sensor_msgs/LaserScan` | RPLidar C1 / Gazebo lidar |
| `/odom` | `nav_msgs/Odometry` | rf2o on real robot, Gazebo plugin in sim |
| `/imu` | `sensor_msgs/Imu` | MPU on Yahboom board / Gazebo IMU |
| `/camera/image_raw`, `/camera/image_raw/compressed` | `Image` / `CompressedImage` | 480×360 by default |
| `/battery_state` | `sensor_msgs/BatteryState` | 1 Hz; sim publishes constant 8.4 V / 100% |
| `/joint_states` | `sensor_msgs/JointState` | Steering + wheel + camera joints |
| `/physicar_driver/calibration_status` | `physicar_interfaces/CalibrationStatus` | Latched (TRANSIENT_LOCAL) |
| `/deepracer/inference` | `physicar_interfaces/DeepracerInference` | speed, steering and full action probability vector |
| `/clock` | `rosgraph_msgs/Clock` | Sim only |

### Services

| Service | Type | Description |
|---|---|---|
| `/physicar_driver/get_calibration` | `physicar_interfaces/GetCalibration` | Read calibration JSON |
| `/physicar_driver/set_calibration` | `physicar_interfaces/SetCalibration` | Write calibration JSON |
| `/deepracer/load_model` | `physicar_interfaces/DeepracerLoadModel` | Load ONNX + metadata |
| `/deepracer/unload_model` | `physicar_interfaces/DeepracerUnloadModel` | |
| `/deepracer/control` | `physicar_interfaces/DeepracerControl` | `start` / `stop` |
| `/deepracer/status` | `physicar_interfaces/DeepracerStatus` | |
| `/deepracer/set_config` | `physicar_interfaces/DeepracerSetConfig` | speed scale, action selection, pan/tilt |
| `/agent/tool/list`, `.../get`, `.../set`, `.../delete`, `.../call`, `.../reset` | `physicar_interfaces/Tool*` | Tool CRUD + invocation |
| `/teleop/joy/get_mapping`, `.../set_mapping` | `physicar_interfaces/{Get,Set}JoyMapping` | Per-button bindings |

---

## Web server (FastAPI on `:8000`)

`webserver_node.py` runs uvicorn directly. CORS, gzip, auth and SSE are
all handled by FastAPI; nginx (when present) only does TLS termination
and `80/443 → 8000` proxying. **Auth bypass** for `127.0.0.0/8` and
`10.42.0.0/24` (hotspot); external IPs need a token issued via `/auth`.

Routers (mounted in `main.py`):

| Prefix | Router | What it does |
|---|---|---|
| `/health` | `health` | Liveness probe |
| `/auth`   | `auth`   | Token issuance / login page |
| `/info`   | `info`   | System info; reports `mode: "real"` or `"sim"` |
| `/state`, `/state/{odom,battery,imu,camera,camera/{pan,tilt},lidar,audio}` | `state` | JSON snapshot or SSE (`?stream=true` / `Accept: text/event-stream`). `/state/camera/image` returns JPEG (or MJPEG with `?stream=true`, optional `?width=&height=`). `/state/lidar?step=N` decimates the scan. |
| `/control/{speed,steering,camera/pan,camera/tilt,audio}` | `control` | Posts a single value to the matching ROS 2 topic |
| `/agent/tool/{list,get,call,set,delete,reset}` | `agent` | Manage and invoke agent tools (Python source + PEP 723 deps) |
| `/calibration`, `/calibration/{steering,pan,tilt,reverse,emergency}` | `calibration` | Read / write `/opt/physicar/calibration.json` |
| `/teleop/joy`, `/teleop/joy/mapping` | `joy` | Joystick mapping CRUD |
| `/teleop` | `teleop` | Source-agnostic teleop status / lock |
| `/network`, `/network/bluetooth` | `network`, `bluetooth` | WiFi / BT pairing |
| `/uistate` | `uistate` | Cross-tab UI state sync |
| `/settings/myapp`, `/settings/myapp/{start,stop,restart,log}` | `myapp` | Host-side student web app slot on port 5000 |
| `/deepracer` | `deepracer` | Model upload / list / select / start / stop |
| `/kiosk`, `/studio`, `/kiosk/calibration*` | `kiosk` | Kiosk + studio HTML UI |

OpenAPI / Swagger at `/docs`, ReDoc at `/redoc`.

### MyApp slot

The host's `physicar-myapp.service` (or supervisord on Codespaces) runs
a single user-supplied Python script as the `physicar` user on port
5000, watching the script's directory with inotify and restarting on
change. nginx proxies `/myapp/` to it with caching disabled.

```bash
# Inspect
curl http://<host>/settings/myapp

# Register a script (starts immediately)
curl -X PUT http://<host>/settings/myapp \
     -H 'Content-Type: application/json' \
     -d '{"path": "/opt/physicar/myapp/main.py"}'

# Tail logs
curl 'http://<host>/settings/myapp/log?tail=200'
```

The script must bind to `0.0.0.0:int(os.environ.get("PORT", 5000))`.

---

## Agent runtime (`physicar_agent`)

`agent_node` owns the rclpy spin loop. `core.py` exposes a ROS-CLI-flavoured
proxy API to user tools:

```python
from physicar_agent import topic, service, action

topic['/odom']                  # latest dict-converted message
topic.raw('/imu')               # original ROS 2 message
topic.pub('/cmd_vel', {'linear': {'x': 0.3}, 'angular': {'z': 0.0}})
topic.list()                    # [(name, type), ...]

service('/physicar_driver/set_calibration', {...})
action('/navigate_to_pose', {...})  # blocking
```

Topic / service / action discovery is automatic — the core walks the
graph at startup, snapshots publisher QoS, and creates matching
subscriptions and clients. New topics are picked up on `topic.refresh()`.

Tool storage:

```
/opt/physicar/agent/
├── tools/         # one .py file per tool, must define def tool(...)
├── venv/          # isolated venv with system-site-packages
└── deps.json      # PEP 723 reference counts; uninstalls when refcount→0
```

Tools may declare dependencies via inline PEP 723 metadata; the registry
parses it, installs into `venv`, and rolls back the file if loading
fails.

---

## DeepRacer

`deepracer_node` always runs but is idle until a model is loaded.

```
/opt/physicar/deepracer/
├── models/<name>/{model.onnx, model_metadata.json}
└── config.json     # {"action_selection": "greedy"|"stochastic", "speed_scale": 1.0, "pan": 0.0, "tilt": 0.0}
```

```bash
# Load + start
ros2 service call /deepracer/load_model    physicar_interfaces/srv/DeepracerLoadModel "{model_name: 'my_model'}"
ros2 service call /deepracer/control       physicar_interfaces/srv/DeepracerControl   "{command: 'start'}"
# Inspect
ros2 service call /deepracer/status        physicar_interfaces/srv/DeepracerStatus    "{}"
ros2 topic echo  /deepracer/inference
# Stop / unload
ros2 service call /deepracer/control       physicar_interfaces/srv/DeepracerControl   "{command: 'stop'}"
ros2 service call /deepracer/unload_model  physicar_interfaces/srv/DeepracerUnloadModel "{}"
```

Or use the equivalent `/deepracer/...` HTTP routes.

---

## Configuration — `physicar_bringup/config/driver_params.yaml`

Excerpt; see the file for the full set.

```yaml
physicar_driver:
  ros__parameters:
    serial_port: "/dev/yahboom"        # udev symlink, not /dev/ttyUSB*
    baudrate: 115200

    # Servo limits (degrees)
    max_pan: 45.0
    max_tilt: 45.0
    max_steering: 25.0                 # wheel angle, sine model derives the servo angle
    max_speed: 3.0                     # m/s

    # Center offsets (degrees from 90°)
    pan_center: 0.0
    tilt_center: 0.0
    steering_center: 0.0

    reverse_direction: false           # true = ESC polarity reversed

    # Emergency stop (LiDAR-based)
    emergency_enabled: true
    emergency_angle_range: 30.0        # total ° (±15° from heading)
    emergency_front_margin: 0.25       # m
    emergency_rear_margin: 0.15        # m

    # Geometry (matches the URDF)
    wheel_radius: 0.0375               # 75 mm wheel
    wheelbase: 0.18                    # m
    track_width: 0.16                  # m
    steering_ratio: 2.0                # sine model: servo = arcsin(sin(wheel) / k)

rplidar_node:
  ros__parameters:
    serial_port: "/dev/rplidar"
    serial_baudrate: 460800
    flip_x_axis: true                  # mounting orientation correction
    scan_mode: "Standard"

camera:
  ros__parameters:
    width: 640
    height: 480
    format: "BGR888"
    FrameDurationLimits: [66666, 66666]   # 15 fps
    AwbEnable: false
    ColourGains: [0.9, 1.0]
    AeEnable: false
    ExposureTime: 10000
    AnalogueGain: 1.5
    # Identity CCM — OV5647's default CCM assumes an IR-cut filter
    ColourCorrectionMatrix: [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    # Capture 640×480 → undistort + resize → publish 480×360
    undistort.fx: 387.89
    undistort.fy: 387.19
    undistort.cx: 312.63
    undistort.cy: 229.36
    undistort.k1: -0.3675
    undistort.k2:  0.1717
    undistort.p1: -0.0021
    undistort.p2: -0.0009
    undistort.k3: -0.0445
    undistort.dist_scale: 0.7          # 0=off, 0.7≈98° FOV (default), 1.0=full
    undistort.out_width: 480
    undistort.out_height: 360

rf2o_laser_odometry:
  ros__parameters:
    laser_scan_topic: "/scan_filtered"
    publish_tf: true
    base_frame_id: "base_footprint"
    odom_frame_id: "odom"
    freq: 20.0
```

Calibration overrides at runtime live in `/opt/physicar/calibration.json`
and are merged on top of the YAML values; `CalibrationStatus.source`
tells you which one is currently active.

---

## On-device paths

| Path | Used for |
|---|---|
| `/opt/physicar/.env` | `SIM=true`, `DEV=true`, ... |
| `/opt/physicar/password` | Web auth password (defaults to `physicar` in DEV / SIM) |
| `/opt/physicar/calibration.json` | Calibration overrides (latched) |
| `/opt/physicar/fastdds-lo.xml` | Loopback-only Fast DDS profile |
| `/opt/physicar/agent/{tools,venv,deps.json}` | Agent tools sandbox |
| `/opt/physicar/deepracer/{models,config.json}` | DeepRacer models + inference config |
| `/opt/physicar/myapp/{run.sh,log}` | Student web app + log |
| `/etc/nginx/...` | TLS-terminating reverse proxy (real robot) |

---

## Running

### Real robot

The Pi image bakes everything in; the `physicar` Docker container runs
`entrypoint.sh` automatically on boot. Logs:

```bash
docker logs -f physicar
ros2 topic list
```

Make code changes:

| Change | What to do |
|---|---|
| Python in any package (`--symlink-install`) | Just kill the node — `pkill -f physicar_driver_node` etc. The launch respawns it. |
| FastAPI code in DEV mode | Auto-reloaded via `os.execv` watchdog |
| YAML parameters | `pkill -f <node>` to pick them up |
| C++ (camera_ros, rf2o) | `colcon build --symlink-install` then kill the node |
| `nginx.conf` | `nginx -s reload` (host) |

### Running in Codespaces (sim)

Workflow when using
[`physicar-for-codespaces`](https://github.com/physicar-ai/physicar-for-codespaces)
(`physicar` branch):

1. **`onCreate`** — installs ROS 2 Jazzy, Gazebo Harmonic, `ros-jazzy-ros-gz`,
   nginx, noVNC, supervisor; pulls the `physicar/sim:1` Docker image; pulls
   the `physicar-sim` and `physicar-ros` submodules; writes
   `SIM=true` into `/opt/physicar/.env`.
2. **`postStart`** — boots `supervisord` from
   `.devcontainer/supervisord.conf`. The supervised programs include
   `xvfb` + `openbox` + `x11vnc` + `novnc` (browser desktop), `nginx`
   (port 80), `gz_websocket` (gzweb on :9002), `sim_api` (:9003), the
   student `myapp` watcher, and `physicar`.
3. **`physicar` program** runs `physicar.sh`, which
   `docker run -d`s the `physicar/sim:1` image with `--network host`,
   bind-mounts `/opt/physicar` and the `physicar-ros` submodule into
   `/root/ros2_ws/src/physicar-ros`, and executes
   `/root/ros2_ws/src/physicar-ros/entrypoint.sh`. With `SIM=true`,
   that triggers `sim.launch.py` and the `ros_gz_bridge` reaches the
   host's Gazebo via the shared host network.

End-user URLs (forwarded as port 80):

- `/studio` — main web UI
- `/kiosk` — touchscreen kiosk
- `/gz/` — Gazebo 3D viewer (gzweb)
- `/vnc/` — desktop view (Gazebo Sim window, terminals)
- `/docs` — REST API reference

---

## License

Copyright 2025 **AICASTLE Inc.** (주식회사 에이아이캐심).
“PhysiCar” is a trademark of AICASTLE Inc.

Unless noted otherwise (see the table below for vendored submodules), all
code in this repository is licensed under **Apache License 2.0** — see
[LICENSE](LICENSE) and [NOTICE](NOTICE).

| Package | License |
|---|---|
| `physicar_*` (this project) | Apache-2.0 |
| `camera_ros` (submodule) | MIT |
| `rplidar_ros` (submodule) | BSD-2-Clause |
| `rf2o_laser_odometry` (submodule) | GPL-3.0 |
