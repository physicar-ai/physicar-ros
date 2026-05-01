#!/usr/bin/env python3
"""
Topic Watchdog Node for PhysiCar.

Monitors critical sensor topics. If a topic stops publishing for longer than
its configured timeout, kills the responsible process (launch will respawn it
because every node has respawn=True).

This handles cases where a node is alive (process still running, ROS spin
working) but its publish callback got stuck — common with libcamera, V4L2,
DDS shared-memory glitches, etc.

Restart policy:
- Startup grace period: ignore stale topics for the first N seconds after
  boot or after any kill (gives respawned nodes time to initialise).
- Cooldown: don't kill the same target twice within `cooldown` seconds.
"""

import os
import signal
import subprocess
import time

import rclpy
from rclpy.node import Node
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy

from sensor_msgs.msg import CompressedImage, LaserScan, BatteryState, Imu
from nav_msgs.msg import Odometry


# topic, msg_type, timeout(s), kill_pattern (pgrep -f)
WATCH_TOPICS = [
    ("/camera/image_raw/compressed", CompressedImage, 5.0, "camera_ros/lib/camera_ros/camera_node"),
    ("/scan",                        LaserScan,        3.0, "rplidar_node"),
    ("/scan_filtered",               LaserScan,        3.0, "scan_filter_node"),
    ("/odom",                        Odometry,         3.0, "rf2o_laser_odometry_node"),
    ("/battery_state",               BatteryState,    15.0, "physicar_driver_node"),
    ("/imu",                         Imu,              5.0, "physicar_driver_node"),
]


class TopicWatchdog(Node):
    def __init__(self):
        super().__init__("topic_watchdog")

        self.declare_parameter("startup_grace_sec", 30.0)
        self.declare_parameter("cooldown_sec", 30.0)
        self.declare_parameter("check_period_sec", 2.0)
        # Disable watchdog under simulation — gz_bridge / image_republish are
        # ExecuteProcess (no respawn), and Gazebo runs on the host which we
        # can't control. Avoid false-positive kills.
        self.declare_parameter("enabled", True)

        self.enabled = bool(self.get_parameter("enabled").value)
        if not self.enabled:
            self.get_logger().info("[Watchdog] disabled (enabled=False)")
            return

        self.startup_grace = float(self.get_parameter("startup_grace_sec").value)
        self.cooldown = float(self.get_parameter("cooldown_sec").value)
        self.check_period = float(self.get_parameter("check_period_sec").value)

        self.start_time = time.monotonic()
        self.last_msg_time: dict[str, float] = {}
        self.last_kill_time: dict[str, float] = {}
        self.subs = []

        qos_be = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
        )
        qos_rel = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
        )

        for topic, msg_type, _timeout, _pattern in WATCH_TOPICS:
            qos = qos_be if msg_type in (CompressedImage, LaserScan, Imu) else qos_rel
            self.last_msg_time[topic] = self.start_time
            self.subs.append(self.create_subscription(
                msg_type, topic,
                lambda _msg, t=topic: self._on_msg(t),
                qos,
            ))

        self.timer = self.create_timer(self.check_period, self._check)
        self.get_logger().info(
            f"[Watchdog] monitoring {len(WATCH_TOPICS)} topics "
            f"(grace={self.startup_grace:.0f}s, cooldown={self.cooldown:.0f}s)"
        )

    def _on_msg(self, topic: str):
        self.last_msg_time[topic] = time.monotonic()

    def _check(self):
        now = time.monotonic()

        if now - self.start_time < self.startup_grace:
            return

        for topic, _msg_type, timeout, pattern in WATCH_TOPICS:
            last = self.last_msg_time.get(topic, self.start_time)
            stale = now - last
            if stale < timeout:
                continue

            last_kill = self.last_kill_time.get(pattern, 0.0)
            if now - last_kill < self.cooldown:
                continue

            self.get_logger().warn(
                f"[Watchdog] {topic} stale for {stale:.1f}s "
                f"(timeout={timeout:.0f}s) → killing '{pattern}'"
            )
            if self._kill(pattern):
                self.last_kill_time[pattern] = now
                # reset all topics that share this pattern so respawn has time
                for t, _m, _to, p in WATCH_TOPICS:
                    if p == pattern:
                        self.last_msg_time[t] = now

    def _kill(self, pattern: str) -> bool:
        try:
            res = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True, text=True, timeout=3,
            )
            pids = [int(p) for p in res.stdout.split() if p.isdigit()]
            # Filter out our own process and the launch parent (defensive)
            my_pid = os.getpid()
            pids = [p for p in pids if p != my_pid]
            if not pids:
                self.get_logger().warn(f"[Watchdog] no process matching '{pattern}'")
                return False
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                    self.get_logger().info(f"[Watchdog] SIGTERM pid={pid} ({pattern})")
                except ProcessLookupError:
                    pass
            return True
        except Exception as e:
            self.get_logger().error(f"[Watchdog] kill failed: {e}")
            return False


def main():
    rclpy.init()
    node = TopicWatchdog()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
