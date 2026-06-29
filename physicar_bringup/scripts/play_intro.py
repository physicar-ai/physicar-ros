#!/usr/bin/env python3
#
# Copyright 2026 AICASTLE Inc.
# Licensed under the Apache License, Version 2.0 (the "License").
#
"""Publish the intro sound to /audio once, so audio_node plays it.

Usage: play_intro.py <path-to-audio-file>

Uses TRANSIENT_LOCAL durability so a late-matching audio_node subscriber still
receives the clip, and waits for an actual subscriber before publishing.
"""
import sys
import time

import rclpy
from rclpy.duration import Duration
from rclpy.qos import (QoSProfile, ReliabilityPolicy, DurabilityPolicy,
                       HistoryPolicy)
from physicar_interfaces.msg import Audio


def main():
    if len(sys.argv) < 2:
        print("usage: play_intro.py <audio-file>")
        return
    path = sys.argv[1]

    rclpy.init()
    node = rclpy.create_node("intro")
    qos = QoSProfile(
        reliability=ReliabilityPolicy.RELIABLE,
        durability=DurabilityPolicy.TRANSIENT_LOCAL,
        history=HistoryPolicy.KEEP_LAST,
        depth=1,
    )
    pub = node.create_publisher(Audio, "/audio", qos)

    # Wait for audio_node to subscribe (up to 30s) instead of a fixed delay.
    deadline = time.time() + 30.0
    while pub.get_subscription_count() == 0 and time.time() < deadline:
        rclpy.spin_once(node, timeout_sec=0.1)

    msg = Audio()
    msg.channel = "intro"
    with open(path, "rb") as f:
        msg.data = list(f.read())
    msg.format = "mp3"
    msg.volume = 1.0
    pub.publish(msg)

    try:
        pub.wait_for_all_acked(Duration(seconds=5))
    except Exception:
        pass

    end = time.time() + 1.0
    while time.time() < end:
        rclpy.spin_once(node, timeout_sec=0.1)

    node.destroy_node()
    rclpy.shutdown()


if __name__ == "__main__":
    main()
