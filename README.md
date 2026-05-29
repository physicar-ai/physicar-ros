# PhysiCar ROS 2

[![ROS 2 Jazzy](https://img.shields.io/badge/ROS%202-Jazzy-blue)](https://docs.ros.org/en/jazzy/)
[![Ubuntu 24.04](https://img.shields.io/badge/Ubuntu-24.04-E95420)](https://releases.ubuntu.com/24.04/)
[![License](https://img.shields.io/badge/License-Apache%202.0-green)](LICENSE)

[한국어](README.ko.md) | English

ROS 2 Jazzy stack for the **PhysiCar** Ackermann-steered RC robot.
Runs natively on Raspberry Pi 5 (Ubuntu 24.04) and inside a Gazebo
Harmonic simulation (GitHub Codespaces / desktop). Same topics, same
web API — the launch file determines the mode.

---

## Run modes at a glance

| Mode | How it starts | Hardware | Where it runs |
|---|---|---|---|
| **Real robot** | `physicar.service` → `physicar.sh` → `robot.launch.py` | RPi 5 + Yahboom board + RPLidar + Pi Camera | Raspberry Pi 5 (native) |
| **Simulation** | `sim.launch.py` | none | Codespaces / desktop + Gazebo Harmonic |

---

## Repository layout

```
physicar-ros/
├── deploy/                       # Device provisioning & runtime
│   ├── install-device.sh           # One-shot installer (run as root)
│   ├── README.md                   # Setup instructions
│   └── device/                     # Runtime files (symlinked to /etc/)
│       ├── physicar.sh               # Boot orchestrator (systemd ExecStart)
│       ├── etc/
│       │   ├── nginx/sites-available/physicar
│       │   ├── systemd/system/physicar.service
│       │   ├── systemd/system/physicar-myapp.service
│       │   ├── netplan/01-netcfg.yaml
│       │   ├── udev/rules.d/99-physicar.rules
│       │   └── ...                   # X11, NetworkManager, chromium, etc.
│       └── home/physicar/bashrc-append
├── updater.sh                    # Auto-updater (git tag based)
├── fastdds-lo.xml                # Loopback-only Fast DDS profile
├── physicar_bringup/             # System launch + drivers + utilities
│   ├── launch/
│   │   ├── robot.launch.py         # Real-robot launch
│   │   └── sim.launch.py           # Gazebo (sim) launch
│   ├── config/
│   │   ├── driver_params.yaml       # Hardware driver parameters
│   │   ├── ekf_params.yaml          # EKF sensor-fusion parameters
│   │   ├── slam_params.yaml         # SLAM Toolbox config
│   │   └── nav2_params.yaml         # Nav2 config
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
│       └── static/                 # Kiosk + studio web UI + login page
├── camera_ros/                   # [submodule] libcamera-based camera driver
├── rplidar_ros/                  # [submodule] RPLidar SDK driver
└── rf2o_laser_odometry/          # [submodule] Laser-only odometry
```

---

## Boot sequence — `deploy/device/physicar.sh`

On the real robot, `physicar.service` (systemd) runs `physicar.sh` as
the `physicar` user on every boot.

1. Load `~/physicar_ws/userdata/.env` (e.g. `DEV=true`).
2. System setup: swap, CPU governor, WiFi hotspot, hostname, Xvfb + VNC.
3. Start nginx, code-server, Bluetooth agent.
4. Source `/opt/ros/jazzy/setup.bash`.
5. `colcon build --symlink-install` (with one clean-build retry on failure).
6. Source `install/setup.bash`.
7. **DDS isolation** — `ROS_AUTOMATIC_DISCOVERY_RANGE=LOCALHOST` so
   other PhysiCars on the same network can't see our topics.
8. **Auto-updater** — unless DEV mode, starts `updater.sh` in the
   background. Periodically `git fetch --tags`; on new tag, checks out
   and kills the launch to trigger a rebuild.
9. **Build + launch loop**:
   - `ros2 launch physicar_bringup robot.launch.py`
   - On update signal, rebuilds and relaunches.

Every ROS node has `respawn=True, respawn_delay≈2s`. Killing a node
(`pkill -f node_name`) is the supported way to pick up new YAML / Python.

---

## Real-robot launch (`robot.launch.py`)

All nodes are spawned with `respawn=True`.

| Stage | Component | Notes |
|---|---|---|
| t=0  | `setup_audio.sh` | One-shot ALSA `dmix` setup for the USB sound card |
| t=0  | `robot_state_publisher` | Publishes TF from the URDF |
| t=0  | `physicar_driver_node` | ESC + servos + IMU + battery; consumes `/speed`, `/steering`, `/cmd_vel`, `/camera/pan`, `/camera/tilt` |
| t=0  | `rplidar_node` (RPLidar C1) | `/scan` |
| t=0  | `scan_filter_node` | `/scan` → `/scan_filtered` (drops NaN / out-of-range) |
| t=0  | `camera_node` (camera_ros) | libcamera capture + undistort + resize → `/camera/image_raw` and `.../compressed` |
| t=0  | `rf2o_laser_odometry_node` | `/scan_filtered` → `/odom/laser` (raw) |
| t=0  | `ekf_filter_node` | Fuses rf2o + IMU → `/odom` + TF |
| t=0  | `audio_node` | Subscribes `/audio` (mixes channels, MP3/PCM/WAV/OGG) |
| t=0  | `deepracer_node` | Always on; idle until a model is loaded |
| t=0  | `joy_node` (SDL2) | Reads `/dev/input/jsX`; `SDL_JOYSTICK_HIDAPI=0` to keep xpadneo BLE pads working |
| t≈2s | `joy_teleop_node` | Maps `/joy` → `/teleop/{speed,steering,camera/pan,camera/tilt}` and `/teleop/status` |
| t≈3s | `agent_node` | Auto-discovers topics/services/actions; loads custom tools |
| t≈3s | `play_intro` | Publishes `intro.mp3` to `/audio` (TRANSIENT_LOCAL, waits for subscriber) |
| t≈4s | `webserver_node` | FastAPI / uvicorn on `127.0.0.1:8000` |
| t≈10s| `topic_watchdog_node` | After a 30 s grace, SIGTERM nodes whose sensor topic is stale → respawn restarts them |

External processes (nginx, hotspot, etc.) are managed **outside** this
package — by the `physicar.sh` orchestrator on the device side and
by `supervisord` on the Codespaces side (see *Running in Codespaces*).

---

## Simulation launch (`sim.launch.py`)

Used in Codespaces / desktop sim environments. Hardware drivers are
skipped (`COLCON_IGNORE` markers also keep `camera_ros` / `rplidar_ros`
from being rebuilt).

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
  - **Gz → ROS 2:** `/imu`, `/scan`, `/joint_states`, `/camera/image_raw`, `/clock`
  - **ROS 2 → Gz:** `/cmd_vel`, `/camera/pan`, `/camera/tilt`
  - `GZ_PARTITION=physicar` so it only sees our world.
- `rf2o_laser_odometry` — identical to the real robot. `/scan_filtered` → `/odom/laser`.
- `ekf_filter_node` — fuses rf2o (`/odom/laser`) + IMU (`/imu`) →
  `/odom` + `odom→base_footprint` TF. Gazebo's internal pose TF is
  **not** bridged — sim uses the same odometry pipeline as real hardware.
- `topic_watchdog_node` — monitors `/odom/laser`. If rf2o stalls after a
  Gazebo world switch (sim time backward jump), sends SIGTERM → respawn
  restarts it.
- `image_transport republish raw → compressed` — provides `/camera/image_raw/compressed`.

Host-side requirements (handled by `physicar-sim` / Codespaces):

- Gazebo Harmonic + the PhysiCar SDF world/model
  ([`physicar-sim`](https://github.com/physicar-ai/physicar-sim))
- `gz launch websocket.gzlaunch` (port 9002) — gzweb 3D viewer
- `sim_api.py` (port 9003) — track switching, world status

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
| `/odom` | `nav_msgs/Odometry` | rf2o + EKF (IMU fusion); same pipeline on real and sim |
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
all handled by FastAPI; nginx does TLS termination and `80/443 → 8000`
proxying. **Auth bypass** for `127.0.0.0/8` and `10.42.0.0/24`
(hotspot); external IPs need a token issued via `/auth`.

Routers (mounted in `main.py`):

| Prefix | Router | What it does |
|---|---|---|
| `/health` | `health` | Liveness probe |
| `/auth`   | `auth`   | Token issuance / login page |
| `/info`   | `info`   | System info; reports `mode: "real"` or `"sim"` |
| `/state`, `/state/{odom,battery,imu,camera,camera/{pan,tilt},lidar,audio}` | `state` | JSON snapshot or SSE (`?stream=true` / `Accept: text/event-stream`). `/state/camera/image` returns JPEG (or MJPEG with `?stream=true`, optional `?width=&height=`). `/state/lidar?step=N` decimates the scan. |
| `/control/{speed,steering,camera/pan,camera/tilt,audio}` | `control` | Posts a single value to the matching ROS 2 topic |
| `/agent/tool/{list,get,call,set,delete,reset}` | `agent` | Manage and invoke agent tools (Python source + PEP 723 deps) |
| `/calibration`, `/calibration/{steering,pan,tilt,reverse,speed_gain}` | `calibration` | Read / write `/home/physicar/physicar_ws/userdata/calibration.json` |
| `/teleop/joy`, `/teleop/joy/mapping` | `joy` | Joystick mapping CRUD |
| `/teleop` | `teleop` | Source-agnostic teleop status / lock |
| `/network`, `/network/bluetooth` | `network`, `bluetooth` | WiFi / BT pairing |
| `/uistate` | `uistate` | Cross-tab UI state sync |
| `/settings/myapp`, `/settings/myapp/{start,stop,restart,log}` | `myapp` | Host-side student web app slot on port 5000 |
| `/deepracer` | `deepracer` | Model upload / list / select / start / stop |
| `/kiosk`, `/studio`, `/kiosk/calibration*` | `kiosk` | Kiosk + studio HTML UI |
| `/api/host` | `sim` | Sim machine management (Codespaces) |

OpenAPI / Swagger at `/docs`, ReDoc at `/redoc`.

### MyApp slot

`physicar-myapp.service` (or supervisord on Codespaces) runs a
single user-supplied Python script as the `physicar` user on port
5000, watching the script's directory with inotify and restarting on
change. nginx proxies `/myapp/` to it with caching disabled.

```bash
# Inspect
curl http://<host>/settings/myapp

# Register a script (starts immediately)
curl -X PUT http://<host>/settings/myapp \
     -H 'Content-Type: application/json' \
     -d '{"path": "/home/physicar/physicar_ws/userdata/myapp/main.py"}'

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
/home/physicar/physicar_ws/userdata/agent/
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
/home/physicar/physicar_ws/userdata/deepracer/
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
    max_pan: 30.0
    max_tilt: 30.0
    max_steering: 20.0                 # wheel angle, sine model derives the servo angle
    max_speed: 3.0                     # m/s

    # Center offsets (degrees from 90°)
    pan_center: 0.0
    tilt_center: 0.0
    steering_center: 0.0

    reverse_direction: false           # true = ESC polarity reversed
    speed_gain: 1.0                    # per-car speed gain (0.1 ~ 5.0)

    # Geometry (matches the URDF)
    wheel_radius: 0.0375               # 75 mm wheel
    wheelbase: 0.18                    # m
    track_width: 0.16                  # m

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

Calibration overrides at runtime live in `/home/physicar/physicar_ws/userdata/calibration.json`
and are merged on top of the YAML values; `CalibrationStatus.source`
tells you which one is currently active.

---

## On-device paths

| Path | Used for |
|---|---|
| `/home/physicar/physicar_ws/src/physicar-ros/` | Repository (source of truth) |
| `/home/physicar/physicar_ws/userdata/.env` | `DEV=true`, ... |
| `/home/physicar/physicar_ws/userdata/password` | Web auth password (falls back to serial hash if absent) |
| `/home/physicar/physicar_ws/userdata/calibration.json` | Calibration overrides (latched) |
| `/home/physicar/physicar_ws/userdata/agent/{tools,venv,deps.json}` | Agent tools sandbox |
| `/home/physicar/physicar_ws/userdata/deepracer/{models,config.json}` | DeepRacer models + inference config |
| `/home/physicar/physicar_ws/userdata/myapp/{run.sh,log}` | Student web app + log |
| `/etc/nginx/sites-available/physicar` | TLS-terminating reverse proxy (symlink → deploy/device/) |
| `/etc/systemd/system/physicar.service` | Service unit (symlink → deploy/device/) |

---

## Running

### Real robot

The `physicar.service` runs natively on boot. Logs:

```bash
journalctl -u physicar -f
ros2 topic list
```

Make code changes:

| Change | What to do |
|---|---|
| Python in any package (`--symlink-install`) | Just kill the node — `pkill -f physicar_driver_node` etc. The launch respawns it. |
| FastAPI code in DEV mode | Auto-reloaded via `os.execv` watchdog |
| YAML parameters | `pkill -f <node>` to pick them up |
| C++ (camera_ros, rf2o) | `colcon build --symlink-install` then kill the node |
| `nginx` site config | `sudo nginx -s reload` |

Service management:

```bash
sudo systemctl restart physicar
sudo systemctl stop physicar
sudo systemctl status physicar
```

### Running in Codespaces (sim)

Workflow when using
[`physicar-for-codespaces`](https://github.com/physicar-ai/physicar-for-codespaces)
(`physicar` branch):

1. **`onCreate`** — installs ROS 2 Jazzy, Gazebo Harmonic, `ros-jazzy-ros-gz`,
   nginx, noVNC, supervisor; pulls the `physicar-sim` and `physicar-ros`
   submodules; writes `SIM=true` into
   `/home/physicar/physicar_ws/userdata/.env`.
2. **`postStart`** — boots `supervisord` from
   `.devcontainer/supervisord.conf`. The supervised programs include
   `xvfb` + `openbox` + `x11vnc` + `novnc` (browser desktop), `nginx`
   (port 80), `gz_websocket` (gzweb on :9002), `sim_api` (:9003), the
   student `myapp` watcher, and `physicar`.
3. **`physicar` program** runs `sim.launch.py` with `SIM=true`, and
   the `ros_gz_bridge` connects to the Gazebo instance.

End-user URLs (forwarded as port 80):

- `/studio` — main web UI
- `/kiosk` — touchscreen kiosk
- `/gz/` — Gazebo 3D viewer (gzweb)
- `/vnc/` — desktop view (Gazebo Sim window, terminals)
- `/docs` — REST API reference

---

## SLAM & Navigation

SLAM and Nav2 run as separate processes (not part of `robot.launch.py`
or `sim.launch.py`). The robot-specific config files are referenced via
environment variables set in `~/.bashrc`:

| Variable | Points to |
|---|---|
| `$SLAM_PARAMS_FILE` | `physicar_bringup/config/slam_params.yaml` |
| `$NAV2_PARAMS_FILE` | `physicar_bringup/config/nav2_params.yaml` |

### SLAM (map building)

```bash
ros2 launch slam_toolbox online_async_launch.py \
    slam_params_file:=$SLAM_PARAMS_FILE use_sim_time:=true
```

Drive the robot (teleop / web UI) to explore the environment, then save the map:

```bash
ros2 run nav2_map_saver map_saver_cli -f ~/maps/my_map --ros-args -p use_sim_time:=true
```

### Navigation (autonomous driving)

Requires a saved map. Run localization and navigation in **separate terminals**:

```bash
# Terminal 1 — localization (map_server + AMCL)
ros2 launch nav2_bringup localization_launch.py \
    map:=~/maps/my_map.yaml params_file:=$NAV2_PARAMS_FILE use_sim_time:=true

# Terminal 2 — navigation stack
ros2 launch nav2_bringup navigation_launch.py \
    params_file:=$NAV2_PARAMS_FILE use_sim_time:=true
```

Send a goal via RViz2 "Nav2 Goal" button or CLI:

```bash
ros2 action send_goal /navigate_to_pose nav2_msgs/action/NavigateToPose \
    "{pose: {header: {frame_id: 'map'}, pose: {position: {x: 1.0, y: 0.0, z: 0.0}, orientation: {w: 1.0}}}}"
```

> **Note:** `navigation_launch.py` is patched at install time to disable
> `docking_server` and `route_server` (not needed for PhysiCar).
> On real hardware, omit `use_sim_time:=true` (it defaults to `false`).

### Key parameters (tuned for PhysiCar)

| Parameter | Value | Rationale |
|---|---|---|
| Controller frequency | 5 Hz | CPU-friendly for Codespaces / RPi 5 |
| MPPI batch size | 500 | Lightweight; sufficient for indoor |
| MPPI model_dt | 0.2 s | Matches controller period |
| Deadband velocity | 0.3 m/s | ESC dead zone (motor won't spin below this) |
| Min turning radius | 0.4 m | Ackermann geometry (wheelbase 0.18 m) |
| Footprint | 0.30 × 0.22 m | Body + wheel clearance |
| LiDAR range | 0.15 – 12.0 m | RPLiDAR C1 specs |

---

## License

Copyright 2026 **AICASTLE Inc.** (주식회사 에이아이캐슬).
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
