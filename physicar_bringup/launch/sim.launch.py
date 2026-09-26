#!/usr/bin/env python3
#
# SPDX-License-Identifier: LicenseRef-PhysiCar-Community-1.0
# Copyright (c) 2026 AICASTLE Inc.
# Licensed under the PhysiCar Community License 1.0 (see LICENSE).

"""
PhysiCar Simulation Launch File

Gazebo simulation mode — hardware drivers are NOT launched.
The simulator is the robot's virtual hardware: it runs Gazebo in its own
container and, like the real robot's hardware, provides the sensor, drive and
compressed camera topics over ROS — this launch equals real.launch.py minus drivers.

Compared to real.launch.py, the following are EXCLUDED:
  - physicar_driver (serial/Yahboom board)
  - physicar_camera (libcamera)
  - physicar_lidar (serial LiDAR)
  - audio_node (no audio hardware)
  - setup_audio / setup_hotspot / setup_nginx

The following run identically to the real robot:
  - robot_state_publisher (TF from URDF)
  - scan_filter (/scan → /scan_filtered)
  - laser_odom (LiDAR-based /odom/laser — Point-to-Line ICP)
  - ekf_filter_node (fuses laser_odom + IMU → /odom)
  - webserver_node (REST API on port 8000)

Provided by the simulator (in place of physicar_driver and the camera):
  - /speed + /steering → /cmd_vel (inverse Ackermann)
  - /battery_state (always full: 8.4V, 100%, 1Hz)
  - /servo/commands subscriber (dummy — no hardware)
  - /camera/image_raw/compressed

Audio in SIM:
  - No audio_node (no USB audio hardware)
  - Webserver streams /audio topic via SSE at /audio
  - gzweb plays audio in browser via Web Audio API

Topic parity — ALL topics available in both real and SIM modes:
  /speed, /steering, /camera/pan, /camera/tilt, /audio,
  /imu, /camera/image_raw/compressed, /scan, /scan_filtered, /odom, /clock,
  /joint_states, /battery_state, /servo/commands
"""

import os
import sys

from ament_index_python.packages import (
    PackageNotFoundError,
    get_package_prefix,
    get_package_share_directory,
)
from launch import LaunchDescription
from launch.actions import ExecuteProcess, TimerAction
from launch.substitutions import Command
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def _have_executable(package, executable):
    """True when `ros2 launch` will be able to resolve package/executable."""
    try:
        prefix = get_package_prefix(package)
    except PackageNotFoundError:
        return False
    return os.access(os.path.join(prefix, 'lib', package, executable), os.X_OK)


