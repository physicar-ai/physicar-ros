#!/usr/bin/env python3
#
# Copyright 2026 AICASTLE Inc.
#
# SPDX-License-Identifier: GPL-3.0-or-later

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    # Declare arguments
    host_arg = DeclareLaunchArgument(
        'host',
        default_value='0.0.0.0',
        description='Web server host address'
    )
    
    port_arg = DeclareLaunchArgument(
        'port',
        default_value='8000',
        description='Web server port'
    )
    
    # WebServer Node
    webserver_node = Node(
        package='physicar_webserver',
        executable='webserver_node.py',
        name='physicar_webserver',
        output='screen',
        parameters=[{
            'host': LaunchConfiguration('host'),
            'port': LaunchConfiguration('port'),
        }]
    )
    
    return LaunchDescription([
        host_arg,
        port_arg,
        webserver_node,
    ])
