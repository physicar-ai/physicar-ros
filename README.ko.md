# PhysiCar ROS 2

[![ROS 2 Jazzy](https://img.shields.io/badge/ROS%202-Jazzy-blue)](https://docs.ros.org/en/jazzy/)
[![Ubuntu 24.04](https://img.shields.io/badge/Ubuntu-24.04-E95420)](https://releases.ubuntu.com/24.04/)
[![License](https://img.shields.io/badge/License-Apache%202.0-green)](LICENSE)

한국어 | [English](README.md)

**PhysiCar** Ackermann 조향 RC 로봇용 ROS 2 Jazzy 스택입니다.
실제 하드웨어(Raspberry Pi 5)와 Gazebo Harmonic 시뮬레이션
(GitHub Codespaces / 데스크톱)에서 코드 변경 없이 동일하게 동작합니다.
런치 파일·토픽·웹 API가 모두 동일하며, `SIM=true` 환경 변수 하나로 모드를 전환합니다.

---

## 실행 모드 한눈에 보기

| 모드 | 시작 방법 | 하드웨어 | Gazebo 위치 |
|---|---|---|---|
| **실제 로봇** | `entrypoint.sh` (`SIM` 미설정) → `robot.launch.py` | RPi 5 + Yahboom 보드 + RPLidar + Pi 카메라 | 해당 없음 |
| **시뮬레이션** | `entrypoint.sh` + `SIM=true` → `sim.launch.py` | 없음 | 호스트(`gz sim`), 컨테이너는 `--network host` |

시뮬레이션 모드에서는 컨테이너에 상위 레이어 ROS 노드만 들어 있고,
월드는 호스트의 Gazebo Harmonic 프로세스에서 별도로 실행되어
`ros_gz_bridge`로 연결됩니다. 웹 UI, REST API, 에이전트, DeepRacer 추론,
게임패드 텔레옵은 실제 로봇과 완전히 동일합니다.

---

## 저장소 구조

```
physicar-ros/
├── entrypoint.sh                 # 컨테이너 엔트리포인트 (build → launch)
├── physicar_bringup/             # 시스템 런치 + 드라이버 + 유틸리티
│   ├── launch/
│   │   ├── robot.launch.py         # 실제 로봇 런치
│   │   └── sim.launch.py           # Gazebo (sim) 런치
│   ├── config/driver_params.yaml   # 모든 노드 파라미터
│   ├── physicar_bringup/
│   │   ├── yahboom_board.py        # Yahboom 확장 보드 시리얼 프로토콜
│   │   └── servo_controller.py     # 서보 각도 클리핑/매핑
│   ├── scripts/
│   │   ├── physicar_driver_node.py   # ESC, 서보, IMU, 배터리, Ackermann
│   │   ├── audio_node.py             # /audio 재생 + 믹싱
│   │   ├── camera_proc_node.py       # 카메라 후처리 보조
│   │   ├── cmd_vel_adapter_node.py   # /speed+/steering ↔ /cmd_vel (sim 전용)
│   │   ├── scan_filter_node.py       # /scan → /scan_filtered (inf/nan 정리)
│   │   ├── topic_watchdog_node.py    # 센서 토픽 stale 시 노드 재시작 트리거
│   │   ├── setup_audio.sh            # USB 오디오 + dmix 초기화
│   │   └── calibration/              # measure_min_speed.py, pulse_coasting_test.py
│   └── sounds/intro.mp3              # 부팅 사운드
├── physicar_description/         # URDF/xacro (지오메트리 단일 출처)
├── physicar_interfaces/          # 커스텀 msg / srv 정의
├── physicar_teleop/              # 게임패드 텔레옵 노드 (joy_teleop_node)
├── physicar_agent/               # AI 에이전트 런타임 + 도구 레지스트리
│   └── physicar_agent/
│       ├── agent_node.py           # rclpy 스피너; 공유 코어 소유
│       ├── core.py                 # 토픽/서비스/액션 자동 발견 프록시
│       ├── registry.py             # 도구 로더 + 격리 venv + PEP 723 의존성
│       └── builtin/{control,state,music}.py
├── physicar_deepracer/           # DeepRacer ONNX 추론 파이프라인
├── physicar_webserver/           # FastAPI 웹 서버 (port 8000)
│   └── physicar_webserver/
│       ├── main.py                 # FastAPI 앱
│       ├── ros_bridge.py           # rclpy ↔ FastAPI 브리지
│       ├── state_manager.py        # 토픽 스냅샷 캐시 + SSE 팬아웃
│       ├── routers/                # /state /control /agent /calibration ...
│       └── static/                 # Kiosk + Studio 웹 UI
├── camera_ros/                   # [submodule] libcamera 기반 카메라 드라이버
├── rplidar_ros/                  # [submodule] RPLidar SDK 드라이버
└── rf2o_laser_odometry/          # [submodule] 레이저 전용 오도메트리
```

---

## 부팅 시퀀스 — `entrypoint.sh`

1. `/opt/physicar/.env` 로드 (예: `SIM=true`, `DEV=true`).
2. `/opt/ros/jazzy/setup.bash` 소스.
3. `SIM=true`이면 `camera_ros` / `rplidar_ros`에 `COLCON_IGNORE` 마커 부착
   (실 하드웨어가 필요한 드라이버이므로 빌드 제외).
4. `colcon build --symlink-install` (실패 시 1회 clean build 재시도).
5. `install/setup.bash` 소스.
6. **DDS 격리** — `ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET` 와
   `/opt/physicar/fastdds-lo.xml` 의 loopback 전용 Fast DDS 프로파일 사용.
   SHM은 비활성화(UDP만 사용)되어 컨테이너의 root 노드와 호스트
   `physicar` 유저 간 UID 경계 문제를 회피하고, 같은 WiFi에 있는 다른
   PhysiCar 들이 우리 토픽을 보지 못하도록 합니다.
7. 런치 분기:
   - `SIM=true` → `ros2 launch physicar_bringup sim.launch.py`
   - 그 외 → `ros2 launch physicar_bringup robot.launch.py`
8. 런치가 죽어도 `exec sleep infinity` 로 컨테이너를 살려 두어 로그 확인 가능.

모든 ROS 노드는 `respawn=True, respawn_delay≈2s` 로 등록되어 있습니다.
새 YAML/Python 을 반영하려면 `pkill -f <node_name>` 으로 죽이면 됩니다(자동 재시작).

---

## 실제 로봇 런치 (`robot.launch.py`)

`SIM` 미설정 시 동작하는 노드 집합.

| 시점 | 컴포넌트 | 비고 |
|---|---|---|
| t=0  | `setup_audio.sh` | USB 사운드카드용 ALSA `dmix` 1회 설정 |
| t=0  | `robot_state_publisher` | URDF로부터 TF 퍼블리시 |
| t=0  | `physicar_driver_node` | ESC + 서보 + IMU + 배터리; `/speed`, `/steering`, `/cmd_vel`, `/camera/pan`, `/camera/tilt` 구독 |
| t=0  | `rplidar_node` (RPLidar C1) | `/scan` |
| t=0  | `scan_filter_node` | `/scan` → `/scan_filtered` (NaN/범위 외 값 제거) |
| t=0  | `camera_node` (camera_ros) | libcamera 캡처 + 왜곡 보정 + 리사이즈 → `/camera/image_raw`, `.../compressed` |
| t=0  | `rf2o_laser_odometry_node` | `/scan_filtered` → `/odom` + TF |
| t=0  | `audio_node` | `/audio` 구독 (채널 믹싱, MP3/PCM/WAV/OGG) |
| t=0  | `deepracer_node` | 항상 실행; 모델 로드 전까지는 idle |
| t=0  | `joy_node` (SDL2) | `/dev/input/jsX` 읽기; xpadneo BLE 패드 호환을 위해 `SDL_JOYSTICK_HIDAPI=0` |
| t≈2s | `joy_teleop_node` | `/joy` → `/teleop/{speed,steering,camera/pan,camera/tilt}` + `/teleop/status` |
| t≈3s | `agent_node` | 토픽/서비스/액션 자동 발견 + 커스텀 도구 로드 |
| t≈3s | `play_intro` | `intro.mp3` 를 `/audio` 에 퍼블리시 (TRANSIENT_LOCAL, 구독자 매칭 대기) |
| t≈4s | `webserver_node` | FastAPI / uvicorn, `127.0.0.1:8000` |
| t≈10s| `topic_watchdog_node` | 30 초 그레이스 후, 센서 토픽 stale 시 해당 노드에 SIGTERM → respawn 으로 재시작 |

nginx, 핫스팟 등 외부 프로세스는 이 패키지 밖에서 관리됩니다 — 디바이스
쪽에서는 호스트의 `physicar.sh` 오케스트레이터, Codespaces 쪽에서는
`supervisord` 가 담당합니다(아래 *Codespaces 에서 실행* 참조).

---

## 시뮬레이션 런치 (`sim.launch.py`)

`SIM=true` 시 활성화. 하드웨어 드라이버는 빌드에서 제외되고
(`COLCON_IGNORE` 마커), 런치에도 포함되지 않습니다.

실행되는 노드:

- `robot_state_publisher` (`use_sim_time: True`)
- `cmd_vel_adapter_node` — 실 드라이버를 대체. 역 Ackermann
  변환 `/speed + /steering → /cmd_vel`. 추가로 항상 만충된
  `/battery_state`, 기본값 `/physicar_driver/calibration_status`
  (TRANSIENT_LOCAL), 더미 `/servo/commands` 구독자를 퍼블리시하여
  실 로봇에 존재하는 모든 토픽이 sim 에서도 동일하게 존재하도록 보장.
- `scan_filter_node`, `deepracer_node`, `joy_node`, `joy_teleop_node`,
  `agent_node`, `webserver_node` — 실 로봇과 완전히 동일.
- `ros_gz_bridge` (`parameter_bridge`) — 다음 토픽을 브리지:
  - **Gz → ROS 2:** `/imu`, `/scan`, `/odom`, `/joint_states`, `/camera/image_raw`, `/clock`
  - **ROS 2 → Gz:** `/cmd_vel`, `/camera/pan`, `/camera/tilt`
  - `GZ_PARTITION=physicar` 로 PhysiCar 월드만 인식.
- `image_transport republish raw → compressed` — `/camera/image_raw/compressed` 제공.

호스트 측 요구사항 (`physicar-sim` / Codespaces 가 처리):

- Gazebo Harmonic + PhysiCar SDF 월드/모델
  ([`physicar-sim`](https://github.com/physicar-ai/physicar-sim))
- `gz launch websocket.gzlaunch` (port 9002) — gzweb 3D 뷰어
- `sim_api.py` (port 9003) — 트랙 전환, 월드 상태

토픽/서비스/메시지 인벤토리는 두 모드에서 **동일**하므로, 같은 에이전트
도구와 HTTP 라우트가 양쪽에서 그대로 동작합니다.

---

## ROS 2 인터페이스

### 토픽

#### 저수준 제어 (드라이버 / cmd_vel_adapter 가 구독)

| 토픽 | 타입 | 단위 | 비고 |
|---|---|---|---|
| `/speed` | `std_msgs/Float64` | m/s | 직접 스로틀 |
| `/steering` | `std_msgs/Float64` | rad | 바퀴 각도 (사인 모델 → 서보) |
| `/camera/pan` | `std_msgs/Float64` | rad | 팬 서보 |
| `/camera/tilt` | `std_msgs/Float64` | rad | 틸트 서보 |
| `/audio` | `physicar_interfaces/Audio` | — | PCM/MP3/WAV/OGG, 멀티채널 |

#### 고수준 제어

| 토픽 | 타입 | 비고 |
|---|---|---|
| `/cmd_vel` | `geometry_msgs/Twist` | `linear.x` (m/s) + `angular.z` (rad/s); 드라이버가 Ackermann `δ = atan(ω·L / v)` 적용 |
| `/teleop/speed`, `/teleop/steering`, `/teleop/camera/pan`, `/teleop/camera/tilt` | `Float64` | prefix 없는 토픽과 단위 동일 |
| `/teleop/status` | `physicar_interfaces/TeleopStatus` | `drive_engaged=true` 이고 fresh 인 동안에는 드라이버가 `/speed`, `/steering`, `/cmd_vel` 을 무시하고 `/teleop/*` 를 우선. `/camera/*` 도 동일 규칙. |

#### 센서 / 상태

| 토픽 | 타입 | 비고 |
|---|---|---|
| `/scan`, `/scan_filtered` | `sensor_msgs/LaserScan` | RPLidar C1 / Gazebo 라이다 |
| `/odom` | `nav_msgs/Odometry` | 실 로봇은 rf2o, sim 은 Gazebo 플러그인 |
| `/imu` | `sensor_msgs/Imu` | Yahboom 보드 MPU / Gazebo IMU |
| `/camera/image_raw`, `/camera/image_raw/compressed` | `Image` / `CompressedImage` | 기본 480×360 |
| `/battery_state` | `sensor_msgs/BatteryState` | 1 Hz; sim 은 8.4 V / 100% 고정 |
| `/joint_states` | `sensor_msgs/JointState` | 조향/바퀴/카메라 조인트 |
| `/physicar_driver/calibration_status` | `physicar_interfaces/CalibrationStatus` | latched (TRANSIENT_LOCAL) |
| `/deepracer/inference` | `physicar_interfaces/DeepracerInference` | speed, steering, 액션 확률 분포 |
| `/clock` | `rosgraph_msgs/Clock` | sim 전용 |

### 서비스

| 서비스 | 타입 | 설명 |
|---|---|---|
| `/physicar_driver/get_calibration` | `physicar_interfaces/GetCalibration` | 캘리브레이션 JSON 조회 |
| `/physicar_driver/set_calibration` | `physicar_interfaces/SetCalibration` | 캘리브레이션 JSON 저장 |
| `/deepracer/load_model` | `physicar_interfaces/DeepracerLoadModel` | ONNX + 메타데이터 로드 |
| `/deepracer/unload_model` | `physicar_interfaces/DeepracerUnloadModel` | |
| `/deepracer/control` | `physicar_interfaces/DeepracerControl` | `start` / `stop` |
| `/deepracer/status` | `physicar_interfaces/DeepracerStatus` | |
| `/deepracer/set_config` | `physicar_interfaces/DeepracerSetConfig` | 속도 스케일, 액션 선택, pan/tilt |
| `/agent/tool/list`, `.../get`, `.../set`, `.../delete`, `.../call`, `.../reset` | `physicar_interfaces/Tool*` | 도구 CRUD + 호출 |
| `/teleop/joy/get_mapping`, `.../set_mapping` | `physicar_interfaces/{Get,Set}JoyMapping` | 버튼별 바인딩 |

---

## 웹 서버 (FastAPI, `:8000`)

`webserver_node.py` 가 uvicorn 을 직접 실행합니다. CORS, gzip, 인증, SSE
모두 FastAPI 가 처리하며, nginx(존재할 때)는 TLS 종료 + `80/443 → 8000`
프록시만 담당합니다. **인증 면제**: `127.0.0.0/8`, `10.42.0.0/24`(핫스팟).
외부 IP 는 `/auth` 에서 발급한 토큰이 필요합니다.

`main.py` 에 마운트된 라우터:

| 접두사 | 라우터 | 역할 |
|---|---|---|
| `/health` | `health` | 라이브니스 프로브 |
| `/auth`   | `auth`   | 토큰 발급 / 로그인 페이지 |
| `/info`   | `info`   | 시스템 정보; `mode: "real"` 또는 `"sim"` 반환 |
| `/state`, `/state/{odom,battery,imu,camera,camera/{pan,tilt},lidar,audio}` | `state` | JSON 스냅샷 또는 SSE (`?stream=true` / `Accept: text/event-stream`). `/state/camera/image` 는 JPEG (또는 `?stream=true` 시 MJPEG, `?width=&height=` 로 리사이즈). `/state/lidar?step=N` 으로 스캔 데시메이트. |
| `/control/{speed,steering,camera/pan,camera/tilt,audio}` | `control` | 매칭되는 ROS 2 토픽으로 단일 값 퍼블리시 |
| `/agent/tool/{list,get,call,set,delete,reset}` | `agent` | 에이전트 도구 관리/호출 (Python 소스 + PEP 723 의존성) |
| `/calibration`, `/calibration/{steering,pan,tilt,reverse,emergency}` | `calibration` | `/opt/physicar/calibration.json` 읽기/쓰기 |
| `/teleop/joy`, `/teleop/joy/mapping` | `joy` | 조이스틱 매핑 CRUD |
| `/teleop` | `teleop` | 소스 무관 텔레옵 상태/락 |
| `/network`, `/network/bluetooth` | `network`, `bluetooth` | WiFi / BT 페어링 |
| `/uistate` | `uistate` | 탭 간 UI 상태 동기화 |
| `/settings/myapp`, `/settings/myapp/{start,stop,restart,log}` | `myapp` | 호스트 측 학생용 웹 앱 슬롯 (포트 5000) |
| `/deepracer` | `deepracer` | 모델 업로드/목록/선택/시작/중지 |
| `/kiosk`, `/studio`, `/kiosk/calibration*` | `kiosk` | 키오스크 + 스튜디오 HTML UI |

OpenAPI/Swagger: `/docs`, ReDoc: `/redoc`.

### MyApp 슬롯

호스트의 `physicar-myapp.service` (Codespaces 에서는 supervisord)가
`physicar` 유저로 단일 사용자 Python 스크립트를 포트 5000 에서 실행하며,
스크립트 디렉토리를 inotify 로 감시하여 변경 시 자동 재시작합니다.
nginx 가 `/myapp/` 를 캐시 비활성화로 프록시합니다.

```bash
# 상태 조회
curl http://<host>/settings/myapp

# 스크립트 등록 (즉시 시작)
curl -X PUT http://<host>/settings/myapp \
     -H 'Content-Type: application/json' \
     -d '{"path": "/opt/physicar/myapp/main.py"}'

# 로그 확인
curl 'http://<host>/settings/myapp/log?tail=200'
```

스크립트는 `0.0.0.0:int(os.environ.get("PORT", 5000))` 에 바인딩해야 합니다.

---

## 에이전트 런타임 (`physicar_agent`)

`agent_node` 가 rclpy 스핀 루프를 소유하며, `core.py` 가 사용자 도구에
ROS-CLI 스타일 프록시 API를 노출합니다:

```python
from physicar_agent import topic, service, action

topic['/odom']                  # 최신 메시지 (dict 변환)
topic.raw('/imu')               # 원본 ROS 2 메시지
topic.pub('/cmd_vel', {'linear': {'x': 0.3}, 'angular': {'z': 0.0}})
topic.list()                    # [(name, type), ...]

service('/physicar_driver/set_calibration', {...})
action('/navigate_to_pose', {...})  # 블로킹
```

토픽/서비스/액션 발견은 자동입니다 — 코어가 시작 시 그래프를 순회하여
퍼블리셔 QoS 를 스냅샷하고 동일한 QoS의 구독/클라이언트를 만듭니다.
새로 등장한 토픽은 `topic.refresh()` 로 반영합니다.

도구 저장소:

```
/opt/physicar/agent/
├── tools/         # 도구 1개당 .py 파일 1개, 반드시 def tool(...) 정의
├── venv/          # system-site-packages 가 있는 격리 venv
└── deps.json      # PEP 723 참조 카운트; 0 이 되면 패키지 제거
```

도구는 PEP 723 인라인 메타데이터로 의존성을 선언할 수 있고, 레지스트리가
이를 파싱해 `venv` 에 설치합니다. 로딩 실패 시 파일은 자동 롤백됩니다.

---

## DeepRacer

`deepracer_node` 는 항상 실행되지만, 모델 로드 전까지는 idle.

```
/opt/physicar/deepracer/
├── models/<name>/{model.onnx, model_metadata.json}
└── config.json     # {"action_selection": "greedy"|"stochastic", "speed_scale": 1.0, "pan": 0.0, "tilt": 0.0}
```

```bash
# 로드 + 시작
ros2 service call /deepracer/load_model    physicar_interfaces/srv/DeepracerLoadModel "{model_name: 'my_model'}"
ros2 service call /deepracer/control       physicar_interfaces/srv/DeepracerControl   "{command: 'start'}"
# 상태 확인
ros2 service call /deepracer/status        physicar_interfaces/srv/DeepracerStatus    "{}"
ros2 topic echo  /deepracer/inference
# 중지 / 언로드
ros2 service call /deepracer/control       physicar_interfaces/srv/DeepracerControl   "{command: 'stop'}"
ros2 service call /deepracer/unload_model  physicar_interfaces/srv/DeepracerUnloadModel "{}"
```

또는 동등한 `/deepracer/...` HTTP 라우트 사용 가능.

---

## 설정 — `physicar_bringup/config/driver_params.yaml`

발췌. 전체는 파일 참조.

```yaml
physicar_driver:
  ros__parameters:
    serial_port: "/dev/yahboom"        # /dev/ttyUSB* 가 아니라 udev 심링크 사용
    baudrate: 115200

    # 서보 한계 (도)
    max_pan: 45.0
    max_tilt: 45.0
    max_steering: 25.0                 # 바퀴 각도; 서보 각도는 사인 모델로 환산
    max_speed: 3.0                     # m/s

    # 중심 오프셋 (90° 기준 도)
    pan_center: 0.0
    tilt_center: 0.0
    steering_center: 0.0

    reverse_direction: false           # true = ESC 극성 반전

    # 비상 정지 (LiDAR 기반)
    emergency_enabled: true
    emergency_angle_range: 30.0        # 전체 각도 (전방 ±15°)
    emergency_front_margin: 0.25       # m
    emergency_rear_margin: 0.15        # m

    # 지오메트리 (URDF와 일치)
    wheel_radius: 0.0375               # 75 mm 바퀴
    wheelbase: 0.18                    # m
    track_width: 0.16                  # m
    steering_ratio: 2.0                # 사인 모델: servo = arcsin(sin(wheel) / k)

rplidar_node:
  ros__parameters:
    serial_port: "/dev/rplidar"
    serial_baudrate: 460800
    flip_x_axis: true                  # 장착 방향 보정
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
    # OV5647 기본 CCM은 IR-cut 필터 환경 가정 → IR 카메라에서 색상 왜곡.
    # 항등 행렬로 덮어써서 libcamera CCM 비활성화
    ColourCorrectionMatrix: [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0]
    # 640×480 캡처 → 왜곡 보정 + 리사이즈 → 480×360 퍼블리시
    undistort.fx: 387.89
    undistort.fy: 387.19
    undistort.cx: 312.63
    undistort.cy: 229.36
    undistort.k1: -0.3675
    undistort.k2:  0.1717
    undistort.p1: -0.0021
    undistort.p2: -0.0009
    undistort.k3: -0.0445
    undistort.dist_scale: 0.7          # 0=off, 0.7≈98° FOV(기본), 1.0=완전 보정
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

런타임 캘리브레이션 오버라이드는 `/opt/physicar/calibration.json` 에 저장되어
YAML 값 위에 머지됩니다. 현재 활성 소스는 `CalibrationStatus.source` 로 확인.

---

## 디바이스 경로

| 경로 | 용도 |
|---|---|
| `/opt/physicar/.env` | `SIM=true`, `DEV=true` 등 |
| `/opt/physicar/password` | 웹 인증 비밀번호 (DEV / SIM 기본값 `physicar`) |
| `/opt/physicar/calibration.json` | 캘리브레이션 오버라이드 (latched) |
| `/opt/physicar/fastdds-lo.xml` | loopback 전용 Fast DDS 프로파일 |
| `/opt/physicar/agent/{tools,venv,deps.json}` | 에이전트 도구 샌드박스 |
| `/opt/physicar/deepracer/{models,config.json}` | DeepRacer 모델 + 추론 설정 |
| `/opt/physicar/myapp/{run.sh,log}` | 학생 웹 앱 + 로그 |
| `/etc/nginx/...` | TLS 종료 리버스 프록시 (실 로봇) |

---

## 실행

### 실제 로봇

Pi 이미지에 모든 게 미리 들어 있고, 부팅 시 `physicar` Docker 컨테이너가
`entrypoint.sh` 를 자동 실행합니다. 로그/상태:

```bash
docker logs -f physicar
ros2 topic list
```

코드 변경 반영:

| 변경 | 방법 |
|---|---|
| 패키지의 Python 코드 (`--symlink-install`) | 노드만 죽이면 됨 — `pkill -f physicar_driver_node` 등. 런치가 자동 재시작. |
| FastAPI 코드 (DEV 모드) | `os.execv` watchdog 으로 자동 재시작 |
| YAML 파라미터 | `pkill -f <node>` |
| C++ (camera_ros, rf2o) | `colcon build --symlink-install` 후 노드 재시작 |
| `nginx.conf` | `nginx -s reload` (호스트) |

### Codespaces 에서 실행 (sim)

[`physicar-for-codespaces`](https://github.com/physicar-ai/physicar-for-codespaces)
의 `physicar` 브랜치 사용 시 흐름:

1. **`onCreate`** — ROS 2 Jazzy, Gazebo Harmonic, `ros-jazzy-ros-gz`, nginx,
   noVNC, supervisor 설치; `physicar/sim:1` Docker 이미지 pull;
   `physicar-sim` / `physicar-ros` 서브모듈 pull; `/opt/physicar/.env` 에
   `SIM=true` 기록.
2. **`postStart`** — `.devcontainer/supervisord.conf` 로 `supervisord` 부팅.
   감독 대상에는 `xvfb` + `openbox` + `x11vnc` + `novnc` (브라우저 데스크톱),
   `nginx` (port 80), `gz_websocket` (gzweb on :9002), `sim_api` (:9003),
   학생용 `myapp` 워처, `physicar` 가 포함됨.
3. **`physicar` 프로그램** 이 `physicar.sh` 를 실행 →
   `physicar/sim:1` 이미지를 `--network host` 로 `docker run -d`,
   `/opt/physicar` 와 `physicar-ros` 서브모듈을
   `/root/ros2_ws/src/physicar-ros` 로 바인드 마운트한 뒤
   `/root/ros2_ws/src/physicar-ros/entrypoint.sh` 실행. `SIM=true` 이므로
   `sim.launch.py` 가 트리거되고, `ros_gz_bridge` 가 host network 를 통해
   호스트 Gazebo 에 도달.

최종 사용자 URL (포트 80 으로 포워딩):

- `/studio` — 메인 웹 UI
- `/kiosk` — 터치스크린 키오스크
- `/gz/` — Gazebo 3D 뷰어 (gzweb)
- `/vnc/` — 데스크톱 뷰 (Gazebo Sim 창, 터미널)
- `/docs` — REST API 문서

---

## 라이선스

Copyright 2025 **주식회사 에이아이캐심 (AICASTLE Inc.)**.
“PhysiCar” 는 주식회사 에이아이캐심의 상표입니다.

아래 표의 서브모듈들을 제외한 이 저장소의 모든 코드는 **Apache License 2.0**으로
배포됩니다 — [LICENSE](LICENSE), [NOTICE](NOTICE) 참조.

| 패키지 | 라이선스 |
|---|---|
| `physicar_*` (본 프로젝트) | Apache-2.0 |
| `camera_ros` (서브모듈) | MIT |
| `rplidar_ros` (서브모듈) | BSD-2-Clause |
| `rf2o_laser_odometry` (서브모듈) | GPL-3.0 |
