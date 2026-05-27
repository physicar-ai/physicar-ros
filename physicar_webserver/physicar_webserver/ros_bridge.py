#!/usr/bin/env python3
"""
ROS Bridge - Singleton ROS2 node for FastAPI integration.
"""

import threading
import time
import asyncio
from typing import Optional, Any, Dict
from concurrent.futures import ThreadPoolExecutor

import rclpy
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from geometry_msgs.msg import Twist
from std_msgs.msg import Float64
from builtin_interfaces.msg import Duration as DurationMsg

# Import calibration services
try:
    from physicar_interfaces.srv import SetCalibration, GetCalibration
    HAS_CALIBRATION_SERVICES = True
except ImportError:
    HAS_CALIBRATION_SERVICES = False

# Import joy teleop mapping services
try:
    from physicar_interfaces.srv import GetJoyMapping, SetJoyMapping
    HAS_JOY_MAPPING_SERVICES = True
except ImportError:
    HAS_JOY_MAPPING_SERVICES = False

# Import joy teleop status message
try:
    from physicar_interfaces.msg import JoyTeleopStatus
    HAS_JOY_STATUS_MSG = True
except ImportError:
    HAS_JOY_STATUS_MSG = False

try:
    from physicar_interfaces.msg import TeleopStatus
    HAS_TELEOP_STATUS_MSG = True
except ImportError:
    HAS_TELEOP_STATUS_MSG = False

# rcl_interfaces SetParameters (used to toggle joy_teleop enabled flag at runtime)
try:
    from rcl_interfaces.srv import SetParameters
    from rcl_interfaces.msg import Parameter, ParameterValue, ParameterType
    HAS_RCL_PARAM_SERVICES = True
except ImportError:
    HAS_RCL_PARAM_SERVICES = False

# Import audio interfaces
try:
    from physicar_interfaces.msg import Audio
    HAS_AUDIO_INTERFACES = True
except ImportError:
    HAS_AUDIO_INTERFACES = False

# Import DeepRacer interfaces
try:
    from physicar_interfaces.srv import (
        DeepracerLoadModel,
        DeepracerUnloadModel,
        DeepracerControl,
        DeepracerStatus,
        DeepracerSetConfig
    )
    HAS_DEEPRACER_INTERFACES = True
except ImportError:
    HAS_DEEPRACER_INTERFACES = False


