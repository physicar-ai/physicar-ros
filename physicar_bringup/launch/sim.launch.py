#!/usr/bin/env python3
#
# Copyright 2026 AICASTLE Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
PhysiCar Simulation Launch File

Gazebo simulation mode — hardware drivers are NOT launched.
Gazebo runs as a separate process; ros_gz_bridge connects via gz-transport.

Compared to robot.launch.py, the following are EXCLUDED:
  - physicar_driver (serial/Yahboom board)
  - camera_ros (libcamera)
  - rplidar_ros (serial LiDAR)
  - audio_node (no audio hardware)
  - setup_audio / setup_hotspot / setup_nginx

The following run identically to the real robot:
  - robot_state_publisher (TF from URDF)
  - scan_filter (/scan → /scan_filtered)
  - rf2o_odometry (LiDAR-based /odom/laser — raw laser odom)
  - ekf_filter_node (fuses rf2o + IMU → /odom)
  - deepracer_node (inference, always runs)
  - agent_node (AI agent tools)
  - webserver_node (REST API on port 8000)

SIM-only processes:
  - ros_gz_bridge: Gazebo ↔ ROS2 topic bridging
  - image_transport republish: /camera/image_raw → /camera/image_raw/compressed
  - cmd_vel_adapter: replaces physicar_driver
    - /speed + /steering → /cmd_vel (inverse Ackermann)
    - /battery_state (always full: 8.4V, 100%, 1Hz)
    - /physicar_driver/calibration_status (default values, transient_local)
    - /servo/commands subscriber (dummy — no hardware)

Host-side requirements:
  - Gazebo Harmonic running with PhysiCar SDF model (physicar-sim repo)

Audio in SIM:
  - No audio_node (no USB audio hardware)
  - Webserver streams /audio topic via SSE at /state/audio
  - gzweb plays audio in browser via Web Audio API

