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
Gazebo runs on the HOST, this container runs with --network host so
ros_gz_bridge (in container) can talk to Gazebo via gz-transport.

Compared to robot.launch.py, the following are EXCLUDED:
  - physicar_driver (serial/Yahboom board)
  - camera_ros (libcamera)
  - rplidar_ros (serial LiDAR)
  - rf2o_odometry (laser odom — Gazebo provides /odom directly)
  - audio_node (no audio hardware)
  - setup_audio / setup_hotspot / setup_nginx

The following run identically to the real robot:
  - robot_state_publisher (TF from URDF)
  - scan_filter (/scan → /scan_filtered)
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

    # URDF file
    urdf_file = os.path.join(pkg_description, 'urdf', 'physicar.urdf.xacro')
    teleop_config = os.path.join(pkg_teleop, 'config', 'joy_mapping.yaml')

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
        executable='deepracer_node.py',
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
            'use_sim_time': True,
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
        parameters=[teleop_config, {'use_sim_time': True}],
        respawn=True,
        respawn_delay=2.0,
    )

    # Scan filter: /scan → /scan_filtered (same as real robot)
    scan_filter = Node(
        package='physicar_bringup',
        executable='scan_filter_node.py',
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
        parameters=[{'use_sim_time': True}],
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
                parameters=[{'use_sim_time': True}],
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
                parameters=[{'use_sim_time': True, 'sim_mode': True}],
                respawn=True,
                respawn_delay=2.0,
            )
        ]
    )

    # ── Gazebo Bridge (container ↔ Gazebo on host via --network host) ──
    # ros_gz_bridge: Gazebo topics ↔ ROS2 topics
    # GZ_PARTITION must match between Gazebo (host) and bridge (container)
    gz_bridge = Node(
        package='ros_gz_bridge',
        executable='parameter_bridge',
        arguments=[
            '/imu@sensor_msgs/msg/Imu[gz.msgs.IMU',
            '/scan@sensor_msgs/msg/LaserScan[gz.msgs.LaserScan',
            '/odom@nav_msgs/msg/Odometry[gz.msgs.Odometry',
            '/joint_states@sensor_msgs/msg/JointState[gz.msgs.Model',
            '/camera/image_raw@sensor_msgs/msg/Image[gz.msgs.Image',
            '/cmd_vel@geometry_msgs/msg/Twist]gz.msgs.Twist',
            '/camera/pan@std_msgs/msg/Float64]gz.msgs.Double',
            '/camera/tilt@std_msgs/msg/Float64]gz.msgs.Double',
            '/clock@rosgraph_msgs/msg/Clock[gz.msgs.Clock',
            '/model/physicar/tf@tf2_msgs/msg/TFMessage[gz.msgs.Pose_V',
        ],
        output='log',
        additional_env={'GZ_PARTITION': 'physicar'},
        respawn=True,
        respawn_delay=2.0,
    )

    # TF Remap: physicar/odom → odom, physicar/base_footprint → base_footprint
    # Needed for SLAM/Nav2 which expect standard frame names
    tf_remap = Node(
        package='physicar_bringup',
        executable='tf_remap_node.py',
        name='tf_remap',
        output='screen',
        parameters=[{'use_sim_time': True}],
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

    return LaunchDescription([
        # Gazebo bridge (Gz ↔ ROS2 topics) — container has --network host
        gz_bridge,
        tf_remap,
        image_republish,
        # ROS nodes only — no hardware, no system processes
        robot_state_publisher,
        cmd_vel_adapter,
        scan_filter,
        deepracer_node,
        agent_node,
        joy_node,
        teleop_node,
        webserver_node,
    ])
