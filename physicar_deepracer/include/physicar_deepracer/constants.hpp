#pragma once

#include <string>

namespace physicar_deepracer {

// Model storage location
inline const std::string MODELS_BASE_PATH = "/opt/physicar/userdata/deepracer/models";

// Config file (JSON, 1st priority)
inline const std::string CONFIG_FILE_PATH = "/opt/physicar/userdata/deepracer/config.json";

// Default config values
inline const std::string DEFAULT_ACTION_SELECTION = "greedy";
constexpr double DEFAULT_PAN = 0.0;
constexpr double DEFAULT_TILT = -15.0;
constexpr double DEFAULT_SPEED_PERCENT = 100.0;

// Model file names
inline const std::string MODEL_METADATA_FILE = "model_metadata.json";
inline const std::string MODEL_FILE = "model.pb";

// Supported sensor configurations
inline const std::string SENSOR_CAMERA = "FRONT_FACING_CAMERA";
inline const std::string SENSOR_LIDAR = "LIDAR";

// Required metadata fields
inline const std::string REQUIRED_TRAINING_ALGORITHM = "clipped_ppo";
inline const std::string REQUIRED_ACTION_SPACE_TYPE = "discrete";

// Default image dimensions for inference
// Camera: 480x320 -> resize to 160x120 grayscale for inference
constexpr int INFERENCE_IMAGE_WIDTH = 160;
constexpr int INFERENCE_IMAGE_HEIGHT = 120;
constexpr int INFERENCE_IMAGE_CHANNELS = 1;  // Grayscale

// LiDAR configuration (matching DeepRacer training simulation)
// Training uses 300 deg FOV: -150 ~ +150 (rear 60 deg excluded)
// ROS standard: 0 deg = front, + = left (CCW), - = right (CW)
// Index order: [0] = -150 deg (right rear) -> [63] = +150 deg (left rear)
constexpr int LIDAR_SECTORS = 64;
constexpr double LIDAR_MIN_ANGLE_DEG = -150.0;
constexpr double LIDAR_MAX_ANGLE_DEG = 150.0;
constexpr double LIDAR_MIN_DIST = 0.15;
constexpr double LIDAR_MAX_DIST = 1.0;

// Inference timing
constexpr double MAX_INFERENCE_RATE_HZ = 15.0;

}  // namespace physicar_deepracer
