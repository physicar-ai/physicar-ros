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
from sensor_msgs.msg import Imu, MagneticField, BatteryState, JointState, LaserScan
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
from physicar_bringup.servo_controller import ServoController


@dataclass
class CalibrationData:
    """Calibration data structure for all servos."""
    # Max values
    max_steering: float = 25.0    # ± degrees (wheel angle)
    max_speed: float = 3.0        # ± m/s
    max_pan: float = 45.0         # ± degrees
    max_tilt: float = 45.0        # ± degrees
    
    # Center offsets (degrees from 90°)
    steering_center: float = 0.0
    pan_center: float = 0.0
    tilt_center: float = 0.0
    
    # ESC direction
    reverse_direction: bool = False  # True: above 90° = forward (reversed ESC)
    
    # Emergency stop enable (None = use YAML param)
    emergency_enabled: Optional[bool] = None
    
    # Metadata
    source: str = 'defaults'  # 'json_file', 'yaml_params', 'defaults'
    is_saved: bool = False
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        d = {
            'max_steering': self.max_steering,
            'max_speed': self.max_speed,
            'max_pan': self.max_pan,
            'max_tilt': self.max_tilt,
            'steering_center': self.steering_center,
            'pan_center': self.pan_center,
            'tilt_center': self.tilt_center,
            'reverse_direction': self.reverse_direction,
        }
        # Only include emergency_enabled if explicitly set
        if self.emergency_enabled is not None:
            d['emergency_enabled'] = self.emergency_enabled
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

        em = data.get('emergency_enabled')
        if em is not None and not isinstance(em, (bool, int)):
            em = None

        return cls(
            max_steering=_f('max_steering', 25.0, 0.1, 90.0),
            max_speed=_f('max_speed', 3.0, 0.1, 10.0),
            max_pan=_f('max_pan', 45.0, 0.1, 90.0),
            max_tilt=_f('max_tilt', 45.0, 0.1, 90.0),
            steering_center=_f('steering_center', 0.0, -30.0, 30.0),
            pan_center=_f('pan_center', 0.0, -30.0, 30.0),
            tilt_center=_f('tilt_center', 0.0, -30.0, 30.0),
            reverse_direction=_b('reverse_direction', False),
            emergency_enabled=(bool(em) if em is not None else None),
            source=source,
            is_saved=(source == 'json_file'),
        )


