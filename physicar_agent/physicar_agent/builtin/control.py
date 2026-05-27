"""Robot motion and camera control."""

from typing import Annotated, Optional
from pydantic import Field

from physicar_agent import topic, service, action, text, image

from std_msgs.msg import Float64
import math
import time


def tool(
    speed: Annotated[Optional[float], Field(description="Speed in m/s (-2.0..2.0, positive=forward)")] = None,
    steering: Annotated[Optional[float], Field(description="Steering in degrees (-26..26, positive=left)")] = None,
    pan: Annotated[Optional[float], Field(description="Camera pan in degrees (-30..30, positive=left)")] = None,
    tilt: Annotated[Optional[float], Field(description="Camera tilt in degrees (-30..30, positive=up)")] = None,
    duration: Annotated[float, Field(description="Duration in seconds")] = 3.0,
) -> dict:
    """Drive the robot or move the camera.

    Examples:
        tool(speed=0.3, duration=2)              # forward 2s
        tool(speed=0.3, steering=20, duration=1)  # forward + turn left
        tool(pan=30)                               # look left
        tool(tilt=20)                              # look up
        tool(speed=0, steering=0)                  # stop
    """
    # Camera control (degrees -> radians).
    if pan is not None:
        topic.pub('/camera/pan', Float64(data=math.radians(float(pan))))
    if tilt is not None:
        topic.pub('/camera/tilt', Float64(data=math.radians(float(tilt))))

    # If no drive command, wait for duration then return (camera-only).
    if speed is None and steering is None:
        time.sleep(duration if duration > 0 else 0.3)
        return text("done")

    spd = float(speed or 0.0)
    steer = float(steering or 0.0)
    duration = duration if duration > 0 else 3.0

    # Send commands.
    topic.pub('/speed', Float64(data=spd))
    topic.pub('/steering', Float64(data=math.radians(steer)))

    # Wait.
    time.sleep(duration)

    # Stop.
    topic.pub('/speed', Float64(data=0.0))

    return text("done")