def generate_launch_description():
    # Package directories
    pkg_description = get_package_share_directory('physicar_description')
    pkg_bringup = get_package_share_directory('physicar_bringup')

    # URDF file — use the per-generation file (<generation>.urdf.xacro) if present, else fall back to gen 1.
    # The gen-1 filename doubles as the generation key (physicar), so it matches the default path naturally.
    _gen = os.environ.get('PHYSICAR_GENERATION', 'physicar')
    urdf_file = os.path.join(pkg_description, 'urdf', f'{_gen}.urdf.xacro')
    if not os.path.exists(urdf_file):
        urdf_file = os.path.join(pkg_description, 'urdf', 'physicar.urdf.xacro')
    driver_config = os.path.join(pkg_bringup, 'config', 'driver_params.yaml')

    # ── Robot Description (same URDF as real robot) ──
    robot_description = ParameterValue(
        Command(['xacro ', urdf_file]),
        value_type=str
    )

    robot_state_publisher = Node(
        package='robot_state_publisher',
        executable='robot_state_publisher',
        output='screen',
        parameters=[
            {'robot_description': robot_description},
            {'use_sim_time': True}
        ],
    )

    # ── Upper-layer nodes (identical to real.launch.py) ──

    # Scan filter: /scan → /scan_filtered (same as real robot)
    scan_filter = Node(
        package='physicar_bringup',
        executable='scan_filter_node',
        name='scan_filter',
        output='screen',
        parameters=[
            {'input_topic': '/scan'},
            {'output_topic': '/scan_filtered'},
            {'use_sim_time': True},
        ],
        respawn=True,
        respawn_delay=2.0,
    )

    # WebServer Node (REST API, direct access on port 8000)
    # Short stagger only — the UI is unusable until this node serves /app,
    # so it must come up as early as possible.
    webserver_node = TimerAction(
        period=1.0,
        actions=[
            Node(
                package='physicar_webserver',
                executable='webserver_node.py',
                name='webserver',
                output='screen',
                parameters=[{'use_sim_time': False, 'sim_mode': True}],
                additional_env={'PHYSICAR_SIM': '1'},
                respawn=True,
                respawn_delay=2.0,
            )
        ]
    )

    # Laser Odometry → /odom/laser (raw, no TF)
    # Point-to-Line ICP scan matching. EKF fuses with IMU → /odom + TF.
    laser_odom = Node(
        package='physicar_laser_odom',
        executable='laser_odom_node',
        name='laser_odom',
        output='log',
        arguments=['--ros-args', '--log-level', 'warn'],
        parameters=[
            {
                'laser_scan_topic': '/scan_filtered',
                'odom_topic': '/odom/laser',
                'publish_tf': False,
                'base_frame_id': 'base_footprint',
                'odom_frame_id': 'odom',
                'use_sim_time': True,
            },
        ],
        respawn=True,
        respawn_delay=2.0,
    )

    # EKF: fuses laser odom (/odom/laser) + IMU (/imu) → /odom + TF
    ekf_config = os.path.join(pkg_bringup, 'config', 'ekf_params.yaml')
    ekf_node = Node(
        package='robot_localization',
        executable='ekf_node',
        name='ekf_filter_node',
        output='log',
        parameters=[ekf_config, {'use_sim_time': True}],
        remappings=[('odometry/filtered', '/odom')],
        respawn=True,
        respawn_delay=2.0,
    )

    # Topic Watchdog (sim mode)
    # Monitors /odom/laser — if laser_odom gets stuck after a Gazebo world switch
    # (sim time backward jump), kills it so respawn=True restarts it fresh.
    topic_watchdog = TimerAction(
        period=10.0,
        actions=[
            Node(
                package='physicar_bringup',
                executable='topic_watchdog_node',
                name='topic_watchdog',
                output='screen',
                parameters=[{'mode': 'sim'}],
                respawn=True,
                respawn_delay=5.0,
            )
        ]
    )

    # A missing executable aborts the ENTIRE launch during startup (respawn
    # cannot help — the process never starts), taking the webserver and every
    # healthy node down with it. Typical cause: source updated without a
    # rebuild. Skip missing nodes loudly so the rest of the robot stays up;
    # the boot script detects the gap and rebuilds.
    actions = []
    for pkg, exe, action, label in [
        ('robot_state_publisher', 'robot_state_publisher',
         robot_state_publisher, 'robot_state_publisher'),
        ('physicar_bringup', 'scan_filter_node', scan_filter, 'scan_filter'),
        ('physicar_laser_odom', 'laser_odom_node', laser_odom, 'laser_odom'),
        ('robot_localization', 'ekf_node', ekf_node, 'ekf_node'),
        ('physicar_webserver', 'webserver_node.py', webserver_node, 'webserver'),
        ('physicar_bringup', 'topic_watchdog_node',
         topic_watchdog, 'topic_watchdog'),
    ]:
        if _have_executable(pkg, exe):
            actions.append(action)
        else:
            print(f"[sim.launch] SKIPPING {label}: executable '{exe}' not found "
                  f"in package '{pkg}' — run colcon build to restore it",
                  file=sys.stderr)
    # AI chat tool-call server (FastAPI :9004) — a plain process, not a ROS
    # node. respawn makes it effectively immortal: /reload exits on purpose
    # and crashes both come back with a fresh interpreter within a second.
    tools_server_py = '/opt/physicar/src/physicar-ros/physicar_tools/tools_server.py'
    if os.path.isfile(tools_server_py):
        actions.append(ExecuteProcess(
            cmd=['/usr/bin/python3', tools_server_py],
            name='tools_server',
            output='screen',
            respawn=True,
            respawn_delay=1.0,
            additional_env={'PHYSICAR_PROFILE': 'sim'},
        ))
    return LaunchDescription(actions)
