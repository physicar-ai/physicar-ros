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
Yahboom ROS Robot Expansion Board Driver

This module wraps the official Rosmaster_Lib library for ROS2 integration.
Uses Yahboom's official protocol implementation.

Communication: Serial USB (/dev/ttyUSB0 or /dev/yahboom)
Baud rate: 115200

PWM Servo channels:
- S1: Throttle/ESC (RC car)
- S2: Steering (RC car)
- S3: Camera Pan
- S4: Camera Tilt
"""

import logging
import time
import threading
from typing import Optional, Tuple

try:
    # Use Rosmaster_Lib bundled inside this package
    from physicar_bringup.Rosmaster_Lib import Rosmaster
except ImportError:
    try:
        # Fallback: system-installed version
        from Rosmaster_Lib import Rosmaster
    except ImportError:
        Rosmaster = None


class YahboomBoard:
    """
    Driver class for Yahboom ROS Robot Expansion Board.
    
    Wraps Rosmaster_Lib for seamless ROS2 integration.
    """

    def __init__(
        self,
        port: str = '/dev/yahboom',
        baudrate: int = 115200,
        timeout: float = 0.1,
        logger: Optional[logging.Logger] = None
    ):
        """
        Initialize Yahboom board connection.

        Args:
            port: Serial port path (used by underlying library)
            baudrate: Serial baud rate (default 115200)
            timeout: Read timeout in seconds
            logger: Optional logger instance for ROS integration
        """
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._logger = logger or logging.getLogger(__name__)
        self._connected = False
        self._lock = threading.RLock()

        # Rosmaster instance
        self._bot: Optional[Rosmaster] = None

        # Current servo positions (0-180 degrees)
        self.servo_positions = [90, 90, 90, 90]  # S1, S2, S3, S4

        # IMU data cache
        self.imu_data = {
            'accel': (0.0, 0.0, 0.0),
            'gyro': (0.0, 0.0, 0.0),
            'attitude': (0.0, 0.0, 0.0)  # roll, pitch, yaw
        }

    def connect(self) -> bool:
        """
        Connect to the Yahboom board via Rosmaster_Lib.

        Returns:
            True if connection successful
        """
        if Rosmaster is None:
            raise ImportError(
                "Rosmaster_Lib is not installed. "
                "Please install it from Yahboom's package."
            )

        try:
            with self._lock:
                self._bot = Rosmaster(com=self.port)
                # Start the receive thread for async data
                self._bot.create_receive_threading()
                # Wait for initialization
                time.sleep(0.5)
                self._connected = True
                return True
        except Exception as e:
            self._logger.error(f"Failed to connect to Yahboom board: {e}")
            self._connected = False
            return False

    def disconnect(self):
        """Disconnect from the board."""
        with self._lock:
            self._connected = False
            self._bot = None

    def is_connected(self) -> bool:
        """Check if board is connected."""
        return self._connected and self._bot is not None

    def set_servo(self, channel: int, angle: float) -> bool:
        """
        Set PWM servo position.

        Args:
            channel: Servo channel (1-4)
                1: Throttle/ESC
                2: Steering
                3: Camera Pan
                4: Camera Tilt
            angle: Angle in degrees (0-180)

        Returns:
            True if command sent successfully
        """
        if not self.is_connected():
            return False

        if channel < 1 or channel > 4:
            raise ValueError(f"Invalid servo channel: {channel}. Must be 1-4.")

        # Clamp angle
        angle = max(0, min(180, angle))

        # Store position
        self.servo_positions[channel - 1] = angle
        
        # Debug log
        self._logger.info(f'set_servo: ch={channel}, angle={angle:.1f}°')

        with self._lock:
            try:
                self._bot.set_pwm_servo(channel, int(angle))
                # Flush serial buffer immediately
                if hasattr(self._bot, 'ser') and self._bot.ser:
                    self._bot.ser.flush()
                return True
            except Exception as e:
                self._logger.error(f"Error setting servo {channel}: {e}")
                return False

    def set_servo_pulse(self, channel: int, pulse_us: int) -> bool:
        """
        Set servo position by pulse width.
        Converts pulse width to angle and uses set_servo.

        Args:
            channel: Servo channel (1-4)
            pulse_us: Pulse width in microseconds (500-2500)

        Returns:
            True if command sent successfully
        """
        # Convert pulse to angle: 500us=0deg, 1500us=90deg, 2500us=180deg
        pulse_us = max(500, min(2500, pulse_us))
        angle = (pulse_us - 500) / 2000.0 * 180.0
        return self.set_servo(channel, angle)

    def set_steering(self, value: float) -> bool:
        """
        [DEPRECATED] Use ServoController.set_steering() instead.
        This method has hardcoded range (45°) - use ServoController for calibrated control.
        """
        import warnings
        warnings.warn("YahboomBoard.set_steering() is deprecated. Use ServoController.set_steering()", DeprecationWarning)
        # Convert -1.0~1.0 to servo angle (0-180)
        servo_angle = 90 + (value * 45)  # +/-45 degrees from center
        return self.set_servo(2, servo_angle)

    def set_throttle(self, value: float) -> bool:
        """
        [DEPRECATED] Use direct ESC control via set_servo(1, angle) with voltage-compensated model.
        This method has hardcoded range (45°) - use physicar_driver_node's ESC model instead.
        """
        import warnings
        warnings.warn("YahboomBoard.set_throttle() is deprecated. Use ESC model in physicar_driver_node", DeprecationWarning)
        # Convert -1.0~1.0 to servo angle
        # 90 = neutral, >90 = forward, <90 = reverse
        servo_angle = 90 + (value * 45)
        return self.set_servo(1, servo_angle)

    def set_pan_tilt(self, pan: float, tilt: float) -> bool:
        """
        Set camera pan-tilt position.

        Args:
            pan: Pan angle in degrees (0-180, 90=center)
            tilt: Tilt angle in degrees (0-180, 90=center)

        Returns:
            True if both commands sent successfully
        """
        result_pan = self.set_servo(3, pan)
        result_tilt = self.set_servo(4, tilt)
        return result_pan and result_tilt

    def read_imu(self) -> Optional[dict]:
        """
        Read IMU data from the board.

        Returns:
            Dictionary with accel, gyro, attitude data or None if failed
        """
        if not self.is_connected():
            return None

        with self._lock:
            try:
                # Get accelerometer data (ax, ay, az) in m/s^2
                accel = self._bot.get_accelerometer_data()

                # Get gyroscope data (gx, gy, gz) in rad/s
                gyro = self._bot.get_gyroscope_data()

                # Get attitude (roll, pitch, yaw) in degrees
                attitude = self._bot.get_imu_attitude_data()

                # Get magnetometer data (mx, my, mz) in µT
                mag = self._bot.get_magnetometer_data()

                self.imu_data = {
                    'accel': accel if accel else (0.0, 0.0, 0.0),
                    'gyro': gyro if gyro else (0.0, 0.0, 0.0),
                    'attitude': attitude if attitude else (0.0, 0.0, 0.0),
                    'mag': mag if mag else (0.0, 0.0, 0.0)
                }

                return self.imu_data

            except Exception as e:
                self._logger.error(f"Error reading IMU: {e}")
                return None

    def set_rgb_led(self, r: int, g: int, b: int) -> bool:
        """
        Set RGB LED color.

        Args:
            r, g, b: Color values (0-255)

        Returns:
            True if command sent successfully
        """
        if not self.is_connected():
            return False

        r = max(0, min(255, r))
        g = max(0, min(255, g))
        b = max(0, min(255, b))

        # Combine to single color value
        color = (r << 16) | (g << 8) | b

        with self._lock:
            try:
                # mode=1 is solid color, speed=100%
                self._bot.set_colorful_effect(1, 100, color)
                return True
            except Exception as e:
                self._logger.error(f"Error setting RGB LED: {e}")
                return False

    def beep(self, duration_ms: int = 100) -> bool:
        """
        Activate buzzer.

        Args:
            duration_ms: Beep duration in milliseconds

        Returns:
            True if command sent successfully
        """
        if not self.is_connected():
            return False

        with self._lock:
            try:
                self._bot.set_beep(duration_ms)
                return True
            except Exception as e:
                self._logger.error(f"Error beeping: {e}")
                return False

    def center_all_servos(self) -> bool:
        """Center all servos to 90 degrees."""
        results = []
        for ch in range(1, 5):
            results.append(self.set_servo(ch, 90))
        return all(results)

    def read_battery_voltage(self) -> Optional[float]:
        """
        Read battery voltage from the board.

        Returns:
            Battery voltage in volts, or None if read fails.
            For 2S LiPo: 6.0V (empty) ~ 8.4V (full)
        """
        if not self.is_connected():
            return None

        with self._lock:
            try:
                voltage = self._bot.get_battery_voltage()
                return voltage if voltage else None
            except Exception as e:
                self._logger.error(f"Error reading battery: {e}")
                return None

    # Battery voltage ranges per cell count
    # Auto-detected by voltage: >9V = 3S, <=9V = 2S
    _BATTERY_RANGES = {
        2: {'min': 6.4, 'max': 8.4, 'low': 6.6},   # 2S: 3.2~4.2V/cell
        3: {'min': 9.6, 'max': 12.6, 'low': 9.9},   # 3S: 3.2~4.2V/cell
    }
    _CELL_DETECT_THRESHOLD = 9.0  # >9V = 3S

    def detect_cell_count(self, voltage: float) -> int:
        """Detect battery cell count from voltage.

        Args:
            voltage: Battery voltage in volts.

        Returns:
            2 or 3 (cell count).
        """
        return 3 if voltage > self._CELL_DETECT_THRESHOLD else 2

    def get_battery_percentage(self) -> Optional[int]:
        """
        Get battery percentage based on voltage.
        Auto-detects 2S/3S by voltage level (threshold: 9V).

        Ranges:
        - 2S: 6.4V (0%) ~ 8.2V (100%)
        - 3S: 9.6V (0%) ~ 12.3V (100%)

        Returns:
            Battery percentage (0-100), or None if read fails.
        """
        voltage = self.read_battery_voltage()
        if voltage is None:
            return None

        cells = self.detect_cell_count(voltage)
        r = self._BATTERY_RANGES[cells]

        percentage = (voltage - r['min']) / (r['max'] - r['min']) * 100
        return max(0, min(100, int(percentage)))

    def get_battery_low_threshold(self, voltage: Optional[float] = None) -> float:
        """Get low-battery voltage threshold for the detected cell count.

        Args:
            voltage: Voltage to detect from. If None, reads from hardware.

        Returns:
            Low threshold voltage (2S: 6.6V, 3S: 9.9V).
        """
        if voltage is None:
            voltage = self.read_battery_voltage()
        if voltage is None:
            return self._BATTERY_RANGES[2]['low']  # default 2S
        cells = self.detect_cell_count(voltage)
        return self._BATTERY_RANGES[cells]['low']

    # ========== Ackermann steering specific methods ==========

    def set_akm_steering_angle(self, angle: int) -> bool:
        """
        Set Ackermann steering angle.
        
        Args:
            angle: Steering angle for Ackermann geometry
            
        Returns:
            True if command sent successfully
        """
        if not self.is_connected():
            return False
            
        with self._lock:
            try:
                self._bot.set_akm_steering_angle(angle)
                return True
            except Exception as e:
                self._logger.error(f"Error setting AKM steering: {e}")
                return False

    def get_encoder(self) -> Optional[Tuple[int, int, int, int]]:
        """
        Get motor encoder values.
        
        Returns:
            Tuple of (m1, m2, m3, m4) encoder counts, or None if failed
        """
        if not self.is_connected():
            return None
            
        with self._lock:
            try:
                return self._bot.get_motor_encoder()
            except Exception as e:
                self._logger.error(f"Error reading encoder: {e}")
                return None
