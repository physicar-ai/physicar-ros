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
PhysiCar Base Driver ROS2 Node
Publishes IMU data and subscribes to servo commands

ESC speed model (empirically measured, power law, 6.5V–8.1V):
- degree = (14 / V) * ((speed + 0.05)^1.65 + 4)
- Relative-error RMSE: 2.1%
"""

import json
import math
import os
import time
from dataclasses import dataclass
from typing import Optional, Dict, Any

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

from geometry_msgs.msg import Twist
from nav_msgs.msg import Odometry
from sensor_msgs.msg import Imu, MagneticField, BatteryState, JointState
from std_msgs.msg import Float64, Float64MultiArray

# Custom interfaces
try:
    from physicar_interfaces.srv import SetCalibration, GetCalibration
    from physicar_interfaces.msg import CalibrationStatus
    HAS_CUSTOM_INTERFACES = True
except ImportError:
    HAS_CUSTOM_INTERFACES = False
    # Fallback to Trigger for backward compatibility
    from std_srvs.srv import Trigger

# Generic teleop status (priority lock).  When drive_engaged=true the driver only
# applies /teleop/speed and /teleop/steering; when camera_engaged=true only
# /teleop/camera/{pan,tilt}.  Other publishers (deepracer, REST) keep
# publishing on the public topics; their messages are simply ignored
# while the lock is held and resume the moment the teleop source releases.
#
# The status carries its own `timeout` field; if no status arrives within
# that window the locks auto-expire so a teleop publisher that crashes
# while holding a lock can't gate the driver permanently.
try:
    from physicar_interfaces.msg import TeleopStatus
    HAS_TELEOP_STATUS = True
except ImportError:
    HAS_TELEOP_STATUS = False

from physicar_bringup.yahboom_board import YahboomBoard
from physicar_bringup.gpio_pwm_board import GpioPwmBoard
from physicar_bringup.servo_controller import ServoController


@dataclass
class CalibrationData:
    """Calibration data structure for all servos."""
    # Center offsets (degrees from 90°)
    steering_center: float = 0.0
    pan_center: float = 0.0
    tilt_center: float = 0.0
    
    # ESC direction
    reverse_direction: bool = False  # True: above 90° = forward (reversed ESC)
    
    # Per-car speed gain (scales ESC duty offset; default 1.0)
    speed_gain: float = 1.0
    
    # Metadata
    source: str = 'defaults'  # 'json_file', 'yaml_params', 'defaults'
    is_saved: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        d = {
            'steering_center': self.steering_center,
            'pan_center': self.pan_center,
            'tilt_center': self.tilt_center,
            'reverse_direction': self.reverse_direction,
            'speed_gain': self.speed_gain,
        }
        return d
    
    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict(), indent=2)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any], source: str = 'unknown') -> 'CalibrationData':
        """Create from dictionary with safe type coercion and bounds.

        Out-of-range or non-numeric values fall back to defaults (logged
        upstream). Bounds are conservative: max_* must be > 0, centers
        bounded to ±0.5 (servos saturate well before that).
        """
        def _f(key, default, lo=None, hi=None):
            v = data.get(key, default)
            try:
                v = float(v)
            except (TypeError, ValueError):
                return default
            if lo is not None and v < lo: return default
            if hi is not None and v > hi: return default
            return v

        def _b(key, default):
            v = data.get(key, default)
            return bool(v) if isinstance(v, (bool, int)) else default

        return cls(
            steering_center=_f('steering_center', 0.0, -30.0, 30.0),
            pan_center=_f('pan_center', 0.0, -30.0, 30.0),
            tilt_center=_f('tilt_center', 0.0, -30.0, 30.0),
            reverse_direction=_b('reverse_direction', False),
            speed_gain=_f('speed_gain', _f('esc_gain', 1.0, 0.1, 5.0), 0.1, 5.0),
            source=source,
            is_saved=(source == 'json_file'),
        )


class PhysicarDriverNode(Node):
    # Default calibration file path (can be overridden by parameter)
    DEFAULT_CALIBRATION_FILE = '/home/physicar/physicar_ws/userdata/calibration.json'
    
    # Servo center angle (standard for RC servos)
    SERVO_CENTER = 90.0
    
    # Feedback-control constants
    FEEDBACK_TIMEOUT = 0.5      # speed-measurement timeout (s)
    FEEDBACK_LOOKAHEAD = 0.2    # predict speed this far ahead (seconds)
    FEEDBACK_P_GAIN = 1.5       # proportional gain
    FEEDBACK_MAX_ADJUST = 0.5   # max adjustment ±0.5 m/s
    SPEED_DEADZONE = 0.01       # below this = stopped (m/s)
    BRAKE_SPEED = 0.3            # reverse-kick brake speed & stop threshold (m/s)

    # Hardware limits (not calibration — these are physical maximums)
    MAX_STEERING = 20.0         # ± degrees (wheel angle)
    MAX_SPEED = 3.0             # ± m/s
    MAX_PAN = 30.0              # ± degrees
    MAX_TILT = 30.0             # ± degrees

    def __init__(self):
        super().__init__('physicar_driver')

        # Declare parameters
        self.declare_parameter('serial_port', '/dev/yahboom')
        self.declare_parameter('baudrate', 115200)
        self.declare_parameter('imu_frame_id', 'imu_link')
        self.declare_parameter('publish_rate', 50.0)
        self.declare_parameter('battery_publish_rate', 1.0)
        self.declare_parameter('battery_low_threshold', 6.6)

        # Servo calibration parameters - center offsets (degrees from 90°)
        self.declare_parameter('steering_center', 0.0)
        self.declare_parameter('pan_center', 0.0)
        self.declare_parameter('tilt_center', 0.0)

        # ESC direction
        self.declare_parameter('reverse_direction', False)  # true: above 90° = forward
        self.declare_parameter('speed_gain', 1.0)  # per-car speed calibration gain

        # Robot physical parameters (from URDF/measurements)
        self.declare_parameter('wheel_radius', 0.0375)
        self.declare_parameter('wheelbase', 0.18)
        self.declare_parameter('track_width', 0.16)
        self.declare_parameter('calibration_file',
                               os.path.expanduser('~/physicar_ws/userdata/calibration.json'))

        # Get parameters
        serial_port = self.get_parameter('serial_port').value
        baudrate = self.get_parameter('baudrate').value
        self.imu_frame_id = self.get_parameter('imu_frame_id').value
        publish_rate = self.get_parameter('publish_rate').value

        # Robot physical parameters
        self.wheel_radius = self.get_parameter('wheel_radius').value
        self.wheelbase = self.get_parameter('wheelbase').value
        self.track_width = self.get_parameter('track_width').value
        self.CALIBRATION_FILE = self.get_parameter('calibration_file').value

        # DEV mode (from environment variable, e.g. userdata/.env)
        self._dev_mode = os.environ.get('DEV', '').lower() == 'true'

        # Initialize hardware (pass ROS logger for consistent logging)
        self.board = YahboomBoard(
            port=serial_port, 
            baudrate=baudrate,
            logger=self.get_logger()
        )
        self.gpio_board = GpioPwmBoard(logger=self.get_logger())
        self.servo = ServoController(self.board, drive_board=self.gpio_board)

        # Calibration state management
        self.calibration = CalibrationData()  # Current calibration
        self.calibration_status_pub = None  # Will be initialized after publisher setup
        self._load_and_apply_calibration()

        # Voltage (updated live from battery)
        self.current_voltage = 7.4  # default
        
        # ESC control state
        
        # Feedback-control state
        self._actual_speed = 0.0           # actual speed measured from rf2o (m/s)
        self._actual_speed_time = 0.0      # time of last speed measurement
        self._current_speed_adjust = 0.0   # speed adjustment (m/s)
        self._target_speed = 0.0           # current target speed
        self._prev_actual_speed = 0.0      # previous actual speed for acceleration
        self._prev_odom_time = 0.0         # previous odom timestamp
        self._brake_active = False         # True while actively braking to stop

        # Speed mapping data logger (DEV mode)
        self._speed_log_file = None
        self._speed_log_target_time = 0.0       # when current target was set
        self._speed_log_last_target = 0.0       # last target speed value
        self._speed_log_written = False          # already logged for this target?
        if self._dev_mode:
            self._speed_log_file = self._open_speed_log()
            self.get_logger().info('DEV mode: speed mapping logger enabled')

        # Connect to board
        if not self.board.connect():
            self.get_logger().error(f'Failed to connect to expansion board on {serial_port}')
        else:
            self.get_logger().info(f'Connected to expansion board on {serial_port}')

        # Connect GPIO PWM board for steering/ESC
        if not self.gpio_board.connect():
            self.get_logger().error('Failed to connect GPIO PWM board')
        else:
            self._initialize_esc()

        # QoS
        qos = QoSProfile(depth=10)
        qos_sensor = QoSProfile(depth=10, reliability=ReliabilityPolicy.BEST_EFFORT)

        # Publishers
        self.imu_pub = self.create_publisher(Imu, '/imu', qos_sensor)
        self.mag_pub = self.create_publisher(MagneticField, '/imu/mag', qos_sensor)
        self.battery_pub = self.create_publisher(BatteryState, '/battery_state', qos)
        self.joint_state_pub = self.create_publisher(JointState, '/joint_states', qos)

        # Joint state tracking
        self.current_steering = 0.0
        self.current_throttle = 0.0
        self.current_pan = 0.0
        self.current_tilt = 0.0
        self.current_pan_normalized = 0.0
        self.current_tilt_normalized = 0.0

        # Subscribers - Low-level control topics (direct control)
        self.create_subscription(Float64, '/speed', self._control_speed_callback, qos)
        self.create_subscription(Float64, '/steering', self._control_steering_callback, qos)

        # Joy teleop priority mirror.  Same payload as /speed,/steering but
        # only consumed while drive_engaged is held.  See HAS_TELEOP_STATUS.
        self.create_subscription(Float64, '/teleop/speed', self._teleop_speed_callback, qos)
        self.create_subscription(Float64, '/teleop/steering', self._teleop_steering_callback, qos)

        # Subscribers - High-level (Ackermann conversion required)
        self.create_subscription(Twist, '/cmd_vel', self.cmd_vel_callback, qos)
        self.create_subscription(Float64, '/camera/pan', self.pan_callback, qos)
        self.create_subscription(Float64, '/camera/tilt', self.tilt_callback, qos)
        self.create_subscription(Float64, '/teleop/camera/pan', self._teleop_pan_callback, qos)
        self.create_subscription(Float64, '/teleop/camera/tilt', self._teleop_tilt_callback, qos)
        self.create_subscription(Float64MultiArray, '/servo/commands', self.servo_callback, qos)

        # Teleop status — freshness-gated, decides which input source to honour.
        # Locks are tracked along with the timestamp of the last status; once
        # `now - last_status_time > timeout` (carried by the message itself)
        # the locks are treated as released regardless of the cached values.
        # Per-source: many sources (joy, web, …) can publish on /teleop/status
        # concurrently.  Effective drive/camera_engaged is OR over all *fresh*
        # sources so e.g. a web client claiming drive_engaged=true isn't
        # cancelled by joy_teleop continuously publishing drive_engaged=false.
        self._teleop_sources: dict = {}  # source -> dict(drive, camera, estop, timeout_sec, last_time_ns)
        self._teleop_status_last_time = None  # most-recent message time (any source)
        self._teleop_status_timeout_sec = 0.5  # default until first status
        # Cached aggregated state (for change-detection logging only).
        self._agg_drive_engaged = False
        self._agg_camera_engaged = False
        if HAS_TELEOP_STATUS:
            self.create_subscription(
                TeleopStatus,
                '/teleop/status',
                self._on_teleop_status,
                qos,
            )
        
        # Subscribe to rf2o odometry (for feedback control)
        # EKF publishes RELIABLE, so must match (BEST_EFFORT cannot receive RELIABLE)
        self.create_subscription(Odometry, '/odom', self.odom_callback, qos)

        # Services - use custom interfaces if available, otherwise fallback
        if HAS_CUSTOM_INTERFACES:
            self.create_service(SetCalibration, '~/set_calibration', self.set_calibration_callback)
            self.create_service(GetCalibration, '~/get_calibration', self.get_calibration_callback)
            
            # Calibration status publisher
            self.calibration_status_pub = self.create_publisher(
                CalibrationStatus, '~/calibration_status', qos)
            # Publish initial calibration status
            self._publish_calibration_status()
            
            self.get_logger().info('Calibration services ready')
        else:
            self.get_logger().warn('physicar_interfaces not found, using Trigger service fallback')
            from std_srvs.srv import Trigger
            self.create_service(Trigger, '~/reload_calibration', self.reload_calibration_callback_legacy)

        # Timers
        self.create_timer(1.0 / publish_rate, self.publish_imu)
        self.create_timer(1.0 / publish_rate, self.publish_joint_states)
        battery_publish_rate = self.get_parameter('battery_publish_rate').value
        self.battery_low_threshold = self.get_parameter('battery_low_threshold').value
        self.battery_low_warned = False
        self._battery_cells_detected = False
        self.create_timer(1.0 / battery_publish_rate, self.publish_battery)

        self.get_logger().info('PhysiCar Driver Node started')

    def odom_callback(self, msg: Odometry):
        """Predictive speed feedback: adjust ESC degree based on predicted speed error."""
        now = time.time()
        actual = msg.twist.twist.linear.x
        dt = now - self._prev_odom_time if self._prev_odom_time > 0 else 0.0

        # Compute acceleration
        if dt > 0.001:
            acceleration = (actual - self._prev_actual_speed) / dt
        else:
            acceleration = 0.0

        self._prev_actual_speed = actual
        self._prev_odom_time = now
        self._actual_speed = actual
        self._actual_speed_time = now

        # Braking: target=0 and actively braking → apply reverse force
        if abs(self._target_speed) < self.SPEED_DEADZONE:
            if not self._brake_active:
                return
            if abs(actual) <= self.BRAKE_SPEED:
                self.servo.set_throttle_speed(0.0)
                self._current_speed_adjust = 0.0
                self._brake_active = False
            else:
                brake_speed = -math.copysign(self.BRAKE_SPEED, actual)
                self.servo.set_throttle_speed(brake_speed, self.current_voltage)
            return

        # predicted = actual + acceleration * lookahead
        predicted = actual + acceleration * self.FEEDBACK_LOOKAHEAD

        # Error = target - predicted (m/s)
        error = self._target_speed - predicted

        # P controller, clamped to ±MAX_ADJUST
        adjust = error * self.FEEDBACK_P_GAIN
        adjust = max(-self.FEEDBACK_MAX_ADJUST,
                    min(self.FEEDBACK_MAX_ADJUST, adjust))
        self._current_speed_adjust = adjust

        # Apply updated ESC speed
        adjusted_speed = self._get_adjusted_speed()
        self.servo.set_throttle_speed(adjusted_speed, self.current_voltage)

        # Speed mapping data logger (DEV mode)
        self._log_speed_mapping(now, actual)

    def _open_speed_log(self):
        """Open (or append to) boot-scoped CSV log file for speed mapping."""
        log_dir = os.path.join(os.path.dirname(self.CALIBRATION_FILE), 'speed_log')
        os.makedirs(log_dir, exist_ok=True)
        try:
            boot_id = open('/proc/sys/kernel/random/boot_id').read().strip()
        except Exception:
            boot_id = f'{int(time.time())}'
        filepath = os.path.join(log_dir, f'speed_map_{boot_id}.csv')
        is_new = not os.path.exists(filepath)
        f = open(filepath, 'a')
        if is_new:
            f.write('timestamp,target_speed,duty_cycle_ns,actual_speed,voltage\n')
            f.flush()
        self.get_logger().info(f'Speed log: {filepath}')
        return f

    def _log_speed_mapping(self, now: float, actual_speed: float):
        """Log duty cycle vs actual speed when target has been stable for 1+ second."""
        if not self._speed_log_file:
            return
        if self._speed_log_written:
            return
        if abs(self._target_speed) < self.SPEED_DEADZONE:
            return
        if self._speed_log_target_time <= 0:
            return
        if now - self._speed_log_target_time < 1.0:
            return
        try:
            duty_ns = int(open('/sys/class/pwm/pwmchip0/pwm1/duty_cycle').read().strip())
            self._speed_log_file.write(
                f'{now:.3f},{self._target_speed:.4f},{duty_ns},{actual_speed:.4f},{self.current_voltage:.2f}\n'
            )
            self._speed_log_file.flush()
            self._speed_log_written = True
            self.get_logger().debug(
                f'Speed log: target={self._target_speed:.3f} duty={duty_ns} actual={actual_speed:.3f}'
            )
        except Exception as e:
            self.get_logger().warning(f'Speed log write failed: {e}')

    def _get_adjusted_speed(self) -> float:
        """Get target speed with feedback adjustment applied.

        Returns:
            Adjusted speed (m/s, signed: positive=forward, negative=reverse)
        """
        if self._target_speed == 0:
            return 0.0

        # Discard stale feedback correction if odom is dead
        if time.time() - self._actual_speed_time > self.FEEDBACK_TIMEOUT:
            self._current_speed_adjust = 0.0

        # Apply absolute adjustment (m/s)
        adjusted = self._target_speed + self._current_speed_adjust
        if self._target_speed > 0:
            return max(self.SPEED_DEADZONE, adjusted)
        else:
            return min(-self.SPEED_DEADZONE, adjusted)

    # ========== Low-level Control Methods ==========
    
    def _control_speed_callback(self, msg: Float64):
        """Handle /speed topic (m/s).  Ignored while joy drive_engaged is held."""
        if self._drive_engaged:
            return
        self._set_speed(msg.data)
    
    def _control_steering_callback(self, msg: Float64):
        """Handle /steering topic (radians).  Ignored while joy drive_engaged is held."""
        if self._drive_engaged:
            return
        self._set_steering_rad(msg.data)

    # ── Joy teleop priority mirrors (always honoured while drive_engaged is held;
    # also honoured when no lock is held so manual joy nudges work even off-lock)

    def _teleop_speed_callback(self, msg: Float64):
        """Handle /teleop/speed (joy_teleop only).  Applied unconditionally
        \u2014 the joy_teleop node only emits these while LB is held or for the
        single zero-frame on release."""
        self._set_speed(msg.data)

    def _teleop_steering_callback(self, msg: Float64):
        """Handle /teleop/steering (joy_teleop only)."""
        self._set_steering_rad(msg.data)

    # ── Teleop status (freshness-gated) ──

    def _on_teleop_status(self, msg) -> None:
        new_source = str(msg.source) if msg.source else 'unknown'

        # Per-message timeout (publisher-declared freshness window).
        timeout_sec = float(msg.timeout.sec) + float(msg.timeout.nanosec) / 1e9
        if timeout_sec <= 0.0:
            timeout_sec = 0.5
        # Track the longest declared timeout for the conservative
        # `_teleop_status_fresh()` legacy helper.
        self._teleop_status_timeout_sec = max(
            self._teleop_status_timeout_sec, timeout_sec
        )
        self._teleop_status_last_time = self.get_clock().now()

        self._teleop_sources[new_source] = {
            'drive': bool(msg.drive_engaged),
            'camera': bool(msg.camera_engaged),
            'estop': bool(msg.estop_latched),
            'timeout_sec': timeout_sec,
            'last_time_ns': self._teleop_status_last_time.nanoseconds,
        }

        # Aggregate: drive/camera_engaged = OR over *fresh* sources.
        agg_drive, agg_camera, primary_source = self._aggregate_teleop()
        if agg_drive != self._agg_drive_engaged:
            self.get_logger().info(
                f"Teleop drive {'engaged' if agg_drive else 'released'} "
                f"(source={primary_source or new_source})"
            )
            self._agg_drive_engaged = agg_drive
        if agg_camera != self._agg_camera_engaged:
            self.get_logger().info(
                f"Teleop camera {'engaged' if agg_camera else 'released'} "
                f"(source={primary_source or new_source})"
            )
            self._agg_camera_engaged = agg_camera

    def _aggregate_teleop(self):
        """Return (drive, camera, primary_source).

        primary_source is the most-recent fresh source claiming drive,
        for logging only.  Stale sources (older than their per-message
        timeout) are skipped.
        """
        now_ns = self.get_clock().now().nanoseconds
        drive = False
        camera = False
        primary = None
        primary_ts = -1
        for src, st in self._teleop_sources.items():
            if (now_ns - st['last_time_ns']) > int(st['timeout_sec'] * 1e9):
                continue
            if st['drive']:
                drive = True
                if st['last_time_ns'] > primary_ts:
                    primary = src
                    primary_ts = st['last_time_ns']
            if st['camera']:
                camera = True
        return drive, camera, primary

    def _teleop_status_fresh(self) -> bool:
        """True if at least one source has published within its timeout."""
        if self._teleop_status_last_time is None:
            return False
        now_ns = self.get_clock().now().nanoseconds
        for st in self._teleop_sources.values():
            if (now_ns - st['last_time_ns']) <= int(st['timeout_sec'] * 1e9):
                return True
        return False

    @property
    def _drive_engaged(self) -> bool:
        drive, _, _ = self._aggregate_teleop()
        return drive

    @property
    def _camera_engaged(self) -> bool:
        _, camera, _ = self._aggregate_teleop()
        return camera

    
    def _set_speed(self, speed_mps: float):
        """Set target speed in m/s. Core low-level speed control.
        
        Args:
            speed_mps: target speed (m/s, positive=forward, negative=reverse)
        """
        try:
            # Apply speed limit
            max_speed = self.MAX_SPEED
            speed_mps = max(-max_speed, min(max_speed, speed_mps))
            
            self._target_speed = speed_mps

            # Track target changes for speed logger (DEV mode)
            if self._dev_mode and abs(speed_mps - self._speed_log_last_target) > 0.001:
                self._speed_log_target_time = time.time()
                self._speed_log_last_target = speed_mps
                self._speed_log_written = False
            
            # Update joint state
            self.current_throttle = speed_mps
            
            # ESC command
            if abs(speed_mps) < self.SPEED_DEADZONE:
                # If odom is stale or already slow → neutral immediately
                # Otherwise odom_callback feedback loop handles braking
                stale = (time.time() - self._actual_speed_time) > self.FEEDBACK_TIMEOUT
                if stale or abs(self._actual_speed) <= self.BRAKE_SPEED:
                    self.servo.set_throttle_speed(0.0)
                    self._current_speed_adjust = 0.0
                    self._brake_active = False
                else:
                    self._brake_active = True
            else:
                self._brake_active = False
                adjusted = self._get_adjusted_speed()
                self.servo.set_throttle_speed(adjusted, self.current_voltage)
                self.get_logger().debug(
                    f'ESC: speed={speed_mps:.2f} m/s, V={self.current_voltage:.1f}V'
                )
        except Exception as e:
            self.get_logger().error(f'_set_speed error: {e}')
    
    def _set_steering_rad(self, steering_rad: float):
        """Set steering angle in radians. Core low-level steering control.
        
        Args:
            steering_rad: wheel steering angle (radians, positive=left turn)
        """
        try:
            steering_deg = math.degrees(steering_rad)
            
            # Angle limit (degrees)
            max_steering = self.MAX_STEERING
            steering_deg = max(-max_steering, min(max_steering, steering_deg))
            
            # Update joint state (radians)
            self.current_steering = math.radians(steering_deg)
            
            # Apply steering
            self.servo.set_steering_wheel_angle(steering_deg)
            self.get_logger().debug(
                f'Steering: {steering_rad:.3f} rad ({steering_deg:.1f}°)'
            )
        except Exception as e:
            self.get_logger().error(f'_set_steering_rad error: {e}')
    
    def _set_steering(self, steering_deg: float):
        """Set steering angle in degrees (convenience wrapper).
        
        Args:
            steering_deg: wheel steering angle (degrees, positive=left turn)
        """
        self._set_steering_rad(math.radians(steering_deg))

    # ========== High-level Command Callbacks ==========

    def cmd_vel_callback(self, msg: Twist):
        """Handle Twist velocity commands with Ackermann steering conversion.
        
        Ackermann kinematics:
        - speed = linear.x (m/s)
        - steering_angle = atan(angular.z * wheelbase / linear.x)
        
        Stopped or in-place rotation (linear.x ≈ 0):
        - steering = sign(angular.z) * max_steering
        """
        if self._drive_engaged:
            return
        try:
            linear_x = msg.linear.x
            angular_z = msg.angular.z
            
            # Ackermann conversion: angular.z → steering_deg
            if abs(linear_x) > 0.01:
                # Normal driving: R = v/ω, steering = atan(L/R) = atan(ω*L/v)
                steering_rad = math.atan(angular_z * self.wheelbase / linear_x)
                steering_deg = math.degrees(steering_rad)
            else:
                # Stopped or in-place rotation
                if abs(angular_z) > 0.01:
                    # Attempt in-place rotation → max steering
                    steering_deg = math.copysign(self.MAX_STEERING, angular_z)
                else:
                    steering_deg = 0.0
            
            self.get_logger().debug(
                f'cmd_vel: v={linear_x:.2f} m/s, ω={angular_z:.3f} rad/s → steering={steering_deg:.1f}°'
            )
            
            # Call low-level control method
            self._set_speed(linear_x)
            self._set_steering(steering_deg)
            
        except Exception as e:
            self.get_logger().error(f'cmd_vel_callback error: {e}')

    def _load_and_apply_calibration(self) -> None:
        """Load calibration and apply to servo controller.
        
        Priority: 1) JSON file  2) YAML params  3) defaults
        """
        # Try loading from JSON file first
        if os.path.exists(self.CALIBRATION_FILE):
            try:
                with open(self.CALIBRATION_FILE, 'r') as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError('calibration file is not a JSON object')
                self.calibration = CalibrationData.from_dict(data, source='json_file')
                self.get_logger().info(f'Loaded calibration from {self.CALIBRATION_FILE}')
            except Exception as e:
                # Bad file is left alone — user can fix it, or the next valid
                # save from the calibration UI overwrites it.
                self.get_logger().error(
                    f'Failed to load {self.CALIBRATION_FILE} ({e}); using YAML defaults')
                self.calibration = self._calibration_from_params()
        else:
            self.get_logger().info(
                f'Calibration file not found: {self.CALIBRATION_FILE}, using ROS parameters')
            self.calibration = self._calibration_from_params()
        
        self._apply_calibration_to_servos()
    
    def _calibration_from_params(self) -> CalibrationData:
        """Create CalibrationData from ROS parameters."""
        return CalibrationData(
            steering_center=self.get_parameter('steering_center').value,
            pan_center=self.get_parameter('pan_center').value,
            tilt_center=self.get_parameter('tilt_center').value,
            reverse_direction=self.get_parameter('reverse_direction').value,
            speed_gain=self.get_parameter('speed_gain').value,
            source='yaml_params',
            is_saved=False,
        )
    
    def _apply_calibration_to_servos(self, move_to_center: str = None) -> None:
        """Apply current calibration to servo controller.
        
        Args:
            move_to_center: Optional channel to move to center after applying calibration.
                           'pan', 'tilt', 'steering', or None.
        """
        cal = self.calibration
        
        # Steering — limits in servo-angle space (after sine model conversion)
        max_servo_offset = math.degrees(math.asin(
            math.sin(math.radians(self.MAX_STEERING)) / self.servo.steering_ratio))
        self.servo.set_limits(ServoController.CHANNEL_STEERING,
                             self.SERVO_CENTER - max_servo_offset,
                             self.SERVO_CENTER + max_servo_offset,
                             self.SERVO_CENTER)
        self.servo.set_trim(ServoController.CHANNEL_STEERING, cal.steering_center)
        
        # Throttle/ESC — speed limit handled in cmd_vel_callback
        # ESC angle is computed unrestricted by the speed model
        self.servo.set_limits(ServoController.CHANNEL_THROTTLE,
                             self.SERVO_CENTER - 30,  # ESC physical limit
                             self.SERVO_CENTER + 30,
                             self.SERVO_CENTER)
        
        # Pan (limits are real angles; 2x scaling is applied in servo_controller)
        self.servo.set_limits(ServoController.CHANNEL_PAN,
                             self.SERVO_CENTER - self.MAX_PAN,
                             self.SERVO_CENTER + self.MAX_PAN,
                             self.SERVO_CENTER)
        self.servo.set_trim(ServoController.CHANNEL_PAN, cal.pan_center)
        
        # Tilt (positive centre = up; lower servo angle = up, so apply negative trim)
        self.servo.set_limits(ServoController.CHANNEL_TILT,
                             self.SERVO_CENTER - self.MAX_TILT,
                             self.SERVO_CENTER + self.MAX_TILT,
                             self.SERVO_CENTER)
        self.servo.set_trim(ServoController.CHANNEL_TILT, -cal.tilt_center)
        
        # Move specified channel to center position
        if move_to_center:
            channel_map = {
                'pan': ServoController.CHANNEL_PAN,
                'tilt': ServoController.CHANNEL_TILT,
                'steering': ServoController.CHANNEL_STEERING,
            }
            if move_to_center in channel_map:
                channel = channel_map[move_to_center]
                self.servo._apply_angle(channel, self.SERVO_CENTER)
                self.get_logger().info(f'{move_to_center} moved to center')
        
        # Log
        self.get_logger().info(f'Calibration applied (source: {cal.source}):')
        self.get_logger().info(f'  Steering: max=±{self.MAX_STEERING}°, center={cal.steering_center}°')
        self.get_logger().info(f'  Max Speed: ±{self.MAX_SPEED} m/s')
        self.get_logger().info(f'  Pan: max=±{self.MAX_PAN}°, center={cal.pan_center}°')
        self.get_logger().info(f'  Tilt: max=±{self.MAX_TILT}°, center={cal.tilt_center}°')
        self.get_logger().info(f'  ESC reverse_direction: {cal.reverse_direction}')
        self.get_logger().info(f'  Speed gain: {cal.speed_gain}')
        
        # Apply reverse_direction as inversion on throttle channel
        self.servo.set_inverted(ServoController.CHANNEL_THROTTLE, cal.reverse_direction)
        
        # Apply ESC gain
        self.servo.speed_gain = cal.speed_gain
        
        # Publish status if available
        self._publish_calibration_status()
    
    def _save_calibration(self) -> bool:
        """Save current calibration to JSON file.
        
        Returns:
            True if saved successfully, False otherwise.
        """
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(self.CALIBRATION_FILE), exist_ok=True)
            
            with open(self.CALIBRATION_FILE, 'w') as f:
                json.dump(self.calibration.to_dict(), f, indent=2)
            
            self.calibration.is_saved = True
            self.get_logger().info(f'Calibration saved to {self.CALIBRATION_FILE}')
            return True
        except Exception as e:
            self.get_logger().error(f'Failed to save calibration: {e}')
            return False
    
    def _publish_calibration_status(self) -> None:
        """Publish current calibration status."""
        if self.calibration_status_pub is None:
            return
        
        msg = CalibrationStatus()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.max_steering = self.MAX_STEERING
        msg.max_speed = self.MAX_SPEED
        msg.max_pan = self.MAX_PAN
        msg.max_tilt = self.MAX_TILT
        msg.steering_center = self.calibration.steering_center
        msg.pan_center = self.calibration.pan_center
        msg.tilt_center = self.calibration.tilt_center
        msg.reverse_direction = self.calibration.reverse_direction
        msg.source = self.calibration.source
        msg.is_saved = self.calibration.is_saved
        msg.file_path = self.CALIBRATION_FILE
        self.calibration_status_pub.publish(msg)

    # ========== Service Callbacks ==========
    
    def set_calibration_callback(self, request, response):
        """Set calibration values, apply, and optionally save.
        
        channel: 'steering', 'pan', 'tilt', 'reverse', 'speed_gain', or 'all' (reload from file)
        
        Range limits:
            pan_center, tilt_center, steering_center: -15 ~ 15°
        
        Note: max_steering, max_speed, max_pan, max_tilt are hardware constants
        and cannot be changed via calibration.
        """
        channel = request.channel.lower()
        
        # Range-limit constants
        LIMITS = {
            'pan_center': (-15.0, 15.0),
            'tilt_center': (-15.0, 15.0),
            'steering_center': (-15.0, 15.0),
        }
        
        def validate_range(name: str, value: float) -> tuple:
            """Range validation. Returns (valid, error_message)."""
            if name not in LIMITS:
                return True, None
            min_val, max_val = LIMITS[name]
            if value < min_val or value > max_val:
                return False, f'{name}={value} out of range [{min_val}, {max_val}]'
            return True, None
        
        if channel == 'all':
            # Reload from file
            self._load_and_apply_calibration()
            response.success = True
            response.message = f'Calibration reloaded from {self.calibration.source}'
        elif channel == 'reverse':
            # Set reverse_direction
            self.calibration.reverse_direction = request.bool_value
            self.calibration.is_saved = False
            self._apply_calibration_to_servos()
            response.success = True
            response.message = f'reverse_direction set to {request.bool_value}'
        elif channel == 'speed':
            # Speed max is a hardware constant — reject
            response.success = False
            response.message = f'max_speed is a hardware constant ({self.MAX_SPEED} m/s), not adjustable'
        elif channel == 'speed_gain':
            # Set per-car speed gain
            gain = request.max_value
            if gain < 0.1 or gain > 5.0:
                response.success = False
                response.message = f'speed_gain={gain} out of range [0.1, 5.0]'
                response.current_calibration_json = self.calibration.to_json()
                return response
            self.calibration.speed_gain = gain
            self.calibration.is_saved = False
            self._apply_calibration_to_servos()
            response.success = True
            response.message = f'speed_gain set to {gain}'
        elif channel in ['steering', 'pan', 'tilt']:
            # Set center only (max is hardware constant)
            valid_center, err_center = validate_range(f'{channel}_center', request.center_value)
            if not valid_center:
                response.success = False
                response.message = err_center
                response.current_calibration_json = self.calibration.to_json()
                return response
            setattr(self.calibration, f'{channel}_center', request.center_value)
            self.calibration.is_saved = False
            self._apply_calibration_to_servos(move_to_center=channel)
            response.success = True
            response.message = f'{channel}: center={request.center_value}° (moved to center)'
        elif channel.endswith('_max') and channel[:-4] in ['steering', 'pan', 'tilt']:
            # Max values are hardware constants — reject
            base = channel[:-4]
            response.success = False
            response.message = f'max_{base} is a hardware constant, not adjustable'
        elif channel.endswith('_center') and channel[:-7] in ['steering', 'pan', 'tilt']:
            # Set center only
            base = channel[:-7]
            valid, err = validate_range(f'{base}_center', request.center_value)
            if not valid:
                response.success = False
                response.message = err
                response.current_calibration_json = self.calibration.to_json()
                return response
            setattr(self.calibration, f'{base}_center', request.center_value)
            self.calibration.is_saved = False
            self._apply_calibration_to_servos(move_to_center=base)
            response.success = True
            response.message = f'{base}: center={request.center_value}° (moved to center)'
        else:
            response.success = False
            response.message = f'Invalid channel: {channel}. Use steering, pan, tilt, steering_center, pan_center, tilt_center, reverse, speed_gain, or all'
            response.current_calibration_json = self.calibration.to_json()
            return response
        
        # Always save to file (ignore save_to_file parameter)
        if self._save_calibration():
            response.message += ' (saved)'
        else:
            response.message += ' (save failed!)'
            response.success = False
        
        response.current_calibration_json = self.calibration.to_json()
        return response
    
    def get_calibration_callback(self, request, response):
        """Get current calibration values."""
        response.success = True
        response.message = f'Current calibration from {self.calibration.source}'
        response.max_steering = self.MAX_STEERING
        response.max_speed = self.MAX_SPEED
        response.max_pan = self.MAX_PAN
        response.max_tilt = self.MAX_TILT
        response.steering_center = self.calibration.steering_center
        response.pan_center = self.calibration.pan_center
        response.tilt_center = self.calibration.tilt_center
        response.reverse_direction = self.calibration.reverse_direction
        response.speed_gain = self.calibration.speed_gain
        response.source = self.calibration.source
        response.calibration_json = self.calibration.to_json()
        return response
    
    def reload_calibration_callback_legacy(self, request, response):
        """Legacy Trigger service for backward compatibility."""
        self._load_and_apply_calibration()
        response.success = True
        response.message = f'Calibration reloaded from {self.calibration.source}'
        return response

    def _initialize_esc(self) -> None:
        """Initialize ESC with proper neutral signal sequence.
        
        The ESC learns its neutral point at power-on.
        Send a stable neutral (90°) signal so the ESC learns the correct neutral point.
        """
        self.get_logger().info('ESC initialization sequence starting...')
        
        # 1. Center all servos
        self.servo.center_all()
        time.sleep(0.1)
        
        # 2. Stabilise ESC neutral signal (repeat 90° signal)
        for _ in range(50):
            self.servo._apply_angle(ServoController.CHANNEL_THROTTLE, 90)
            time.sleep(0.02)
        
        # 3. Wait for ESC to arm (time for it to learn neutral)
        time.sleep(0.5)
        
        # 4. Final check — all servos centred
        self.servo.center_all()
        
        self.get_logger().info('ESC initialization complete - neutral point set to 90°')

    def pan_callback(self, msg: Float64):
        """Handle camera pan command.
        
        Input: radians (-max_pan_rad to +max_pan_rad)
        Positive = left, Negative = right
        """
        if self._camera_engaged:
            return
        self._apply_pan(msg.data)

    def _teleop_pan_callback(self, msg: Float64):
        """Joy teleop priority mirror for pan — always applied."""
        self._apply_pan(msg.data)

    def _apply_pan(self, radians_in: float) -> None:
        
        # Convert to degrees for internal processing
        degrees_in = math.degrees(radians_in)
        
        # Clamp to limits (degrees)
        max_deg = self.MAX_PAN
        degrees_in = max(-max_deg, min(max_deg, degrees_in))
        
        # Convert to normalized (-1 to 1)
        normalized = degrees_in / max_deg if max_deg > 0 else 0.0
        
        # Store for joint_state (radians)
        self.current_pan = math.radians(degrees_in)
        self.current_pan_normalized = normalized
        
        # Apply pan only (independent of tilt)
        pan_limits = self.servo.limits[self.servo.CHANNEL_PAN]
        pan_angle = pan_limits.from_normalized(normalized)
        self.servo._apply_angle(self.servo.CHANNEL_PAN, pan_angle)

    def tilt_callback(self, msg: Float64):
        """Handle camera tilt command.
        
        Input: radians (-max_tilt_rad to +max_tilt_rad)
        Positive = up, Negative = down
        """
        if self._camera_engaged:
            return
        self._apply_tilt(msg.data)

    def _teleop_tilt_callback(self, msg: Float64):
        """Joy teleop priority mirror for tilt — always applied."""
        self._apply_tilt(msg.data)

    def _apply_tilt(self, radians_in: float) -> None:
        
        # Convert to degrees for internal processing
        degrees_in = math.degrees(radians_in)
        
        # Clamp to limits (degrees)
        max_deg = self.MAX_TILT
        degrees_in = max(-max_deg, min(max_deg, degrees_in))
        
        # Convert to normalized (-1 to 1)
        normalized = degrees_in / max_deg if max_deg > 0 else 0.0
        
        # Store for joint_state (radians, positive = up)
        self.current_tilt = math.radians(degrees_in)
        
        # Invert for servo: positive input = up = servo goes negative direction
        servo_normalized = -normalized
        
        self.current_tilt_normalized = servo_normalized
        
        # Apply tilt only (independent of pan)
        tilt_limits = self.servo.limits[self.servo.CHANNEL_TILT]
        tilt_angle = tilt_limits.from_normalized(servo_normalized)
        self.servo._apply_angle(self.servo.CHANNEL_TILT, tilt_angle)

    def servo_callback(self, msg: Float64MultiArray):
        """Direct servo control. msg.data = [channel, angle_degrees]"""
        if len(msg.data) >= 2:
            channel = int(msg.data[0])
            angle = msg.data[1]
            if 1 <= channel <= 4:
                self.get_logger().info(f'servo_callback: ch={channel}, angle={angle:.1f}°')
                self.board.set_servo(channel, angle)

    def publish_imu(self):
        """Read and publish IMU data."""
        if not self.board.is_connected():
            return

        imu_data = self.board.read_imu()
        if imu_data is None:
            return

        msg = Imu()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = self.imu_frame_id

        # IMU chip axes remapped to vehicle body frame (X-fwd, Y-left, Z-up)
        # SDK axes → ROS body frame: x=-sdk_y, y=-sdk_x, z=sdk_z
        accel = imu_data.get('accel', (0, 0, 0))
        msg.linear_acceleration.x = -accel[1]
        msg.linear_acceleration.y = -accel[0]
        msg.linear_acceleration.z = accel[2]

        gyro = imu_data.get('gyro', (0, 0, 0))
        msg.angular_velocity.x = -gyro[1]
        msg.angular_velocity.y = -gyro[0]
        msg.angular_velocity.z = -gyro[2]

        msg.orientation_covariance[0] = -1.0

        self.imu_pub.publish(msg)

        # Publish magnetometer data
        mag = imu_data.get('mag')
        if mag:
            mag_msg = MagneticField()
            mag_msg.header = msg.header
            # Rosmaster_Lib returns µT; ROS MagneticField expects Tesla
            mag_msg.magnetic_field.x = mag[0] * 1e-6
            mag_msg.magnetic_field.y = mag[1] * 1e-6
            mag_msg.magnetic_field.z = mag[2] * 1e-6
            self.mag_pub.publish(mag_msg)

    def publish_joint_states(self):
        """Publish joint states for visualization with Ackermann geometry."""
        msg = JointState()
        msg.header.stamp = self.get_clock().now().to_msg()

        msg.name = [
            'front_left_steering_joint',
            'front_right_steering_joint',
            'camera_pan_joint',
            'camera_tilt_joint',
            'front_left_wheel_joint',
            'front_right_wheel_joint',
            'rear_left_wheel_joint',
            'rear_right_wheel_joint'
        ]

        # Ackermann steering geometry: inner wheel turns more than outer
        # current_steering is the center steering angle (radians)
        center_steer = self.current_steering
        
        if abs(center_steer) > 0.001:
            # Calculate turning radius from center steering angle
            # R = L / tan(center_steer)
            turn_radius = self.wheelbase / math.tan(abs(center_steer))
            
            # Inner and outer wheel angles
            # inner: arctan(L / (R - W/2))
            # outer: arctan(L / (R + W/2))
            inner_angle = math.atan(self.wheelbase / (turn_radius - self.track_width / 2))
            outer_angle = math.atan(self.wheelbase / (turn_radius + self.track_width / 2))
            
            if center_steer > 0:  # Turning left
                left_steer = inner_angle   # Left is inner
                right_steer = outer_angle  # Right is outer
            else:  # Turning right
                left_steer = -outer_angle  # Left is outer
                right_steer = -inner_angle # Right is inner
        else:
            left_steer = 0.0
            right_steer = 0.0

        msg.position = [
            left_steer,
            right_steer,
            self.current_pan,
            self.current_tilt,
            0.0, 0.0, 0.0, 0.0
        ]

        # wheel angular velocity = linear velocity / wheel radius
        wheel_angular_velocity = self.current_throttle / self.wheel_radius

        msg.velocity = [
            0.0, 0.0,
            0.0, 0.0,
            wheel_angular_velocity,
            wheel_angular_velocity,
            wheel_angular_velocity,
            wheel_angular_velocity
        ]

        msg.effort = []
        self.joint_state_pub.publish(msg)

    def publish_battery(self):
        """Read and publish battery status."""
        if not self.board.is_connected():
            return

        voltage = self.board.read_battery_voltage()
        if voltage is None:
            return
        
        # On first read: detect cell count → low-voltage threshold + initial voltage correction
        if not self._battery_cells_detected:
            self._battery_cells_detected = True
            cells = self.board.detect_cell_count(voltage)
            self.battery_low_threshold = self.board.get_battery_low_threshold(voltage)
            self.get_logger().info(
                f'Battery detected: {cells}S LiPo ({voltage:.1f}V), '
                f'low threshold: {self.battery_low_threshold:.1f}V'
            )
        
        # Store live voltage (used to correct ESC angle)
        self.current_voltage = voltage

        percentage = self.board.get_battery_percentage()

        msg = BatteryState()
        msg.header.stamp = self.get_clock().now().to_msg()
        msg.header.frame_id = 'base_link'
        msg.voltage = voltage
        msg.percentage = float(percentage) / 100.0 if percentage else 0.0
        msg.design_capacity = 2.0
        msg.present = True

        if voltage < self.battery_low_threshold:
            msg.power_supply_status = BatteryState.POWER_SUPPLY_STATUS_DISCHARGING
            msg.power_supply_health = BatteryState.POWER_SUPPLY_HEALTH_DEAD
            if not self.battery_low_warned:
                self.get_logger().warn(f'LOW BATTERY! Voltage: {voltage:.2f}V ({percentage}%)')
                self.battery_low_warned = True
                self.board.beep(500)
        else:
            msg.power_supply_status = BatteryState.POWER_SUPPLY_STATUS_DISCHARGING
            msg.power_supply_health = BatteryState.POWER_SUPPLY_HEALTH_GOOD
            self.battery_low_warned = False

        msg.power_supply_technology = BatteryState.POWER_SUPPLY_TECHNOLOGY_LIPO
        self.battery_pub.publish(msg)

    def destroy_node(self):
        """Cleanup on shutdown."""
        self.get_logger().info('Shutting down PhysiCar Driver...')
        if self.gpio_board.is_connected():
            self.servo.set_throttle_speed(0.0)
            self.servo.center_all()
            self.gpio_board.disconnect()
        if self.board.is_connected():
            self.board.disconnect()
        super().destroy_node()


def main(args=None):
    rclpy.init(args=args)
    node = PhysicarDriverNode()

    # Use MultiThreadedExecutor for blocking service/action callbacks
    from rclpy.executors import MultiThreadedExecutor
    executor = MultiThreadedExecutor()
    executor.add_node(node)

    try:
        executor.spin()
    except KeyboardInterrupt:
        pass
    finally:
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
