#include "physicar_deepracer/model_loader.hpp"
#include "physicar_deepracer/constants.hpp"

#include <algorithm>
#include <array>
#include <cstdio>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <set>
#include <sstream>

#include <rclcpp/logging.hpp>

namespace fs = std::filesystem;

namespace physicar_deepracer {

// No eager directory creation: an empty ~/physicar_ws/deepracer in the student
// workspace is noise. Upload/config-save paths create directories on demand.
ModelLoader::ModelLoader(rclcpp::Logger logger) : logger_(logger) {}

std::vector<std::string> ModelLoader::list_models() const {
    std::vector<std::string> models;
    if (!fs::exists(MODELS_BASE_PATH)) return models;

    for (const auto& entry : fs::directory_iterator(MODELS_BASE_PATH)) {
        if (!entry.is_directory()) continue;
        auto name = entry.path().filename().string();
        auto metadata_path = entry.path() / MODEL_METADATA_FILE;
        auto model_path = entry.path() / MODEL_FILE;
        if (fs::exists(metadata_path) && fs::exists(model_path)) {
            models.push_back(name);
        }
    }
    return models;
}

std::pair<bool, std::string>
ModelLoader::validate_metadata(const nlohmann::json& metadata) const {
    // Check sensor configuration
    if (!metadata.contains("sensor")) {
        return {false, "Missing 'sensor' field in metadata"};
    }
    if (!metadata["sensor"].is_array()) {
        return {false, "'sensor' field must be a list of strings"};
    }
    for (const auto& s : metadata["sensor"]) {
        if (!s.is_string()) {
            return {false, "'sensor' list must contain only strings"};
        }
    }

    std::set<std::string> sensors;
    for (const auto& s : metadata["sensor"]) {
        sensors.insert(s.get<std::string>());
    }

    // Must have at least camera
    if (sensors.find(SENSOR_CAMERA) == sensors.end()) {
        return {false, "Missing required sensor: " + SENSOR_CAMERA};
    }

    // All sensors must be supported
    const std::set<std::string> supported = {SENSOR_CAMERA, SENSOR_LIDAR};
    for (const auto& s : sensors) {
        if (supported.find(s) == supported.end()) {
            return {false, "Unsupported sensor: " + s};
        }
    }

    // Check training algorithm
    if (metadata.value("training_algorithm", "") != REQUIRED_TRAINING_ALGORITHM) {
        return {false, "Invalid training_algorithm: " +
                metadata.value("training_algorithm", "(missing)") +
                ". Required: " + REQUIRED_TRAINING_ALGORITHM};
    }

    // Check action space type
    if (metadata.value("action_space_type", "") != REQUIRED_ACTION_SPACE_TYPE) {
        return {false, "Invalid action_space_type: " +
                metadata.value("action_space_type", "(missing)") +
                ". Required: " + REQUIRED_ACTION_SPACE_TYPE};
    }

    // Check action space
    if (!metadata.contains("action_space")) {
        return {false, "Missing 'action_space' field in metadata"};
    }
    const auto& action_space = metadata["action_space"];
    if (!action_space.is_array() || action_space.empty()) {
        return {false, "action_space must be a non-empty list"};
    }

    for (size_t i = 0; i < action_space.size(); ++i) {
        const auto& action = action_space[i];
        if (!action.is_object()) {
            return {false, "Action " + std::to_string(i) + " must be an object"};
        }
        if (!action.contains("speed") || !action.contains("steering_angle")) {
            return {false, "Action " + std::to_string(i) +
                    " missing 'speed' or 'steering_angle'"};
        }
        if (!action["speed"].is_number() || !action["steering_angle"].is_number()) {
            return {false, "Action " + std::to_string(i) +
                    " has non-numeric speed/steering_angle"};
        }
    }

    return {true, ""};
}

std::vector<ActionSpace>
ModelLoader::parse_action_space(const nlohmann::json& metadata) const {
    std::vector<ActionSpace> actions;
    for (size_t i = 0; i < metadata["action_space"].size(); ++i) {
        const auto& a = metadata["action_space"][i];
        actions.push_back(ActionSpace{
            static_cast<int>(i),
            a["speed"].get<double>(),
            a["steering_angle"].get<double>()
        });
    }
    return actions;
}

std::tuple<bool, std::string, std::string>
ModelLoader::convert_to_tflite(const std::string& model_path,
                               const std::string& model_name,
                               const nlohmann::json& /*metadata*/) {
    auto model_dir = fs::path(MODELS_BASE_PATH) / model_name;
    auto tflite_path = (model_dir / "model.tflite").string();

    // Check if already converted
    if (fs::exists(tflite_path)) {
        RCLCPP_INFO(logger_, "Using cached TFLite model: %s", tflite_path.c_str());
        return {true, tflite_path, ""};
    }

    RCLCPP_INFO(logger_, "Converting model to TFLite via Python: %s", model_path.c_str());

    // Call Python model_loader for conversion (requires tensorflow)
    std::string cmd =
        "python3 -c \""
        "import os; os.environ['TF_CPP_MIN_LOG_LEVEL']='3'; "
        "from physicar_deepracer.model_loader import ModelLoader; "
        "ml = ModelLoader(); "
        "s, m, _ = ml.load_model('" + model_name + "'); "
        "print(m); "
        "exit(0 if s else 1)\" 2>&1";

    std::array<char, 256> buffer;
    std::string output;
    FILE* pipe = popen(cmd.c_str(), "r");
    if (!pipe) {
        return {false, "", "Failed to run Python converter"};
    }
    while (fgets(buffer.data(), buffer.size(), pipe) != nullptr) {
        output += buffer.data();
    }
    int ret = pclose(pipe);

    if (ret != 0) {
        return {false, "", "Python conversion failed: " + output};
    }

    if (!fs::exists(tflite_path)) {
        return {false, "", "Conversion completed but tflite file not found"};
    }

    RCLCPP_INFO(logger_, "TFLite model saved: %s", tflite_path.c_str());
    return {true, tflite_path, ""};
}

std::tuple<bool, std::string, std::shared_ptr<ModelInfo>>
ModelLoader::load_model(const std::string& model_name) {
    auto model_dir = fs::path(MODELS_BASE_PATH) / model_name;

    // Check model directory exists
    if (!fs::is_directory(model_dir)) {
        return {false, "Model not found: " + model_name, nullptr};
    }

    // Load metadata
    auto metadata_path = model_dir / MODEL_METADATA_FILE;
    if (!fs::exists(metadata_path)) {
        return {false, "Missing " + MODEL_METADATA_FILE, nullptr};
    }

    nlohmann::json metadata;
    try {
        std::ifstream f(metadata_path);
        metadata = nlohmann::json::parse(f);
    } catch (const nlohmann::json::parse_error& e) {
        return {false, std::string("Invalid JSON in metadata: ") + e.what(), nullptr};
    }

    // Validate metadata
    auto [is_valid, error_msg] = validate_metadata(metadata);
    if (!is_valid) {
        return {false, "Invalid metadata: " + error_msg, nullptr};
    }

    // Check model file exists
    auto model_path = (model_dir / MODEL_FILE).string();
    if (!fs::exists(model_path)) {
        return {false, "Missing model file: " + MODEL_FILE, nullptr};
    }

    // Convert to TFLite
    auto [success, tflite_path, conv_error] =
        convert_to_tflite(model_path, model_name, metadata);
    if (!success) {
        return {false, conv_error, nullptr};
    }

    // Parse action space
    auto action_space = parse_action_space(metadata);

    // Determine sensor config
    std::set<std::string> sensors;
    for (const auto& s : metadata["sensor"]) {
        sensors.insert(s.get<std::string>());
    }
    bool has_lidar = sensors.count(SENSOR_LIDAR) > 0;

    // Create model info
    auto model_info = std::make_shared<ModelInfo>();
    model_info->name = model_name;
    model_info->path = model_dir.string();
    model_info->metadata = metadata;
    model_info->action_space = action_space;
    model_info->tflite_path = tflite_path;
    model_info->has_lidar = has_lidar;

    std::string sensor_str = has_lidar ? "camera+lidar" : "camera-only";
    RCLCPP_INFO(logger_, "Model loaded: %s (%s)", model_name.c_str(), sensor_str.c_str());
    RCLCPP_INFO(logger_, "Actions: %zu", action_space.size());

    return {true, "Model loaded successfully", model_info};
}

}  // namespace physicar_deepracer
