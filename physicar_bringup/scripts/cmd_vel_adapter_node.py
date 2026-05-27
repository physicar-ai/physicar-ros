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

try:
    from physicar_interfaces.msg import CalibrationStatus
    HAS_CALIBRATION_MSG = True
except ImportError:
    HAS_CALIBRATION_MSG = False

try:
    from physicar_interfaces.msg import TeleopStatus
    HAS_TELEOP_STATUS = True
except ImportError:
    HAS_TELEOP_STATUS = False


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

        # Teleop drive-lock state — mirrors physicar_driver_node behaviour, with
        # freshness gating: if /teleop/status stops arriving within its
        # declared timeout the lock auto-releases.  Per-source aggregation
        # (drive_engaged = OR over fresh sources) so multiple publishers
        # (joy, web, …) can coexist without cancelling each other.
        self._teleop_sources: dict = {}
        self._teleop_status_last_time = None
        self._teleop_status_timeout_sec = 0.5
        if HAS_TELEOP_STATUS:
            self.create_subscription(
                TeleopStatus,
                '/teleop/status',
                self._on_teleop_status,
                qos,
            )

        # Subscribers — same topics as physicar_driver
        self.create_subscription(Float64, '/speed', self._speed_cb, qos)
        self.create_subscription(Float64, '/steering', self._steering_cb, qos)

        # Joy teleop priority mirrors — always honoured.
        self.create_subscription(Float64, '/teleop/speed', self._teleop_speed_cb, qos)
        self.create_subscription(Float64, '/teleop/steering', self._teleop_steering_cb, qos)

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

        # SIM: publish calibration status with default values (once, latched via transient_local)
        if HAS_CALIBRATION_MSG:
            from rclpy.qos import QoSDurabilityPolicy
            cal_qos = QoSProfile(
                depth=1,
                durability=QoSDurabilityPolicy.TRANSIENT_LOCAL,
            )
            self._cal_pub = self.create_publisher(
                CalibrationStatus, '/physicar_driver/calibration_status', cal_qos)
            # Publish once after a short delay so subscribers can connect
            self._cal_timer = self.create_timer(1.0, self._publish_calibration_once, clock=wall_clock)

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

    def _publish_calibration_once(self):
        """Publish default calibration status (SIM has no real calibration)."""
        msg = CalibrationStatus()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.max_steering = 20.0
        msg.max_speed = self.max_speed
        msg.max_pan = 45.0
        msg.max_tilt = 45.0
        msg.steering_center = 0.0
        msg.pan_center = 0.0
        msg.tilt_center = 0.0
        msg.reverse_direction = False
        msg.source = 'sim_defaults'
        msg.is_saved = True
        msg.file_path = ''
        self._cal_pub.publish(msg)
        self.get_logger().info('Published SIM calibration status (defaults)')
        # Cancel timer — only need to publish once (transient_local keeps it)
        self.destroy_timer(self._cal_timer)

    def _speed_cb(self, msg: Float64):
        if self._drive_engaged():
            return
        self._apply_speed(msg.data)

    def _teleop_speed_cb(self, msg: Float64):
        self._apply_speed(msg.data)

    def _apply_speed(self, value: float):
        speed = max(-self.max_speed, min(self.max_speed, value))
        self._speed = speed
        self._publish_cmd_vel()

    def _steering_cb(self, msg: Float64):
        if self._drive_engaged():
            return
        self._apply_steering(msg.data)

    def _teleop_steering_cb(self, msg: Float64):
        self._apply_steering(msg.data)

    def _apply_steering(self, value: float):
        self._steering = max(
            -self.max_steering_rad,
            min(self.max_steering_rad, value),
        )
        self._publish_cmd_vel()

    def _on_teleop_status(self, msg) -> None:
        source = str(msg.source) if msg.source else 'unknown'
        timeout_sec = float(msg.timeout.sec) + float(msg.timeout.nanosec) / 1e9
        if timeout_sec <= 0.0:
            timeout_sec = 0.5
        self._teleop_status_timeout_sec = max(
            self._teleop_status_timeout_sec, timeout_sec
        )
        self._teleop_status_last_time = self.get_clock().now()

        prev_drive = self._drive_engaged()
        self._teleop_sources[source] = {
            'drive': bool(msg.drive_engaged),
            'timeout_sec': timeout_sec,
            'last_time_ns': self._teleop_status_last_time.nanoseconds,
        }
        new_drive = self._drive_engaged()
        if new_drive != prev_drive:
            self.get_logger().info(
                f"Teleop drive {'engaged' if new_drive else 'released'} "
                f"(source={source})"
            )

    def _drive_engaged(self) -> bool:
        """OR over all fresh sources."""
        if self._teleop_status_last_time is None:
            return False
        now_ns = self.get_clock().now().nanoseconds
        for st in self._teleop_sources.values():
            if (now_ns - st['last_time_ns']) > int(st['timeout_sec'] * 1e9):
                continue
            if st['drive']:
                return True
        return False


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