class PhysicarDriverNode(Node):
    # Default calibration file path (can be overridden by parameter)
    DEFAULT_CALIBRATION_FILE = '/opt/physicar/calibration.json'
    
    # Servo center angle (standard for RC servos)
    SERVO_CENTER = 90.0
    
    # ESC speed-model constants (measured, power law, 6.5V–8.1V)
    # Model: degree = (ESC_A / V) * ((speed - ESC_K)^ESC_Q + ESC_P)
    # Relative-error RMSE: 2.1%
    ESC_A = 14.0       # scale factor
    ESC_K = -0.05      # speed offset (negative, so speed - k = speed + 0.05)
    ESC_Q = 1.65       # exponent
    ESC_P = 4.0        # constant term
    
    # Feedback-control constants
    FEEDBACK_TOLERANCE = 0.05   # target-speed tolerance (m/s)
    FEEDBACK_TIMEOUT = 0.5      # speed-measurement timeout (s)
    FEEDBACK_MAX_ADJUST = 1     # max degree adjustment (±1)
    
    # Emergency Stop constants
    EMERGENCY_SCAN_TIMEOUT = 0.05     # scan timeout (s)
    EMERGENCY_ODOM_TIMEOUT = 0.1      # odom timeout (s)

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

        # Max limits
        self.declare_parameter('max_steering', 25.0)  # degrees (wheel angle)
        self.declare_parameter('max_speed', 3.0)      # m/s
        self.declare_parameter('max_pan', 45.0)       # degrees
        self.declare_parameter('max_tilt', 45.0)      # degrees

        # ESC direction
        self.declare_parameter('reverse_direction', False)  # true: above 90° = forward

        # Robot physical parameters (from URDF/measurements)
        self.declare_parameter('wheel_radius', 0.0375)  # meters (37.5mm, 7.5cm diameter)
        self.declare_parameter('wheelbase', 0.18)       # meters (front-rear axle distance, measured)
        self.declare_parameter('track_width', 0.16)     # meters (left-right wheel distance, measured)
        self.declare_parameter('steering_ratio', 2.0)  # k = Rs/Rk (servo horn / knuckle arm ratio)
        self.declare_parameter('calibration_file', self.DEFAULT_CALIBRATION_FILE)
        
        # Emergency Stop parameters
        self.declare_parameter('emergency_enabled', True)        # Enable Emergency Stop
        self.declare_parameter('emergency_angle_range', 30.0)    # total detection angle range (°)
        self.declare_parameter('emergency_front_margin', 0.25)   # front collision margin (m)
        self.declare_parameter('emergency_rear_margin', 0.15)    # rear collision margin (m)

        # Get parameters
        serial_port = self.get_parameter('serial_port').value
        baudrate = self.get_parameter('baudrate').value
        self.imu_frame_id = self.get_parameter('imu_frame_id').value
        publish_rate = self.get_parameter('publish_rate').value

        # Robot physical parameters
        self.wheel_radius = self.get_parameter('wheel_radius').value
        self.wheelbase = self.get_parameter('wheelbase').value
        self.track_width = self.get_parameter('track_width').value
        self.steering_ratio = self.get_parameter('steering_ratio').value  # k for sine model
        self.CALIBRATION_FILE = self.get_parameter('calibration_file').value
        
        # Emergency Stop parameters
        self.emergency_enabled = self.get_parameter('emergency_enabled').value
        self.emergency_angle_range = self.get_parameter('emergency_angle_range').value / 2.0  # total → half-angle
        self.emergency_front_margin = self.get_parameter('emergency_front_margin').value
        self.emergency_rear_margin = self.get_parameter('emergency_rear_margin').value

        # Initialize hardware (pass ROS logger for consistent logging)
        self.board = YahboomBoard(
            port=serial_port, 
            baudrate=baudrate,
            logger=self.get_logger()
        )
        self.servo = ServoController(self.board)

        # Calibration state management
        self.calibration = CalibrationData()  # Current calibration
        self.calibration_status_pub = None  # Will be initialized after publisher setup
        self._load_and_apply_calibration()

        # Voltage (updated live from battery)
        self.current_voltage = 7.4  # default
        
        # ESC control state
        self._last_esc_direction = 0  # last ESC direction (1=forward, -1=reverse, 0=stopped)
        
        # Feedback-control state
        self._actual_speed = 0.0           # actual speed measured from rf2o (m/s)
        self._actual_speed_time = 0.0      # time of last speed measurement
        self._current_degree_adjust = 0    # current degree adjustment (-5 ~ +5)
        self._target_speed = 0.0           # current target speed
        
        # ESC state tracking
        self._last_esc_angle = self.SERVO_CENTER  # last ESC angle sent
        
        # Emergency state
        self._front_min_dist = float('inf')
        self._rear_min_dist = float('inf')
        self._scan_time = 0.0
        self._emergency_active = False
        
        # Brake state
        self._braking = False  # whether currently braking
        self._brake_direction = 0  # brake direction (1=brake-while-reversing, -1=brake-while-forward)
        self._brake_timer = None  # brake timer
        self._brake_speed_threshold = 0.1  # below this speed, braking is complete (m/s)

        # Connect to board
        if not self.board.connect():
            self.get_logger().error(f'Failed to connect to expansion board on {serial_port}')
        else:
            self.get_logger().info(f'Connected to expansion board on {serial_port}')
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
        self.create_subscription(Odometry, '/odom', self.odom_callback, qos_sensor)
        
        # Subscribe to LiDAR (for Emergency Stop)
        self.create_subscription(LaserScan, '/scan', self.scan_callback, qos_sensor)

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

    def scan_callback(self, msg: LaserScan):
        """From the LiDAR scan, compute front/rear minimum distance and check Emergency."""
        self._scan_time = time.time()
        
        # Current wheel angle (degrees) — current_steering is in radians
        wheel_angle_deg = math.degrees(self.current_steering)
        
        # Front centre angle = wheel angle; rear centre angle = wheel angle + 180°
        front_center = wheel_angle_deg
        rear_center = wheel_angle_deg + 180.0
        
        # angle → index converter
        def angle_to_idx(angle_deg):
            angle_rad = math.radians(angle_deg)
            return int((angle_rad - msg.angle_min) / msg.angle_increment)
        
        # Compute minimum distance within range (with intensity-based filtering)
        def get_min_dist(center_deg, half_range):
            start_idx = angle_to_idx(center_deg - half_range)
            end_idx = angle_to_idx(center_deg + half_range)
            n = len(msg.ranges)
            has_intensities = len(msg.intensities) == n
            
            min_dist = float('inf')
            for i in range(start_idx, end_idx + 1):
                idx = i % n
                if 0 <= idx < n:
                    r = msg.ranges[idx]
                    # intensity below 10 = low confidence → ignore
                    if has_intensities and msg.intensities[idx] < 10:
                        continue
                    if math.isfinite(r) and r > 0 and r < min_dist:
                        min_dist = r
            return min_dist
        
        self._front_min_dist = get_min_dist(front_center, self.emergency_angle_range)
        self._rear_min_dist = get_min_dist(rear_center, self.emergency_angle_range)
        
        # Emergency check and automatic recovery
        was_emergency = self._emergency_active
        is_emergency = self._check_emergency(self._target_speed)
        
        if is_emergency and not was_emergency:
            # Emergency triggered → active brake (reverse speed if odom present, else 90°)
            if abs(self._actual_speed) > self._brake_speed_threshold:
                self._start_active_brake(self._actual_speed)
                self.get_logger().warn(f'EMERGENCY BRAKE: speed={self._actual_speed:.2f} m/s')
            else:
                self.board.set_servo(1, self.SERVO_CENTER)
                self.get_logger().warn(f'Emergency active, ESC stopped (no odom or already stopped)')
        elif was_emergency and not is_emergency:
            # Emergency cleared → restore ESC (unless braking, in which case brake handles it)
            if self._braking:
                self.get_logger().info(f'Emergency cleared, but braking in progress - brake will handle ESC')
            else:
                esc_angle = self._compute_esc_angle_with_adjust()
                self._last_esc_angle = esc_angle
                self.board.set_servo(1, esc_angle)
                self.get_logger().info(f'Emergency cleared, resuming ESC: {esc_angle:.1f}° (speed={self._target_speed:.2f} m/s)')
    
    def _check_emergency(self, target_speed: float) -> bool:
        """Check whether Emergency Stop conditions are met.
        
        Returns:
            True if a stop is required
        """
        # Check whether Emergency is disabled
        if not self.emergency_enabled:
            self._emergency_active = False
            return False
        
        # Check scan timeout
        if time.time() - self._scan_time > self.EMERGENCY_SCAN_TIMEOUT:
            self._emergency_active = False
            return False
        
        # Check odom timeout — if data is stale, fall back to target_speed only
        odom_valid = (time.time() - self._actual_speed_time) < self.EMERGENCY_ODOM_TIMEOUT
        actual_speed = self._actual_speed if odom_valid else 0.0
        
        # If actual_speed opposes target_speed, treat actual_speed as 0
        if odom_valid and (target_speed * actual_speed < 0):
            actual_speed = 0.0
        
        # Use average of target_speed and actual_speed (conservative)
        avg_speed = (target_speed + actual_speed) / 2.0 if odom_valid else target_speed
        
        if abs(avg_speed) < 0.05:  # essentially stopped
            self._emergency_active = False
            return False
        
        if avg_speed > 0:
            # Going forward: predict front-collision distance
            brake_time = 0.15 + avg_speed / 5.0  # braking time (0.15s base + speed-proportional)
            front_collision_dist = self._front_min_dist - self.emergency_front_margin
            predicted_dist = front_collision_dist - (avg_speed * brake_time)
            if predicted_dist < 0:
                if not self._emergency_active:
                    self.get_logger().warn(
                        f'EMERGENCY STOP: front={self._front_min_dist:.2f}m, '
                        f'avg_speed={avg_speed:.2f}m/s (target={target_speed:.2f}, actual={actual_speed:.2f}), '
                        f'predicted={predicted_dist:.2f}m'
                    )
                self._emergency_active = True
                return True
        else:
            # Going reverse: predict rear-collision distance
            speed = abs(avg_speed)
            brake_time = 0.15 + speed / 5.0  # braking time (0.15s base + speed-proportional)
            rear_collision_dist = self._rear_min_dist - self.emergency_rear_margin
            predicted_dist = rear_collision_dist - (speed * brake_time)
            if predicted_dist < 0:
                if not self._emergency_active:
                    self.get_logger().warn(
                        f'EMERGENCY STOP: rear={self._rear_min_dist:.2f}m, '
                        f'avg_speed={speed:.2f}m/s (target={abs(target_speed):.2f}, actual={abs(actual_speed):.2f}), '
                        f'predicted={predicted_dist:.2f}m'
                    )
                self._emergency_active = True
                return True
        
        self._emergency_active = False
        return False

    def odom_callback(self, msg: Odometry):
        """Store actual speed from rf2o odometry and run feedback control.
        
        On every odom message, compare against target speed and adjust degree.
        """
        self._actual_speed = msg.twist.twist.linear.x
        self._actual_speed_time = time.time()
        
        # Skip adjustment when stopped
        if self._target_speed == 0:
            return
        
        # Skip adjustment when Emergency is active
        if self._emergency_active:
            return
        
        # Compute feedback adjustment
        actual = abs(self._actual_speed)
        target = abs(self._target_speed)
        error = target - actual
        
        # Skip adjustment when error is within tolerance
        if abs(error) < self.FEEDBACK_TOLERANCE:
            return
        
        # Slower than target → increase degree; faster → decrease degree
        prev_adjust = self._current_degree_adjust
        if error > 0:
            self._current_degree_adjust = min(
                self._current_degree_adjust + 1, 
                self.FEEDBACK_MAX_ADJUST
            )
        else:
            self._current_degree_adjust = max(
                self._current_degree_adjust - 1, 
                -self.FEEDBACK_MAX_ADJUST
            )
        
        # Resend ESC if the adjustment changed
        if self._current_degree_adjust != prev_adjust:
            esc_angle = self._compute_esc_angle_with_adjust()
            self.board.set_servo(1, esc_angle)
            self.get_logger().info(
                f'Feedback: target={target:.2f}, actual={actual:.2f}, '
                f'adjust={self._current_degree_adjust}, ESC={esc_angle:.1f}°'
            )
    
    def _compute_esc_angle_with_adjust(self) -> float:
        """Compute ESC angle from the current target speed and adjustment."""
        if self._target_speed == 0:
            return self.SERVO_CENTER
        
        V = self.current_voltage
        abs_speed = abs(self._target_speed)
        
        # Power-law model
        target_degree = (self.ESC_A / V) * ((abs_speed - self.ESC_K) ** self.ESC_Q + self.ESC_P)
        base_degree = round(target_degree)
        degree = base_degree + self._current_degree_adjust
        
        if self._last_esc_direction > 0:
            return self.SERVO_CENTER + degree
        else:
            return self.SERVO_CENTER - degree

    def _wheel_to_servo_angle(self, wheel_angle_deg: float) -> float:
        """Convert wheel angle to servo angle (sine model).
        
        Kinematics: Rs × sin(θs) = Rk × sin(θw)
        Hence: θs = arcsin(sin(θw) / k), where k = Rs/Rk
        
        Args:
            wheel_angle_deg: wheel angle (degrees)
            
        Returns:
            servo_angle_offset: servo angle offset (degrees, displacement from centre)
        """
        if abs(wheel_angle_deg) < 0.001:
            return 0.0
        
        wheel_rad = math.radians(wheel_angle_deg)
        sin_servo = math.sin(wheel_rad) / self.steering_ratio
        
        # Range check for arcsin (-1 ~ 1)
        sin_servo = max(-1.0, min(1.0, sin_servo))
        servo_rad = math.asin(sin_servo)
        
        return math.degrees(servo_rad)

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
        
        All speed-related logic:
        - Speed limit (max_speed)
        - Save target_speed
        - Cancel braking
        - Emergency check
        - Compute ESC angle and send
        - Active brake
        
        Args:
            speed_mps: target speed (m/s, positive=forward, negative=reverse)
        """
        try:
            # Apply speed limit
            max_speed = self.calibration.max_speed
            speed_mps = max(-max_speed, min(max_speed, speed_mps))
            
            # Save target_speed (for restore when Emergency clears)
            self._target_speed = speed_mps
            
            # Update joint state
            self.current_throttle = speed_mps
            
            # Movement command cancels braking (only when not in Emergency)
            if abs(speed_mps) > 0.01 and self._braking and not self._emergency_active:
                self._stop_brake()
            
            # When in Emergency, ignore ESC commands (scan_callback handles brake)
            if self._emergency_active:
                return
            
            # ESC command
            if abs(speed_mps) < 0.01:
                # Stop command → ESC neutral
                self.board.set_servo(1, self.SERVO_CENTER)
                self.get_logger().debug(f'ESC STOP: 90° (actual={self._actual_speed:.2f} m/s)')
                # If actually moving, engage active brake
                if abs(self._actual_speed) > self._brake_speed_threshold:
                    self._start_active_brake(self._actual_speed)
            else:
                esc_angle = self._velocity_to_esc_angle(speed_mps)
                self._last_esc_angle = esc_angle
                self.board.set_servo(1, esc_angle)
                self.get_logger().debug(
                    f'ESC: {esc_angle:.1f}° (speed={speed_mps:.2f} m/s, V={self.current_voltage:.1f}V)'
                )
        except Exception as e:
            self.get_logger().error(f'_set_speed error: {e}')
    
    def _set_steering_rad(self, steering_rad: float):
        """Set steering angle in radians. Core low-level steering control.
        
        All steering-related logic:
        - Angle limit (max_steering)
        - Update joint state
        - Convert to servo angle via sine model
        - Apply steering_center calibration
        
        Args:
            steering_rad: wheel steering angle (radians, positive=left turn)
        """
        try:
            # Convert radians → degrees (for internal processing)
            steering_deg = math.degrees(steering_rad)
            
            # Angle limit (degrees)
            max_steering = self.calibration.max_steering
            steering_deg = max(-max_steering, min(max_steering, steering_deg))
            
            # Update joint state (radians)
            self.current_steering = math.radians(steering_deg)
            
            # Convert wheel angle → servo angle via sine model
            servo_angle_offset = self._wheel_to_servo_angle(steering_deg)
            center_offset = self._wheel_to_servo_angle(self.calibration.steering_center)
            steering_angle = self.SERVO_CENTER + servo_angle_offset + center_offset
            
            # Steering is always applied, regardless of Emergency
            self.board.set_servo(2, steering_angle)
            self.get_logger().debug(
                f'Steering: {steering_rad:.3f} rad ({steering_deg:.1f}°) → servo {steering_angle:.1f}°'
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
                    steering_deg = math.copysign(self.calibration.max_steering, angular_z)
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

    def _start_active_brake(self, actual_speed: float):
        """Begin active braking: brake in the opposite direction of current speed.
        
        Args:
            actual_speed: current actual speed (m/s, positive=forward, negative=reverse)
        """
        if self._braking:
            return  # already braking
        
        self._braking = True
        # Keep _target_speed (used for Emergency recovery; cmd_vel=0 already set)
        
        # Brake opposite to current direction of motion
        # actual_speed > 0 (going forward) → need reverse brake
        # actual_speed < 0 (going reverse) → need forward brake
        if actual_speed > 0:
            self._brake_direction = -1  # reverse braking
        else:
            self._brake_direction = 1   # forward braking
        
        self.get_logger().info(f'Active brake started: speed={actual_speed:.2f} m/s, direction={self._brake_direction}')
        
        # Start brake timer (check speed and brake every 50ms)
        self._brake_timer = self.create_timer(0.05, self._brake_tick)
    
    def _brake_tick(self):
        """Brake-timer callback: check speed and apply reverse-direction brake."""
        # Continue braking even during Emergency (Emergency may have started the brake)
        
        actual_speed = self._actual_speed
        abs_speed = abs(actual_speed)
        
        # Brake complete when speed drops below threshold
        if abs_speed <= self._brake_speed_threshold:
            self._stop_brake()
            return
        
        # Check whether direction has flipped (over-braking caused reverse motion)
        # _brake_direction > 0: was originally reversing → now actual > 0 means flipped
        # _brake_direction < 0: was originally going forward → now actual < 0 means flipped
        if actual_speed * self._brake_direction > 0:
            self.get_logger().info(f'Brake direction reversed: speed={actual_speed:.2f} m/s, stopping')
            self._stop_brake()
            return
        
        # Apply reverse-direction brake
        # Smooth braking: brake force proportional to speed
        # Faster → stronger reverse, slower → weaker reverse
        brake_degree = min(int(abs_speed * 10) + 3, 8)  # 3~8 degree range
        
        if self._brake_direction > 0:
            # Forward brake (was going reverse)
            if self.calibration.reverse_direction:
                esc_angle = self.SERVO_CENTER + brake_degree
            else:
                esc_angle = self.SERVO_CENTER - brake_degree
        else:
            # Reverse brake (was going forward)
            if self.calibration.reverse_direction:
                esc_angle = self.SERVO_CENTER - brake_degree
            else:
                esc_angle = self.SERVO_CENTER + brake_degree
        
        self.board.set_servo(1, esc_angle)
        self.get_logger().debug(f'Brake tick: speed={actual_speed:.2f}, esc={esc_angle:.1f}°')
    
    def _stop_brake(self):
        """Brake complete: clean up the timer and stop the ESC."""
        if self._brake_timer:
            self._brake_timer.cancel()
            self._brake_timer = None
        
        self._braking = False
        self._brake_direction = 0
        # Keep _target_speed (used to restore when Emergency clears)
        self._current_degree_adjust = 0
        
        # Stop ESC
        self.board.set_servo(1, self.SERVO_CENTER)
        self.get_logger().info('Active brake complete')

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
                
                # Apply emergency_enabled from JSON if set
                if self.calibration.emergency_enabled is not None:
                    self.emergency_enabled = self.calibration.emergency_enabled
                    self.get_logger().info(f'Emergency enabled from calibration: {self.emergency_enabled}')
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
            max_steering=self.get_parameter('max_steering').value,
            max_speed=self.get_parameter('max_speed').value,
            max_pan=self.get_parameter('max_pan').value,
            max_tilt=self.get_parameter('max_tilt').value,
            steering_center=self.get_parameter('steering_center').value,
            pan_center=self.get_parameter('pan_center').value,
            tilt_center=self.get_parameter('tilt_center').value,
            reverse_direction=self.get_parameter('reverse_direction').value,
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
        
        # Steering
        self.servo.set_limits(ServoController.CHANNEL_STEERING,
                             self.SERVO_CENTER - cal.max_steering,
                             self.SERVO_CENTER + cal.max_steering,
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
                             self.SERVO_CENTER - cal.max_pan,
                             self.SERVO_CENTER + cal.max_pan,
                             self.SERVO_CENTER)
        self.servo.set_trim(ServoController.CHANNEL_PAN, cal.pan_center)
        
        # Tilt (positive centre = up; lower servo angle = up, so apply negative trim)
        self.servo.set_limits(ServoController.CHANNEL_TILT,
                             self.SERVO_CENTER - cal.max_tilt,
                             self.SERVO_CENTER + cal.max_tilt,
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
        self.get_logger().info(f'  Steering: max=±{cal.max_steering}°, center={cal.steering_center}°')
        self.get_logger().info(f'  Max Speed: ±{cal.max_speed} m/s')
        self.get_logger().info(f'  Pan: max=±{cal.max_pan}°, center={cal.pan_center}°')
        self.get_logger().info(f'  Tilt: max=±{cal.max_tilt}°, center={cal.tilt_center}°')
        self.get_logger().info(f'  ESC reverse_direction: {cal.reverse_direction}')
        
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
        msg.max_steering = self.calibration.max_steering
        msg.max_speed = self.calibration.max_speed
        msg.max_pan = self.calibration.max_pan
        msg.max_tilt = self.calibration.max_tilt
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
        
        channel: 'steering', 'speed', 'pan', 'tilt', 'reverse', or 'all' (reload from file)
        
        Range limits:
            max_pan: 0 ~ 45°
            max_tilt: 0 ~ 30°
            max_steering: 0 ~ 25°
            max_speed: 0 ~ 3.0 m/s
            pan_center, tilt_center, steering_center: -15 ~ 15°
        """
        channel = request.channel.lower()
        
        # Range-limit constants (max must be ≥1.0 to avoid divide-by-zero)
        LIMITS = {
            'max_pan': (1.0, 60.0),
            'max_tilt': (1.0, 45.0),
            'max_steering': (1.0, 25.0),
            'max_speed': (0.1, 3.0),
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
        elif channel == 'emergency':
            # Set emergency_enabled
            self.calibration.emergency_enabled = request.bool_value
            self.emergency_enabled = request.bool_value  # Also update runtime value
            # When disabled, also clear any active emergency state
            if not request.bool_value:
                self._emergency_active = False
            self.calibration.is_saved = False
            response.success = True
            response.message = f'emergency_enabled set to {request.bool_value}'
            self.get_logger().info(f'Emergency stop {"enabled" if request.bool_value else "disabled"}')
        elif channel == 'speed':
            # Speed only has max (no center)
            valid, err = validate_range('max_speed', request.max_value)
            if not valid:
                response.success = False
                response.message = err
                response.current_calibration_json = self.calibration.to_json()
                return response
            self.calibration.max_speed = request.max_value
            self.calibration.is_saved = False
            self._apply_calibration_to_servos()
            response.success = True
            response.message = f'max_speed set to ±{request.max_value} m/s'
        elif channel in ['steering', 'pan', 'tilt']:
            # Set both max and center
            valid_max, err_max = validate_range(f'max_{channel}', request.max_value)
            valid_center, err_center = validate_range(f'{channel}_center', request.center_value)
            if not valid_max:
                response.success = False
                response.message = err_max
                response.current_calibration_json = self.calibration.to_json()
                return response
            if not valid_center:
                response.success = False
                response.message = err_center
                response.current_calibration_json = self.calibration.to_json()
                return response
            setattr(self.calibration, f'max_{channel}', request.max_value)
            setattr(self.calibration, f'{channel}_center', request.center_value)
            self.calibration.is_saved = False
            self._apply_calibration_to_servos(move_to_center=channel)
            response.success = True
            response.message = f'{channel}: max=±{request.max_value}°, center={request.center_value}° (moved to center)'
        elif channel.endswith('_max') and channel[:-4] in ['steering', 'pan', 'tilt']:
            # Set max only
            base = channel[:-4]
            valid, err = validate_range(f'max_{base}', request.max_value)
            if not valid:
                response.success = False
                response.message = err
                response.current_calibration_json = self.calibration.to_json()
                return response
            setattr(self.calibration, f'max_{base}', request.max_value)
            self.calibration.is_saved = False
            self._apply_calibration_to_servos(move_to_center=base)
            response.success = True
            response.message = f'{base}: max=±{request.max_value}° (moved to center)'
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
            response.message = f'Invalid channel: {channel}. Use steering, pan, tilt, steering_max, steering_center, pan_max, pan_center, tilt_max, tilt_center, speed, reverse, emergency, or all'
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
        response.max_steering = self.calibration.max_steering
        response.max_speed = self.calibration.max_speed
        response.max_pan = self.calibration.max_pan
        response.max_tilt = self.calibration.max_tilt
        response.steering_center = self.calibration.steering_center
        response.pan_center = self.calibration.pan_center
        response.tilt_center = self.calibration.tilt_center
        response.reverse_direction = self.calibration.reverse_direction
        response.emergency_enabled = self.emergency_enabled  # Use runtime value
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
            self.board.set_servo(1, 90)  # ESC = channel 1
            time.sleep(0.02)
        
        # 3. Wait for ESC to arm (time for it to learn neutral)
        time.sleep(0.5)
        
        # 4. Final check — all servos centred
        self.servo.center_all()
        
        self.get_logger().info('ESC initialization complete - neutral point set to 90°')

    def _velocity_to_esc_angle(self, target_speed: float) -> float:
        """Convert cmd_vel to ESC servo angle using power law model.
        
        Model (empirically measured, power law, 6.5V–8.1V):
        - degree = (A / V) * ((speed - K)^Q + P)
        - ESC angle = 90 + degree (forward), 90 - degree (reverse) [reverse_direction=true]
        - Relative-error RMSE: 2.1%
        
        Sets the initial degree only; feedback adjustment runs in odom_callback.
        """
        if target_speed == 0:
            self._last_esc_direction = 0
            self._current_degree_adjust = 0  # reset adjustment when stopped
            return self.SERVO_CENTER  # stopped
        
        V = self.current_voltage
        abs_speed = abs(target_speed)
        is_forward = target_speed > 0
        
        # Determine ESC direction (taking reverse_direction into account)
        if self.calibration.reverse_direction:
            # Reversed ESC: above 90° = forward
            self._last_esc_direction = 1 if is_forward else -1
        else:
            # Normal ESC: below 90° = forward
            self._last_esc_direction = -1 if is_forward else 1
        
        # Power-law model: degree = (A / V) * ((speed - K)^Q + P)
        target_degree = (self.ESC_A / V) * ((abs_speed - self.ESC_K) ** self.ESC_Q + self.ESC_P)
        
        # Round to integer degree (initial value; feedback adjustment runs in odom_callback)
        degree = round(target_degree)
        
        # Final angle calculation
        if self._last_esc_direction > 0:
            return self.SERVO_CENTER + degree
        else:
            return self.SERVO_CENTER - degree

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
        max_deg = self.calibration.max_pan
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
        max_deg = self.calibration.max_tilt
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

        accel = imu_data.get('accel', (0, 0, 0))
        msg.linear_acceleration.x = accel[0]
        msg.linear_acceleration.y = accel[1]
        msg.linear_acceleration.z = accel[2]

        gyro = imu_data.get('gyro', (0, 0, 0))
        msg.angular_velocity.x = gyro[0]
        msg.angular_velocity.y = gyro[1]
        msg.angular_velocity.z = gyro[2]

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
        if self.board.is_connected():
            self.servo.emergency_stop()
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
