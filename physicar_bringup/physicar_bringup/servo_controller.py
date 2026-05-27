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
from typing import Optional, Tuple
import math

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

    # ESC speed-model constants (power-law fit from speed_map data)
    # Model: offset_ns = G_dir * (a * |speed|^k + d)
    #        duty_ns   = CENTER ± offset_ns
    # Per-type constants (type determined by reverse_direction flag):
    #   Type1 (reverse_direction=False): a1, k1, d1, p1
    #   Type2 (reverse_direction=True):  a2, k2, d2, p2
    # G (esc_gain) is a per-car calibration scalar (default 1.0).
    ESC_A1 = 37937;  ESC_K1 = 1.150;  ESC_D1 = 73177;  ESC_P1 = 0.810
    ESC_A2 = 37773;  ESC_K2 = 1.186;  ESC_D2 = 93981;  ESC_P2 = 0.913
    ESC_REF_VOLTAGE = 7.4   # reference voltage for calibration data
    ESC_CENTER_NS = 1_500_000
    SERVO_CENTER = 90.0

    # Steering sine-model ratio (k = Rs/Rk, servo horn / knuckle arm)
    DEFAULT_STEERING_RATIO = 0.96

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

        # Steering sine-model ratio
        self.steering_ratio = self.DEFAULT_STEERING_RATIO

        # ESC per-car calibration gain (default 1.0)
        self.esc_gain = 1.0

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

    def set_steering_wheel_angle(self, wheel_angle_deg: float) -> bool:
        """Set steering from wheel angle (degrees) via Ackermann sine model.

        Kinematics: Rs * sin(θs) = Rk * sin(θw)
        Hence: θs = arcsin(sin(θw) / k), where k = steering_ratio

        Args:
            wheel_angle_deg: wheel angle in degrees (positive=left)

        Returns:
            True on success
        """
        if abs(wheel_angle_deg) < 0.001:
            return self._apply_angle(self.CHANNEL_STEERING, self.SERVO_CENTER)

        wheel_rad = math.radians(wheel_angle_deg)
        sin_servo = math.sin(wheel_rad) / self.steering_ratio
        sin_servo = max(-1.0, min(1.0, sin_servo))
        servo_offset = math.degrees(math.asin(sin_servo))

        angle = self.SERVO_CENTER + servo_offset
        return self._apply_angle(self.CHANNEL_STEERING, angle)

    def set_throttle(self, normalized: float) -> bool:
        """Set throttle position (-1.0=reverse, 0=neutral, 1.0=forward)."""
        limits = self.limits[self.CHANNEL_THROTTLE]
        angle = limits.from_normalized(normalized)
        return self._apply_angle(self.CHANNEL_THROTTLE, angle)

    def set_throttle_speed(self, speed_mps: float, voltage: float = 7.4) -> bool:
        """Convert speed (m/s) to ESC duty cycle and apply directly.

        Uses power-law model with per-type constants:
            offset_ns = G_dir * (a * |speed|^k + d) * (V_ref / voltage)
            duty_ns   = CENTER ± offset_ns

        Type is selected by reverse_direction (inverted flag).
        G (esc_gain) is a per-car calibration scalar.
        p is the forward/reverse asymmetry ratio for each type.
        Voltage compensation: higher voltage → less offset needed.

        Args:
            speed_mps: target speed (positive=forward, negative=reverse)
            voltage: current battery voltage (V)

        Returns:
            True on success
        """
        if abs(speed_mps) < 0.001:
            if self.drive_board:
                return self.drive_board.set_duty_ns(
                    self.CHANNEL_THROTTLE, self.ESC_CENTER_NS)
            return self._apply_angle(self.CHANNEL_THROTTLE, self.SERVO_CENTER)

        reverse_direction = self.inverted.get(self.CHANNEL_THROTTLE, False)
        abs_speed = abs(speed_mps)

        # Select per-type constants
        if reverse_direction:
            a, k, d, p = self.ESC_A2, self.ESC_K2, self.ESC_D2, self.ESC_P2
        else:
            a, k, d, p = self.ESC_A1, self.ESC_K1, self.ESC_D1, self.ESC_P1

        # Direction-dependent gain: p applied to the weaker direction
        #   rev=F: G_fwd=G, G_bwd=G*p   |  rev=T: G_fwd=G*p, G_bwd=G
        if reverse_direction:
            g_dir = self.esc_gain * p if speed_mps > 0 else self.esc_gain
        else:
            g_dir = self.esc_gain if speed_mps > 0 else self.esc_gain * p

        # Voltage compensation: calibrated at V_ref, scale inversely
        v_comp = self.ESC_REF_VOLTAGE / max(voltage, 5.0)

        offset = g_dir * (a * abs_speed ** k + d) * v_comp

        # Forward = lower duty (non-inverted) or higher duty (inverted)
        sign = -1 if speed_mps > 0 else 1
        if reverse_direction:
            sign = -sign
        duty_ns = int(self.ESC_CENTER_NS + sign * offset)

        if self.drive_board:
            return self.drive_board.set_duty_ns(self.CHANNEL_THROTTLE, duty_ns)
        # Fallback: convert duty_ns to angle
        angle = (duty_ns - 500_000) / 2_000_000 * 180.0
        return self.board.set_servo(self.CHANNEL_THROTTLE, angle)

    def speed_to_duty_offset_ns(self, speed_mps: float) -> float:
        """Convert absolute speed to ESC duty offset in nanoseconds (for logging/debug).

        Args:
            speed_mps: absolute speed (m/s, must be > 0)

        Returns:
            ESC duty offset from center (ns)
        """
        reverse_direction = self.inverted.get(self.CHANNEL_THROTTLE, False)
        if reverse_direction:
            a, k, d = self.ESC_A2, self.ESC_K2, self.ESC_D2
        else:
            a, k, d = self.ESC_A1, self.ESC_K1, self.ESC_D1
        return self.esc_gain * (a * speed_mps ** k + d)

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
