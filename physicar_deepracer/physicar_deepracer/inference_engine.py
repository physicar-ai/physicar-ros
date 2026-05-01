"""
TFLite Inference Engine for PhysiCar DeepRacer

Handles:
1. Loading TFLite model
2. Image preprocessing (grayscale, resize)
3. LiDAR data preprocessing
4. Running inference
5. Action selection from probabilities
"""

import os
import numpy as np
from typing import Optional, Tuple, List, Dict, Any
from dataclasses import dataclass, field
from enum import Enum

# Pre-import tflite runtime at module level to avoid cold start on first model load
try:
    import tflite_runtime.interpreter as _tflite
except ImportError:
    try:
        import tensorflow.lite as _tflite
    except ImportError:
        _tflite = None

from . import constants
from .model_loader import ModelInfo, ActionSpace


class ActionSelectionMode(Enum):
    """Action selection strategy for discrete action space"""
    GREEDY = "greedy"        # Always select highest probability action
    STOCHASTIC = "stochastic"  # Sample from probability distribution
    MEAN = "mean"            # Weighted average of all actions by probability


@dataclass
class InferenceResult:
    """Result of a single inference"""
    action_index: int
    action: ActionSpace
    probabilities: np.ndarray
    speed: float           # m/s
    steering_angle: float  # degrees (negative = left, positive = right)


@dataclass
class LoadedModel:
    """A model loaded into memory with its TFLite interpreter"""
    model_info: ModelInfo
    interpreter: Any
    input_details: Any
    output_details: Any


