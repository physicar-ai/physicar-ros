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
PhysiCar Bringup Launch File
Launches all hardware drivers for the real robot
"""

import os

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    ExecuteProcess,
    TimerAction,
)
from launch.conditions import IfCondition
from launch.substitutions import Command, LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.parameter_descriptions import ParameterValue


def generate_launch_description():
    # Package directories
    pkg_bringup = get_package_share_directory('physicar_bringup')
    pkg_description = get_package_share_directory('physicar_description')
    pkg_teleop = get_package_share_directory('physicar_teleop')

    # Config files
    driver_config = os.path.join(pkg_bringup, 'config', 'driver_params.yaml')
    teleop_config = os.path.join(pkg_teleop, 'config', 'joy_mapping.yaml')

    # Scripts directory
    scripts_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        'scripts'
    )

    # URDF file
    urdf_file = os.path.join(pkg_description, 'urdf', 'physicar.urdf.xacro')

    # Launch arguments (camera/lidar always enabled - hardware required)
    use_camera = LaunchConfiguration('use_camera', default='true')
    use_lidar = LaunchConfiguration('use_lidar', default='true')
    serial_port = LaunchConfiguration('serial_port', default='/dev/yahboom')

    # Declare launch arguments
    declare_use_camera = DeclareLaunchArgument(
        'use_camera',
        default_value='true',
        description='Enable camera driver'
    )

    declare_use_lidar = DeclareLaunchArgument(
        'use_lidar',
        default_value='true',
        description='Enable LiDAR driver'
    )

    declare_serial_port = DeclareLaunchArgument(
        'serial_port',
        default_value='/dev/yahboom',
        description='Serial port for expansion board (set by udev rules)'
    )

    # Robot description from URDF/xacro
    robot_description = ParameterValue(
        Command(['xacro ', urdf_file]),
        value_type=str
    )

    # Robot State Publisher (publishes TF from URDF)
    robot_state_publisher = Node(
        package='robot_state_publisher',
        executable='robot_state_publisher',
        output='screen',
        parameters=[
            {'robot_description': robot_description},
            {'use_sim_time': False}
        ],
        respawn=True,
        respawn_delay=2.0,
    )

    # PhysiCar Base Driver
    physicar_driver = Node(
        package='physicar_bringup',
        executable='physicar_driver_node.py',
        name='physicar_driver',
        output='screen',
        parameters=[
            driver_config,
            {'serial_port': serial_port}
        ],
        respawn=True,
        respawn_delay=2.0,
    )

    # RPLidar C1 Driver (rplidar_ros)
    # Using submodule with C1 support (requires version > 2.1.4)
    rplidar_driver = Node(
        package='rplidar_ros',
        executable='rplidar_node',
        name='rplidar_node',
        output='screen',
        parameters=[driver_config],
        condition=IfCondition(use_lidar),
        respawn=True,
        respawn_delay=2.0,
    )

    # Scan Filter (filters invalid readings for rf2o)
    # Converts inf, nan, out-of-range values to 0 for proper handling
    scan_filter = Node(
        package='physicar_bringup',
        executable='scan_filter_node.py',
        name='scan_filter',
        output='log',
        parameters=[{
            'input_topic': '/scan',
            'output_topic': '/scan_filtered',
            'range_margin': 0.1,
        }],
        condition=IfCondition(use_lidar),
        respawn=True,
        respawn_delay=2.0,
    )

    # Camera Driver (Raspberry Pi Camera via camera_ros/libcamera)
    # Compressed output is handled by undistort_node, so remapping is removed
    camera_driver = Node(
        package='camera_ros',
        executable='camera_node',
        name='camera',
        output='screen',
        parameters=[driver_config],
        remappings=[
            ('~/image_raw', '/camera/image_raw'),
            ('~/camera_info', '/camera/camera_info'),
        ],
        condition=IfCondition(use_camera),
        respawn=True,
        respawn_delay=2.0,
    )

    # Camera: camera_ros captures 640x480 → undistort+resize → publishes 480x360 image_raw
    # camera_ros publishes /camera/image_raw/compressed automatically (image_transport standard)

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
            {'laser_scan_topic': '/scan_filtered'},
        ],
        condition=IfCondition(use_lidar),
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
        parameters=[ekf_config],
        remappings=[('odometry/filtered', '/odom')],
        condition=IfCondition(use_lidar),
        respawn=True,
        respawn_delay=2.0,
    )

    # Audio Node (TTS, file playback, streaming)
    # Sounds directory path for audio files
    sounds_dir = os.path.join(pkg_bringup, 'sounds')
    audio_node = Node(
        package='physicar_bringup',
        executable='audio_node.py',
        name='audio_node',
        output='screen',
        parameters=[{
            'default_volume': 1.0,
            'default_language': 'ko',
            'sounds_dir': sounds_dir,
        }],
        respawn=True,
        respawn_delay=2.0,
    )

    # DeepRacer inference node (always runs)
    deepracer_node = Node(
        package='physicar_deepracer',
        executable='deepracer_node.py',
        name='deepracer',
        output='screen',
        respawn=True,
        respawn_delay=2.0,
    )

    # Joystick driver (SDL2-based, normalises Xbox/PS/Switch controllers)
    # Reads /dev/input/jsX from host via --privileged.
    # SDL_JOYSTICK_HIDAPI=0: disable SDL's HIDAPI backend so SDL falls back
    # to the kernel evdev/joystick interface.  The HIDAPI backend mis-parses
    # xpadneo's BLE Xbox controller HID reports (only battery events get
    # through, sticks/buttons silently dropped).  evdev fallback is fine
    # because xpadneo already exposes a standard js0/event device.
    joy_node = Node(
        package='joy',
        executable='joy_node',
        name='joy_node',
        output='log',
        parameters=[{
            # 20Hz autorepeat: joy_node only emits a /joy message when an axis
            # or button *changes*, so if the user holds LB perfectly still the
            # topic goes silent.  Our stale-joy guard (0.5s) would then
            # falsely release the lock.  Forcing 20Hz republish of the held
            # state keeps the lock latched as long as the button is down.
            'autorepeat_rate': 20.0,
            'deadzone': 0.05,
            'sticky_buttons': False,
        }],
        additional_env={'SDL_JOYSTICK_HIDAPI': '0'},
        respawn=True,
        respawn_delay=3.0,
    )

    # Gamepad teleop — translates /joy into /speed, /steering, /camera/{pan,tilt}
    teleop_node = TimerAction(
        period=2.0,
        actions=[
            Node(
                package='physicar_teleop',
                executable='joy_teleop_node',
                name='physicar_joy_teleop',
                output='screen',
                parameters=[teleop_config],
                respawn=True,
                respawn_delay=2.0,
            )
        ]
    )

    # Agent Node (tool management, auto-detection of topics/services/actions)
    # 3-second delay so other nodes have started first
    agent_node = TimerAction(
        period=3.0,
        actions=[
            Node(
                package='physicar_agent',
                executable='agent_node',
                name='agent_node',
                output='screen',
                respawn=True,
                respawn_delay=2.0,
            )
        ]
    )

    # WebServer Node (REST API)
    # 4-second delay so it starts after the Agent node
    webserver_node = TimerAction(
        period=4.0,
        actions=[
            Node(
                package='physicar_webserver',
                executable='webserver_node.py',
                name='webserver',
                output='screen',
                respawn=True,
                respawn_delay=2.0,
            )
        ]
    )

    # Topic Watchdog (restart trigger)
    # On stale sensor topic, SIGTERM the responsible node → respawn=True restarts it.
    # Starts monitoring after startup_grace=30s.
    topic_watchdog = TimerAction(
        period=10.0,
        actions=[
            Node(
                package='physicar_bringup',
                executable='topic_watchdog_node.py',
                name='topic_watchdog',
                output='screen',
                respawn=True,
                respawn_delay=5.0,
            )
        ]
    )

    # ── System processes (non-ROS) ──

    # USB Audio setup (runs once at startup)
    setup_audio = ExecuteProcess(
        cmd=['bash', os.path.join(scripts_dir, 'setup_audio.sh')],
        output='screen',
    )

    # Play intro sound — wait for audio_node subscriber instead of fixed delay.
    # Uses TRANSIENT_LOCAL so a late-matching subscriber still receives it,
    # and waits for actual subscriber match before publishing.
    intro_sound = os.path.join(sounds_dir, 'intro.mp3')
    play_intro = TimerAction(
        period=3.0,
        actions=[
            ExecuteProcess(
                cmd=['python3', '-c', (
                    'import rclpy, time\n'
                    'from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy\n'
                    'from physicar_interfaces.msg import Audio\n'
                    'rclpy.init()\n'
                    'node = rclpy.create_node("intro")\n'
                    'qos = QoSProfile(reliability=ReliabilityPolicy.RELIABLE,\n'
                    '                 durability=DurabilityPolicy.TRANSIENT_LOCAL,\n'
                    '                 history=HistoryPolicy.KEEP_LAST, depth=1)\n'
                    'pub = node.create_publisher(Audio, "/audio", qos)\n'
                    'deadline = time.time() + 30.0\n'
                    'while pub.get_subscription_count() == 0 and time.time() < deadline:\n'
                    '    rclpy.spin_once(node, timeout_sec=0.1)\n'
                    'msg = Audio()\n'
                    'msg.channel = "intro"\n'
                    f'msg.data = list(open("{intro_sound}", "rb").read())\n'
                    'msg.format = "mp3"\n'
                    'msg.volume = 1.0\n'
                    'pub.publish(msg)\n'
                    'try: pub.wait_for_all_acked(rclpy.duration.Duration(seconds=5))\n'
                    'except Exception: pass\n'
                    'end = time.time() + 1.0\n'
                    'while time.time() < end: rclpy.spin_once(node, timeout_sec=0.1)\n'
                    'node.destroy_node()\n'
                    'rclpy.shutdown()\n'
                )],
                output='log',
            )
        ]
    )

    return LaunchDescription([
        declare_use_camera,
        declare_use_lidar,
        declare_serial_port,
        # System setup
        setup_audio,
        # ROS nodes
        robot_state_publisher,
        physicar_driver,
        rplidar_driver,
        scan_filter,
        camera_driver,
        rf2o_odometry,
        ekf_node,
        audio_node,
        deepracer_node,
        agent_node,
        joy_node,
        teleop_node,
        webserver_node,
        topic_watchdog,
        play_intro,
    ])
