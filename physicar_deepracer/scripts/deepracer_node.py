#!/usr/bin/env python3
"""
PhysiCar DeepRacer Node

ROS 2 node that provides DeepRacer inference for autonomous driving.

Services:
    - deepracer/load_model: Load a model from /opt/physicar/userdata/deepracer/models/
    - deepracer/control: Start/stop inference
    - deepracer/status: Get current status
    - deepracer/set_config: Set runtime configuration

Subscriptions:
    - /camera/image_raw/compressed: Camera images
    - /scan: LiDAR scans

Publications:
    - /speed: Speed command (m/s) when inference is running
    - /steering: Steering command (radians) when inference is running

Configuration:
    - 1st priority: /opt/physicar/userdata/deepracer/config.json
    - 2nd priority: ROS parameters (default values)
"""

import json
import math
import os
import time
from dataclasses import dataclass, asdict
from threading import Lock
from typing import Optional

import rclpy
from rclpy.node import Node
from rclpy.callback_groups import MutuallyExclusiveCallbackGroup, ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.qos import QoSProfile, ReliabilityPolicy, HistoryPolicy
from rclpy.time import Time

import numpy as np
from cv_bridge import CvBridge

from sensor_msgs.msg import CompressedImage, LaserScan
from std_msgs.msg import Float64, Header

from physicar_interfaces.msg import DeepracerInference
from physicar_interfaces.srv import (
    DeepracerLoadModel,
    DeepracerUnloadModel,
    DeepracerControl,
    DeepracerStatus,
    DeepracerSetConfig
)

from physicar_deepracer import constants
from physicar_deepracer.model_loader import ModelLoader, ModelInfo
from physicar_deepracer.inference_engine import InferenceEngine, ActionSelectionMode


@dataclass
class DeepracerConfig:
    """Runtime configuration for DeepRacer node"""
    action_selection: str = "greedy"  # 'greedy', 'stochastic', or 'mean'
    pan: float = 0.0   # Camera pan angle (degrees)
    tilt: float = 0.0  # Camera tilt angle (degrees)
    speed_percent: float = 100.0  # Speed multiplier percent (50 ~ 150)
    source: str = "default"  # 'default', 'json_file', 'ros_params'
    
    def to_dict(self) -> dict:
        return {
            "action_selection": self.action_selection,
            "pan": self.pan,
            "tilt": self.tilt,
            "speed_percent": self.speed_percent,
        }
    
    @classmethod
    def from_dict(cls, data: dict, source: str = "unknown") -> "DeepracerConfig":
        """Build from dict with safe coercion and bounds.

        Bad values silently fall back to defaults — the only contract is
        "we never crash on user-supplied JSON". Bounds:
          - action_selection ∈ {greedy, stochastic, mean}
          - pan/tilt: ±90 deg
          - speed_percent: 0–200
        """
        defaults = constants.DEFAULT_CONFIG

        def _f(key, default, lo, hi):
            v = data.get(key, default)
            try:
                v = float(v)
            except (TypeError, ValueError):
                return default
            return max(lo, min(hi, v))

        action = data.get("action_selection", defaults["action_selection"])
        if action not in ("greedy", "stochastic", "mean"):
            action = defaults["action_selection"]

        return cls(
            action_selection=action,
            pan=_f("pan", defaults["pan"], -90.0, 90.0),
            tilt=_f("tilt", defaults["tilt"], -90.0, 90.0),
            speed_percent=_f("speed_percent", defaults["speed_percent"], 0.0, 200.0),
            source=source,
        )


