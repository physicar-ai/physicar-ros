"""
Model Loader for PhysiCar DeepRacer

Handles:
1. Model validation (metadata, file structure)
2. TensorFlow .pb to TFLite conversion
3. Action space parsing
"""

import os
import json
from typing import Dict, List, Tuple, Optional, Any
from dataclasses import dataclass

from . import constants


@dataclass
class ActionSpace:
    """Represents a single action in discrete action space"""
    index: int
    speed: float           # m/s
    steering_angle: float  # degrees


@dataclass
class ModelInfo:
    """Information about a loaded model"""
    name: str
    path: str
    metadata: Dict[str, Any]
    action_space: List[ActionSpace]
    tflite_path: str
    has_lidar: bool = True  # Whether the model uses LiDAR input
    

class ModelLoader:
    """
    Loads and validates DeepRacer models for PhysiCar
    
    Model structure:
        /opt/physicar/userdata/deepracer/models/<model_name>/
            ├── agent/model.pb         # TensorFlow frozen graph
            ├── model_metadata.json    # Model configuration
            └── ...
    """
    
    def __init__(self, logger=None):
        self.logger = logger
        self._current_model: Optional[ModelInfo] = None
        
        # Ensure directories exist
        os.makedirs(constants.MODELS_BASE_PATH, exist_ok=True)
    
    def _log(self, msg: str, level: str = "info"):
        if self.logger:
            try:
                getattr(self.logger, level)(msg)
            except (ValueError, RuntimeError):
                # Fallback to print if logger has issues
                print(f"[{level.upper()}] {msg}")
        else:
            print(f"[{level.upper()}] {msg}")
    
    def list_models(self) -> List[str]:
        """List available models in the models directory"""
        if not os.path.exists(constants.MODELS_BASE_PATH):
            return []
        
        models = []
        for name in os.listdir(constants.MODELS_BASE_PATH):
            model_dir = os.path.join(constants.MODELS_BASE_PATH, name)
            if os.path.isdir(model_dir):
                # Check for required files
                metadata_path = os.path.join(model_dir, constants.MODEL_METADATA_FILE)
                model_path = os.path.join(model_dir, constants.MODEL_FILE)
                if os.path.exists(metadata_path) and os.path.exists(model_path):
                    models.append(name)
        
        return models
    
    def validate_metadata(self, metadata: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Validate model metadata against PhysiCar requirements.
        
        Supported sensor configs:
          - ["FRONT_FACING_CAMERA"]             (camera only)
          - ["FRONT_FACING_CAMERA", "LIDAR"]    (camera + LiDAR)
        
        Returns:
            Tuple of (is_valid, error_message)
        """
        # Check sensor configuration
        if "sensor" not in metadata:
            return False, "Missing 'sensor' field in metadata"
        if not isinstance(metadata["sensor"], list):
            return False, "'sensor' field must be a list of strings"
        if not all(isinstance(s, str) for s in metadata["sensor"]):
            return False, "'sensor' list must contain only strings"

        sensors = set(metadata["sensor"])
        
        # Must have at least camera
        if not constants.REQUIRED_SENSORS.issubset(sensors):
            return False, f"Missing required sensors: {constants.REQUIRED_SENSORS - sensors}"
        
        # All sensors must be supported
        unsupported = sensors - constants.SUPPORTED_SENSORS
        if unsupported:
            return False, f"Unsupported sensors: {unsupported}. Supported: {constants.SUPPORTED_SENSORS}"
        
        # Check training algorithm (only PPO supported)
        if metadata.get("training_algorithm") != constants.REQUIRED_TRAINING_ALGORITHM:
            return False, f"Invalid training_algorithm: {metadata.get('training_algorithm')}. Required: {constants.REQUIRED_TRAINING_ALGORITHM}"
        
        # Check action space type (only discrete supported)
        if metadata.get("action_space_type") != constants.REQUIRED_ACTION_SPACE_TYPE:
            return False, f"Invalid action_space_type: {metadata.get('action_space_type')}. Required: {constants.REQUIRED_ACTION_SPACE_TYPE}"
        
        # Note: vehicle_type is optional and not validated
        # DeepRacer Console exports may not include this field
        
        # Check action space
        if "action_space" not in metadata:
            return False, "Missing 'action_space' field in metadata"
        
        action_space = metadata["action_space"]
        if not isinstance(action_space, list) or len(action_space) == 0:
            return False, "action_space must be a non-empty list"
        
        for i, action in enumerate(action_space):
            if not isinstance(action, dict):
                return False, f"Action {i} must be an object, got {type(action).__name__}"
            if "speed" not in action or "steering_angle" not in action:
                return False, f"Action {i} missing 'speed' or 'steering_angle'"
            try:
                float(action["speed"]); float(action["steering_angle"])
            except (TypeError, ValueError):
                return False, f"Action {i} has non-numeric speed/steering_angle"
        
        return True, ""
    
    def _parse_action_space(self, metadata: Dict[str, Any]) -> List[ActionSpace]:
        """Parse action space from metadata"""
        actions = []
        for i, action in enumerate(metadata["action_space"]):
            actions.append(ActionSpace(
                index=i,
                speed=float(action["speed"]),
                steering_angle=float(action["steering_angle"])
            ))
        return actions
    
    def _convert_to_tflite(self, model_path: str, model_name: str, 
                           metadata: Dict[str, Any]) -> Tuple[bool, str, str]:
        """
        Convert TensorFlow .pb model to TFLite format
        
        Returns:
            Tuple of (success, tflite_path, error_message)
        """
        # TFLite file stored alongside model.pb in agent/ folder
        model_dir = os.path.join(constants.MODELS_BASE_PATH, model_name)
        tflite_path = os.path.join(model_dir, "model.tflite")
        
        # Check if already converted
        if os.path.exists(tflite_path):
            self._log(f"Using cached TFLite model: {tflite_path}")
            return True, tflite_path, ""
        
        self._log(f"Converting model to TFLite: {model_path}")
        
        try:
            # Import TensorFlow only when needed
            os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'  # Suppress TF warnings
            import tensorflow.compat.v1 as tf
            tf.disable_v2_behavior()
            
            # Load frozen graph
            with tf.gfile.GFile(model_path, 'rb') as f:
                graph_def = tf.GraphDef()
                graph_def.ParseFromString(f.read())
            
            # Build input shapes and names from metadata sensors
            input_shapes = {}
            input_arrays = []
            sensors = set(metadata.get("sensor", []))
            
            # Camera input: [1, height, width, channels] (always required)
            camera_input_name = constants.NETWORK_INPUT_FORMAT[
                constants.SensorInputTypes.FRONT_FACING_CAMERA
            ].format(constants.INPUT_HEAD_NAME)
            input_shapes[camera_input_name] = [
                1, 
                constants.INFERENCE_IMAGE_HEIGHT, 
                constants.INFERENCE_IMAGE_WIDTH, 
                constants.INFERENCE_IMAGE_CHANNELS
            ]
            input_arrays.append(camera_input_name)
            
            # LiDAR input: [1, sectors] (only if model uses LiDAR)
            if "LIDAR" in sensors:
                lidar_input_name = constants.NETWORK_INPUT_FORMAT[
                    constants.SensorInputTypes.LIDAR
                ].format(constants.INPUT_HEAD_NAME)
                input_shapes[lidar_input_name] = [1, constants.LIDAR_SECTORS]
                input_arrays.append(lidar_input_name)
            
            self._log(f"Input shapes: {input_shapes}")
            self._log(f"Output: {constants.MODEL_OUTPUT_NAME}")
            
            # Convert to TFLite
            converter = tf.lite.TFLiteConverter.from_frozen_graph(
                graph_def_file=model_path,
                input_shapes=input_shapes,
                input_arrays=input_arrays,
                output_arrays=[constants.MODEL_OUTPUT_NAME]
            )
            converter.allow_custom_ops = True
            
            # Use FP16 quantization for faster inference on ARM
            converter.optimizations = [tf.lite.Optimize.DEFAULT]
            converter.target_spec.supported_types = [tf.float16]
            
            tflite_model = converter.convert()
            
            # Save TFLite model
            with open(tflite_path, 'wb') as f:
                f.write(tflite_model)
            
            self._log(f"TFLite model saved: {tflite_path}")
            return True, tflite_path, ""
            
        except Exception as e:
            error_msg = f"Failed to convert model: {str(e)}"
            self._log(error_msg, "error")
            return False, "", error_msg
    
    def load_model(self, model_name: str) -> Tuple[bool, str, Optional[ModelInfo]]:
        """
        Load a model by name
        
        Args:
            model_name: Name of the model directory
            
        Returns:
            Tuple of (success, message, model_info)
        """
        model_dir = os.path.join(constants.MODELS_BASE_PATH, model_name)
        
        # Check model directory exists
        if not os.path.isdir(model_dir):
            return False, f"Model not found: {model_name}", None
        
        # Load metadata
        metadata_path = os.path.join(model_dir, constants.MODEL_METADATA_FILE)
        if not os.path.exists(metadata_path):
            return False, f"Missing {constants.MODEL_METADATA_FILE}", None
        
        try:
            with open(metadata_path, 'r') as f:
                metadata = json.load(f)
        except json.JSONDecodeError as e:
            return False, f"Invalid JSON in metadata: {e}", None
        
        # Validate metadata
        is_valid, error_msg = self.validate_metadata(metadata)
        if not is_valid:
            return False, f"Invalid metadata: {error_msg}", None
        
        # Check model file exists
        model_path = os.path.join(model_dir, constants.MODEL_FILE)
        if not os.path.exists(model_path):
            return False, f"Missing model file: {constants.MODEL_FILE}", None
        
        # Convert to TFLite
        success, tflite_path, error_msg = self._convert_to_tflite(
            model_path, model_name, metadata
        )
        if not success:
            return False, error_msg, None
        
        # Parse action space
        action_space = self._parse_action_space(metadata)
        
        # Determine sensor config
        sensors = set(metadata.get("sensor", []))
        has_lidar = "LIDAR" in sensors
        
        # Create model info
        model_info = ModelInfo(
            name=model_name,
            path=model_dir,
            metadata=metadata,
            action_space=action_space,
            tflite_path=tflite_path,
            has_lidar=has_lidar,
        )
        
        self._current_model = model_info
        
        action_summary = [
            f"[{a.index}] speed:{a.speed}, angle:{a.steering_angle}"
            for a in action_space
        ]
        
        sensor_str = "camera+lidar" if has_lidar else "camera-only"
        self._log(f"Model loaded: {model_name} ({sensor_str})")
        self._log(f"Actions: {len(action_space)}")
        
        return True, "Model loaded successfully", model_info
    
    @property
    def current_model(self) -> Optional[ModelInfo]:
        """Get currently loaded model"""
        return self._current_model
