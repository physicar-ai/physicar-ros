# PhysiCar ROS 2

[![ROS 2 Jazzy](https://img.shields.io/badge/ROS%202-Jazzy-blue)](https://docs.ros.org/en/jazzy/)
[![Ubuntu 24.04](https://img.shields.io/badge/Ubuntu-24.04-E95420)](https://releases.ubuntu.com/24.04/)
[![License](https://img.shields.io/badge/License-Apache%202.0-green)](LICENSE)

한국어 | [English](README.md)

**PhysiCar** ROS 2 Jazzy 스택. Raspberry Pi 5 (Ubuntu 24.04) 에서 실행.

---

## 차량 스펙

| 항목 | 사양 |
|------|------|
| 컴퓨터 | Raspberry Pi 5 (8 GB) |
| LiDAR | 360°, 0.10–16 m, 10 Hz |
| IMU | 6축 (가속도 + 자이로), 50 Hz |
| 카메라 | 480×360, 15 fps, MF, FOV 100°, Night Vision |
| 카메라 팬 | ±30° |
| 카메라 틸트 | ±30° |
| 배터리 | 2S 리튬 7.4 V |
| 조향 | Ackermann |
| 최대 속도 | 3.0 m/s |
| 최대 바퀴 조향각 | ±20° |

---

## 설치

[deploy/README.md](deploy/README.md) 참고.

---

## 실행 모드

`.env` 파일로 설정 (`/opt/physicar/userdata/.env`).

| 변수 | 기본값 | 설명 |
|------|--------|------|
| `SIM` | `false` | `false`: 실제 로봇에서 실행, `true`: physicar-sim 시뮬레이션 환경에서 실행 |
| `DEV` | `false` | `false`: 자동 업데이트 활성, `true`: 자동 업데이트 비활성, 코드 직접 수정 가능 |

---

## ROS 2 인터페이스

커스텀 메시지/서비스 정의: [physicar_interfaces/](physicar_interfaces/)

| 이름 | 종류 | 타입 | 설명 |
|------|------|------|------|
| `/agent/tool/call` | service | [`ToolCall`](physicar_interfaces/srv/ToolCall.srv) | 도구 실행 |
| `/agent/tool/get` | service | [`ToolGet`](physicar_interfaces/srv/ToolGet.srv) | 도구 조회 |
| `/agent/tool/init` | service | [`ToolInit`](physicar_interfaces/srv/ToolInit.srv) | 도구 초기화 |
| `/agent/tool/list` | service | [`ToolList`](physicar_interfaces/srv/ToolList.srv) | 도구 목록 |
| `/agent/tool/load` | service | [`ToolLoad`](physicar_interfaces/srv/ToolLoad.srv) | 도구 리로드 |
| `/agent/tool/set` | service | [`ToolSet`](physicar_interfaces/srv/ToolSet.srv) | 도구 등록/수정 |
| `/audio` | topic | [`Audio`](physicar_interfaces/msg/Audio.msg) | 오디오 재생 |
| `/battery_state` | topic | [`BatteryState`](https://docs.ros2.org/latest/api/sensor_msgs/msg/BatteryState.html) | 배터리 상태 (1 Hz) |
| `/camera/image_raw/compressed` | topic | [`CompressedImage`](https://docs.ros2.org/latest/api/sensor_msgs/msg/CompressedImage.html) | 카메라 영상 (JPEG) |
| `/camera/pan` | topic | [`Float64`](https://docs.ros2.org/latest/api/std_msgs/msg/Float64.html) | 팬 (rad) |
| `/camera/tilt` | topic | [`Float64`](https://docs.ros2.org/latest/api/std_msgs/msg/Float64.html) | 틸트 (rad) |
| `/cmd_vel` | topic | [`Twist`](https://docs.ros2.org/latest/api/geometry_msgs/msg/Twist.html) | 속도 + 조향 (Ackermann 변환) |
| `/deepracer/control` | service | [`DeepracerControl`](physicar_interfaces/srv/DeepracerControl.srv) | 추론 시작/정지 |
| `/deepracer/inference` | topic | [`DeepracerInference`](physicar_interfaces/msg/DeepracerInference.msg) | 추론 결과 |
| `/deepracer/load_model` | service | [`DeepracerLoadModel`](physicar_interfaces/srv/DeepracerLoadModel.srv) | 모델 로드 |
| `/deepracer/unload_model` | service | [`DeepracerUnloadModel`](physicar_interfaces/srv/DeepracerUnloadModel.srv) | 모델 언로드 |
| `/deepracer/set_config` | service | [`DeepracerSetConfig`](physicar_interfaces/srv/DeepracerSetConfig.srv) | 속도 스케일, 액션 선택 |
| `/deepracer/status` | service | [`DeepracerStatus`](physicar_interfaces/srv/DeepracerStatus.srv) | 상태 조회 |
| `/imu` | topic | [`Imu`](https://docs.ros2.org/latest/api/sensor_msgs/msg/Imu.html) | IMU (50 Hz) |
| `/odom` | topic | [`Odometry`](https://docs.ros2.org/latest/api/nav_msgs/msg/Odometry.html) | 오도메트리 |
| `/physicar_driver/calibration_status` | topic | [`CalibrationStatus`](physicar_interfaces/msg/CalibrationStatus.msg) | 캘리브레이션 상태 |
| `/physicar_driver/get_calibration` | service | [`GetCalibration`](physicar_interfaces/srv/GetCalibration.srv) | 캘리브레이션 조회 |
| `/physicar_driver/set_calibration` | service | [`SetCalibration`](physicar_interfaces/srv/SetCalibration.srv) | 캘리브레이션 저장 |
| `/physicar_joy_teleop/get_mapping` | service | [`GetJoyMapping`](physicar_interfaces/srv/GetJoyMapping.srv) | 조이스틱 매핑 조회 |
| `/physicar_joy_teleop/set_mapping` | service | [`SetJoyMapping`](physicar_interfaces/srv/SetJoyMapping.srv) | 조이스틱 매핑 설정 |
| `/scan` | topic | [`LaserScan`](https://docs.ros2.org/latest/api/sensor_msgs/msg/LaserScan.html) | LiDAR 스캔 (원본) |
| `/scan_filtered` | topic | [`LaserScan`](https://docs.ros2.org/latest/api/sensor_msgs/msg/LaserScan.html) | LiDAR 스캔 (필터링) |
| `/speed` | topic | [`Float64`](https://docs.ros2.org/latest/api/std_msgs/msg/Float64.html) | 속도 (m/s) |
| `/steering` | topic | [`Float64`](https://docs.ros2.org/latest/api/std_msgs/msg/Float64.html) | 조향각 (rad) |
| `/teleop/camera/pan` | topic | [`Float64`](https://docs.ros2.org/latest/api/std_msgs/msg/Float64.html) | 텔레옵 팬 (우선순위 높음) |
| `/teleop/camera/tilt` | topic | [`Float64`](https://docs.ros2.org/latest/api/std_msgs/msg/Float64.html) | 텔레옵 틸트 (우선순위 높음) |
| `/teleop/speed` | topic | [`Float64`](https://docs.ros2.org/latest/api/std_msgs/msg/Float64.html) | 텔레옵 속도 (우선순위 높음) |
| `/teleop/status` | topic | [`TeleopStatus`](physicar_interfaces/msg/TeleopStatus.msg) | 텔레옵 상태 |
| `/teleop/steering` | topic | [`Float64`](https://docs.ros2.org/latest/api/std_msgs/msg/Float64.html) | 텔레옵 조향 (우선순위 높음) |

---

## 웹 API

FastAPI `127.0.0.1:8000`, nginx 리버스 프록시 (80/443).

| 경로 | 설명 |
|------|------|
| `/health` | 라이브니스 |
| `/auth` | 토큰 발급 |
| `/info` | 시스템 정보 (`mode: "real"` / `"sim"`) |
| `/state/{odom,battery,imu,camera,lidar,...}` | 센서 스냅샷, `?stream=true` → SSE |
| `/state/camera/image` | JPEG, `?stream=true` → MJPEG |
| `/control/{speed,steering,camera/pan,camera/tilt,audio}` | 단일 값 퍼블리시 |
| `/calibration` | 캘리브레이션 읽기/쓰기 |
| `/teleop/joy/mapping` | 조이스틱 매핑 CRUD |
| `/agent/tool/{list,get,call,set,delete,reset}` | 에이전트 도구 |
| `/deepracer/{models,upload,start,stop,...}` | DeepRacer 관리 |
| `/settings/myapp` | 학생 웹 앱 (포트 5000) |
| `/kiosk` | 키오스크 UI |
| `/studio` | 스튜디오 UI |
| `/docs` | OpenAPI 문서 |

---

## 라이선스

Copyright 2026 **주식회사 에이아이캐슬 (AICASTLE Inc.)**.

| 패키지 | 라이선스 |
|--------|----------|
| `physicar_*` | Apache-2.0 |
| `camera_ros` | MIT |
| `rplidar_ros` | BSD-2-Clause |
| `rf2o_laser_odometry` | GPL-3.0 |