class DeepracerNode(Node):
    """
    PhysiCar DeepRacer inference node
    """
    
    def __init__(self):
        super().__init__('deepracer')
        
        self.get_logger().info("DeepRacer node starting...")
        
        # Components
        self.model_loader = ModelLoader(logger=self.get_logger())
        self.inference_engine = InferenceEngine(logger=self.get_logger())
        self.cv_bridge = CvBridge()
        
        # Configuration
        self.config: DeepracerConfig = None
        
        # State
        self._inference_running = False
        self._loaded_models: dict[str, ModelInfo] = {}  # name → ModelInfo
        self._lock = Lock()
        
        # Latest sensor data
        self._latest_image = None
        self._latest_scan = None
        self._image_stamp = None
        self._scan_stamp = None
        
        # Timing
        self._last_inference_time = 0.0
        self._inference_interval = 1.0 / constants.MAX_INFERENCE_RATE_HZ
        self._inference_rate_actual = 0.0
        
        # Sensor timeout tracking (for auto-stop on missing data)
        self._image_timeout = 2.0  # seconds - stop if no image for this long
        self._lidar_timeout = 5.0  # seconds - use fallback if no lidar for this long
        self._image_missing_logged = False
        self._lidar_missing_logged = False
        
        # Callback groups for concurrent execution
        self._service_cb_group = MutuallyExclusiveCallbackGroup()
        self._sensor_cb_group = ReentrantCallbackGroup()
        
        # Parameters (fallback values, JSON file takes priority)
        self.declare_parameter('action_selection', constants.DEFAULT_CONFIG['action_selection'])
        self.declare_parameter('pan', float(constants.DEFAULT_CONFIG['pan']))
        self.declare_parameter('tilt', float(constants.DEFAULT_CONFIG['tilt']))
        
        # Load configuration (JSON file > ROS params > defaults)
        self._load_config()
        self._apply_config()
        
        # Camera pan/tilt publishers
        self._pan_pub = self.create_publisher(Float64, '/camera/pan', 10)
        self._tilt_pub = self.create_publisher(Float64, '/camera/tilt', 10)
        
        # Services
        self._load_model_srv = self.create_service(
            DeepracerLoadModel,
            'deepracer/load_model',
            self._load_model_callback,
            callback_group=self._service_cb_group
        )
        
        self._unload_model_srv = self.create_service(
            DeepracerUnloadModel,
            'deepracer/unload_model',
            self._unload_model_callback,
            callback_group=self._service_cb_group
        )
        
        self._control_srv = self.create_service(
            DeepracerControl,
            'deepracer/control',
            self._control_callback,
            callback_group=self._service_cb_group
        )
        
        self._status_srv = self.create_service(
            DeepracerStatus,
            'deepracer/status',
            self._status_callback,
            callback_group=self._service_cb_group
        )
        
        self._set_config_srv = self.create_service(
            DeepracerSetConfig,
            'deepracer/set_config',
            self._set_config_callback,
            callback_group=self._service_cb_group
        )
        
        # QoS for sensor topics (match camera/lidar publishers which use BEST_EFFORT)
        sensor_qos = QoSProfile(
            reliability=ReliabilityPolicy.BEST_EFFORT,
            history=HistoryPolicy.KEEP_LAST,
            depth=1
        )
        
        # Subscribers (compressed image for efficiency)
        self._image_sub = self.create_subscription(
            CompressedImage,
            '/camera/image_raw/compressed',
            self._image_callback,
            sensor_qos,
            callback_group=self._sensor_cb_group
        )
        
        self._scan_sub = self.create_subscription(
            LaserScan,
            '/scan',
            self._scan_callback,
            sensor_qos,
            callback_group=self._sensor_cb_group
        )
        
        # Publishers - Low-level control
        self._speed_pub = self.create_publisher(Float64, '/speed', 10)
        self._steering_pub = self.create_publisher(Float64, '/steering', 10)
        
        # Publisher - Inference result
        self._inference_pub = self.create_publisher(DeepracerInference, '/deepracer/inference', 10)
        
        # Inference timer (runs at max rate when inference is enabled)
        self._inference_timer = self.create_timer(
            self._inference_interval,
            self._inference_loop,
            callback_group=self._sensor_cb_group
        )

        # Camera-hold timer: while inference is running, keep republishing the
        # configured pan/tilt at a low rate so the camera springs back to the
        # configured framing the moment any other publisher (joy_teleop
        # camera_engaged, REST /control/camera/{pan,tilt}, agent) lets go.  5Hz is more
        # than enough — servo I2C writes are cheap but not free.
        self._camera_hold_timer = self.create_timer(
            0.2,  # 5Hz
            self._camera_hold_tick,
            callback_group=self._sensor_cb_group,
        )
        
        self.get_logger().info("DeepRacer node ready")
        self.get_logger().info(f"Models path: {constants.MODELS_BASE_PATH}")
        self.get_logger().info(f"Available models: {self.model_loader.list_models()}")
    
    def _load_model_callback(self, request, response):
        """Handle load_model service request.
        
        Loads model into memory pool and sets as active.
        If already loaded, just switches active (instant).
        Does NOT stop running inference — the active model switches seamlessly.
        """
        self.get_logger().info(f"Loading model: {request.model_name}")
        
        try:
            with self._lock:
                # Load model (ModelLoader handles validation + TFLite conversion)
                success, message, model_info = self.model_loader.load_model(request.model_name)
                
                if not success:
                    response.success = False
                    response.message = message
                    response.model_path = ""
                    response.action_space_json = "[]"
                    return response
                
                # Load into inference engine (adds to pool + sets active)
                success, message = self.inference_engine.load_model(model_info)
                
                if not success:
                    response.success = False
                    response.message = message
                    response.model_path = ""
                    response.action_space_json = "[]"
                    return response
                
                self._loaded_models[model_info.name] = model_info
                
                response.success = True
                response.message = message
                response.model_path = model_info.tflite_path
                # Action space as JSON array
                action_space_list = [
                    {"speed": a.speed, "steering_angle": a.steering_angle}
                    for a in model_info.action_space
                ]
                response.action_space_json = json.dumps(action_space_list)
                
                self.get_logger().info(f"Loaded models: {list(self._loaded_models.keys())}")
        except Exception as e:
            self.get_logger().error(f"Exception in load_model: {e}")
            response.success = False
            response.message = f"Internal error: {str(e)}"
            response.model_path = ""
            response.action_space_json = "[]"
        
        return response
    
    def _unload_model_callback(self, request, response):
        """Handle unload_model service request.
        
        Removes model from memory. Empty name = unload all.
        Stops inference if the active model is unloaded.
        """
        name = request.model_name
        self.get_logger().info(f"Unloading model: {name or '(all)'}")
        
        try:
            with self._lock:
                # Stop inference if unloading active model or all models
                active = self.inference_engine.active_model_name
                if not name or name == active:
                    if self._inference_running:
                        self._inference_running = False
                        self._speed_pub.publish(Float64(data=0.0))
                        self._steering_pub.publish(Float64(data=0.0))
                        self.get_logger().info("Stopped inference (active model unloaded)")
                
                success, message = self.inference_engine.unload_model(name)
                
                if success:
                    if not name:
                        self._loaded_models.clear()
                    else:
                        self._loaded_models.pop(name, None)
                
                response.success = success
                response.message = message
        except Exception as e:
            self.get_logger().error(f"Exception in unload_model: {e}")
            response.success = False
            response.message = f"Internal error: {str(e)}"
        
        return response
    
    def _control_callback(self, request, response):
        """Handle control service request (start/stop inference)"""
        with self._lock:
            if request.start:
                # Start inference
                if not self.inference_engine.is_loaded:
                    response.success = False
                    response.message = "No model loaded. Call load_model first."
                    return response
                
                # Set camera to configured position before starting
                self._set_camera_position()
                
                self._inference_running = True
                response.success = True
                response.message = "Inference started"
                self.get_logger().info("Inference started")
            else:
                # Stop inference
                self._inference_running = False
                
                # Send stop command
                self._speed_pub.publish(Float64(data=0.0))
                self._steering_pub.publish(Float64(data=0.0))
                
                # Reset camera to neutral position
                self._reset_camera_position()
                
                response.success = True
                response.message = "Inference stopped"
                self.get_logger().info("Inference stopped")
        
        return response
    
    def _status_callback(self, request, response):
        """Handle status service request"""
        with self._lock:
            response.model_loaded = self.inference_engine.is_loaded
            response.inference_running = self._inference_running
            
            # Active model info
            active_info = self.inference_engine.active_model_info
            if active_info:
                response.model_name = active_info.name
                response.model_path = active_info.tflite_path
                response.action_count = len(active_info.action_space)
                action_space_list = [
                    {"speed": a.speed, "steering_angle": a.steering_angle}
                    for a in active_info.action_space
                ]
                response.action_space_json = json.dumps(action_space_list)
            else:
                response.model_name = ""
                response.model_path = ""
                response.action_count = 0
                response.action_space_json = "[]"
            
            # All loaded models
            response.loaded_models_json = json.dumps(self.inference_engine.loaded_model_names)
            
            response.inference_rate = self._inference_rate_actual
            response.inference_count = self.inference_engine.inference_count
            
            if self.inference_engine.last_result:
                result = self.inference_engine.last_result
                last_action_obj = {
                    "index": result.action_index,
                    "speed": result.speed,
                    "steering_angle": result.steering_angle
                }
                response.last_action = json.dumps(last_action_obj)
            else:
                response.last_action = ""
            
            # Configuration fields
            response.action_selection_mode = self.config.action_selection
            response.config_source = self.config.source
            response.camera_pan = self.config.pan
            response.camera_tilt = self.config.tilt
            response.speed_percent = self.config.speed_percent
        
        return response
    
    def _set_config_callback(self, request, response):
        """Handle set_config service request - change runtime configuration"""
        key = request.key.lower()
        
        with self._lock:
            if key == 'all':
                # Reload from file
                self._load_config()
                self._apply_config()
                response.success = True
                response.message = f"Config reloaded from {self.config.source}"
                
            elif key == 'action_selection':
                value = request.string_value.lower()
                if value not in ('greedy', 'stochastic', 'mean'):
                    response.success = False
                    response.message = f"Invalid action_selection: '{value}'. Use 'greedy', 'stochastic', or 'mean'"
                    return response
                self.config.action_selection = value
                self._apply_config()
                response.success = True
                response.message = f"action_selection set to '{value}'"
                
            elif key == 'pan':
                value = request.float_value
                if not -30.0 <= value <= 30.0:
                    response.success = False
                    response.message = f"pan {value} out of range [-30.0, 30.0]"
                    return response
                self.config.pan = value
                response.success = True
                response.message = f"pan set to {value}°"
                
            elif key == 'tilt':
                value = request.float_value
                if not -30.0 <= value <= 30.0:
                    response.success = False
                    response.message = f"tilt {value} out of range [-30.0, 30.0]"
                    return response
                self.config.tilt = value
                response.success = True
                response.message = f"tilt set to {value}°"
                
            elif key == 'speed_percent':
                value = request.float_value
                if not 50.0 <= value <= 150.0:
                    response.success = False
                    response.message = f"speed_percent {value} out of range [50, 150]"
                    return response
                self.config.speed_percent = value
                response.success = True
                response.message = f"speed_percent set to {int(value)}%"
                
            else:
                response.success = False
                response.message = f"Unknown key: '{key}'. Valid keys: action_selection, pan, tilt, speed_percent, all"
                return response
            
            # Save to file if requested
            if request.save_to_file:
                if self._save_config():
                    response.message += f" (saved to {constants.CONFIG_FILE_PATH})"
                else:
                    response.message += " (save failed)"
        
        self.get_logger().info(response.message)
        return response
    
    def _load_config(self):
        """Load configuration from JSON file or ROS parameters.
        
        Priority: 1) JSON file  2) ROS params  3) defaults
        """
        config_file = constants.CONFIG_FILE_PATH
        
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r') as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ValueError('config file is not a JSON object')
                self.config = DeepracerConfig.from_dict(data, source='json_file')
                self.get_logger().info(f"Loaded config from {config_file}")
            except Exception as e:
                # Bad file is left alone — next valid save from the UI
                # overwrites it.
                self.get_logger().error(
                    f"Failed to load {config_file} ({e}); using ROS params")
                self.config = self._config_from_params()
        else:
            self.get_logger().info(f"Config file not found: {config_file}, using ROS parameters")
            self.config = self._config_from_params()
    
    def _config_from_params(self) -> DeepracerConfig:
        """Create DeepracerConfig from ROS parameters."""
        return DeepracerConfig(
            action_selection=self.get_parameter('action_selection').value,
            pan=self.get_parameter('pan').value,
            tilt=self.get_parameter('tilt').value,
            source='ros_params',
        )
    
    def _apply_config(self):
        """Apply current configuration to inference engine."""
        if self.config.action_selection == 'stochastic':
            self.inference_engine.action_selection_mode = ActionSelectionMode.STOCHASTIC
        elif self.config.action_selection == 'mean':
            self.inference_engine.action_selection_mode = ActionSelectionMode.MEAN
        else:
            self.inference_engine.action_selection_mode = ActionSelectionMode.GREEDY
        
        self.get_logger().info(f"Config applied (source: {self.config.source}):")
        self.get_logger().info(f"  action_selection: {self.config.action_selection}")
        self.get_logger().info(f"  pan: {self.config.pan}°")
        self.get_logger().info(f"  tilt: {self.config.tilt}°")
        self.get_logger().info(f"  speed_percent: {self.config.speed_percent}")
    
    def _set_camera_position(self):
        """Set camera pan/tilt to configured position (called when inference starts)."""
        # Convert degrees (config) to radians (topic)
        pan_msg = Float64()
        pan_msg.data = math.radians(self.config.pan)
        self._pan_pub.publish(pan_msg)
        
        tilt_msg = Float64()
        tilt_msg.data = math.radians(self.config.tilt)
        self._tilt_pub.publish(tilt_msg)
        
        self.get_logger().info(f"Camera position set: pan={self.config.pan}° ({pan_msg.data:.3f} rad), tilt={self.config.tilt}° ({tilt_msg.data:.3f} rad)")

    def _reset_camera_position(self):
        """Reset camera pan/tilt to neutral (0°, 0°) when inference stops."""
        self._pan_pub.publish(Float64(data=0.0))
        self._tilt_pub.publish(Float64(data=0.0))
        self.get_logger().info("Camera position reset to neutral (0°, 0°)")

    def _camera_hold_tick(self):
        """Re-publish configured pan/tilt at 5Hz while inference is running.

        Cheap (two Float64s) and lets the camera auto-return to the configured
        framing the moment another publisher (joy camera_engaged release, REST,
        agent) stops contesting the topic."""
        if not self._inference_running:
            return
        pan_msg = Float64()
        pan_msg.data = math.radians(self.config.pan)
        self._pan_pub.publish(pan_msg)
        tilt_msg = Float64()
        tilt_msg.data = math.radians(self.config.tilt)
        self._tilt_pub.publish(tilt_msg)

    def _save_config(self) -> bool:
        """Save current configuration to JSON file."""
        try:
            config_dir = os.path.dirname(constants.CONFIG_FILE_PATH)
            os.makedirs(config_dir, exist_ok=True)
            
            with open(constants.CONFIG_FILE_PATH, 'w') as f:
                json.dump(self.config.to_dict(), f, indent=4)
            
            self.config.source = 'json_file'
            self.get_logger().info(f"Config saved to {constants.CONFIG_FILE_PATH}")
            return True
        except Exception as e:
            self.get_logger().error(f"Failed to save config: {e}")
            return False
    
    def _image_callback(self, msg: CompressedImage):
        """Handle incoming camera image"""
        if self._inference_running:
            try:
                # Decode compressed image only when inference is active
                self._latest_image = self.cv_bridge.compressed_imgmsg_to_cv2(msg, "bgr8")
            except Exception as e:
                self.get_logger().error(f"Image decode error: {e}")
                return
        # Always update stamp (used for freshness check)
        self._image_stamp = Time.from_msg(msg.header.stamp)
    
    def _scan_callback(self, msg: LaserScan):
        """Handle incoming LiDAR scan"""
        self._latest_scan = msg
        # Convert to rclpy.time.Time for proper time arithmetic
        self._scan_stamp = Time.from_msg(msg.header.stamp)
    
    def _inference_loop(self):
        """Main inference loop - runs at fixed rate (timer-based)"""
        if not self._inference_running:
            return
        
        current_time = time.time()
        
        # Check image availability (critical - auto-stop if missing)
        if self._latest_image is None or self._image_stamp is None:
            if not self._image_missing_logged:
                self.get_logger().warn("No camera image received yet, waiting...")
                self._image_missing_logged = True
            return
        
        # Check image freshness (auto-stop if stale)
        image_age = (self.get_clock().now() - self._image_stamp).nanoseconds / 1e9
        if image_age > self._image_timeout:
            if self._inference_running:
                self.get_logger().error(f"Camera image timeout ({image_age:.1f}s > {self._image_timeout}s). Auto-stopping inference.")
                self._inference_running = False
                # Send stop command
                self._speed_pub.publish(Float64(data=0.0))
                self._steering_pub.publish(Float64(data=0.0))
            return
        
        self._image_missing_logged = False
        
        # Check LiDAR availability (only if model uses LiDAR)
        active_info = self.inference_engine.active_model_info
        model_has_lidar = active_info and active_info.has_lidar
        use_fallback_lidar = False
        
        if model_has_lidar:
            if self._latest_scan is None or self._scan_stamp is None:
                use_fallback_lidar = True
                if not self._lidar_missing_logged:
                    self.get_logger().warn("No LiDAR data received, using fallback (max distance)")
                    self._lidar_missing_logged = True
            else:
                # Check LiDAR freshness
                lidar_age = (self.get_clock().now() - self._scan_stamp).nanoseconds / 1e9
                if lidar_age > self._lidar_timeout:
                    use_fallback_lidar = True
                    if not self._lidar_missing_logged:
                        self.get_logger().warn(f"LiDAR data stale ({lidar_age:.1f}s), using fallback")
                        self._lidar_missing_logged = True
                else:
                    self._lidar_missing_logged = False
        
        with self._lock:
            if not self._inference_running:
                return
            
            try:
                if not model_has_lidar:
                    # Camera-only model: no LiDAR input needed
                    image_input = self.inference_engine.preprocess_image(self._latest_image)
                    result = self.inference_engine.run_inference(image_input)
                elif use_fallback_lidar:
                    # Camera+LiDAR model, but LiDAR missing: use fallback
                    lidar_input = np.full((1, constants.LIDAR_SECTORS), constants.LIDAR_MAX_DIST, dtype=np.float32)
                    image_input = self.inference_engine.preprocess_image(self._latest_image)
                    result = self.inference_engine.run_inference(image_input, lidar_input)
                else:
                    # Camera+LiDAR model, normal path
                    result = self.inference_engine.infer_from_raw(
                        self._latest_image,
                        np.array(self._latest_scan.ranges),
                        self._latest_scan.angle_min,
                        self._latest_scan.angle_increment
                    )
                
                if result is None:
                    return
                
                # Publish to low-level control topics directly
                # speed: m/s (from action space), multiplied by speed_percent
                # steering_angle: radians (converted from degrees in action space)
                # No additional conversion needed - driver_node handles calibration, limits, ESC model
                
                speed_msg = Float64()
                speed_msg.data = result.speed * (self.config.speed_percent / 100.0)
                self._speed_pub.publish(speed_msg)
                
                steering_msg = Float64()
                steering_msg.data = math.radians(result.steering_angle)  # degrees → radians
                self._steering_pub.publish(steering_msg)
                
                # Publish inference result
                inference_msg = DeepracerInference()
                inference_msg.header.stamp = self.get_clock().now().to_msg()
                inference_msg.speed = result.speed
                inference_msg.steering_angle = result.steering_angle
                inference_msg.probabilities = result.probabilities.tolist()
                self._inference_pub.publish(inference_msg)
                
                # Update timing
                elapsed = current_time - self._last_inference_time
                if elapsed > 0:
                    self._inference_rate_actual = 1.0 / elapsed
                self._last_inference_time = current_time
                
            except Exception as e:
                self.get_logger().error(f"Inference loop error: {e}")


def main(args=None):
    rclpy.init(args=args)
    
    try:
        node = DeepracerNode()
        
        # Use multi-threaded executor for concurrent callbacks
        executor = MultiThreadedExecutor(num_threads=4)
        executor.add_node(node)
        
        try:
            executor.spin()
        finally:
            executor.shutdown()
            node.destroy_node()
    
    except KeyboardInterrupt:
        pass
    finally:
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