class InferenceEngine:
    """
    TFLite inference engine for DeepRacer models.
    
    Supports multiple models loaded simultaneously.
    One model is "active" for inference at a time.
    Switching between loaded models is instant (no re-loading).
    """
    
    def __init__(self, logger=None, action_selection_mode: ActionSelectionMode = ActionSelectionMode.GREEDY):
        self.logger = logger
        self._loaded_models: Dict[str, LoadedModel] = {}
        self._active_model_name: Optional[str] = None
        
        # Action selection mode
        self._action_selection_mode = action_selection_mode
        
        # Inference statistics
        self._inference_count = 0
        self._last_result: Optional[InferenceResult] = None
    
    def _log(self, msg: str, level: str = "info"):
        if self.logger:
            getattr(self.logger, level)(msg)
        else:
            print(f"[{level.upper()}] {msg}")
    
    def load_model(self, model_info: ModelInfo) -> Tuple[bool, str]:
        """
        Load TFLite model into memory pool and set as active.
        
        If the model is already loaded, just switches active model (instant).
        Multiple models can be loaded simultaneously.
        
        Args:
            model_info: Model information from ModelLoader
            
        Returns:
            Tuple of (success, message)
        """
        name = model_info.name
        
        # Already loaded? Just switch active
        if name in self._loaded_models:
            self._active_model_name = name
            self._log(f"Model '{name}' already loaded, switched to active")
            return True, f"Model '{name}' already loaded, switched to active"
        
        if not os.path.exists(model_info.tflite_path):
            return False, f"TFLite file not found: {model_info.tflite_path}"
        
        try:
            if _tflite is None:
                return False, "tflite_runtime and tensorflow are both unavailable"
            
            tflite = _tflite
            
            # Create interpreter with multi-threading
            num_threads = max(1, os.cpu_count() - 2) if os.cpu_count() else 2
            interpreter = tflite.Interpreter(
                model_path=model_info.tflite_path,
                num_threads=num_threads
            )
            interpreter.allocate_tensors()
            
            # Get input/output details
            input_details = interpreter.get_input_details()
            output_details = interpreter.get_output_details()
            
            # Store loaded model
            self._loaded_models[name] = LoadedModel(
                model_info=model_info,
                interpreter=interpreter,
                input_details=input_details,
                output_details=output_details,
            )
            self._active_model_name = name
            self._inference_count = 0
            
            self._log(f"TFLite model '{name}' loaded with {num_threads} threads")
            self._log(f"Input tensors: {len(input_details)}")
            for i, inp in enumerate(input_details):
                self._log(f"  [{i}] {inp['name']}: {inp['shape']} {inp['dtype']}")
            self._log(f"Output tensors: {len(output_details)}")
            for i, out in enumerate(output_details):
                self._log(f"  [{i}] {out['name']}: {out['shape']} {out['dtype']}")
            self._log(f"Loaded models: {list(self._loaded_models.keys())}")
            
            return True, "Model loaded successfully"
            
        except Exception as e:
            error_msg = f"Failed to load TFLite model: {str(e)}"
            self._log(error_msg, "error")
            return False, error_msg
    
    def unload_model(self, name: str) -> Tuple[bool, str]:
        """
        Unload a model from memory.
        
        Args:
            name: Model name, or empty string to unload all
            
        Returns:
            Tuple of (success, message)
        """
        if not name:
            # Unload all
            count = len(self._loaded_models)
            self._loaded_models.clear()
            self._active_model_name = None
            self._last_result = None
            self._log(f"Unloaded all {count} models")
            return True, f"Unloaded all {count} models"
        
        if name not in self._loaded_models:
            return False, f"Model '{name}' is not loaded"
        
        del self._loaded_models[name]
        
        # If we unloaded the active model, pick another or None
        if self._active_model_name == name:
            if self._loaded_models:
                self._active_model_name = next(iter(self._loaded_models))
                self._log(f"Active model switched to '{self._active_model_name}'")
            else:
                self._active_model_name = None
                self._last_result = None
        
        self._log(f"Model '{name}' unloaded. Loaded: {list(self._loaded_models.keys())}")
        return True, f"Model '{name}' unloaded"
    
    def preprocess_image(self, image: np.ndarray) -> np.ndarray:
        """
        Preprocess camera image for inference
        
        Args:
            image: BGR image from camera (480x320 or similar)
            
        Returns:
            Preprocessed image [1, 120, 160, 1] float32, normalized 0-1
        """
        import cv2
        
        # Convert to grayscale if color
        if len(image.shape) == 3 and image.shape[2] == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        else:
            gray = image
        
        # Resize to inference dimensions
        resized = cv2.resize(
            gray, 
            (constants.INFERENCE_IMAGE_WIDTH, constants.INFERENCE_IMAGE_HEIGHT),
            interpolation=cv2.INTER_AREA
        )
        
        # Keep 0-255 range as float32 (DeepRacer training does NOT normalize)
        img_float = resized.astype(np.float32)
        
        # Shape: [1, height, width, channels]
        return img_float.reshape(
            1, 
            constants.INFERENCE_IMAGE_HEIGHT, 
            constants.INFERENCE_IMAGE_WIDTH, 
            constants.INFERENCE_IMAGE_CHANNELS
        )
    
    def preprocess_lidar(self, ranges: np.ndarray, angle_min: float, 
                         angle_increment: float) -> np.ndarray:
        """
        Preprocess LiDAR scan data for inference
        
        DeepRacer training simulation uses 300° FOV: -150° ~ +150°
        (rear 60° is excluded)
        
        ROS standard coordinate:
        - 0° = front
        - + = left (counter-clockwise)
        - - = right (clockwise)
        
        Output index order (matching training):
        - Index 0 = -150° (right rear)
        - Index 31-32 = 0° (front)
        - Index 63 = +150° (left rear)
        
        Args:
            ranges: Raw range data from LiDAR
            angle_min: Minimum angle (radians)
            angle_increment: Angle between samples (radians)
            
        Returns:
            Interpolated data [1, 64] float32, distances in meters (clipped 0.15-1.0)
        """
        # Target angle range (300° FOV, excluding rear 60°)
        target_min_deg = constants.LIDAR_MIN_ANGLE_DEG  # -150°
        target_max_deg = constants.LIDAR_MAX_ANGLE_DEG  # +150°
        num_values = constants.LIDAR_SECTORS  # 64
        min_dist = constants.LIDAR_MIN_DIST  # 0.15m
        max_dist = constants.LIDAR_MAX_DIST  # 1.0m
        
        # Build arrays of (angle_deg, distance) for samples within target range
        lidar_angles = []
        lidar_ranges = []
        
        for i, distance in enumerate(ranges):
            # Calculate angle in radians, then convert to degrees
            angle_rad = angle_min + i * angle_increment
            # Convert to degrees in range -180 to +180
            angle_deg = np.degrees(angle_rad)
            # Normalize to -180 ~ +180 range
            while angle_deg > 180:
                angle_deg -= 360
            while angle_deg < -180:
                angle_deg += 360
            
            # Check if within target range: -150° to +150°
            # This excludes the rear 60° (from -180° to -150° and +150° to +180°)
            if target_min_deg - 0.5 <= angle_deg <= target_max_deg + 0.5:
                # Clip distance, handle inf as max_dist
                if not np.isfinite(distance) or distance <= 0:
                    clipped_dist = max_dist
                else:
                    clipped_dist = np.clip(distance, min_dist, max_dist)
                
                lidar_angles.append(angle_deg)
                lidar_ranges.append(clipped_dist)
        
        # If no samples in range, return max distance array
        if len(lidar_angles) < 2:
            return np.full((1, num_values), max_dist, dtype=np.float32)
        
        # Sort by angle (ascending: -150° → +150°)
        sorted_indices = np.argsort(lidar_angles)
        lidar_angles = np.array(lidar_angles)[sorted_indices]
        lidar_ranges = np.array(lidar_ranges)[sorted_indices]
        
        # Ensure boundary angles exist for interpolation
        if lidar_angles[0] > target_min_deg:
            lidar_angles = np.insert(lidar_angles, 0, target_min_deg)
            lidar_ranges = np.insert(lidar_ranges, 0, max_dist)
        if lidar_angles[-1] < target_max_deg:
            lidar_angles = np.append(lidar_angles, target_max_deg)
            lidar_ranges = np.append(lidar_ranges, max_dist)
        
        # Generate target angles (evenly spaced from -150° to +150°)
        # Index 0 = -150° (right rear), Index 63 = +150° (left rear)
        target_angles = np.linspace(target_min_deg, target_max_deg, num_values)
        
        # Interpolate to get 64 evenly-spaced values
        interpolated = np.interp(target_angles, lidar_angles, lidar_ranges)
        
        # Shape: [1, num_values]
        return interpolated.astype(np.float32).reshape(1, num_values)
    
    def run_inference(self, image: np.ndarray, lidar: Optional[np.ndarray] = None) -> Optional[InferenceResult]:
        """
        Run inference on preprocessed inputs.
        
        Input tensors are matched by name (deepracer-custom-car pattern):
          - 'FRONT_FACING_CAMERA' or 'observation' → image
          - 'LIDAR' → lidar (skipped if model has no LiDAR tensor)
        
        Args:
            image: Preprocessed image [1, 120, 160, 1]
            lidar: Preprocessed LiDAR [1, 64] or None for camera-only models
            
        Returns:
            InferenceResult or None on error
        """
        if self._active_model_name is None or self._active_model_name not in self._loaded_models:
            self._log("No model loaded", "error")
            return None
        
        active = self._loaded_models[self._active_model_name]
        
        try:
            # Set inputs by tensor name dispatch (handles any sensor combo)
            for inp in active.input_details:
                name = inp['name'].lower()
                if 'camera' in name or 'observation' in name:
                    active.interpreter.set_tensor(inp['index'], image)
                elif 'lidar' in name:
                    if lidar is not None:
                        active.interpreter.set_tensor(inp['index'], lidar)
            
            # Run inference
            active.interpreter.invoke()
            
            # Get output probabilities
            output = active.interpreter.get_tensor(active.output_details[0]['index'])
            probabilities = output.flatten()
            
            # Convert to proper probability distribution (softmax if needed)
            # TFLite PPO output is already softmax, but ensure non-negative and sum to 1
            probs = np.clip(probabilities, 0, None)
            prob_sum = probs.sum()
            if prob_sum > 0:
                probs = probs / prob_sum
            else:
                probs = np.ones_like(probs) / len(probs)
            
            model_info = active.model_info
            
            # Select action based on mode
            if self._action_selection_mode == ActionSelectionMode.GREEDY:
                # Always select highest probability action
                action_idx = int(np.argmax(probs))
                action = model_info.action_space[action_idx]
                speed = action.speed
                steering_angle = action.steering_angle
            elif self._action_selection_mode == ActionSelectionMode.MEAN:
                # Weighted average of all actions by probability
                speed = sum(probs[i] * model_info.action_space[i].speed 
                           for i in range(len(probs)))
                steering_angle = sum(probs[i] * model_info.action_space[i].steering_angle 
                                    for i in range(len(probs)))
                action_idx = int(np.argmax(probs))  # Report highest prob action for logging
                action = model_info.action_space[action_idx]
            else:  # STOCHASTIC
                # Sample from probability distribution
                action_idx = int(np.random.choice(len(probs), p=probs))
                action = model_info.action_space[action_idx]
                speed = action.speed
                steering_angle = action.steering_angle
            
            result = InferenceResult(
                action_index=action_idx,
                action=action,
                probabilities=probabilities,
                speed=speed,
                steering_angle=steering_angle
            )
            
            self._inference_count += 1
            self._last_result = result
            
            return result
            
        except Exception as e:
            self._log(f"Inference error: {str(e)}", "error")
            return None
    
    def infer_from_raw(self, image: np.ndarray, ranges: Optional[np.ndarray] = None,
                       angle_min: float = 0.0, angle_increment: float = 0.0) -> Optional[InferenceResult]:
        """
        Convenience method: preprocess and run inference in one call.
        
        Args:
            image: Raw BGR camera image
            ranges: Raw LiDAR range data (None for camera-only models)
            angle_min: LiDAR minimum angle
            angle_increment: LiDAR angle increment
            
        Returns:
            InferenceResult or None
        """
        proc_image = self.preprocess_image(image)
        proc_lidar = None
        if ranges is not None:
            proc_lidar = self.preprocess_lidar(ranges, angle_min, angle_increment)
        return self.run_inference(proc_image, proc_lidar)
    
    @property
    def inference_count(self) -> int:
        return self._inference_count
    
    @property
    def last_result(self) -> Optional[InferenceResult]:
        return self._last_result
    
    @property
    def is_loaded(self) -> bool:
        return self._active_model_name is not None and self._active_model_name in self._loaded_models
    
    @property
    def active_model_name(self) -> Optional[str]:
        return self._active_model_name
    
    @property
    def active_model_info(self) -> Optional[ModelInfo]:
        if self._active_model_name and self._active_model_name in self._loaded_models:
            return self._loaded_models[self._active_model_name].model_info
        return None
    
    @property
    def loaded_model_names(self) -> List[str]:
        return list(self._loaded_models.keys())
    
    @property
    def action_selection_mode(self) -> ActionSelectionMode:
        return self._action_selection_mode
    
    @action_selection_mode.setter
    def action_selection_mode(self, mode: ActionSelectionMode):
        self._action_selection_mode = mode
        if self.logger:
            self.logger.info(f"Action selection mode set to: {mode.value}")
