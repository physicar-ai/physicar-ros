#pragma once

#include <memory>
#include <string>
#include <tuple>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>
#include <rclcpp/logger.hpp>

namespace physicar_deepracer {

struct ActionSpace {
    int index;
    double speed;           // m/s
    double steering_angle;  // degrees
};

struct ModelInfo {
    std::string name;
    std::string path;
    nlohmann::json metadata;
    std::vector<ActionSpace> action_space;
    std::string tflite_path;
    bool has_lidar = true;
};

class ModelLoader {
public:
    explicit ModelLoader(rclcpp::Logger logger);

    std::vector<std::string> list_models() const;

    std::pair<bool, std::string> validate_metadata(const nlohmann::json& metadata) const;

    std::tuple<bool, std::string, std::shared_ptr<ModelInfo>>
    load_model(const std::string& model_name);

private:
    std::vector<ActionSpace> parse_action_space(const nlohmann::json& metadata) const;

    std::tuple<bool, std::string, std::string>
    convert_to_tflite(const std::string& model_path,
                      const std::string& model_name,
                      const nlohmann::json& metadata);

    rclcpp::Logger logger_;
};

}  // namespace physicar_deepracer
