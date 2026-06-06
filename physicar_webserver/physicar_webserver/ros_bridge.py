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
            
            # Service clients (calibration) — device only (no servo in SIM)
            if HAS_CALIBRATION_SERVICES and not is_sim_mode():
                self._get_calibration_client = self._node.create_client(
                    GetCalibration, '/physicar_driver/get_calibration'
                )
                self._set_calibration_client = self._node.create_client(
                    SetCalibration, '/physicar_driver/set_calibration'
                )

            # Audio publisher
            if HAS_AUDIO_INTERFACES:
                self._audio_pub = self._node.create_publisher(Audio, '/audio', qos)
            
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