class ROSBridge:
    """
    Singleton ROS2 bridge for FastAPI.
    
    Manages a background ROS2 node with publishers/subscribers.
    """
    _instance: Optional['ROSBridge'] = None
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
        self._node: Optional[Node] = None
        self._spin_thread: Optional[threading.Thread] = None
        self._running = False
        
        # Publishers (will be created after init)
        self._speed_pub = None
        self._steering_pub = None
        self._pan_pub = None
        self._tilt_pub = None
        self._cmd_vel_pub = None
        
        # Service clients
        self._get_calibration_client = None
        self._set_calibration_client = None
        self._get_joy_mapping_client = None
        self._set_joy_mapping_client = None
        self._joy_set_params_client = None

        # Web teleop publishers (created in init()).  Mirror joy_teleop's
        # /teleop/{speed,steering,camera/pan,camera/tilt} + /teleop/status
        # so REST clients can drive the robot through the same priority
        # gate as the gamepad.
        self._teleop_speed_pub = None
        self._teleop_steering_pub = None
        self._teleop_pan_pub = None
        self._teleop_tilt_pub = None
        self._teleop_status_pub = None

        # Web teleop engagement state (drives the heartbeat below).
        self._web_drive_engaged: bool = False
        self._web_camera_engaged: bool = False
        self._web_estop_latched: bool = False
        # If no command/heartbeat call lands within this window the web
        # source is auto-released, mirroring the joy "deadman" semantic.
        self._web_teleop_timeout_sec: float = 0.5
        self._web_last_activity: float = 0.0
        self._web_state_lock = threading.Lock()
        self._web_status_timer = None  # ROS timer @ 30 Hz

        # Joy teleop status (joy-only fields, currently just `enabled`).
        self._joy_status = {
            'enabled': False,
            'received': False,
        }
        # Generic teleop status (any source).  `received` flips true on the
        # first message; `fresh` reflects whether the cached values are
        # within the publisher-declared timeout window.
        self._teleop_status = {
            'source': '',
            'drive_engaged': False,
            'camera_engaged': False,
            'estop_latched': False,
            'timeout_sec': 0.5,
            'received': False,
            'last_time': 0.0,  # monotonic seconds (time.time())
        }
        
        # Audio publisher
        self._audio_pub = None
        
        # DeepRacer service clients
        self._deepracer_load_model_client = None
        self._deepracer_unload_model_client = None
        self._deepracer_control_client = None
        self._deepracer_status_client = None
        self._deepracer_set_config_client = None
        
        # Thread pool for async service calls
        self._executor = ThreadPoolExecutor(max_workers=4)
        
        # State tracking
        self._last_cmd_vel = {'linear_x': 0.0, 'angular_z': 0.0, 'time': 0.0}
        self._last_pan = 0.0
        self._last_tilt = 0.0
    
    def init(self, node: Node = None) -> bool:
        """Initialize ROS2 publishers and service clients.
        
        Args:
            node: External ROS2 node to use. If None, creates own node.
        """
        try:
            if node is not None:
                # Use external node (from webserver_node.py)
                self._node = node
                self._external_node = True
            else:
                # Create own node (standalone mode)
                if not rclpy.ok():
                    rclpy.init()
                self._node = rclpy.create_node('webserver_bridge')
                self._external_node = False
            
            # QoS profiles
            qos = QoSProfile(depth=10)
            
            # Publishers - Low-level control
            self._speed_pub = self._node.create_publisher(Float64, '/speed', qos)
            self._steering_pub = self._node.create_publisher(Float64, '/steering', qos)
            self._pan_pub = self._node.create_publisher(Float64, '/camera/pan', qos)
            self._tilt_pub = self._node.create_publisher(Float64, '/camera/tilt', qos)
            
            # High-level control (Ackermann conversion in driver)
            self._cmd_vel_pub = self._node.create_publisher(Twist, '/cmd_vel', qos)
            
            # Service clients (calibration)
            if HAS_CALIBRATION_SERVICES:
                self._get_calibration_client = self._node.create_client(
                    GetCalibration, '/physicar_driver/get_calibration'
                )
                self._set_calibration_client = self._node.create_client(
                    SetCalibration, '/physicar_driver/set_calibration'
                )

            # Service clients (joy teleop mapping)
            if HAS_JOY_MAPPING_SERVICES:
                self._get_joy_mapping_client = self._node.create_client(
                    GetJoyMapping, '/physicar_joy_teleop/get_mapping'
                )
                self._set_joy_mapping_client = self._node.create_client(
                    SetJoyMapping, '/physicar_joy_teleop/set_mapping'
                )

            # Parameter client to toggle joy_teleop's `enabled` flag at runtime
            if HAS_RCL_PARAM_SERVICES:
                self._joy_set_params_client = self._node.create_client(
                    SetParameters, '/physicar_joy_teleop/set_parameters'
                )

            # Joy teleop status subscription — carries sticky joy-specific
            # config (currently just `enabled`).  Latched so this server
            # learns the value immediately on (re)start.
            if HAS_JOY_STATUS_MSG:
                self._node.create_subscription(
                    JoyTeleopStatus,
                    '/physicar_joy_teleop/status',
                    self._on_joy_status,
                    QoSProfile(
                        depth=1,
                        reliability=ReliabilityPolicy.RELIABLE,
                        durability=DurabilityPolicy.TRANSIENT_LOCAL,
                    ),
                )

            # Generic teleop status — source-agnostic lock state with
            # publisher-declared freshness timeout.
            if HAS_TELEOP_STATUS_MSG:
                self._node.create_subscription(
                    TeleopStatus,
                    '/teleop/status',
                    self._on_teleop_status,
                    QoSProfile(depth=1, reliability=ReliabilityPolicy.RELIABLE),
                )
            
            # Audio publisher
            if HAS_AUDIO_INTERFACES:
                self._audio_pub = self._node.create_publisher(Audio, '/audio', qos)
            
            # Web teleop publishers — same topic names as joy_teleop so the
            # driver / cmd_vel_adapter consume them through the existing
            # gate.  /teleop/status is published at 30 Hz by a timer below
            # whenever any web engagement flag is held.
            self._teleop_speed_pub    = self._node.create_publisher(Float64, '/teleop/speed', qos)
            self._teleop_steering_pub = self._node.create_publisher(Float64, '/teleop/steering', qos)
            self._teleop_pan_pub      = self._node.create_publisher(Float64, '/teleop/camera/pan', qos)
            self._teleop_tilt_pub     = self._node.create_publisher(Float64, '/teleop/camera/tilt', qos)
            if HAS_TELEOP_STATUS_MSG:
                teleop_status_qos = QoSProfile(
                    depth=1,
                    reliability=ReliabilityPolicy.RELIABLE,
                    durability=DurabilityPolicy.VOLATILE,
                )
                self._teleop_status_pub = self._node.create_publisher(
                    TeleopStatus, '/teleop/status', teleop_status_qos
                )
                # 30 Hz heartbeat — only emits while engaged or for a
                # one-shot "release" frame after disengagement.
                self._web_status_timer = self._node.create_timer(
                    1.0 / 30.0, self._tick_web_teleop_status
                )
            
            # DeepRacer service clients
            if HAS_DEEPRACER_INTERFACES:
                self._deepracer_load_model_client = self._node.create_client(
                    DeepracerLoadModel, '/deepracer/load_model'
                )
                self._deepracer_unload_model_client = self._node.create_client(
                    DeepracerUnloadModel, '/deepracer/unload_model'
                )
                self._deepracer_control_client = self._node.create_client(
                    DeepracerControl, '/deepracer/control'
                )
                self._deepracer_status_client = self._node.create_client(
                    DeepracerStatus, '/deepracer/status'
                )
                self._deepracer_set_config_client = self._node.create_client(
                    DeepracerSetConfig, '/deepracer/set_config'
                )
            
            # Start spin thread only if we created our own node
            # (external node is spun by its owner)
            if not self._external_node:
                self._running = True
                self._spin_thread = threading.Thread(target=self._spin_loop, daemon=True)
                self._spin_thread.start()
            
            self._node.get_logger().info('ROS Bridge initialized')
            return True
            
        except Exception as e:
            print(f'[ROSBridge] Init error: {e}')
            return False
    
    def _spin_loop(self):
        """Background thread for spinning ROS2 node."""
        while self._running and rclpy.ok():
            rclpy.spin_once(self._node, timeout_sec=0.1)
    
    def shutdown(self):
        """Shutdown ROS2 resources."""
        self._running = False
        if self._spin_thread:
            self._spin_thread.join(timeout=1.0)
        # Only destroy node if we created it
        if self._node and not getattr(self, '_external_node', False):
            self._node.destroy_node()
            try:
                rclpy.shutdown()
            except:
                pass
        print('[PhysiCar API] ROS Bridge shutdown')
    
    @property
    def is_ready(self) -> bool:
        """Check if ROS bridge is ready."""
        return self._node is not None and rclpy.ok()

    # ========================================================================
    # Teleop / Joy status (lock state cache for REST clients)
    # ========================================================================

    def _on_joy_status(self, msg) -> None:
        """Cache the latest JoyTeleopStatus.  Joy-specific fields only."""
        self._joy_status = {
            'enabled': bool(msg.enabled),
            'received': True,
        }

    def _on_teleop_status(self, msg) -> None:
        """Cache the latest source-agnostic TeleopStatus."""
        timeout_sec = float(msg.timeout.sec) + float(msg.timeout.nanosec) / 1e9
        if timeout_sec <= 0.0:
            timeout_sec = 0.5
        self._teleop_status = {
            'source': str(msg.source) if msg.source else '',
            'drive_engaged': bool(msg.drive_engaged),
            'camera_engaged': bool(msg.camera_engaged),
            'estop_latched': bool(msg.estop_latched),
            'timeout_sec': timeout_sec,
            'received': True,
            'last_time': time.time(),
        }

    def get_joy_status(self) -> Dict[str, Any]:
        """Return the latest cached joy-specific status."""
        return dict(self._joy_status)

    def get_teleop_status(self) -> Dict[str, Any]:
        """Return the latest cached generic teleop status, with a `fresh`
        flag indicating whether the cached values are still within the
        publisher-declared timeout window.  Stale status is treated as
        "all locks released" by consumers (driver, cmd_vel_adapter)."""
        snap = dict(self._teleop_status)
        if snap['received']:
            age = time.time() - snap['last_time']
            snap['fresh'] = age < snap['timeout_sec']
        else:
            snap['fresh'] = False
        # If stale, surface released locks to the client so the UI doesn't
        # show a phantom lock from a dead publisher.
        if not snap['fresh']:
            snap['drive_engaged'] = False
            snap['camera_engaged'] = False
        snap.pop('last_time', None)
        return snap

    # ========================================================================
    # Publishers - Low-level control
    # ========================================================================
    
    def publish_speed(self, speed: float) -> bool:
        """Publish speed to /speed (m/s)."""
        if not self._speed_pub:
            return False
        
        msg = Float64()
        msg.data = float(speed)
        self._speed_pub.publish(msg)
        return True
    
    def publish_steering(self, angle: float) -> bool:
        """Publish steering angle to /steering (radians)."""
        if not self._steering_pub:
            return False
        
        msg = Float64()
        msg.data = float(angle)
        self._steering_pub.publish(msg)
        return True
    
    def publish_pan(self, angle: float) -> bool:
        """Publish camera pan angle to /camera/pan (radians)."""
        if not self._pan_pub:
            return False
        
        msg = Float64()
        msg.data = float(angle)
        self._pan_pub.publish(msg)
        self._last_pan = angle
        return True
    
    def publish_tilt(self, angle: float) -> bool:
        """Publish camera tilt angle to /camera/tilt (radians)."""
        if not self._tilt_pub:
            return False
        
        msg = Float64()
        msg.data = float(angle)
        self._tilt_pub.publish(msg)
        self._last_tilt = angle
        return True
    
    # ========================================================================
    # Publishers - High-level control
    # ========================================================================
    
    def publish_cmd_vel(self, linear_x: float, angular_z: float) -> bool:
        """Publish velocity command to /cmd_vel (Ackermann conversion in driver)."""
        if not self._cmd_vel_pub:
            return False
        
        msg = Twist()
        msg.linear.x = float(linear_x)
        msg.angular.z = float(angular_z)
        self._cmd_vel_pub.publish(msg)
        
        self._last_cmd_vel = {
            'linear_x': linear_x,
            'angular_z': angular_z,
            'time': time.time()
        }
        return True

    # ========================================================================
    # Web Teleop — publishes /teleop/{speed,steering,camera/pan,camera/tilt}
    # plus the source-agnostic /teleop/status priority claim.
    # ========================================================================

    def _bump_web_activity(self) -> None:
        with self._web_state_lock:
            self._web_last_activity = time.time()

    def _web_engaged_locked(self) -> bool:
        """Caller must hold _web_state_lock."""
        return (
            self._web_drive_engaged
            or self._web_camera_engaged
            or self._web_estop_latched
        )

    def _tick_web_teleop_status(self) -> None:
        """30 Hz callback: refresh /teleop/status while web is engaged.

        Auto-releases drive / camera engagement if no command lands within
        ``_web_teleop_timeout_sec`` (deadman semantic).  Once everything is
        released we stop publishing so other sources (joy) regain the gate.
        """
        if not self._teleop_status_pub or not HAS_TELEOP_STATUS_MSG:
            return
        publish_release = False
        with self._web_state_lock:
            now = time.time()
            stale = (now - self._web_last_activity) > self._web_teleop_timeout_sec
            if stale and (self._web_drive_engaged or self._web_camera_engaged):
                self._web_drive_engaged = False
                self._web_camera_engaged = False
                publish_release = True  # one final frame so consumers see it
            engaged = self._web_engaged_locked()
            drive = self._web_drive_engaged
            camera = self._web_camera_engaged
            estop = self._web_estop_latched
            timeout = self._web_teleop_timeout_sec
        if not engaged and not publish_release:
            return
        msg = TeleopStatus()
        msg.source = 'web'
        msg.drive_engaged = drive
        msg.camera_engaged = camera
        msg.estop_latched = estop
        timeout_ns = int(timeout * 1e9)
        msg.timeout = DurationMsg(
            sec=timeout_ns // 1_000_000_000,
            nanosec=timeout_ns % 1_000_000_000,
        )
        self._teleop_status_pub.publish(msg)

    def publish_teleop_speed(self, speed: float) -> bool:
        """Publish to /teleop/speed and auto-engage web drive."""
        if not self._teleop_speed_pub:
            return False
        msg = Float64()
        msg.data = float(speed)
        self._teleop_speed_pub.publish(msg)
        with self._web_state_lock:
            self._web_drive_engaged = True
            self._web_last_activity = time.time()
        # Push status immediately so the very first command isn't gated
        # by the up-to-33 ms wait until the next timer tick.
        self._tick_web_teleop_status()
        return True

    def publish_teleop_steering(self, angle: float) -> bool:
        """Publish to /teleop/steering and auto-engage web drive."""
        if not self._teleop_steering_pub:
            return False
        msg = Float64()
        msg.data = float(angle)
        self._teleop_steering_pub.publish(msg)
        with self._web_state_lock:
            self._web_drive_engaged = True
            self._web_last_activity = time.time()
        self._tick_web_teleop_status()
        return True

    def publish_teleop_pan(self, angle: float) -> bool:
        """Publish to /teleop/camera/pan and auto-engage web camera."""
        if not self._teleop_pan_pub:
            return False
        msg = Float64()
        msg.data = float(angle)
        self._teleop_pan_pub.publish(msg)
        with self._web_state_lock:
            self._web_camera_engaged = True
            self._web_last_activity = time.time()
        self._tick_web_teleop_status()
        return True

    def publish_teleop_tilt(self, angle: float) -> bool:
        """Publish to /teleop/camera/tilt and auto-engage web camera."""
        if not self._teleop_tilt_pub:
            return False
        msg = Float64()
        msg.data = float(angle)
        self._teleop_tilt_pub.publish(msg)
        with self._web_state_lock:
            self._web_camera_engaged = True
            self._web_last_activity = time.time()
        self._tick_web_teleop_status()
        return True

    def engage_web_teleop(
        self,
        drive: Optional[bool] = None,
        camera: Optional[bool] = None,
        estop: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """Manually claim/release individual web teleop locks."""
        with self._web_state_lock:
            if drive is not None:
                self._web_drive_engaged = bool(drive)
            if camera is not None:
                self._web_camera_engaged = bool(camera)
            if estop is not None:
                self._web_estop_latched = bool(estop)
            self._web_last_activity = time.time()
            snap = {
                'drive_engaged': self._web_drive_engaged,
                'camera_engaged': self._web_camera_engaged,
                'estop_latched': self._web_estop_latched,
            }
        self._tick_web_teleop_status()
        return snap

    def release_web_teleop(self) -> Dict[str, Any]:
        """Release all web teleop locks immediately."""
        with self._web_state_lock:
            self._web_drive_engaged = False
            self._web_camera_engaged = False
            self._web_estop_latched = False
            self._web_last_activity = time.time()
        # Emit one release frame so consumers see drive/camera=false.
        self._tick_web_teleop_status()
        return {
            'drive_engaged': False,
            'camera_engaged': False,
            'estop_latched': False,
        }

    def get_web_teleop_state(self) -> Dict[str, Any]:
        """Snapshot of the local web teleop state (what we are publishing)."""
        with self._web_state_lock:
            now = time.time()
            return {
                'drive_engaged': self._web_drive_engaged,
                'camera_engaged': self._web_camera_engaged,
                'estop_latched': self._web_estop_latched,
                'timeout_sec': self._web_teleop_timeout_sec,
                'idle_sec': (now - self._web_last_activity) if self._web_last_activity else None,
            }

    # ========================================================================
    # Audio Publisher
    # ========================================================================
    
    def publish_audio(
        self,
        data: bytes = b'',
        channel: str = "default",
        format: str = "",
        sample_rate: int = 16000,
        audio_channels: int = 1,
        bits_per_sample: int = 16,
        volume: float = 1.0,
        stop: bool = False,
        stop_all: bool = False,
    ) -> Dict[str, Any]:
        """Publish audio data to /audio topic.
        
        Args:
            data: Audio data (PCM or encoded)
            channel: Channel name
            format: Data format ("" or "pcm" for raw PCM, "mp3", "wav", etc.)
            sample_rate: Sample rate for PCM
            audio_channels: 1=mono, 2=stereo
            bits_per_sample: 8, 16, 24, 32
            volume: Volume 0.0 ~ 1.0
            stop: Stop this channel
            stop_all: Stop all channels
        """
        if not HAS_AUDIO_INTERFACES or not self._audio_pub:
            return {'success': False, 'message': 'Audio publisher not available'}
        
        msg = Audio()
        msg.channel = channel
        msg.data = list(data) if data else []
        msg.format = format
        msg.sample_rate = sample_rate
        msg.audio_channels = audio_channels
        msg.bits_per_sample = bits_per_sample
        msg.volume = float(volume)
        msg.stop = stop
        msg.stop_all = stop_all
        
        self._audio_pub.publish(msg)
        
        if stop_all:
            return {'success': True, 'message': 'Stop all signal sent'}
        elif stop:
            return {'success': True, 'message': f'Stop signal sent to channel: {channel}'}
        else:
            return {'success': True, 'message': 'Audio data published'}
    
    # ========================================================================
    # State Getters
    # ========================================================================
    
    def get_last_cmd_vel(self) -> Dict[str, float]:
        return self._last_cmd_vel.copy()
    
    def get_pan(self) -> float:
        return self._last_pan
    
    def get_tilt(self) -> float:
        return self._last_tilt
    
    # ========================================================================
    # Service Helpers
    # ========================================================================
    
    def _call_service_sync(self, client, request, timeout: float = 5.0):
        """Call a service synchronously (for use in executor).
        
        Note: Don't call spin_once here - the background _spin_loop thread
        handles spinning. Just wait for the future to complete.
        """
        if not client.wait_for_service(timeout_sec=2.0):
            return None
        
        future = client.call_async(request)
        
        # Wait for future completion - spin is handled by _spin_loop thread
        start = time.time()
        while not future.done():
            if time.time() - start > timeout:
                return None
            time.sleep(0.05)  # Small sleep to avoid busy waiting
        
        return future.result()
    
    # ========================================================================
    # Calibration Services
    # ========================================================================
    
    async def get_calibration(self) -> Dict[str, Any]:
        """Get current calibration values."""
        if not HAS_CALIBRATION_SERVICES or not self._get_calibration_client:
            raise Exception("Calibration services not available")
        
        request = GetCalibration.Request()
        
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            self._executor,
            self._call_service_sync,
            self._get_calibration_client,
            request
        )
        
        if response is None:
            raise Exception("Service call failed or timeout")
        
        return {
            'success': response.success,
            'message': response.message,
            'max_steering': response.max_steering,
            'max_speed': response.max_speed,
            'max_pan': response.max_pan,
            'max_tilt': response.max_tilt,
            'steering_center': response.steering_center,
            'pan_center': response.pan_center,
            'tilt_center': response.tilt_center,
            'reverse_direction': response.reverse_direction,
            'source': response.source,
        }
    
    async def set_calibration(
        self,
        channel: str,
        max_value: Optional[float] = None,
        center_value: Optional[float] = None,
        bool_value: Optional[bool] = None,
        save: bool = False,
    ) -> Dict[str, Any]:
        """Set calibration values for a specific channel.
        
        Args:
            channel: 'steering', 'speed', 'pan', 'tilt', 'reverse', or 'all'
            max_value: max limit (degrees or m/s for speed)
            center_value: center offset (degrees)
            bool_value: for reverse_direction
            save: save to calibration.json after applying
        """
        if not HAS_CALIBRATION_SERVICES or not self._set_calibration_client:
            raise Exception("Calibration services not available")
        
        request = SetCalibration.Request()
        request.channel = channel
        if max_value is not None:
            request.max_value = float(max_value)
        if center_value is not None:
            request.center_value = float(center_value)
        if bool_value is not None:
            request.bool_value = bool_value
        request.save_to_file = save
        
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            self._executor,
            self._call_service_sync,
            self._set_calibration_client,
            request
        )
        
        if response is None:
            raise Exception("Service call failed or timeout")
        
        return {
            'success': response.success,
            'message': response.message,
        }
    
    # ========================================================================
    # Joy Teleop Mapping Services
    # ========================================================================

    async def get_joy_mapping(self) -> Dict[str, Any]:
        """Get current joystick teleop mapping."""
        if not HAS_JOY_MAPPING_SERVICES or not self._get_joy_mapping_client:
            raise Exception("Joy mapping services not available")

        request = GetJoyMapping.Request()

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            self._executor,
            self._call_service_sync,
            self._get_joy_mapping_client,
            request,
        )

        if response is None:
            raise Exception("Service call failed or timeout")

        import json as _json
        try:
            mapping = _json.loads(response.mapping_json) if response.mapping_json else {}
        except _json.JSONDecodeError:
            mapping = {}

        return {
            'success': response.success,
            'message': response.message,
            'source': response.source,
            'mapping': mapping,
        }

    async def set_joy_mapping(
        self,
        key: str = '',
        int_value: int = 0,
        float_value: float = 0.0,
        bool_value: bool = False,
        mapping_json: str = '',
        save: bool = False,
    ) -> Dict[str, Any]:
        """Set joystick teleop mapping (single key or bulk JSON)."""
        if not HAS_JOY_MAPPING_SERVICES or not self._set_joy_mapping_client:
            raise Exception("Joy mapping services not available")

        request = SetJoyMapping.Request()
        request.key = key
        request.int_value = int(int_value)
        request.float_value = float(float_value)
        request.bool_value = bool(bool_value)
        request.mapping_json = mapping_json
        request.save_to_file = save

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            self._executor,
            self._call_service_sync,
            self._set_joy_mapping_client,
            request,
        )

        if response is None:
            raise Exception("Service call failed or timeout")

        import json as _json
        try:
            mapping = _json.loads(response.mapping_json) if response.mapping_json else {}
        except _json.JSONDecodeError:
            mapping = {}

        return {
            'success': response.success,
            'message': response.message,
            'mapping': mapping,
        }

    async def set_joy_enabled(self, enabled: bool) -> Dict[str, Any]:
        """Toggle joy_teleop's `enabled` parameter via /set_parameters.

        When false, joy_teleop publishes nothing on /speed,/steering,/camera/*
        and reports drive_engaged/camera_engaged=false in its status.  Other
        publishers (deepracer, REST /control) regain the topics immediately.
        """
        if not HAS_RCL_PARAM_SERVICES or not self._joy_set_params_client:
            raise Exception("Joy parameter service not available")

        request = SetParameters.Request()
        param = Parameter()
        param.name = 'enabled'
        param.value = ParameterValue(type=ParameterType.PARAMETER_BOOL, bool_value=bool(enabled))
        request.parameters = [param]

        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            self._executor,
            self._call_service_sync,
            self._joy_set_params_client,
            request,
        )

        if response is None:
            raise Exception("Service call failed or timeout")
        if not response.results or not response.results[0].successful:
            reason = response.results[0].reason if response.results else 'unknown'
            return {'success': False, 'message': reason, 'enabled': None}

        return {'success': True, 'message': 'ok', 'enabled': bool(enabled)}

    # ========================================================================
    # DeepRacer Services
    # ========================================================================
    
    async def deepracer_load_model(self, model_name: str) -> Dict[str, Any]:
        """Load a DeepRacer model by name."""
        if not HAS_DEEPRACER_INTERFACES or not self._deepracer_load_model_client:
            raise Exception("DeepRacer services not available")
        
        request = DeepracerLoadModel.Request()
        request.model_name = model_name
        
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            self._executor,
            self._call_service_sync,
            self._deepracer_load_model_client,
            request,
            30.0  # Longer timeout for model loading
        )
        
        if response is None:
            raise Exception("Service call failed or timeout")
        
        return {
            'success': response.success,
            'message': response.message,
            'model_path': response.model_path,
            'action_space_json': response.action_space_json,
        }
    
    async def deepracer_unload_model(self, model_name: str = "") -> Dict[str, Any]:
        """Unload a DeepRacer model from memory. Empty name = unload all."""
        if not HAS_DEEPRACER_INTERFACES or not self._deepracer_unload_model_client:
            raise Exception("DeepRacer services not available")
        
        request = DeepracerUnloadModel.Request()
        request.model_name = model_name
        
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            self._executor,
            self._call_service_sync,
            self._deepracer_unload_model_client,
            request
        )
        
        if response is None:
            raise Exception("Service call failed or timeout")
        
        return {
            'success': response.success,
            'message': response.message,
        }
    
    async def deepracer_control(self, start: bool) -> Dict[str, Any]:
        """Start/stop DeepRacer inference."""
        if not HAS_DEEPRACER_INTERFACES or not self._deepracer_control_client:
            raise Exception("DeepRacer services not available")
        
        request = DeepracerControl.Request()
        request.start = start
        
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            self._executor,
            self._call_service_sync,
            self._deepracer_control_client,
            request
        )
        
        if response is None:
            raise Exception("Service call failed or timeout")
        
        return {
            'success': response.success,
            'message': response.message,
        }
    
    async def deepracer_status(self) -> Dict[str, Any]:
        """Get DeepRacer status."""
        if not HAS_DEEPRACER_INTERFACES or not self._deepracer_status_client:
            raise Exception("DeepRacer services not available")
        
        request = DeepracerStatus.Request()
        
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            self._executor,
            self._call_service_sync,
            self._deepracer_status_client,
            request
        )
        
        if response is None:
            raise Exception("Service call failed or timeout")
        
        return {
            'success': True,
            'model_loaded': response.model_loaded,
            'inference_running': response.inference_running,
            'model_name': response.model_name,
            'model_path': response.model_path,
            'action_count': response.action_count,
            'action_space_json': response.action_space_json,
            'loaded_models_json': response.loaded_models_json,
            'inference_rate': response.inference_rate,
            'inference_count': response.inference_count,
            'last_action': response.last_action,
            # Configuration
            'action_selection_mode': response.action_selection_mode,
            'config_source': response.config_source,
            'camera_pan': response.camera_pan,
            'camera_tilt': response.camera_tilt,
            'speed_percent': response.speed_percent,
        }
    
    async def deepracer_set_config(
        self,
        key: str,
        string_value: str = "",
        float_value: float = 0.0,
        save_to_file: bool = False,
    ) -> Dict[str, Any]:
        """Set DeepRacer configuration.
        
        Args:
            key: 'action_selection', 'pan', 'tilt', or 'all'
            string_value: For 'action_selection': 'greedy' or 'stochastic'
            float_value: For 'pan'/'tilt' in degrees
            save_to_file: Save config to file after applying
        """
        if not HAS_DEEPRACER_INTERFACES or not self._deepracer_set_config_client:
            raise Exception("DeepRacer services not available")
        
        request = DeepracerSetConfig.Request()
        request.key = key
        request.string_value = string_value
        request.float_value = float_value
        request.save_to_file = save_to_file
        
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            self._executor,
            self._call_service_sync,
            self._deepracer_set_config_client,
            request
        )
        
        if response is None:
            raise Exception("Service call failed or timeout")
        
        return {
            'success': response.success,
            'message': response.message,
        }


# Global instance getter
_ros_bridge: Optional[ROSBridge] = None

def get_ros_bridge() -> ROSBridge:
    """Get the global ROSBridge instance."""
    global _ros_bridge
    if _ros_bridge is None:
        _ros_bridge = ROSBridge()
    return _ros_bridge


def init_ros_bridge() -> bool:
    """Initialize the global ROSBridge instance."""
    bridge = get_ros_bridge()
    return bridge.init()


def shutdown_ros_bridge():
    """Shutdown the global ROSBridge instance."""
    global _ros_bridge
    if _ros_bridge:
        _ros_bridge.shutdown()
        _ros_bridge = None
