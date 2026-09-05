# PhysiCar ROS 2

[![ROS 2 Jazzy](https://img.shields.io/badge/ROS%202-Jazzy-blue)](https://docs.ros.org/en/jazzy/)
[![Ubuntu 24.04](https://img.shields.io/badge/Ubuntu-24.04-E95420)](https://releases.ubuntu.com/24.04/)
[![License](https://img.shields.io/badge/License-GPL--3.0-blue)](LICENSE)

<p align="center">
  <img src="logo.png" alt="logo" width="480" style="max-width: 100%;">
</p>

The ROS 2 Jazzy stack for **PhysiCar AI**, a Physical AI education platform.

### 🌐 Official site: [https://physicar.ai](https://physicar.ai)

## Vehicle Specs

| Item | Spec |
|------|------|
| Computer | Raspberry Pi 5 (8 GB) |
| LiDAR | 360°, 0.15–16 m, 10 Hz |
| IMU | 9-axis (accelerometer + gyroscope + magnetometer), 50 Hz |
| Camera | 480×360, 15 fps, MF, FOV 100°, Night Vision |
| Camera Pan | ±30° |
| Camera Tilt | ±30° |
| Battery | 2S Lithium 7.4 V |
| Steering | Ackermann |
| Max Speed | 3.0 m/s |
| Max Wheel Steering Angle | ±20° |

## Installation

See [deploy/README.md](deploy/README.md). The source is installed at `/opt/physicar/src/physicar-ros`.

## Run Modes

Configured via the `.env` file (`/opt/physicar/userdata/.env`).

| Variable | Default | Description |
|----------|---------|-------------|
| `SIM` | `false` | `false`: run on the real robot, `true`: run on physicar-sim (simulation environment) |
| `DEV` | `false` | `false`: auto-update enabled, `true`: auto-update disabled, code can be edited directly |

## ROS 2 Interfaces

### Sensors

| Name | Kind | Type | Description |
|------|------|------|-------------|
| `/camera/image_raw` | topic | [`Image`](https://docs.ros2.org/latest/api/sensor_msgs/msg/Image.html) | Camera image (RGB, undistorted 480×360) |
| `/battery_state` | topic | [`BatteryState`](https://docs.ros2.org/latest/api/sensor_msgs/msg/BatteryState.html) | Battery state (1 Hz) |
| `/imu` | topic | [`Imu`](https://docs.ros2.org/latest/api/sensor_msgs/msg/Imu.html) | IMU (50 Hz) — orientation not provided (`orientation_covariance[0] = -1`) |
| `/odom` | topic | [`Odometry`](https://docs.ros2.org/latest/api/nav_msgs/msg/Odometry.html) | Odometry |
| `/scan` | topic | [`LaserScan`](https://docs.ros2.org/latest/api/sensor_msgs/msg/LaserScan.html) | LiDAR scan (raw) |
| `/scan_filtered` | topic | [`LaserScan`](https://docs.ros2.org/latest/api/sensor_msgs/msg/LaserScan.html) | LiDAR scan (filtered) |

### Control

| Name | Kind | Type | Description |
|------|------|------|-------------|
| `/cmd_vel` | topic | [`Twist`](https://docs.ros2.org/latest/api/geometry_msgs/msg/Twist.html) | Velocity + steering (Ackermann conversion) |
| `/speed` | topic | [`Float64`](https://docs.ros2.org/latest/api/std_msgs/msg/Float64.html) | Speed (m/s) — commands expire after the driver's `cmd_timeout` (default 1 s, `0` disables) without renewal; publish periodically for sustained driving |
| `/steering` | topic | [`Float64`](https://docs.ros2.org/latest/api/std_msgs/msg/Float64.html) | Steering angle (rad) |
| `/camera/pan` | topic | [`Float64`](https://docs.ros2.org/latest/api/std_msgs/msg/Float64.html) | Camera pan (rad) |
| `/camera/tilt` | topic | [`Float64`](https://docs.ros2.org/latest/api/std_msgs/msg/Float64.html) | Camera tilt (rad) |

## Web API

Interactive docs at `/docs` (OpenAPI).

### Sensor Queries

Query endpoints support real-time streaming via `?stream=true` (camera uses MJPEG, others use SSE).

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/states` | Full state snapshot (select with `?include=odom,battery,imu`) |
| `GET` | `/speed` | Speed (m/s) |
| `GET` | `/steering` | Steering angle (rad) |
| `GET` | `/odom` | Odometry |
| `GET` | `/battery` | Battery state |
| `GET` | `/imu` | IMU — acceleration + gyro (6-axis; `orientation` is not provided and reads all-zero) |
| `GET` | `/lidar` | LiDAR scan |
| `GET` | `/camera` | Camera image (JPEG, resize with `?width`/`?height`; `?format=ansi` draws it right in the terminal, `&stream=true` makes it a live terminal view) |
| `GET` | `/camera/pan` | Camera pan angle |
| `GET` | `/camera/tilt` | Camera tilt angle |

### Control (Publish)

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/speed` | Speed command — `{"value": m/s, "duration": seconds?}`. Without `duration` it expires after `cmd_timeout` (~1 s) unless renewed. With `duration` the server keeps the command alive, publishes 0 at the end, and the response returns after the drive finishes (`stopped`) or when a newer command supersedes it |
| `POST` | `/steering` | Steering command |
| `POST` | `/camera/pan` | Camera pan |
| `POST` | `/camera/tilt` | Camera tilt |
| `WS` | `/speed/stream`, `/steering/stream` | Streaming write — each frame is the same `{"value": x}` as the POST. Dead-man switch: on disconnect the value is zeroed, so a dead client can't leave the robot driving. Used by the App control UI |

### Audio

Command-based playback on the robot speaker (played in the browser viewer in SIM). Send a command, the server handles decoding and buffering.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/audio` | List of currently playing items |
| `POST` | `/audio/play` | Play one of `url` / `path` / `data` (base64 audio file). Options: `volume` (0–1), `loop`, `replace` |
| `POST` | `/audio/stop` | Stop by `id`, or everything with `{"all": true}` |
| `POST` | `/audio/volume` | Change volume of a playing item (`id`, `volume` 0–1) |
| `WS` | `/audio/stream` | Realtime PCM16 playback stream (`?sample_rate=24000&channels=1&volume=1.0`), e.g. OpenAI Realtime API voice output. Binary frames = raw PCM16; close = stop |

### Racing

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/racing/run` | Start a job — `{"job": "rb-run" \| "sl-train" \| "rl-train" \| "ml-run", "model"?, "base"?, "steps"?, "worlds"?}`. Everything about a run rides on the request: `model` (ml-run; default newest), `base` (train jobs; continue from), `steps`/`worlds` (rl-train). Requesting `ml-run` while ml-run is already driving swaps the model LIVE — no restart |
| `POST` | `/racing/stop` | Stop the running job |
| `GET` | `/racing/status` | Lock, run gates, log tail, training progress |
| `GET`/`POST` | `/racing/rb/settings` | Rule-Based settings, applied LIVE (`speed, gain, hue*, sat*, val*, crop, pan, tilt`; `{"reset": true}`) |
| `GET`/`POST` | `/racing/ml/settings` | The ML profile SHARED by Supervised/Reinforcement, applied LIVE (`speed, left, right, pan, tilt, mode`) |
| `GET` | `/racing/ml/models` | The model store (hash-named, never overwritten) |
| `GET` | `/racing/code/{job}` | The executed script with current settings substituted |

## MyApp

- Your own robot web app.
- Launch an app on port **5000** and it becomes accessible at `/myapp/`.
- While a Racing course runs, it takes over port 5000 for its live view (the managed MyApp process is stopped first); the port is released when the course stops.
- Path rules
    - nginx strips `/myapp` from `/myapp/` requests and forwards them to the app (5000). So the app only needs to be written relative to its own root (`/`).
    - Write HTML links, static resources, redirects, and `fetch` as **relative paths**. Absolute paths (`/...`) point outside `/myapp/` and will break.

- Auto-start script
    - `/opt/physicar/userdata/myapp.sh`: runs automatically at boot. The command that launches the app on port 5000.
        ```
        python3 /home/physicar/physicar_ws/app.py
        ```
    - `/opt/physicar/userdata/myapp.log`: execution log of the auto-start script.

## Racing

- Guided driving courses in the PhysiCar panel's **Racing** tab: **Rule-Based** (HSV line tracing) and **Machine Learning** — one CNN taught two ways.
- **Flow**: gather the teacher on the Train page (Supervised: button-labeled photos / Reinforcement: a reward function, simulator only) → `Train the model` → drive it on the Inference page. Both teachers train the SAME network into ONE model store; Inference is identical either way.
- **Jobs** — one at a time, machine-wide: `rb-run`, `sl-train`, `rl-train`, `ml-run`, started with `POST /racing/run {"job", "model"?, "base"?, "steps"?, "worlds"?}`. Everything about a run rides on the request; nothing is stored. Requesting `ml-run` while it is already driving swaps the model LIVE (no restart).
- **Runs are detached**: the runner spawns the course script under a lock in `/opt/physicar/userdata/racing/` — every window sees the same run, and any window (or `POST /racing/stop`) stops it. `GET /racing/status` returns the lock, run gates, log tail and training progress.
- **Models**: training checkpoints continuously into `ml/checkpoint.pt`, and HOWEVER a run ends (finish, Stop, crash) the runner files the last checkpoint into `userdata/racing/ml/models/` as `<sha256(pt)[:8]>.pt` plus a derived deployable `.onnx`. The `.pt` is the interchange format — download/upload it, continue training from it (`base`), across teachers too (supervised pretrain → reinforcement finetune works).
- **Settings apply live**: `/racing/{rb|ml}/settings` — a running script re-reads them within a frame.
- **The code is the course**: scripts ship standalone in `physicar_webserver/racing_assets/`; `GET /racing/code/{job}` returns the exact executed script with the current settings substituted (what the panel's View Code shows).
- A running course serves its live view on port 5000 (the MYAPP slot). The panel, the AI chat's `racing_*` tools and a student's curl all drive the same API.

## Tool Server (`physicar_tools`)

FastAPI service (`physicar_tools/tools_server.py`, loopback `127.0.0.1:9004`) serving the AI chat's Python tools: the bundled `robot.py` (Web API mirror), `sim.py` (sim API), `utils.py`, `racing.py` (the Racing runner: status/run/models/stop/settings), plus the user-editable `/opt/physicar/userdata/custom_tools.py`. Launched by the bringup launch files with `respawn=True` — a crash or an intentional `/reload` comes back within a second, and a broken custom script never takes the server down (the last working module keeps serving while the import error is reported).

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/physicar-ext/tools` | Tool list + parameter schemas (+ per-script import errors) |
| `POST` | `/physicar-ext/tools/<name>` | Run a tool — `{"args": {...}, "session": "<chat session>"}` |
| `POST` | `/physicar-ext/wake` | Redeem a one-shot wake ticket — `{"wake_id": "...", "note": "optional"}`; starts an automatic turn in the chat that reserved it |
| `POST` | `/physicar-ext/wake/status` | Ticket state (`{"wake_id"}`) or a session's outstanding tickets (`{"session"}`) |
| `GET` | `/physicar-ext/wakes/<session>` | Long-poll pending wakes (used by the VSCode extension) |
| `GET` | `/physicar-ext/health` | Loaded modules, import errors, process RSS |
| `POST` | `/physicar-ext/reload` | Restart the interpreter — picks up newly installed libraries and replaced model weights |

Wake tickets are reserved from the chat (`utils_wake_reserve`) or in tool code (`from pcwake import reserve, redeem`); they are one-shot and in-memory. See `physicar_tools/pcwake.py` for the contract.

## License

Copyright 2026 **AICASTLE Inc.**

| Package | License |
|---------|---------|
| `physicar_*` (this project) | GPL-3.0 |
| `physicar_camera` (vendored from camera_ros) | MIT |
| `physicar_lidar` (vendored from rplidar_ros) | BSD-2-Clause |
