#pragma once

#include <map>
#include <memory>
#include <mutex>
#include <optional>
#include <random>
#include <string>
#include <utility>
#include <vector>

#include <opencv2/core.hpp>
#include <rclcpp/logger.hpp>
#include <tensorflow/lite/interpreter.h>
#include <tensorflow/lite/model.h>

#include "physicar_deepracer/model_loader.hpp"

namespace physicar_deepracer {

enum class ActionSelectionMode { GREEDY, STOCHASTIC, MEAN };

struct InferenceResult {
    int action_index;
    ActionSpace action;
    std::vector<float> probabilities;
    double speed;
    double steering_angle;
};

struct LoadedModel {
    std::shared_ptr<ModelInfo> model_info;
    std::unique_ptr<tflite::FlatBufferModel> flat_buffer_model;
    std::unique_ptr<tflite::Interpreter> interpreter;
};

class InferenceEngine {
public:
    explicit InferenceEngine(
        rclcpp::Logger logger,
        ActionSelectionMode mode = ActionSelectionMode::GREEDY);

    std::pair<bool, std::string> load_model(std::shared_ptr<ModelInfo> model_info);
    std::pair<bool, std::string> unload_model(const std::string& name);

    std::vector<float> preprocess_image(const cv::Mat& image) const;

    std::vector<float> preprocess_lidar(
        const std::vector<float>& ranges,
        float angle_min, float angle_increment) const;

    std::optional<InferenceResult> run_inference(
        const std::vector<float>& image_input,
        const std::vector<float>* lidar_input = nullptr);

    std::optional<InferenceResult> infer_from_raw(
        const cv::Mat& image,
        const std::vector<float>* ranges = nullptr,
        float angle_min = 0.0f, float angle_increment = 0.0f);

    bool is_loaded() const;
    std::string active_model_name() const { return active_model_name_; }
    std::shared_ptr<ModelInfo> active_model_info() const;
    std::vector<std::string> loaded_model_names() const;
    int64_t inference_count() const { return inference_count_; }
    std::optional<InferenceResult> last_result() const { return last_result_; }

    void set_action_selection_mode(ActionSelectionMode mode);
    ActionSelectionMode action_selection_mode() const { return action_selection_mode_; }

private:
    rclcpp::Logger logger_;
    std::map<std::string, std::unique_ptr<LoadedModel>> loaded_models_;
    std::string active_model_name_;
    ActionSelectionMode action_selection_mode_;
    int64_t inference_count_ = 0;
    std::optional<InferenceResult> last_result_;
    std::mt19937 rng_;
};

}  // namespace physicar_deepracer
