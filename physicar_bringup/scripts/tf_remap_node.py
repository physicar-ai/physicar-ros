#!/usr/bin/env python3
"""
TF Frame Remapper for Gazebo Simulation.

Gazebo publishes TF with namespaced frames (e.g., physicar/odom, physicar/base_footprint).
This node remaps them to standard ROS 2 conventions (odom, base_footprint)
so SLAM/Nav2 can work without additional configuration.

Subscriptions:
  - /model/physicar/tf (or from gz_bridge remapped to /tf): Gazebo TF

Publications:
  - /tf: Remapped TF with standard frame names
"""

import re
import rclpy
from rclpy.node import Node
from tf2_msgs.msg import TFMessage


class TFRemap(Node):
    """Remaps Gazebo-namespaced TF frames to standard ROS 2 frame names."""

    # Frame name mappings: Gazebo → ROS 2
    FRAME_MAP = {
        'physicar/odom': 'odom',
        'physicar/base_footprint': 'base_footprint',
        'physicar/base_link': 'base_link',
    }

    def __init__(self):
        super().__init__('tf_remap')

        # Subscribe to Gazebo TF (from gz_bridge, which remaps to /tf)
        # We need a separate subscription to the raw Gazebo TF before robot_state_publisher combines them
        self.sub = self.create_subscription(
            TFMessage,
            '/model/physicar/tf',
            self.tf_callback,
            10
        )

        # Publish remapped TF
        self.pub = self.create_publisher(TFMessage, '/tf', 10)

        self.get_logger().info('TF Remap node started')

    def remap_frame(self, frame_id: str) -> str:
        """Remap frame name using the mapping table."""
        return self.FRAME_MAP.get(frame_id, frame_id)

    def tf_callback(self, msg: TFMessage):
        """Remap frame names and republish."""
        remapped = TFMessage()
        for transform in msg.transforms:
            t = transform
            t.header.frame_id = self.remap_frame(t.header.frame_id)
            t.child_frame_id = self.remap_frame(t.child_frame_id)
            remapped.transforms.append(t)

        self.pub.publish(remapped)


def main(args=None):
    rclpy.init(args=args)
    node = TFRemap()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
