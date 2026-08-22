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

import collections
import math
import time

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
        self.declare_parameter('cmd_timeout', 1.0)   # drive-command validity window (s), 0=off
        self.declare_parameter('min_speed', 0.3)       # m/s (ESC dead zone)

        self.wheelbase = self.get_parameter('wheelbase').value
        self.max_steering_rad = math.radians(
            self.get_parameter('max_steering').value
        )
        self.max_speed = self.get_parameter('max_speed').value
        self.min_speed = self.get_parameter('min_speed').value
        self.cmd_timeout = float(self.get_parameter('cmd_timeout').value)

        # Current state
        self._speed = 0.0      # m/s
        self._steering = 0.0   # rad
        self._last_speed_cmd = time.monotonic()
        # Standstill steering settle window — see _publish_cmd_vel
        self._steer_settle_until = 0.0

        qos = QoSProfile(depth=10)

        # Subscribers — same topics as physicar_driver
        self.create_subscription(Float64, '/speed', self._speed_cb, qos)
        self.create_subscription(Float64, '/steering', self._steering_cb, qos)

        # Publisher
        self._cmd_vel_pub = self.create_publisher(Twist, '/cmd_vel', qos)

        # External /cmd_vel — same contract as the real device driver, which
        # subscribes /cmd_vel directly and converges it into the shared
        # speed/steering state + watchdog (speed expires after cmd_timeout,
        # steering angle persists). Without this, a direct /cmd_vel publish
        # bypassed the watchdog and the sim car kept driving forever.
        # The adapter also PUBLISHES /cmd_vel, so it hears its own messages:
        # each publish queues its expected one-per-publish DDS echo here and
        # the echo is swallowed; anything else on the topic is external.
        self._echo_expect = collections.deque()
        self.create_subscription(Twist, '/cmd_vel', self._cmd_vel_cb, qos)

        self.get_logger().info(
            f'CmdVel adapter ready (L={self.wheelbase}m, '
            f'max_steer={math.degrees(self.max_steering_rad):.0f}°, '
            f'max_speed={self.max_speed}m/s)'
        )

        # Wall clock — fires even when /clock (sim time) is not yet available
        wall_clock = Clock(clock_type=ClockType.STEADY_TIME)

        # Drive-command watchdog — auto-stop when /speed has not been refreshed within cmd_timeout
        # (same contract as the device-side physicar_driver cmd_timeout)
        if self.cmd_timeout > 0.0:
            self.create_timer(0.1, self._cmd_watchdog, clock=wall_clock)

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
        self._last_speed_cmd = time.monotonic()
        self._publish_cmd_vel()

    def _cmd_watchdog(self):
        # Close the standstill settle window: emit one final true-zero Twist
        # so the tiny carrier velocity does not persist (no parking creep).
        if (self._steer_settle_until
                and time.monotonic() >= self._steer_settle_until
                and abs(self._speed) < 1e-3):
            self._steer_settle_until = 0.0
            self._publish_cmd_vel()
        if abs(self._speed) < 1e-3:
            return
        if (time.monotonic() - self._last_speed_cmd) > self.cmd_timeout:
            self.get_logger().warn(
                f'drive command stale (>{self.cmd_timeout:.1f}s) - auto stop')
            self._speed = 0.0
            self._publish_cmd_vel()

    def _cmd_vel_cb(self, msg: Twist):
        now = time.monotonic()
        # Prune unmatched echoes (should not happen) so the queue cannot wedge
        while self._echo_expect and now - self._echo_expect[0][2] > 1.0:
            self._echo_expect.popleft()
        if (self._echo_expect
                and self._echo_expect[0][0] == msg.linear.x
                and self._echo_expect[0][1] == msg.angular.z):
            self._echo_expect.popleft()
            return
        # Same twist -> (speed, steering) conversion as physicar_driver_node.cpp:
        # a (0,0) twist is an explicit stop AND recenters the steering.
        vx = msg.linear.x
        wz = msg.angular.z
        if abs(vx) > 0.01:
            steer = math.atan(wz * self.wheelbase / vx)
        elif abs(wz) > 0.01:
            steer = math.copysign(self.max_steering_rad, wz)
        else:
            steer = 0.0
        self._steering = max(-self.max_steering_rad,
                             min(self.max_steering_rad, steer))
        self._speed = max(-self.max_speed, min(self.max_speed, vx))
        self._last_speed_cmd = time.monotonic()
        self._publish_cmd_vel()

    def _steering_cb(self, msg: Float64):
        self._steering = max(
            -self.max_steering_rad,
            min(self.max_steering_rad, msg.data),
        )
        # Keep the carrier alive briefly so a steering change at standstill —
        # including a return to 0 — actually reaches the steering joints.
        self._steer_settle_until = time.monotonic() + 1.2
        self._publish_cmd_vel()


    def _publish_cmd_vel(self):
        """Convert /speed + /steering → /cmd_vel.

        Inverse Ackermann:
          linear.x  = speed
          angular.z = speed * tan(steering) / wheelbase

        Standstill steering: a Twist cannot express "steering angle at v=0",
        so a tiny carrier velocity (1 mm/s) conveys the angle. CRITICAL: the
        carrier must also be used when steering RETURNS TO 0 — the Ackermann
        plugin treats an exact (0,0) Twist as "stop regulating" and freezes
        the steering joints wherever they are, which used to leave a wheel
        pinned at the limit after releasing the keys (verified against the
        plugin in isolation). The settle window then ends with one true (0,0)
        so the carrier does not cause perpetual creep while parked.
        """
        twist = Twist()
        v = self._speed
        if abs(v) < 0.001 and (abs(self._steering) > 0.001
                               or time.monotonic() < self._steer_settle_until):
            v = 0.001
        twist.linear.x = v
        twist.angular.z = v * math.tan(self._steering) / self.wheelbase

        self._echo_expect.append((twist.linear.x, twist.angular.z, time.monotonic()))
        if len(self._echo_expect) > 16:
            self._echo_expect.popleft()
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
