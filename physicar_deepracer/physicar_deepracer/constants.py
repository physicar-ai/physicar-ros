"""
Constants for PhysiCar DeepRacer integration
"""

import os
from enum import Enum

# Model storage location
MODELS_BASE_PATH = "/opt/physicar/userdata/deepracer/models"

# Config file (JSON, 1st priority)
CONFIG_FILE_PATH = "/opt/physicar/userdata/deepracer/config.json"

# Default config values
DEFAULT_CONFIG = {
    "action_selection": "greedy",  # 'greedy', 'stochastic', or 'mean'
    "pan": 0,   # Camera pan angle (degrees) to set when inference starts
    "tilt": -15,  # Camera tilt angle (degrees) to set when inference starts
    "speed_percent": 100,  # Speed multiplier percent (50 ~ 150)
}

# Model file names
MODEL_METADATA_FILE = "model_metadata.json"
MODEL_FILE = "model.pb"

# Supported sensor configurations
# Camera is always required. LiDAR is optional.
SUPPORTED_SENSORS = {"FRONT_FACING_CAMERA", "LIDAR"}
REQUIRED_SENSORS = {"FRONT_FACING_CAMERA"}  # Must have at least camera

# Other required metadata fields
REQUIRED_TRAINING_ALGORITHM = "clipped_ppo"
REQUIRED_ACTION_SPACE_TYPE = "discrete"

# Sensor input specifications
class SensorInputTypes(Enum):
    """Sensor types matching DeepRacer convention"""
    FRONT_FACING_CAMERA = 5
    LIDAR = 2


class TrainingAlgorithms(Enum):
    """Training algorithms (only PPO supported for PhysiCar)"""
    CLIPPED_PPO = 1


# Network input name format
NETWORK_INPUT_FORMAT = {
    SensorInputTypes.FRONT_FACING_CAMERA: "main_level/agent/{}/online/network_0/FRONT_FACING_CAMERA/FRONT_FACING_CAMERA",
    SensorInputTypes.LIDAR: "main_level/agent/{}/online/network_0/LIDAR/LIDAR",
}

# Input head name for PPO
INPUT_HEAD_NAME = "main"

# Model output name (PPO policy head)
MODEL_OUTPUT_NAME = "main_level/agent/main/online/network_1/ppo_head_0/policy"

# Default image dimensions for inference
# Camera: 480x320 → resize to 160x120 grayscale for inference
INFERENCE_IMAGE_WIDTH = 160
INFERENCE_IMAGE_HEIGHT = 120
INFERENCE_IMAGE_CHANNELS = 1  # Grayscale

# LiDAR configuration (matching DeepRacer training simulation)
# Training uses 300° FOV: -150° ~ +150° (rear 60° excluded)
# ROS standard: 0° = front, + = left (CCW), - = right (CW)
# Index order: [0] = -150° (right rear) → [63] = +150° (left rear)
LIDAR_SECTORS = 64  # Number of interpolated values
LIDAR_MIN_ANGLE_DEG = -150.0  # Start angle (right rear)
LIDAR_MAX_ANGLE_DEG = 150.0   # End angle (left rear)
LIDAR_MIN_DIST = 0.15  # Minimum distance (meters)
LIDAR_MAX_DIST = 1.0   # Maximum distance (meters)

# Inference timing
MAX_INFERENCE_RATE_HZ = 15.0  # Max inference rate to prevent overload
