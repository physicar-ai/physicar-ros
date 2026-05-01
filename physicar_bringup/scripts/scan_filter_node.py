#!/usr/bin/env python3
"""
Scan Filter Node for PhysiCar

Filters invalid LaserScan readings (inf, nan, out of range) to improve
downstream odometry estimation (rf2o_laser_odometry).

Subscribes: /scan
Publishes: /scan_filtered
"""

import math

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, QoSReliabilityPolicy, QoSDurabilityPolicy
from sensor_msgs.msg import LaserScan


class ScanFilterNode(Node):
    def __init__(self):
        super().__init__('scan_filter')
        
        # Parameters
        self.declare_parameter('input_topic', '/scan')
        self.declare_parameter('output_topic', '/scan_filtered')
        self.declare_parameter('range_margin', 0.1)  # margin from range_max
        
        input_topic = self.get_parameter('input_topic').value
        output_topic = self.get_parameter('output_topic').value
        self.range_margin = self.get_parameter('range_margin').value
        
        # QoS for lidar (best effort, volatile)
        sensor_qos = QoSProfile(
            reliability=QoSReliabilityPolicy.BEST_EFFORT,
            durability=QoSDurabilityPolicy.VOLATILE,
            depth=1
        )
        
        # Subscriber and Publisher
        self.subscription = self.create_subscription(
            LaserScan,
            input_topic,
            self.scan_callback,
            sensor_qos
        )
        
        self.publisher = self.create_publisher(
            LaserScan,
            output_topic,
            sensor_qos
        )
        
        self.get_logger().info(f'Scan filter: {input_topic} -> {output_topic}')
    
    def scan_callback(self, msg: LaserScan):
        """Filter invalid readings and republish."""
        # Create filtered message (copy metadata)
        filtered = LaserScan()
        filtered.header = msg.header
        filtered.angle_min = msg.angle_min
        filtered.angle_max = msg.angle_max
        filtered.angle_increment = msg.angle_increment
        filtered.time_increment = msg.time_increment
        filtered.scan_time = msg.scan_time
        filtered.range_min = msg.range_min
        filtered.range_max = msg.range_max
        
        # Filter ranges
        # rf2o treats 0 as invalid, so convert bad readings to 0
        effective_max = msg.range_max - self.range_margin
        filtered_ranges = []
        
        for r in msg.ranges:
            if (math.isfinite(r) and 
                r > msg.range_min and 
                r < effective_max):
                filtered_ranges.append(r)
            else:
                filtered_ranges.append(0.0)
        
        filtered.ranges = filtered_ranges
        
        # Copy intensities if present
        if msg.intensities:
            filtered.intensities = list(msg.intensities)
        
        self.publisher.publish(filtered)


def main(args=None):
    rclpy.init(args=args)
    node = ScanFilterNode()
    
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
