"""Robot motion and camera control."""

from typing import Annotated
from pydantic import Field

from physicar_agent import topic

from std_msgs.msg import Float64
import math
import time


def _execute_single(step: dict) -> dict:
    """Execute a single control step (input units: degrees; converted to radians internally)."""
    pan = step.get('pan')
    tilt = step.get('tilt')
    speed = step.get('speed', 0.0)
    steering = step.get('steering', 0.0)
    max_duration = step.get('max_duration', 3.0)
    max_distance = step.get('max_distance', 1.0)

    # Camera control (degrees -> radians).
    if pan is not None:
        topic.pub('/camera/pan', Float64(data=math.radians(float(pan))))
    if tilt is not None:
        topic.pub('/camera/tilt', Float64(data=math.radians(float(tilt))))

    # Validate parameters.
    max_duration = max_duration if max_duration > 0 else 3.0
    max_distance = max_distance if max_distance > 0 else 1.0

    # Initial pose.
    odom = topic.get('/odom', {'pose': {'pose': {'position': {'x': 0, 'y': 0}}}})
    pos = odom.get('pose', {}).get('pose', {}).get('position', {})
    prev_x, prev_y = pos.get('x', 0), pos.get('y', 0)
    start_time = time.time()
    distance = 0.0

    # Send the low-level commands directly.
    # speed: m/s, steering: degrees -> radians.
    topic.pub('/speed', Float64(data=float(speed)))
    topic.pub('/steering', Float64(data=math.radians(float(steering))))

    # Loop until either the duration or distance budget is exhausted.
    while True:
        time.sleep(0.05)
        elapsed = time.time() - start_time

        # Travelled-distance integration.
        odom = topic.get('/odom', {'pose': {'pose': {'position': {'x': 0, 'y': 0}}}})
        pos = odom.get('pose', {}).get('pose', {}).get('position', {})
        curr_x, curr_y = pos.get('x', 0), pos.get('y', 0)
        dx = curr_x - prev_x
        dy = curr_y - prev_y
        distance += math.sqrt(dx**2 + dy**2)
        prev_x, prev_y = curr_x, curr_y

        if elapsed >= max_duration or distance >= max_distance:
            break

    # Stop.
    topic.pub('/speed', Float64(data=0.0))

    return {
        "duration": time.time() - start_time,
        "distance": distance
    }


def tool(
    steps: Annotated[list, Field(description="List of control steps. Each step: {pan?, tilt?, speed?, steering?, max_duration?, max_distance?}")]
) -> dict:
    """Robot motion and camera control.

    Units:
        - pan: degrees (-45..45, positive = left, negative = right)
        - tilt: degrees (-30..30, positive = up, negative = down)
        - steering: degrees (-25..25, positive = turn left, negative = turn right)
        - speed: m/s (-2.0..2.0, positive = forward, negative = reverse)
        - max_duration: seconds (max wall-clock time per step)
        - max_distance: metres (max travel distance per step)

    Examples:
        # Drive forward for 2 seconds.
        control(steps=[{"speed": 0.3, "max_duration": 2}])

        # Drive forward while turning left.
        control(steps=[{"speed": 0.3, "steering": 20, "max_duration": 1}])

        # Nod (up - down - up - centre).
        control(steps=[
            {"tilt": 20, "max_duration": 0.3},
            {"tilt": -20, "max_duration": 0.3},
            {"tilt": 20, "max_duration": 0.3},
            {"tilt": 0, "max_duration": 0.3}
        ])

        # Shake head (left - right - left - centre).
        control(steps=[
            {"pan": 30, "max_duration": 0.3},
            {"pan": -30, "max_duration": 0.3},
            {"pan": 30, "max_duration": 0.3},
            {"pan": 0, "max_duration": 0.3}
        ])

        # Look around.
        control(steps=[
            {"pan": 40, "tilt": 0, "max_duration": 1},
            {"pan": 0, "tilt": 20, "max_duration": 1},
            {"pan": -40, "tilt": 0, "max_duration": 1},
            {"pan": 0, "tilt": -20, "max_duration": 1},
            {"pan": 0, "tilt": 0, "max_duration": 0.5}
        ])

        # Dance (head + body wiggle).
        control(steps=[
            {"pan": 30, "tilt": 15, "speed": 0.2, "steering": 20, "max_duration": 0.5},
            {"pan": -30, "tilt": -15, "speed": 0.2, "steering": -20, "max_duration": 0.5},
            {"pan": 30, "tilt": 15, "speed": -0.2, "steering": 20, "max_duration": 0.5},
            {"pan": -30, "tilt": -15, "speed": -0.2, "steering": -20, "max_duration": 0.5},
            {"pan": 0, "tilt": 0, "speed": 0, "steering": 0, "max_duration": 0.3}
        ])
    """
    results = []
    total_duration = 0.0
    total_distance = 0.0

    for step in steps:
        result = _execute_single(step)
        results.append(result)
        total_duration += result['duration']
        total_distance += result['distance']

    return {
        "results": results,
        "total_duration": total_duration,
        "total_distance": total_distance
    }
