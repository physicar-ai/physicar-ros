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
GPIO PWM Board for PhysiCar
Controls steering servo and ESC via RPi5 RP1 hardware PWM (sysfs interface).

Hardware mapping:
- Channel 1 (Throttle/ESC) → pwmchip0/pwm1 (GPIO13, board pin 33)
- Channel 2 (Steering)     → pwmchip0/pwm0 (GPIO12, board pin 32)

Requires dtoverlay pwm0-gpio13 to enable RP1 PWM0 and mux GPIO12/13.
"""

import os

# sysfs base path for RP1 PWM0
_PWMCHIP = '/sys/class/pwm/pwmchip0'

# Servo PWM parameters
_PERIOD_NS = 20_000_000       # 50 Hz
_MIN_DUTY_NS = 500_000        # 0° → 0.5 ms
_DUTY_RANGE_NS = 2_000_000    # 180° span → 2.0 ms (0.5 ms to 2.5 ms)

# Channel → sysfs PWM index
_CHANNEL_MAP = {
    1: 1,   # Throttle/ESC → pwm1 (GPIO13)
    2: 0,   # Steering      → pwm0 (GPIO12)
}


class GpioPwmBoard:
    """Hardware PWM driver via sysfs for steering and ESC.

    Provides the same ``set_servo(channel, angle)`` interface as
    ``YahboomBoard`` so it can be used as a drop-in replacement for
    drive channels (1 and 2).
    """

    def __init__(self, logger=None):
        self._logger = logger
        self._connected = False

    # ------------------------------------------------------------------
    # sysfs helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _pwm_path(pwm_index: int, attr: str = '') -> str:
        path = os.path.join(_PWMCHIP, f'pwm{pwm_index}')
        if attr:
            path = os.path.join(path, attr)
        return path

    @staticmethod
    def _write(path: str, value: str) -> None:
        with open(path, 'w') as f:
            f.write(value)

    @staticmethod
    def _read(path: str) -> str:
        with open(path, 'r') as f:
            return f.read().strip()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """Export PWM channels, set period and enable."""
        try:
            for ch, pwm_idx in _CHANNEL_MAP.items():
                pwm_dir = self._pwm_path(pwm_idx)
                # Export if not already exported
                if not os.path.isdir(pwm_dir):
                    self._write(os.path.join(_PWMCHIP, 'export'), str(pwm_idx))

                # Set period (must be set before duty_cycle on first use)
                self._write(self._pwm_path(pwm_idx, 'period'), str(_PERIOD_NS))

                # Start at neutral (90°)
                neutral_duty = _MIN_DUTY_NS + _DUTY_RANGE_NS // 2  # 1,500,000 ns
                self._write(self._pwm_path(pwm_idx, 'duty_cycle'), str(neutral_duty))

                # Enable
                self._write(self._pwm_path(pwm_idx, 'enable'), '1')

            self._connected = True
            if self._logger:
                self._logger.info('GpioPwmBoard: connected (pwm0=steering, pwm1=ESC)')
            return True
        except Exception as e:
            if self._logger:
                self._logger.error(f'GpioPwmBoard: connect failed: {e}')
            return False

    def disconnect(self):
        """Set neutral and disable PWM channels."""
        try:
            neutral_duty = _MIN_DUTY_NS + _DUTY_RANGE_NS // 2
            for pwm_idx in _CHANNEL_MAP.values():
                pwm_dir = self._pwm_path(pwm_idx)
                if os.path.isdir(pwm_dir):
                    self._write(self._pwm_path(pwm_idx, 'duty_cycle'), str(neutral_duty))
                    self._write(self._pwm_path(pwm_idx, 'enable'), '0')
        except Exception as e:
            if self._logger:
                self._logger.error(f'GpioPwmBoard: disconnect error: {e}')
        self._connected = False

    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Servo control
    # ------------------------------------------------------------------

    def set_servo(self, channel: int, angle: float) -> bool:
        """Set servo angle (0–180°, float precision).

        Converts angle to duty-cycle nanoseconds:
            duty_ns = 500000 + (angle / 180) × 2000000

        Args:
            channel: 1 (throttle/ESC) or 2 (steering).
            angle: Servo angle in degrees (0.0–180.0). Float values are
                   preserved — no rounding to integer degrees.

        Returns:
            True on success.
        """
        pwm_idx = _CHANNEL_MAP.get(channel)
        if pwm_idx is None:
            return False

        # Clamp to valid range
        angle = max(0.0, min(180.0, angle))

        duty_ns = int(_MIN_DUTY_NS + (angle / 180.0) * _DUTY_RANGE_NS)
        try:
            self._write(self._pwm_path(pwm_idx, 'duty_cycle'), str(duty_ns))
            return True
        except Exception as e:
            if self._logger:
                self._logger.error(f'GpioPwmBoard: set_servo ch{channel} failed: {e}')
            return False
