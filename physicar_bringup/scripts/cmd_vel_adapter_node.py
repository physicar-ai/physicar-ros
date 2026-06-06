#!/usr/bin/env python3
"""
CmdVel Adapter Node for PhysiCar (SIM mode)

Converts low-level control topics (/speed, /steering) to /cmd_vel (Twist)
for Gazebo simulation. This is the inverse of the Ackermann conversion
in physicar_driver_node.py.

Real robot:
  /speed (m/s)  ──┐
                   ├──▶ physicar_driver → ESC + servo
  /steering (rad) ─┘

Simulation (this node):
  /speed (m/s)  ──┐
                   ├──▶ cmd_vel_adapter → /cmd_vel (Twist) → Gazebo
  /steering (rad) ─┘

Ackermann kinematics (same as physicar_driver_node.py):
  Real (forward):  steering = atan(ω * L / v)
  SIM  (inverse):  ω = v * tan(steering) / L

Physical parameters (from URDF/driver):
  wheelbase = 0.18 m
  max_steering = 20° (0.3491 rad)
  max_speed = 3.0 m/s

Subscribes:
  /speed (std_msgs/Float64) - target speed in m/s
  /steering (std_msgs/Float64) - steering angle in radians

Publishes:
  /cmd_vel (geometry_msgs/Twist) - velocity command for Gazebo
"""

import math

import rclpy
from rclpy.clock import Clock, ClockType
from rclpy.node import Node
from rclpy.qos import QoSProfile

from geometry_msgs.msg import Twist
from sensor_msgs.msg import BatteryState
from std_msgs.msg import Float64, Float64MultiArray


class CmdVelAdapterNode(Node):
    def __init__(self):
        super().__init__('cmd_vel_adapter')

        # Physical parameters (same defaults as physicar_driver_node)
        self.declare_parameter('wheelbase', 0.18)
        self.declare_parameter('max_steering', 20.0)  # degrees
        self.declare_parameter('max_speed', 3.0)      # m/s
        self.declare_parameter('min_speed', 0.3)       # m/s (ESC dead zone)

        self.wheelbase = self.get_parameter('wheelbase').value
        self.max_steering_rad = math.radians(
            self.get_parameter('max_steering').value
        )
        self.max_speed = self.get_parameter('max_speed').value
        self.min_speed = self.get_parameter('min_speed').value

        # Current state
        self._speed = 0.0      # m/s
        self._steering = 0.0   # rad

        qos = QoSProfile(depth=10)

        # Subscribers — same topics as physicar_driver
        self.create_subscription(Float64, '/speed', self._speed_cb, qos)
        self.create_subscription(Float64, '/steering', self._steering_cb, qos)

        # Publisher
        self._cmd_vel_pub = self.create_publisher(Twist, '/cmd_vel', qos)

        self.get_logger().info(
            f'CmdVel adapter ready (L={self.wheelbase}m, '
            f'max_steer={math.degrees(self.max_steering_rad):.0f}°, '
            f'max_speed={self.max_speed}m/s)'
        )

        # Wall clock — fires even when /clock (sim time) is not yet available
        wall_clock = Clock(clock_type=ClockType.STEADY_TIME)

        # SIM: publish fake full battery (1Hz)
        self._battery_pub = self.create_publisher(BatteryState, '/battery_state', qos)
        self.create_timer(1.0, self._publish_battery, clock=wall_clock)

        # SIM: dummy /servo/commands subscriber (topic exists but no hardware)
        self.create_subscription(
            Float64MultiArray, '/servo/commands', lambda msg: None, qos)

    def _publish_battery(self):
        msg = BatteryState()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.voltage = 8.4          # Full 2S LiPo
        msg.percentage = 1.0       # 100%
        msg.current = 0.0
        msg.power_supply_status = BatteryState.POWER_SUPPLY_STATUS_FULL
        msg.power_supply_technology = BatteryState.POWER_SUPPLY_TECHNOLOGY_LIPO
        msg.present = True
        self._battery_pub.publish(msg)

    def _speed_cb(self, msg: Float64):
        speed = max(-self.max_speed, min(self.max_speed, msg.data))
        self._speed = speed
        self._publish_cmd_vel()

    def _steering_cb(self, msg: Float64):
        self._steering = max(
            -self.max_steering_rad,
            min(self.max_steering_rad, msg.data),
        )
        self._publish_cmd_vel()


    def _publish_cmd_vel(self):
        """Convert /speed + /steering → /cmd_vel.

        Inverse Ackermann:
          linear.x  = speed
          angular.z = speed * tan(steering) / wheelbase

        physicar_driver_node.py cmd_vel_callback inverse:
          forward: steering = atan(ω * L / v)
          inverse: ω = v * tan(steering) / L
        """
        twist = Twist()
        # If speed=0 but steering is non-zero, use a tiny speed to convey angle
        v = self._speed
        if abs(v) < 0.001 and abs(self._steering) > 0.001:
            v = 0.001
        twist.linear.x = v
        twist.angular.z = v * math.tan(self._steering) / self.wheelbase

        self._cmd_vel_pub.publish(twist)


def main(args=None):
    rclpy.init(args=args)
    node = CmdVelAdapterNode()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