Topic parity — ALL topics available in both real and SIM modes:
  /speed, /steering, /camera/pan, /camera/tilt, /audio,
  /imu, /camera/image_raw/compressed, /scan, /scan_filtered, /odom, /clock,
  /joint_states, /battery_state, /physicar_driver/calibration_status,
  /servo/commands, /deepracer/inference
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import ExecuteProcess, TimerAction
from launch.substitutions import Command
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    # Package directories
    pkg_description = get_package_share_directory('physicar_description')
    pkg_teleop = get_package_share_directory('physicar_teleop')
    pkg_bringup = get_package_share_directory('physicar_bringup')

    # URDF file
    urdf_file = os.path.join(pkg_description, 'urdf', 'physicar.urdf.xacro')
    teleop_config = os.path.join(pkg_teleop, 'config', 'joy_mapping.yaml')
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

    # ── Upper-layer nodes (identical to robot.launch.py) ──

    # DeepRacer inference node (always runs — same as real robot)
    deepracer_node = Node(
        package='physicar_deepracer',
        executable='deepracer_node',
        name='deepracer',
        output='screen',
        parameters=[{'use_sim_time': True}],
        respawn=True,
        respawn_delay=2.0,
    )

    # Joystick driver (SDL2-based, normalises Xbox/PS/Switch controllers)
    # SDL_JOYSTICK_HIDAPI=0: see robot.launch.py for rationale.
    joy_node = Node(
        package='joy',
        executable='joy_node',
        name='joy_node',
        output='log',
        parameters=[{
            'autorepeat_rate': 20.0,
            'deadzone': 0.05,
            'sticky_buttons': False,
            'use_sim_time': False,
        }],
        additional_env={'SDL_JOYSTICK_HIDAPI': '0'},
        respawn=True,
        respawn_delay=3.0,
    )

    # Gamepad teleop — publishes /speed, /steering, /camera/{pan,tilt}
    teleop_node = Node(
        package='physicar_teleop',
        executable='joy_teleop_node',
        name='physicar_joy_teleop',
        output='screen',
        parameters=[teleop_config, {'use_sim_time': False}],
        respawn=True,
        respawn_delay=2.0,
    )

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

    # CmdVel Adapter: /speed + /steering → /cmd_vel (inverse Ackermann)
    # Replaces physicar_driver's role of accepting these topics
    cmd_vel_adapter = Node(
        package='physicar_bringup',
        executable='cmd_vel_adapter_node.py',
        name='cmd_vel_adapter',
        output='screen',
        parameters=[{'use_sim_time': False}],
        respawn=True,
        respawn_delay=2.0,
    )

    # Agent Node
    agent_node = TimerAction(
        period=3.0,
        actions=[
            Node(
                package='physicar_agent',
                executable='agent_node',
                name='agent_node',
                output='screen',
                parameters=[{'use_sim_time': False}],
                respawn=True,
                respawn_delay=2.0,
            )
        ]
    )

    # WebServer Node (REST API, direct access on port 8000)
    webserver_node = TimerAction(
        period=4.0,
        actions=[
            Node(
                package='physicar_webserver',
                executable='webserver_node.py',
                name='webserver',
                output='screen',
                parameters=[{'use_sim_time': False, 'sim_mode': True}],
                respawn=True,
                respawn_delay=2.0,
            )
        ]
    )

    # ── Gazebo Bridge (Gazebo ↔ ROS2) ──
    # ros_gz_bridge: Gazebo topics ↔ ROS2 topics
    # GZ_PARTITION must match between Gazebo and bridge
    gz_bridge = Node(
        package='ros_gz_bridge',
        executable='parameter_bridge',
        arguments=[
            '/imu@sensor_msgs/msg/Imu[gz.msgs.IMU',
            '/mag@sensor_msgs/msg/MagneticField[gz.msgs.Magnetometer',
            '/scan@sensor_msgs/msg/LaserScan[gz.msgs.LaserScan',
            '/joint_states@sensor_msgs/msg/JointState[gz.msgs.Model',
            '/camera/image_raw@sensor_msgs/msg/Image[gz.msgs.Image',
            '/cmd_vel@geometry_msgs/msg/Twist]gz.msgs.Twist',
            '/camera/pan@std_msgs/msg/Float64]gz.msgs.Double',
            '/camera/tilt@std_msgs/msg/Float64]gz.msgs.Double',
            '/clock@rosgraph_msgs/msg/Clock[gz.msgs.Clock',
        ],
        remappings=[
            ('/mag', '/imu/mag'),
        ],
        output='log',
        additional_env={'GZ_PARTITION': 'physicar'},
        respawn=True,
        respawn_delay=2.0,
    )

    # RF2O Laser Odometry → /odom/laser (raw, no TF)
    # EKF fuses this with IMU → /odom (final) + odom→base_footprint TF
    rf2o_odometry = Node(
        package='rf2o_laser_odometry',
        executable='rf2o_laser_odometry_node',
        name='rf2o_laser_odometry',
        output='log',
        arguments=['--ros-args', '--log-level', 'error'],
        parameters=[
            driver_config,
            {
                'laser_scan_topic': '/scan_filtered',
                'use_sim_time': True,
            },
        ],
        respawn=True,
        respawn_delay=2.0,
    )

    # EKF: fuses rf2o (/odom/laser) + IMU (/imu) → /odom + TF
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

    # image_transport: /camera/image_raw (raw) → /camera/image_raw/compressed (jpeg)
    image_republish = ExecuteProcess(
        cmd=[
            'ros2', 'run', 'image_transport', 'republish', 'raw', 'compressed',
            '--ros-args',
            '-r', 'in:=/camera/image_raw',
            '-r', 'out/compressed:=/camera/image_raw/compressed',
        ],
        output='log',
    )

    # Topic Watchdog (sim mode)
    # Monitors /odom/laser — if rf2o gets stuck after a Gazebo world switch
    # (sim time backward jump blocks its Rate::sleep()), kills it so
    # respawn=True restarts it fresh.  Uses wall time (no use_sim_time)
    # so it's immune to sim time issues.
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

    return LaunchDescription([
        # Gazebo bridge (Gz ↔ ROS2 topics)
        gz_bridge,
        image_republish,
        # ROS nodes only — no hardware, no system processes
        robot_state_publisher,
        cmd_vel_adapter,
        scan_filter,
        rf2o_odometry,
        ekf_node,
        deepracer_node,
        agent_node,
        joy_node,
        teleop_node,
        webserver_node,
        topic_watchdog,
    ])
