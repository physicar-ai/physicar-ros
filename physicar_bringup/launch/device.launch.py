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

    # Sounds directory
    sounds_dir = os.path.join(pkg_bringup, 'sounds')

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
        executable='physicar_driver_node',
        name='physicar_driver',
        output='screen',
        parameters=[
            driver_config,
            {'serial_port': serial_port}
        ],
        respawn=True,
        respawn_delay=2.0,
    )

    # RPLidar C1 Driver (physicar_lidar, vendored from slamtec rplidar_ros)
    # C1 support requires SDK version > 2.1.4
    rplidar_driver = Node(
        package='physicar_lidar',
        executable='rplidar_node',
        name='rplidar_node',
        output='screen',
        parameters=[driver_config],
        condition=IfCondition(use_lidar),
        respawn=True,
        respawn_delay=2.0,
    )

    # Scan Filter (filters invalid readings for laser odom)
    # Converts inf, nan, out-of-range values to 0 for proper handling
    scan_filter = Node(
        package='physicar_bringup',
        executable='scan_filter_node',
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

    # Camera Driver (Raspberry Pi Camera via physicar_camera/libcamera)
    # Compressed output is handled by undistort_node, so remapping is removed
    camera_driver = Node(
        package='physicar_camera',
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

    # Camera: physicar_camera captures 640x480 → undistort+resize → publishes 480x360 image_raw
    # physicar_camera publishes /camera/image_raw/compressed automatically (image_transport standard)

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
            },
        ],
        condition=IfCondition(use_lidar),
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
        parameters=[ekf_config],
        remappings=[('odometry/filtered', '/odom')],
        condition=IfCondition(use_lidar),
        respawn=True,
        respawn_delay=2.0,
    )

    # DeepRacer inference node (always runs)
    deepracer_node = Node(
        package='physicar_deepracer',
        executable='deepracer_node',
        name='deepracer',
        output='screen',
        respawn=True,
        respawn_delay=2.0,
    )

    # Joystick driver (SDL2-based, normalises Xbox/PS/Switch controllers)
    # Reads /dev/input/jsX directly.
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
        period=16.0,
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

    # WebServer Node (REST API)
    webserver_node = TimerAction(
        period=18.0,
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
        period=20.0,
        actions=[
            Node(
                package='physicar_bringup',
                executable='topic_watchdog_node',
                name='topic_watchdog',
                output='screen',
                # Disabled: its own subscriptions can starve during the boot
                # discovery burst, so it SIGTERMs healthy nodes ("stale" topics
                # it never matched) in an endless kill loop that also leaves
                # zombie /dev/shm ports behind. respawn=True on each node
                # already covers real crashes.
                parameters=[{'enabled': False, 'startup_grace_sec': 60.0}],
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

    # Play intro sound — mpv straight to ALSA (the audio system itself is a
    # webserver subsystem now; the boot jingle doesn't need to wait for it).
    # Skip in DEV mode to avoid annoying sound during development.
    intro_sound = os.path.join(sounds_dir, 'intro.mp3')
    is_dev = os.environ.get('DEV', '').lower() == 'true'
    play_intro = TimerAction(
        period=20.0,
        actions=[
            ExecuteProcess(
                cmd=['mpv', '--no-video', '--really-quiet', intro_sound],
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
        TimerAction(period=6.0, actions=[scan_filter]),
        camera_driver,
        TimerAction(period=11.0, actions=[laser_odom]),
        TimerAction(period=16.0, actions=[ekf_node]),
        deepracer_node,
        joy_node,
        teleop_node,
        webserver_node,
        topic_watchdog,
        play_intro,
    ])
