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
State Manager - Unified ROS2 state subscription and streaming.

Provides both one-shot reads and SSE streaming for all robot state.
"""

import asyncio
import base64
import io
import json
import math
import queue as queue_module
import threading
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Tuple, Union

from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy, DurabilityPolicy

# ROS message types
from nav_msgs.msg import Odometry
from sensor_msgs.msg import BatteryState, Imu, LaserScan, CompressedImage, JointState, Joy
from std_msgs.msg import Float64
from geometry_msgs.msg import Twist
from physicar_interfaces.msg import DeepracerInference

try:
    from physicar_interfaces.msg import Audio
    HAS_AUDIO_MSG = True
except ImportError:
    HAS_AUDIO_MSG = False

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class TopicConfig:
    """Configuration for a ROS topic subscription."""
    topic: str
    msg_type: type
    qos: QoSProfile
    processor: Optional[Callable[[Any], dict]] = None  # msg -> dict


@dataclass
class StateBuffer:
    """Thread-safe buffer for state data with condition notification.

    Supports lazy processing: callbacks store only the raw message
    (update_raw), and dict conversion happens on first read via
    _ensure_processed().  This avoids wasting CPU on high-frequency
    topics when no client is reading.
    """
    data: Optional[dict] = None
    raw_msg: Optional[Any] = None
    seq: int = 0
    _data_seq: int = -1  # seq at which data was last computed
    last_ts: float = 0.0  # monotonic time of last update; 0 = never
    processor: Optional[Callable[[Any], dict]] = None
    lock: threading.Lock = field(default_factory=threading.Lock)
    condition: threading.Condition = field(default=None)
    
    def __post_init__(self):
        if self.condition is None:
            self.condition = threading.Condition(self.lock)
    
    def update(self, data: dict, raw_msg: Any = None):
        """Update buffer with pre-processed data and notify waiters."""
        with self.condition:
            self.data = data
            self.raw_msg = raw_msg
            self.seq += 1
            self._data_seq = self.seq
            self.last_ts = time.monotonic()
            self.condition.notify_all()

    def update_raw(self, raw_msg: Any):
        """Store raw message without processing. Dict computed lazily on read."""
        with self.condition:
            self.raw_msg = raw_msg
            self.seq += 1
            self.last_ts = time.monotonic()
            self.condition.notify_all()

    def _ensure_processed(self):
        """Compute data from raw_msg if stale. Must be called with lock held."""
        if self._data_seq != self.seq and self.processor and self.raw_msg is not None:
            self.data = self.processor(self.raw_msg)
            self._data_seq = self.seq

    def get(self) -> Optional[dict]:
        """Get current processed data (non-blocking, lazy)."""
        with self.lock:
            self._ensure_processed()
            return self.data

    def age(self) -> Optional[float]:
        """Seconds since the last update, or None if never updated."""
        with self.lock:
            if self.last_ts == 0.0:
                return None
            return time.monotonic() - self.last_ts
    
    def wait(self, last_seq: int, timeout: float = 0.5) -> Tuple[Optional[dict], int]:
        """Wait for new data. Returns (data, new_seq)."""
        with self.condition:
            if self.seq == last_seq:
                self.condition.wait(timeout)
            if self.seq != last_seq:
                self._ensure_processed()
                return self.data, self.seq
            return None, last_seq


# =============================================================================
# QoS Profiles
# =============================================================================

QOS_SENSOR_BEST_EFFORT = QoSProfile(
    reliability=ReliabilityPolicy.BEST_EFFORT,
    history=HistoryPolicy.KEEP_LAST,
    depth=1,
)

QOS_RELIABLE = QoSProfile(
    reliability=ReliabilityPolicy.RELIABLE,
    history=HistoryPolicy.KEEP_LAST,
    depth=10,
)


# =============================================================================
# Message Processors
# =============================================================================

def process_odometry(msg: Odometry) -> dict:
    """Process Odometry message to dict."""
    pos = msg.pose.pose.position
    ori = msg.pose.pose.orientation
    lin = msg.twist.twist.linear
    ang = msg.twist.twist.angular
    
    # Convert quaternion to yaw (simplified)
    siny_cosp = 2.0 * (ori.w * ori.z + ori.x * ori.y)
    cosy_cosp = 1.0 - 2.0 * (ori.y * ori.y + ori.z * ori.z)
    yaw = math.atan2(siny_cosp, cosy_cosp)
    
    return {
        "position": {
            "x": round(pos.x, 4),
            "y": round(pos.y, 4),
            "z": round(pos.z, 4),
        },
        "orientation": {
            "yaw": round(math.degrees(yaw), 2),
            "quaternion": {
                "x": round(ori.x, 4),
                "y": round(ori.y, 4),
                "z": round(ori.z, 4),
                "w": round(ori.w, 4),
            }
        },
        "velocity": {
            "linear": round(lin.x, 4),
            "angular": round(ang.z, 4),
        },
        "frame_id": msg.header.frame_id,
    }


def process_battery(msg: BatteryState) -> dict:
    """Process BatteryState message to dict."""
    return {
        "voltage": round(msg.voltage, 2),
        "percentage": round(msg.percentage * 100, 1) if msg.percentage >= 0 else None,
        "current": round(msg.current, 3) if not math.isnan(msg.current) else None,
        "charging": msg.power_supply_status == BatteryState.POWER_SUPPLY_STATUS_CHARGING,
        "present": msg.present,
    }


def process_imu(msg: Imu) -> dict:
    """Process Imu message to dict."""
    return {
        "acceleration": {
            "x": round(msg.linear_acceleration.x, 4),
            "y": round(msg.linear_acceleration.y, 4),
            "z": round(msg.linear_acceleration.z, 4),
        },
        "gyro": {
            "x": round(msg.angular_velocity.x, 4),
            "y": round(msg.angular_velocity.y, 4),
            "z": round(msg.angular_velocity.z, 4),
        },
        "orientation": {
            "x": round(msg.orientation.x, 4),
            "y": round(msg.orientation.y, 4),
            "z": round(msg.orientation.z, 4),
            "w": round(msg.orientation.w, 4),
        },
    }


def process_joints(msg: JointState) -> dict:
    """Process JointState message to dict."""
    joints = {}
    for i, name in enumerate(msg.name):
        joints[name] = {
            "position": round(msg.position[i], 4) if i < len(msg.position) else None,
            "velocity": round(msg.velocity[i], 4) if i < len(msg.velocity) else None,
            "effort": round(msg.effort[i], 4) if i < len(msg.effort) else None,
        }
    return {"joints": joints}


def process_laser_scan(msg: LaserScan, step_deg: float = 1.0) -> dict:
    """Process LaserScan message to dict.
    
    For invalid readings (inf, nan, out of range), uses the minimum valid value
    from neighboring readings within ±step range. Returns null only if all
    neighbors are also invalid.
    """
    angle_increment_deg = math.degrees(msg.angle_increment)
    angle_min_deg = math.degrees(msg.angle_min)
    num_readings = len(msg.ranges)
    
    def is_valid(r: float) -> bool:
        """Check if a range reading is valid."""
        return math.isfinite(r) and r > msg.range_min and r < msg.range_max
    
    def get_min_in_range(center_idx: int, window_deg: float) -> float | None:
        """Get minimum valid value within ±window_deg around center_idx."""
        window_samples = int(window_deg / angle_increment_deg) + 1
        min_val = None
        
        for offset in range(-window_samples, window_samples + 1):
            idx = center_idx + offset
            if 0 <= idx < num_readings:
                r = msg.ranges[idx]
                if is_valid(r):
                    if min_val is None or r < min_val:
                        min_val = r
        return min_val
    
    ranges_dict = {}
    for i, r in enumerate(msg.ranges):
        angle_deg = angle_min_deg + i * angle_increment_deg
        
        # Check if angle is divisible by step (with tolerance)
        remainder = abs(angle_deg % step_deg)
        if remainder < 0.25 or remainder > (step_deg - 0.25):
            angle_key = round(angle_deg / step_deg) * step_deg
            if angle_key == int(angle_key):
                angle_str = str(int(angle_key))
            else:
                angle_str = f"{angle_key:.1f}"
            
            if is_valid(r):
                ranges_dict[angle_str] = round(r, 3)
            else:
                # Invalid reading: use minimum from neighbors within ±step/2 range
                min_neighbor = get_min_in_range(i, step_deg / 2)
                if min_neighbor is not None:
                    ranges_dict[angle_str] = round(min_neighbor, 3)
                else:
                    ranges_dict[angle_str] = None
    
    return {
        "step": step_deg,
        "count": len(ranges_dict),
        "range_min": round(msg.range_min, 3),
        "range_max": round(msg.range_max, 3),
        "ranges": ranges_dict,
        "meta": {
            "units": {"angle": "deg", "range": "m"},
            "orientation": {"0": "front", "90": "left", "-90": "right", "180": "rear"},
        },
    }


def process_float64(msg: Float64) -> dict:
    """Process Float64 message to dict."""
    return {"value": round(msg.data, 4)}


def process_twist(msg: Twist) -> dict:
    """Process Twist (cmd_vel) message to dict."""
    return {
        "linear": {
            "x": round(msg.linear.x, 4),
            "y": round(msg.linear.y, 4),
            "z": round(msg.linear.z, 4),
        },
        "angular": {
            "x": round(msg.angular.x, 4),
            "y": round(msg.angular.y, 4),
            "z": round(msg.angular.z, 4),
        },
    }

def process_deepracer_inference(msg: DeepracerInference) -> dict:
    """Process DeepracerInference message to dict."""
    return {
        "speed": round(msg.speed, 4),
        "steering_angle": round(msg.steering_angle, 2),
        "probabilities": [round(p, 4) for p in msg.probabilities],
        "timestamp": msg.header.stamp.sec + msg.header.stamp.nanosec / 1e9,
    }

def process_joy(msg: Joy) -> dict:
    """Process Joy message to dict.

    Used by the joystick mapping UI to show what the user is pressing
    in real time so they can identify axis/button indexes.  Buttons are
    rounded to int (joy_node always sends 0/1 but the field is float-ish).
    """
    return {
        "axes": [round(float(a), 4) for a in msg.axes],
        "buttons": [int(b) for b in msg.buttons],
        "timestamp": msg.header.stamp.sec + msg.header.stamp.nanosec / 1e9,
    }

# =============================================================================
# Topic Configurations
# =============================================================================

TOPIC_CONFIGS: Dict[str, TopicConfig] = {
    "odom": TopicConfig(
        topic="/odom",
        msg_type=Odometry,
        qos=QOS_SENSOR_BEST_EFFORT,
        processor=process_odometry,
    ),
    "battery": TopicConfig(
        topic="/battery_state",
        msg_type=BatteryState,
        qos=QOS_RELIABLE,
        processor=process_battery,
    ),
    "imu": TopicConfig(
        topic="/imu",
        msg_type=Imu,
        qos=QOS_SENSOR_BEST_EFFORT,
        processor=process_imu,
    ),
    "joints": TopicConfig(
        topic="/joint_states",
        msg_type=JointState,
        qos=QOS_RELIABLE,
        processor=process_joints,
    ),
    "lidar": TopicConfig(
        topic="/scan_filtered",
        msg_type=LaserScan,
        qos=QOS_SENSOR_BEST_EFFORT,
        processor=lambda msg: process_laser_scan(msg, 1.0),
    ),
    "camera_pan": TopicConfig(
        topic="/camera/pan",
        msg_type=Float64,
        qos=QOS_RELIABLE,
        processor=process_float64,
    ),
    "camera_tilt": TopicConfig(
        topic="/camera/tilt",
        msg_type=Float64,
        qos=QOS_RELIABLE,
        processor=process_float64,
    ),
    "deepracer_inference": TopicConfig(
        topic="/deepracer/inference",
        msg_type=DeepracerInference,
        qos=QOS_RELIABLE,
        processor=process_deepracer_inference,
    ),
    "joy": TopicConfig(
        topic="/joy",
        msg_type=Joy,
        # joy_node publishes RELIABLE; match it so messages actually arrive.
        qos=QOS_RELIABLE,
        processor=process_joy,
    ),
}


# =============================================================================
# StateManager
# =============================================================================

class StateManager:
    """
    Unified state manager for all ROS2 sensor data.
    
    Provides:
    - One-shot reads via get_once()
    - SSE streaming via stream_sse()
    - WebSocket streaming via stream_ws()
    """
    
    _instance: Optional['StateManager'] = None
    _lock = threading.Lock()
    
    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
            return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._initialized = True
        self._node = None
        self._subscriptions: Dict[str, Any] = {}
        self._buffers: Dict[str, StateBuffer] = {}
        
        # Special buffer for camera (bytes, not dict)
        self._camera_buffer = StateBuffer()
        self._camera_subscription = None
        
        # Audio streaming (queue-based, all messages matter)
        self._audio_subscription = None
        self._audio_queues: list = []  # List of queue.Queue for SSE clients
        self._audio_lock = threading.Lock()
        
        # Published command state (what we sent, not subscribed)
        self._cmd_state = {
            "speed": 0.0,
            "steering": 0.0,
            "pan": 0.0,
            "tilt": 0.0,
        }
        self._cmd_lock = threading.Lock()
    
    def init(self, node) -> bool:
        """Initialize with ROS2 node."""
        if self._node is not None:
            return True
        
        self._node = node
        
        # Create buffers for all configured topics
        for key in TOPIC_CONFIGS:
            self._buffers[key] = StateBuffer()
        
        self._node.get_logger().info('[StateManager] Initialized')
        return True
    
    def _ensure_subscription(self, key: str) -> bool:
        """Ensure subscription exists for a topic (lazy initialization)."""
        if key in self._subscriptions:
            return True
        
        if self._node is None:
            return False
        
        config = TOPIC_CONFIGS.get(key)
        if config is None:
            return False
        
        buffer = self._buffers[key]
        buffer.processor = config.processor

        def callback(msg):
            buffer.update_raw(msg)
        
        self._subscriptions[key] = self._node.create_subscription(
            config.msg_type,
            config.topic,
            callback,
            config.qos,
        )
        
        self._node.get_logger().info(f'[StateManager] Subscribed to {config.topic}')
        return True
    
    def _ensure_camera_subscription(self) -> bool:
        """Ensure camera subscription exists."""
        if self._camera_subscription is not None:
            return True
        
        if self._node is None:
            return False
        
        qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=1,
        )
        
        def callback(msg: CompressedImage):
            self._camera_buffer.update({"format": msg.format}, bytes(msg.data))
        
        self._camera_subscription = self._node.create_subscription(
            CompressedImage,
            '/camera/image_raw/compressed',
            callback,
            qos,
        )
        
        self._node.get_logger().info('[StateManager] Subscribed to camera')
        return True
    
    # -------------------------------------------------------------------------
    # Command State (what we published)
    # -------------------------------------------------------------------------
    
    def update_cmd_state(self, **kwargs):
        """Update published command state."""
        with self._cmd_lock:
            for key, value in kwargs.items():
                if key in self._cmd_state:
                    self._cmd_state[key] = value
    
    def get_cmd_state(self) -> dict:
        """Get current command state."""
        with self._cmd_lock:
            return self._cmd_state.copy()
    
    # -------------------------------------------------------------------------
    # One-Shot Reads
    # -------------------------------------------------------------------------
    
    def get_once(self, key: str, **kwargs) -> Optional[dict]:
        """Get current state for a resource (non-blocking)."""
        if key == "camera":
            # Return camera info, not image
            return self._get_camera_info()
        
        if key == "speed":
            return {"value": self.get_cmd_state()["speed"]}
        
        if key == "steering":
            return {"value": self.get_cmd_state()["steering"]}
        
        if key == "calibration":
            # Calibration needs service call, handled separately
            return None
        
        if key == "lidar":
            # Special handling for step parameter
            step = kwargs.get("step", 1.0)
            self._ensure_subscription("lidar")
            buffer = self._buffers.get("lidar")
            if buffer and buffer.raw_msg:
                return process_laser_scan(buffer.raw_msg, step)
            return None
        
        # Standard topic subscriptions
        self._ensure_subscription(key)
        buffer = self._buffers.get(key)
        return buffer.get() if buffer else None

    def buffer_age(self, key: str) -> Optional[float]:
        """Seconds since the last message on `key`, or None if never seen.

        Used for liveness checks (e.g. is the joystick still connected:
        is /joy publishing within the last ~1s?) without keeping an SSE
        consumer alive.
        """
        self._ensure_subscription(key)
        buffer = self._buffers.get(key)
        if buffer is None:
            return None
        return buffer.age()

    def get_summary(self) -> dict:
        """Get summary of all states (deprecated, use get_all_states)."""
        return self.get_all_states()
    
    def get_all_states(self) -> dict:
        """Get all states from all configured topics."""
        result = {}
        
        # 1. Command state (speed, steering, pan, tilt)
        cmd = self.get_cmd_state()
        result["cmd"] = cmd
        
        # 2. All configured topics
        for key in TOPIC_CONFIGS:
            self._ensure_subscription(key)
            buffer = self._buffers.get(key)
            if buffer:
                data = buffer.get()
                if data is not None:
                    result[key] = data
        
        # 3. Camera info (not image)
        result["camera"] = self._get_camera_info()
        
        return result
    
    def _get_camera_info(self) -> dict:
        """Get camera info."""
        self._ensure_camera_subscription()
        cmd = self.get_cmd_state()
        has_frame = self._camera_buffer.raw_msg is not None
        
        # Get resolution from ROS camera node parameters (cached)
        resolution = self._get_camera_resolution()
        
        return {
            "status": "streaming" if has_frame else "no_frame",
            "pan": cmd["pan"],
            "tilt": cmd["tilt"],
            "topic": "/camera/image_raw/compressed",
            "resolution": resolution,
        }
    
    def _get_camera_resolution(self) -> str:
        """Get camera resolution from ROS camera node parameters."""
        if not hasattr(self, '_cached_resolution'):
            self._cached_resolution = None
        
        if self._cached_resolution:
            return self._cached_resolution
        
        try:
            # Try to get parameters from camera node
            from rcl_interfaces.srv import GetParameters
            client = self._node.create_client(GetParameters, '/camera/get_parameters')
            if client.wait_for_service(timeout_sec=0.5):
                request = GetParameters.Request()
                request.names = ['width', 'height']
                future = client.call_async(request)
                
                # Wait with timeout
                import time
                start = time.time()
                while not future.done() and time.time() - start < 0.5:
                    time.sleep(0.01)
                
                if future.done():
                    result = future.result()
                    if len(result.values) >= 2:
                        width = result.values[0].integer_value
                        height = result.values[1].integer_value
                        if width > 0 and height > 0:
                            self._cached_resolution = f"{width}x{height}"
                            return self._cached_resolution
        except Exception:
            pass
        
        # Fallback to default
        return "480x320"
    
    # -------------------------------------------------------------------------
    # Camera Image
    # -------------------------------------------------------------------------
    
    def get_camera_image(self, width: Optional[int] = None, height: Optional[int] = None) -> Optional[bytes]:
        """Get camera image (optionally resized)."""
        self._ensure_camera_subscription()
        
        with self._camera_buffer.lock:
            raw = self._camera_buffer.raw_msg
        
        if raw is None:
            return None
        
        if width is None and height is None:
            return raw
        
        return self._resize_jpeg(raw, width, height)
    
    def wait_camera_frame(self, last_seq: int, timeout: float = 0.1) -> Tuple[Optional[bytes], int]:
        """Wait for new camera frame."""
        self._ensure_camera_subscription()
        
        with self._camera_buffer.condition:
            if self._camera_buffer.seq == last_seq:
                self._camera_buffer.condition.wait(timeout)
            
            if self._camera_buffer.seq != last_seq:
                return self._camera_buffer.raw_msg, self._camera_buffer.seq
            return None, last_seq
    
    def _resize_jpeg(self, jpeg_data: bytes, width: Optional[int], height: Optional[int]) -> bytes:
        """Resize JPEG image (aspect-ratio preserved, width takes priority)."""
        if not PIL_AVAILABLE or (width is None and height is None):
            return jpeg_data
        
        try:
            img = Image.open(io.BytesIO(jpeg_data))
            orig_w, orig_h = img.size
            
            # width has priority, aspect ratio preserved
            if width:
                new_w = width
                new_h = int(orig_h * width / orig_w)
            else:
                new_h = height
                new_w = int(orig_w * height / orig_h)
            
            # Don't upscale — only downscale
            if new_w >= orig_w or new_h >= orig_h:
                return jpeg_data
            
            img = img.resize((new_w, new_h), Image.Resampling.BILINEAR)
            output = io.BytesIO()
            img.save(output, format='JPEG', quality=80, optimize=False)
            return output.getvalue()
        except Exception:
            return jpeg_data
    
    # -------------------------------------------------------------------------
    # SSE Streaming
    # -------------------------------------------------------------------------
    
    async def stream_sse(self, key: str, **kwargs) -> AsyncGenerator[str, None]:
        """Generate SSE stream for a resource."""
        loop = asyncio.get_event_loop()
        last_seq = -1
        first_msg = True
        
        if key == "lidar":
            step = kwargs.get("step", 1.0)
            self._ensure_subscription("lidar")
            buffer = self._buffers["lidar"]
            
            while True:
                msg, new_seq = await loop.run_in_executor(
                    None, buffer.wait, last_seq, 0.5
                )
                
                if msg is None:
                    yield "event: keepalive\ndata: {}\n\n"
                    continue
                
                last_seq = new_seq
                
                # Use cached data for default step, reprocess only for custom step
                if step == 1.0:
                    data = msg
                else:
                    with buffer.lock:
                        raw_msg = buffer.raw_msg
                    if raw_msg is None:
                        continue
                    data = process_laser_scan(raw_msg, step)
                
                if first_msg:
                    first_msg = False
                yield f"data: {json.dumps(data)}\n\n"
            return
        
        # Standard topics
        self._ensure_subscription(key)
        buffer = self._buffers.get(key)
        
        if buffer is None:
            yield f"event: error\ndata: {{\"error\": \"Unknown resource: {key}\"}}\n\n"
            return
        
        while True:
            data, new_seq = await loop.run_in_executor(
                None, buffer.wait, last_seq, 0.5
            )
            
            if data is None:
                yield "event: keepalive\ndata: {}\n\n"
                continue
            
            last_seq = new_seq
            yield f"data: {json.dumps(data)}\n\n"
    
    async def stream_all_sse(self, include: Optional[List[str]] = None) -> AsyncGenerator[str, None]:
        """
        Generate SSE stream for multiple states combined.
        
        Args:
            include: List of keys to include. Default: ["cmd", "odom", "battery"]
                     Available: cmd, odom, battery, imu, joints, camera_pan, camera_tilt
                     (lidar excluded by default - too heavy)
        """
        # Default lightweight set
        if include is None:
            include = ["cmd", "odom", "battery"]
        
        # Ensure subscriptions
        for key in include:
            if key != "cmd":
                self._ensure_subscription(key)
        
        loop = asyncio.get_event_loop()
        last_seqs = {key: -1 for key in include}
        
        while True:
            result = {}
            changed = False
            
            # Collect current state for each included key
            for key in include:
                if key == "cmd":
                    result["cmd"] = self.get_cmd_state()
                    changed = True  # cmd always included
                else:
                    buffer = self._buffers.get(key)
                    if buffer:
                        with buffer.lock:
                            if buffer.seq != last_seqs[key]:
                                last_seqs[key] = buffer.seq
                                changed = True
                                buffer._ensure_processed()
                            if buffer.data is not None:
                                result[key] = buffer.data
            
            if changed and result:
                yield f"data: {json.dumps(result)}\n\n"
            
            # Wait a bit before next poll
            await asyncio.sleep(0.05)  # 20Hz max
    async def stream_camera_mjpeg(
        self, 
        width: Optional[int] = None, 
        height: Optional[int] = None
    ) -> AsyncGenerator[bytes, None]:
        """Generate MJPEG stream."""
        loop = asyncio.get_event_loop()
        last_seq = -1
        
        while True:
            frame, new_seq = await loop.run_in_executor(
                None, self.wait_camera_frame, last_seq, 0.1
            )
            
            if frame is None:
                continue
            
            last_seq = new_seq
            
            if width or height:
                frame = await loop.run_in_executor(
                    None, self._resize_jpeg, frame, width, height
                )
            
            if frame:
                yield b"--frame\r\n"
                yield b"Content-Type: image/jpeg\r\n"
                yield f"Content-Length: {len(frame)}\r\n\r\n".encode()
                yield frame
                yield b"\r\n"

    async def stream_cmd_state_sse(self, key: str) -> AsyncGenerator[str, None]:
        """
        Generate SSE stream for command state (pan, tilt).
        Uses polling since cmd_state doesn't have topic subscription.
        """
        last_value = None
        
        while True:
            await asyncio.sleep(0.1)  # 10Hz polling
            
            current = self.get_cmd_state().get(key)
            if current != last_value:
                last_value = current
                data = {"value": current}
                yield f"data: {json.dumps(data)}\n\n"

    # -------------------------------------------------------------------------
    # Audio Streaming (queue-based — all messages matter, not just latest)
    # -------------------------------------------------------------------------

    def _ensure_audio_subscription(self):
        """Subscribe to /audio topic for streaming to browser."""
        if self._audio_subscription or self._node is None or not HAS_AUDIO_MSG:
            return

        qos = QoSProfile(
            reliability=ReliabilityPolicy.RELIABLE,
            history=HistoryPolicy.KEEP_ALL,
            depth=100,
        )

        def callback(msg):
            data = {
                "channel": msg.channel,
                "format": msg.format,
                "sample_rate": msg.sample_rate,
                "channels": msg.audio_channels,
                "bits": msg.bits_per_sample,
                "volume": msg.volume,
                "stop": msg.stop,
                "stop_all": msg.stop_all,
            }
            if msg.data:
                data["data"] = base64.b64encode(bytes(msg.data)).decode()

            json_str = json.dumps(data)

            with self._audio_lock:
                for q in self._audio_queues:
                    try:
                        q.put_nowait(json_str)
                    except queue_module.Full:
                        pass  # drop for slow clients

        self._audio_subscription = self._node.create_subscription(
            Audio, '/audio', callback, qos
        )
        self._node.get_logger().info('[StateManager] Subscribed to /audio for streaming')

    async def stream_audio_sse(self) -> AsyncGenerator[str, None]:
        """Generate SSE stream for /audio topic data."""
        self._ensure_audio_subscription()

        q = queue_module.Queue(maxsize=200)
        with self._audio_lock:
            self._audio_queues.append(q)

        loop = asyncio.get_event_loop()

        try:
            while True:
                try:
                    data = await loop.run_in_executor(
                        None, lambda: q.get(timeout=5.0)
                    )
                    yield f"data: {data}\n\n"
                except queue_module.Empty:
                    yield "event: keepalive\ndata: {}\n\n"
        finally:
            with self._audio_lock:
                self._audio_queues.remove(q)


# Singleton instance
state_manager = StateManager()


def get_state_manager() -> StateManager:
    """Get the singleton StateManager instance."""
    return state_manager
