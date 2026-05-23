#!/usr/bin/env python3
#
# Copyright 2026 AICASTLE Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Servo Controller for PhysiCar
Maps control commands to physical servo channels

Servo Channel Mapping:
- S1: Throttle/ESC (0-180°, neutral=90°)
- S2: Steering     (0-180°, center=90°)
- S3: Camera Pan   (0-180°, center=90°)
- S4: Camera Tilt  (0-180°, center=90°)
"""

from dataclasses import dataclass
from typing import Optional

from .yahboom_board import YahboomBoard
from .gpio_pwm_board import GpioPwmBoard


@dataclass
class ServoLimits:
    """Servo angle limits in degrees."""
    min_angle: float
    max_angle: float
    center_angle: float = 90.0

    def clamp(self, angle: float) -> float:
        """Clamp angle to limits."""
        return max(self.min_angle, min(self.max_angle, angle))

    def from_normalized(self, value: float) -> float:
        """
        Convert normalized value (-1 to 1) to angle.

        Args:
            value: Normalized value (-1 = min, 0 = center, 1 = max)

        Returns:
            Servo angle in degrees
        """
        if value >= 0:
            return self.center_angle + value * (self.max_angle - self.center_angle)
        else:
            return self.center_angle + value * (self.center_angle - self.min_angle)


class ServoController:
    """High-level servo controller for PhysiCar."""

    # Servo channel definitions
    CHANNEL_THROTTLE = 1   # S1: ESC/Throttle
    CHANNEL_STEERING = 2   # S2: Steering servo
    CHANNEL_PAN = 3        # S3: Camera Pan
    CHANNEL_TILT = 4       # S4: Camera Tilt

    def __init__(self, board: Optional[YahboomBoard] = None,
                 drive_board: Optional[GpioPwmBoard] = None):
        """
        Initialize servo controller.

        Args:
            board: YahboomBoard instance. If None, creates a new one.
            drive_board: Optional GpioPwmBoard for ch1 (throttle) and ch2
                (steering).  When provided, drive channels use hardware
                GPIO PWM while camera channels still go through *board*.
        """
        self.board = board or YahboomBoard()
        self.drive_board = drive_board

        # Servo limits - initialised to a safe minimal range.
        # Actual values are set by physicar_driver_node._apply_calibration().
        # Priority: 1) /home/physicar/physicar_ws/userdata/calibration.json  2) driver_params.yaml
        self.limits = {
            self.CHANNEL_THROTTLE: ServoLimits(90, 90, 90),  # S1: initial stopped state
            self.CHANNEL_STEERING: ServoLimits(90, 90, 90),  # S2: initial centre
            self.CHANNEL_PAN: ServoLimits(90, 90, 90),       # S3: initial centre
            self.CHANNEL_TILT: ServoLimits(90, 90, 90),      # S4: initial centre
        }

        # Servo trim values (calibration offsets)
        self.trim = {
            self.CHANNEL_THROTTLE: 0,
            self.CHANNEL_STEERING: 0,
            self.CHANNEL_PAN: 0,
            self.CHANNEL_TILT: 0,
        }

        # Invert flags (for servos mounted in opposite direction)
        self.inverted = {
            self.CHANNEL_THROTTLE: False,
            self.CHANNEL_STEERING: False,
            self.CHANNEL_PAN: False,
            self.CHANNEL_TILT: False,
        }

    def connect(self) -> bool:
        """Connect to the Yahboom board."""
        return self.board.connect()

    def disconnect(self):
        """Disconnect from the board."""
        self.board.disconnect()

    def set_trim(self, channel: int, trim_degrees: float):
        """Set trim offset for a servo channel."""
        if channel in self.trim:
            self.trim[channel] = trim_degrees

    def set_inverted(self, channel: int, inverted: bool):
        """Set inversion flag for a servo channel."""
        if channel in self.inverted:
            self.inverted[channel] = inverted

    def set_limits(self, channel: int, min_angle: float, max_angle: float, center_angle: float = 90.0):
        """Set angle limits for a servo channel."""
        if channel in self.limits:
            self.limits[channel] = ServoLimits(min_angle, max_angle, center_angle)

    def _apply_angle(self, channel: int, angle: float) -> bool:
        """
        Apply angle to servo with inversion, limits, and trim.
        
        Order: inversion -> clamp to limits -> apply trim
        
        Drive channels (1, 2) are routed to drive_board (GpioPwmBoard)
        when available; camera channels (3, 4) always go to board (YahboomBoard).
        """
        if self.inverted.get(channel, False):
            angle = 180 - angle

        # Clamp to limits first
        limits = self.limits.get(channel)
        if limits:
            angle = limits.clamp(angle)

        # Apply trim after clamp (shifts entire range, preserving limit span)
        angle += self.trim.get(channel, 0)

        # Route drive channels to GPIO PWM board when available
        if self.drive_board and channel in (self.CHANNEL_THROTTLE, self.CHANNEL_STEERING):
            return self.drive_board.set_servo(channel, angle)

        return self.board.set_servo(channel, angle)

    def set_pan(self, angle: float) -> bool:
        """Set camera pan angle (0-180°, 90=center)."""
        return self._apply_angle(self.CHANNEL_PAN, angle)

    def set_tilt(self, angle: float) -> bool:
        """Set camera tilt angle (0-180°, 90=center)."""
        return self._apply_angle(self.CHANNEL_TILT, angle)

    def set_steering(self, normalized: float) -> bool:
        """Set steering position (-1.0=left, 0=center, 1.0=right)."""
        limits = self.limits[self.CHANNEL_STEERING]
        angle = limits.from_normalized(normalized)
        return self._apply_angle(self.CHANNEL_STEERING, angle)

    def set_throttle(self, normalized: float) -> bool:
        """Set throttle position (-1.0=reverse, 0=neutral, 1.0=forward)."""
        limits = self.limits[self.CHANNEL_THROTTLE]
        angle = limits.from_normalized(normalized)
        return self._apply_angle(self.CHANNEL_THROTTLE, angle)

    def set_pan_tilt_normalized(self, pan: float, tilt: float) -> bool:
        """Set camera pan-tilt with normalized values."""
        pan_limits = self.limits[self.CHANNEL_PAN]
        tilt_limits = self.limits[self.CHANNEL_TILT]

        pan_angle = pan_limits.from_normalized(pan)
        tilt_angle = tilt_limits.from_normalized(tilt)

        result_pan = self._apply_angle(self.CHANNEL_PAN, pan_angle)
        result_tilt = self._apply_angle(self.CHANNEL_TILT, tilt_angle)

        return result_pan and result_tilt

    def center_all(self) -> bool:
        """Center all servos to their default positions."""
        results = []
        for channel, limits in self.limits.items():
            results.append(self._apply_angle(channel, limits.center_angle))
        return all(results)

    def emergency_stop(self) -> bool:
        """Emergency stop - center steering and set throttle to neutral."""
        steering_ok = self._apply_angle(
            self.CHANNEL_STEERING,
            self.limits[self.CHANNEL_STEERING].center_angle
        )
        throttle_ok = self._apply_angle(
            self.CHANNEL_THROTTLE,
            self.limits[self.CHANNEL_THROTTLE].center_angle
        )
        return steering_ok and throttle_ok
