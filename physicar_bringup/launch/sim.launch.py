#!/usr/bin/env python3
#
# Copyright 2026 AICASTLE Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""
PhysiCar Simulation Launch File

Gazebo simulation mode — hardware drivers are NOT launched.
Gazebo runs as a separate process; ros_gz_bridge connects via gz-transport.

Compared to device.launch.py, the following are EXCLUDED:
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

SIM-only processes:
  - ros_gz_bridge: Gazebo ↔ ROS2 topic bridging
  - image_transport republish: /camera/image_raw → /camera/image_raw/compressed
  - cmd_vel_adapter: replaces physicar_driver
    - /speed + /steering → /cmd_vel (inverse Ackermann)
    - /battery_state (always full: 8.4V, 100%, 1Hz)
    - /servo/commands subscriber (dummy — no hardware)

Host-side requirements:
  - Gazebo Harmonic running with PhysiCar SDF model (physicar-sim repo)

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

    # URDF file
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

    # ── Upper-layer nodes (identical to device.launch.py) ──

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
        ('ros_gz_bridge', 'parameter_bridge', gz_bridge, 'gz_bridge'),
        ('image_transport', 'republish', image_republish, 'image_republish'),
        ('robot_state_publisher', 'robot_state_publisher',
         robot_state_publisher, 'robot_state_publisher'),
        ('physicar_bringup', 'cmd_vel_adapter_node.py',
         cmd_vel_adapter, 'cmd_vel_adapter'),
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
    return LaunchDescription(actions)
