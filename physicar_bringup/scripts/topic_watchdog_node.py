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

from rosgraph_msgs.msg import Clock
from sensor_msgs.msg import CameraInfo, LaserScan, BatteryState, Imu
from nav_msgs.msg import Odometry


# topic, msg_type, timeout(s), kill_pattern (pgrep -f)
REAL_WATCH_TOPICS = [
    ("/camera/camera_info", CameraInfo, 5.0, "camera_ros/lib/camera_ros/camera_node"),
    ("/scan",                        LaserScan,        3.0, "rplidar_node"),
    ("/scan_filtered",               LaserScan,        3.0, "scan_filter_node"),
    ("/odom",                        Odometry,         3.0, "ekf_filter_node"),
    ("/battery_state",               BatteryState,    15.0, "physicar_driver_node"),
    ("/imu",                         Imu,              5.0, "physicar_driver_node"),
]

# SIM mode: watch rf2o, EKF, and Gazebo bridge topics.
# rf2o is the primary target: its Rate::sleep() blocks on sim time backward
# jumps after a world switch, stalling /odom/laser and cascading to EKF.
# /scan comes from ros_gz_bridge — if gz sim restarts, the bridge may lose
# its gz-transport connection and stop forwarding. Killing it triggers
# respawn (respawn=True in sim.launch.py), reconnecting to the new gz sim.
SIM_WATCH_TOPICS = [
    ("/scan",       LaserScan, 10.0, "ros_gz_bridge"),
    ("/odom/laser", Odometry,   5.0, "rf2o_laser_odometry_node"),
    ("/odom",       Odometry,  15.0, "ekf_filter_node"),
]


class TopicWatchdog(Node):
    def __init__(self):
        super().__init__("topic_watchdog")

        self.declare_parameter("startup_grace_sec", 30.0)
        self.declare_parameter("cooldown_sec", 30.0)
        self.declare_parameter("check_period_sec", 2.0)
        self.declare_parameter("enabled", True)
        self.declare_parameter("mode", "real")  # "real" or "sim"

        self.enabled = bool(self.get_parameter("enabled").value)
        if not self.enabled:
            self.get_logger().info("[Watchdog] disabled (enabled=False)")
            return

        mode = str(self.get_parameter("mode").value)
        self._topics = SIM_WATCH_TOPICS if mode == "sim" else REAL_WATCH_TOPICS

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

        for topic, msg_type, _timeout, _pattern in self._topics:
            qos = qos_be if msg_type in (LaserScan, Imu) else qos_rel
            self.last_msg_time[topic] = self.start_time
            self.subs.append(self.create_subscription(
                msg_type, topic,
                lambda _msg, t=topic: self._on_msg(t),
                qos,
            ))

        self.timer = self.create_timer(self.check_period, self._check)
        self.get_logger().info(
            f"[Watchdog] mode={mode}, monitoring {len(self._topics)} topics "
            f"(grace={self.startup_grace:.0f}s, cooldown={self.cooldown:.0f}s)"
        )

        # SIM mode: monitor /clock for backward time jumps (world switch).
        # When sim time jumps backward by >1s, immediately kill all watched
        # processes instead of waiting for stale timeouts.
        self._last_sim_sec = 0.0
        if mode == "sim":
            self.create_subscription(
                Clock, "/clock",
                self._on_clock,
                QoSProfile(
                    reliability=ReliabilityPolicy.BEST_EFFORT,
                    history=HistoryPolicy.KEEP_LAST, depth=1,
                ),
            )

    # ── Clock jump detection (sim only) ──

    def _on_clock(self, msg: Clock):
        sec = msg.clock.sec + msg.clock.nanosec * 1e-9
        prev = self._last_sim_sec
        self._last_sim_sec = sec

        # Ignore initial messages (prev == 0) and normal forward ticks
        if prev < 1.0 or sec >= prev:
            return

        # Backward jump detected (e.g. 120s → 0.5s) → world switch
        now = time.monotonic()

        # Respect startup grace
        if now - self.start_time < self.startup_grace:
            return

        self.get_logger().warn(
            f"[Watchdog] /clock jumped backward ({prev:.1f}s → {sec:.1f}s) "
            "— world switch detected, killing watched processes"
        )

        killed = set()
        for _topic, _msg_type, _timeout, pattern in self._topics:
            if pattern in killed:
                continue
            last_kill = self.last_kill_time.get(pattern, 0.0)
            if now - last_kill < self.cooldown:
                continue
            if self._kill(pattern):
                self.last_kill_time[pattern] = now
                killed.add(pattern)
        # Reset all topic timestamps so stale check doesn't re-trigger
        for t, _m, _to, _p in self._topics:
            self.last_msg_time[t] = now

    def _on_msg(self, topic: str):
        self.last_msg_time[topic] = time.monotonic()

    def _check(self):
        now = time.monotonic()

        if now - self.start_time < self.startup_grace:
            return

        for topic, _msg_type, timeout, pattern in self._topics:
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
                for t, _m, _to, p in self._topics:
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
