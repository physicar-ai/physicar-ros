#!/usr/bin/env python3
"""Test script for DeepRacer components"""

import math
import numpy as np

# Add the package path (resolve from this script's location)
import sys
from pathlib import Path
_pkg_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_pkg_root))

from physicar_deepracer.model_loader import ModelLoader
from physicar_deepracer.inference_engine import InferenceEngine, ActionSelectionMode
from physicar_deepracer import constants


def test_model_loader():
    print("=" * 50)
    print("TEST: Model Loader")
    print("=" * 50)
    
    loader = ModelLoader()
    models = loader.list_models()
    print(f"Available models: {models}")
    
    if not models:
        print("ERROR: No models found!")
        return None
    
    model_name = 'physicar-test-model'
    print(f"\nLoading model: {model_name}")
    success, msg, info = loader.load_model(model_name)
    print(f"Result: success={success}, message={msg}")
    
    if not success:
        print("ERROR: Model loading failed!")
        return None
    
    print(f"\nModel Info:")
    print(f"  Name: {info.name}")
    print(f"  TFLite Path: {info.tflite_path}")
    print(f"  Action count: {len(info.action_space)}")
    print(f"\nAction Space (from model_metadata.json):")
    for a in info.action_space:
        print(f"  [{a.index}] speed={a.speed} m/s, steering_angle={a.steering_angle}° (degrees)")
    
    return info


def test_inference_engine(model_info):
    print("\n" + "=" * 50)
    print("TEST: Inference Engine")
    print("=" * 50)
    
    engine = InferenceEngine()
    success, msg = engine.load_model(model_info)
    print(f"Load result: success={success}, message={msg}")
    
    if not success:
        print("ERROR: Engine loading failed!")
        return
    
    print(f"Engine is loaded: {engine.is_loaded}")
    
    # Create dummy inputs
    print("\nCreating dummy inputs...")
    dummy_image = np.random.randint(0, 255, (480, 640, 3), dtype=np.uint8)
    dummy_lidar_ranges = np.random.uniform(0.15, 1.0, 360).astype(np.float32)
    angle_min = -math.pi  # -180°
    angle_increment = 2 * math.pi / 360  # 1° per reading
    
    print(f"  Image shape: {dummy_image.shape}")
    print(f"  LiDAR ranges: {len(dummy_lidar_ranges)} readings")
    print(f"  LiDAR angle range: {math.degrees(angle_min):.1f}° to {math.degrees(angle_min + angle_increment * 360):.1f}°")
    
    # Test greedy mode
    print("\n--- Testing GREEDY mode ---")
    engine.action_selection_mode = ActionSelectionMode.GREEDY
    result = engine.infer_from_raw(dummy_image, dummy_lidar_ranges, angle_min, angle_increment)
    
    if result:
        print(f"Action index: {result.action_index}")
        print(f"Speed: {result.speed} m/s")
        print(f"Steering angle: {result.steering_angle}° (degrees from metadata)")
        
        # Show cmd_vel conversion
        wheelbase = 0.18
        steering_rad = math.radians(result.steering_angle)
        angular_vel = (result.speed / wheelbase) * math.tan(steering_rad) if abs(result.speed) > 0.01 else 0.0
        angular_vel = max(-2.0, min(2.0, angular_vel))
        
        print(f"\n--> cmd_vel conversion:")
        print(f"    linear.x = {result.speed} m/s (direct from action)")
        print(f"    steering_rad = radians({result.steering_angle}°) = {steering_rad:.4f} rad")
        print(f"    angular.z = ({result.speed} / {wheelbase}) * tan({steering_rad:.4f}) = {angular_vel:.4f} rad/s")
    else:
        print("ERROR: Inference failed!")
        return
    
    # Test stochastic mode
    print("\n--- Testing STOCHASTIC mode ---")
    engine.action_selection_mode = ActionSelectionMode.STOCHASTIC
    results = []
    for i in range(5):
        result = engine.infer_from_raw(dummy_image, dummy_lidar_ranges, angle_min, angle_increment)
        if result:
            results.append(result.action_index)
    print(f"5 stochastic selections: {results}")
    
    print(f"\nTotal inference count: {engine.inference_count}")


def test_lidar_preprocessing():
    print("\n" + "=" * 50)
    print("TEST: LiDAR Preprocessing")
    print("=" * 50)
    
    print(f"\nLiDAR Config from constants:")
    print(f"  LIDAR_MIN_ANGLE_DEG = {constants.LIDAR_MIN_ANGLE_DEG}°")
    print(f"  LIDAR_MAX_ANGLE_DEG = {constants.LIDAR_MAX_ANGLE_DEG}°")
    print(f"  LIDAR_SECTORS = {constants.LIDAR_SECTORS}")
    print(f"  FOV = {constants.LIDAR_MAX_ANGLE_DEG - constants.LIDAR_MIN_ANGLE_DEG}° (front-facing, excludes rear 60°)")
    
    # Create a temporary engine to test preprocessing
    engine = InferenceEngine()
    
    # Simulate RPLidar A1 (360° scan)
    num_readings = 360
    angle_min = -math.pi  # -180°
    angle_increment = 2 * math.pi / num_readings
    
    # Create test pattern: distance increases with angle
    ranges = np.array([0.5 + (i / 360) * 0.5 for i in range(num_readings)], dtype=np.float32)
    
    result = engine.preprocess_lidar(ranges, angle_min, angle_increment)
    print(f"\nPreprocessed LiDAR output shape: {result.shape}")
    print(f"Sample values (64 sectors from -150° to +150°):")
    print(f"  First 5: {result[0, :5]}")
    print(f"  Last 5: {result[0, -5:]}")
    print(f"  Min: {result.min():.3f}, Max: {result.max():.3f}")


def test_config():
    print("\n" + "=" * 50)
    print("TEST: Configuration")
    print("=" * 50)
    
    print(f"\nConfig file path: {constants.CONFIG_FILE_PATH}")
    print(f"Default config: {constants.DEFAULT_CONFIG}")
    
    import os
    import json
    if os.path.exists(constants.CONFIG_FILE_PATH):
        with open(constants.CONFIG_FILE_PATH, 'r') as f:
            saved_config = json.load(f)
        print(f"Saved config: {saved_config}")
    else:
        print("No saved config file exists yet")


def main():
    print("\n" + "#" * 60)
    print("# PhysiCar DeepRacer Component Tests")
    print("#" * 60)
    
    test_config()
    test_lidar_preprocessing()
    
    model_info = test_model_loader()
    if model_info:
        test_inference_engine(model_info)
    
    print("\n" + "=" * 50)
    print("All tests completed!")
    print("=" * 50)


if __name__ == '__main__':
    main()
