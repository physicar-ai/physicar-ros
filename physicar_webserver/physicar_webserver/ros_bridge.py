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

from physicar_webserver.sim import is_sim_mode

# Import calibration services
try:
    from physicar_interfaces.srv import SetCalibration, GetCalibration
    HAS_CALIBRATION_SERVICES = True
except ImportError:
    HAS_CALIBRATION_SERVICES = False

# Import joy teleop mapping services + status message
try:
    from physicar_interfaces.srv import GetJoyMapping, SetJoyMapping
    HAS_JOY_MAPPING_SERVICES = True
except ImportError:
    HAS_JOY_MAPPING_SERVICES = False

try:
    from physicar_interfaces.msg import JoyTeleopStatus
    HAS_JOY_STATUS_MSG = True
except ImportError:
    HAS_JOY_STATUS_MSG = False

# rcl_interfaces SetParameters — used to toggle joy_teleop's `enabled` flag
try:
    from rcl_interfaces.srv import SetParameters
    from rcl_interfaces.msg import Parameter, ParameterValue, ParameterType
    HAS_RCL_PARAM_SERVICES = True
except ImportError:
    HAS_RCL_PARAM_SERVICES = False


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
        
        # Joy teleop service clients + cached status (joy-only fields)
        self._get_joy_mapping_client = None
        self._set_joy_mapping_client = None
        self._joy_set_params_client = None
        self._joy_status: Dict[str, Any] = {'enabled': False, 'received': False}

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
            
            # Service clients (calibration) — device only (no servo in SIM)
            if HAS_CALIBRATION_SERVICES and not is_sim_mode():
                self._get_calibration_client = self._node.create_client(
                    GetCalibration, '/physicar_driver/get_calibration'
                )
                self._set_calibration_client = self._node.create_client(
                    SetCalibration, '/physicar_driver/set_calibration'
                )
            
            # Joy teleop mapping service clients
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

            # Joy teleop status subscription — latched (TRANSIENT_LOCAL) so this
            # server learns the `enabled` value immediately on (re)start.
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
    # Joy Teleop (mapping + enable toggle)
    # ========================================================================

    def _on_joy_status(self, msg) -> None:
        """Cache the latest JoyTeleopStatus (joy-only fields)."""
        self._joy_status = {
            'enabled': bool(msg.enabled),
            'received': True,
        }

    def get_joy_status(self) -> Dict[str, Any]:
        """Return the latest cached joy-specific status."""
        return dict(self._joy_status)

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

        When false, joy_teleop publishes nothing on the /teleop/* topics and
        reports drive_engaged/camera_engaged=false in TeleopStatus. Other
        publishers (REST control) regain the topics immediately.
        """
        if not HAS_RCL_PARAM_SERVICES or not self._joy_set_params_client:
            raise Exception("Joy parameter service not available")

        request = SetParameters.Request()
        param = Parameter()
        param.name = 'enabled'
        param.value = ParameterValue(
            type=ParameterType.PARAMETER_BOOL, bool_value=bool(enabled)
        )
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
            'speed_gain': response.speed_gain,
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
