#include "physicar_deepracer/inference_engine.hpp"
#include "physicar_deepracer/constants.hpp"

#include <algorithm>
#include <cmath>
#include <cstring>
#include <numeric>
#include <thread>

#include <opencv2/imgproc.hpp>
#include <rclcpp/logging.hpp>
#include <tensorflow/lite/kernels/register.h>

namespace physicar_deepracer {

InferenceEngine::InferenceEngine(rclcpp::Logger logger, ActionSelectionMode mode)
    : logger_(logger),
      action_selection_mode_(mode),
      rng_(std::random_device{}()) {}

std::pair<bool, std::string>
InferenceEngine::load_model(std::shared_ptr<ModelInfo> model_info) {
    const auto& name = model_info->name;

    // Already loaded? Just switch active
    if (loaded_models_.count(name)) {
        active_model_name_ = name;
        RCLCPP_INFO(logger_, "Model '%s' already loaded, switched to active", name.c_str());
        return {true, "Model '" + name + "' already loaded, switched to active"};
    }

    try {
        auto loaded = std::make_unique<LoadedModel>();
        loaded->model_info = model_info;

        // Load FlatBuffer model
        loaded->flat_buffer_model =
            tflite::FlatBufferModel::BuildFromFile(model_info->tflite_path.c_str());
        if (!loaded->flat_buffer_model) {
            return {false, "Failed to load TFLite file: " + model_info->tflite_path};
        }

        // Build interpreter
        tflite::ops::builtin::BuiltinOpResolver resolver;
        tflite::InterpreterBuilder builder(*loaded->flat_buffer_model, resolver);

        int num_threads = std::max(1,
            static_cast<int>(std::thread::hardware_concurrency()) - 2);
        builder.SetNumThreads(num_threads);
        builder(&loaded->interpreter);

        if (!loaded->interpreter) {
            return {false, "Failed to build TFLite interpreter"};
        }

        if (loaded->interpreter->AllocateTensors() != kTfLiteOk) {
            return {false, "Failed to allocate TFLite tensors"};
        }

        // Log input/output tensor details
        const auto& inputs = loaded->interpreter->inputs();
        RCLCPP_INFO(logger_, "TFLite model '%s' loaded with %d threads",
                    name.c_str(), num_threads);
        RCLCPP_INFO(logger_, "Input tensors: %zu", inputs.size());
        for (size_t i = 0; i < inputs.size(); ++i) {
            const auto* tensor = loaded->interpreter->tensor(inputs[i]);
            std::string shape_str = "[";
            for (int d = 0; d < tensor->dims->size; ++d) {
                if (d > 0) shape_str += ", ";
                shape_str += std::to_string(tensor->dims->data[d]);
            }
            shape_str += "]";
            RCLCPP_INFO(logger_, "  [%zu] %s: %s", i, tensor->name, shape_str.c_str());
        }

        const auto& outputs = loaded->interpreter->outputs();
        RCLCPP_INFO(logger_, "Output tensors: %zu", outputs.size());
        for (size_t i = 0; i < outputs.size(); ++i) {
            const auto* tensor = loaded->interpreter->tensor(outputs[i]);
            std::string shape_str = "[";
            for (int d = 0; d < tensor->dims->size; ++d) {
                if (d > 0) shape_str += ", ";
                shape_str += std::to_string(tensor->dims->data[d]);
            }
            shape_str += "]";
            RCLCPP_INFO(logger_, "  [%zu] %s: %s", i, tensor->name, shape_str.c_str());
        }

        active_model_name_ = name;
        inference_count_ = 0;

        // Log all loaded models
        loaded_models_[name] = std::move(loaded);
        std::string loaded_str;
        for (const auto& [n, _] : loaded_models_) {
            if (!loaded_str.empty()) loaded_str += ", ";
            loaded_str += n;
        }
        RCLCPP_INFO(logger_, "Loaded models: [%s]", loaded_str.c_str());

        return {true, "Model loaded successfully"};

    } catch (const std::exception& e) {
        return {false, std::string("Failed to load TFLite model: ") + e.what()};
    }
}

std::pair<bool, std::string>
InferenceEngine::unload_model(const std::string& name) {
    if (name.empty()) {
        // Unload all
        int count = static_cast<int>(loaded_models_.size());
        loaded_models_.clear();
        active_model_name_.clear();
        last_result_.reset();
        RCLCPP_INFO(logger_, "Unloaded all %d models", count);
        return {true, "Unloaded all " + std::to_string(count) + " models"};
    }

    if (!loaded_models_.count(name)) {
        return {false, "Model '" + name + "' is not loaded"};
    }

    loaded_models_.erase(name);

    // If we unloaded the active model, pick another or none
    if (active_model_name_ == name) {
        if (!loaded_models_.empty()) {
            active_model_name_ = loaded_models_.begin()->first;
            RCLCPP_INFO(logger_, "Active model switched to '%s'",
                        active_model_name_.c_str());
        } else {
            active_model_name_.clear();
            last_result_.reset();
        }
    }

    std::string loaded_str;
    for (const auto& [n, _] : loaded_models_) {
        if (!loaded_str.empty()) loaded_str += ", ";
        loaded_str += n;
    }
    RCLCPP_INFO(logger_, "Model '%s' unloaded. Loaded: [%s]",
                name.c_str(), loaded_str.c_str());
    return {true, "Model '" + name + "' unloaded"};
}

std::vector<float> InferenceEngine::preprocess_image(const cv::Mat& image) const {
    cv::Mat gray;
    if (image.channels() == 3) {
        cv::cvtColor(image, gray, cv::COLOR_BGR2GRAY);
    } else {
        gray = image;
    }

    cv::Mat resized;
    cv::resize(gray, resized,
               cv::Size(INFERENCE_IMAGE_WIDTH, INFERENCE_IMAGE_HEIGHT),
               0, 0, cv::INTER_AREA);

    // Keep 0-255 range as float32 (DeepRacer training does NOT normalize)
    cv::Mat float_img;
    resized.convertTo(float_img, CV_32F);

    // Flatten to [1, H, W, 1] contiguous array
    std::vector<float> result(
        INFERENCE_IMAGE_HEIGHT * INFERENCE_IMAGE_WIDTH * INFERENCE_IMAGE_CHANNELS);
    std::memcpy(result.data(), float_img.data, result.size() * sizeof(float));
    return result;
}

std::vector<float> InferenceEngine::preprocess_lidar(
    const std::vector<float>& ranges,
    float angle_min, float angle_increment) const {

    constexpr double target_min_deg = LIDAR_MIN_ANGLE_DEG;  // -150
    constexpr double target_max_deg = LIDAR_MAX_ANGLE_DEG;  // +150
    constexpr int num_values = LIDAR_SECTORS;               // 64
    constexpr double min_dist = LIDAR_MIN_DIST;             // 0.15
    constexpr double max_dist = LIDAR_MAX_DIST;             // 1.0

    // Build arrays of (angle_deg, distance) for samples within target range
    std::vector<double> lidar_angles;
    std::vector<double> lidar_ranges;

    for (size_t i = 0; i < ranges.size(); ++i) {
        double angle_rad = angle_min + static_cast<double>(i) * angle_increment;
        double angle_deg = angle_rad * 180.0 / M_PI;
        // Normalize to -180 ~ +180
        while (angle_deg > 180.0) angle_deg -= 360.0;
        while (angle_deg < -180.0) angle_deg += 360.0;

        // Check if within target range: -150 to +150
        if (angle_deg >= target_min_deg - 0.5 && angle_deg <= target_max_deg + 0.5) {
            double dist = static_cast<double>(ranges[i]);
            double clipped_dist;
            if (!std::isfinite(dist) || dist <= 0.0) {
                clipped_dist = max_dist;
            } else {
                clipped_dist = std::clamp(dist, min_dist, max_dist);
            }
            lidar_angles.push_back(angle_deg);
            lidar_ranges.push_back(clipped_dist);
        }
    }

    // If no samples in range, return max distance array
    if (lidar_angles.size() < 2) {
        return std::vector<float>(num_values, static_cast<float>(max_dist));
    }

    // Sort by angle ascending
    std::vector<size_t> indices(lidar_angles.size());
    std::iota(indices.begin(), indices.end(), 0);
    std::sort(indices.begin(), indices.end(),
              [&](size_t a, size_t b) { return lidar_angles[a] < lidar_angles[b]; });

    std::vector<double> sorted_angles(lidar_angles.size());
    std::vector<double> sorted_ranges(lidar_angles.size());
    for (size_t i = 0; i < indices.size(); ++i) {
        sorted_angles[i] = lidar_angles[indices[i]];
        sorted_ranges[i] = lidar_ranges[indices[i]];
    }

    // Ensure boundary angles exist for interpolation
    if (sorted_angles.front() > target_min_deg) {
        sorted_angles.insert(sorted_angles.begin(), target_min_deg);
        sorted_ranges.insert(sorted_ranges.begin(), max_dist);
    }
    if (sorted_angles.back() < target_max_deg) {
        sorted_angles.push_back(target_max_deg);
        sorted_ranges.push_back(max_dist);
    }

    // Generate target angles (evenly spaced from -150 to +150)
    std::vector<double> target_angles(num_values);
    for (int i = 0; i < num_values; ++i) {
        target_angles[i] = target_min_deg +
            static_cast<double>(i) * (target_max_deg - target_min_deg) /
            static_cast<double>(num_values - 1);
    }

    // Linear interpolation (np.interp equivalent)
    std::vector<float> result(num_values);
    for (int i = 0; i < num_values; ++i) {
        double x = target_angles[i];

        // Find bracketing indices in sorted_angles
        if (x <= sorted_angles.front()) {
            result[i] = static_cast<float>(sorted_ranges.front());
        } else if (x >= sorted_angles.back()) {
            result[i] = static_cast<float>(sorted_ranges.back());
        } else {
            // Binary search for the right bracket
            auto it = std::lower_bound(sorted_angles.begin(), sorted_angles.end(), x);
            size_t idx = static_cast<size_t>(std::distance(sorted_angles.begin(), it));
            if (idx == 0) idx = 1;
            double x0 = sorted_angles[idx - 1];
            double x1 = sorted_angles[idx];
            double y0 = sorted_ranges[idx - 1];
            double y1 = sorted_ranges[idx];
            double t = (x1 != x0) ? (x - x0) / (x1 - x0) : 0.0;
            result[i] = static_cast<float>(y0 + t * (y1 - y0));
        }
    }

    return result;
}

std::optional<InferenceResult> InferenceEngine::run_inference(
    const std::vector<float>& image_input,
    const std::vector<float>* lidar_input) {

    if (active_model_name_.empty() || !loaded_models_.count(active_model_name_)) {
        RCLCPP_ERROR(logger_, "No model loaded");
        return std::nullopt;
    }

    auto& active = loaded_models_[active_model_name_];
    auto* interpreter = active->interpreter.get();

    try {
        // Set inputs by tensor name dispatch
        const auto& inputs = interpreter->inputs();
        for (size_t i = 0; i < inputs.size(); ++i) {
            const auto* tensor = interpreter->tensor(inputs[i]);
            std::string name(tensor->name);
            std::transform(name.begin(), name.end(), name.begin(), ::tolower);

            if (name.find("camera") != std::string::npos ||
                name.find("observation") != std::string::npos) {
                float* input_data = interpreter->typed_tensor<float>(inputs[i]);
                std::memcpy(input_data, image_input.data(),
                           image_input.size() * sizeof(float));
            } else if (name.find("lidar") != std::string::npos) {
                if (lidar_input) {
                    float* input_data = interpreter->typed_tensor<float>(inputs[i]);
                    std::memcpy(input_data, lidar_input->data(),
                               lidar_input->size() * sizeof(float));
                }
            }
        }

        // Run inference
        if (interpreter->Invoke() != kTfLiteOk) {
            RCLCPP_ERROR(logger_, "TFLite Invoke failed");
            return std::nullopt;
        }

        // Get output probabilities
        int output_idx = interpreter->outputs()[0];
        const auto* output_tensor = interpreter->tensor(output_idx);
        const float* output_data = interpreter->typed_tensor<float>(output_idx);
        int output_size = 1;
        for (int d = 0; d < output_tensor->dims->size; ++d) {
            output_size *= output_tensor->dims->data[d];
        }

        std::vector<float> probabilities(output_data, output_data + output_size);

        // Convert to proper probability distribution
        // TFLite PPO output is already softmax, but ensure non-negative and sum to 1
        std::vector<float> probs(probabilities.size());
        for (size_t i = 0; i < probabilities.size(); ++i) {
            probs[i] = std::max(0.0f, probabilities[i]);
        }
        float prob_sum = std::accumulate(probs.begin(), probs.end(), 0.0f);
        if (prob_sum > 0.0f) {
            for (auto& p : probs) p /= prob_sum;
        } else {
            float uniform = 1.0f / static_cast<float>(probs.size());
            std::fill(probs.begin(), probs.end(), uniform);
        }

        const auto& model_info = active->model_info;
        const auto& action_space = model_info->action_space;

        InferenceResult result;
        result.probabilities = probabilities;

        if (action_selection_mode_ == ActionSelectionMode::GREEDY) {
            result.action_index = static_cast<int>(
                std::distance(probs.begin(),
                             std::max_element(probs.begin(), probs.end())));
            result.action = action_space[result.action_index];
            result.speed = result.action.speed;
            result.steering_angle = result.action.steering_angle;

        } else if (action_selection_mode_ == ActionSelectionMode::MEAN) {
            double speed = 0.0, steering = 0.0;
            for (size_t i = 0; i < probs.size(); ++i) {
                speed += probs[i] * action_space[i].speed;
                steering += probs[i] * action_space[i].steering_angle;
            }
            result.speed = speed;
            result.steering_angle = steering;
            result.action_index = static_cast<int>(
                std::distance(probs.begin(),
                             std::max_element(probs.begin(), probs.end())));
            result.action = action_space[result.action_index];

        } else {  // STOCHASTIC
            std::discrete_distribution<int> dist(probs.begin(), probs.end());
            result.action_index = dist(rng_);
            result.action = action_space[result.action_index];
            result.speed = result.action.speed;
            result.steering_angle = result.action.steering_angle;
        }

        inference_count_++;
        last_result_ = result;
        return result;

    } catch (const std::exception& e) {
        RCLCPP_ERROR(logger_, "Inference error: %s", e.what());
        return std::nullopt;
    }
}

std::optional<InferenceResult> InferenceEngine::infer_from_raw(
    const cv::Mat& image,
    const std::vector<float>* ranges,
    float angle_min, float angle_increment) {

    auto proc_image = preprocess_image(image);
    if (ranges) {
        auto proc_lidar = preprocess_lidar(*ranges, angle_min, angle_increment);
        return run_inference(proc_image, &proc_lidar);
    }
    return run_inference(proc_image);
}

bool InferenceEngine::is_loaded() const {
    return !active_model_name_.empty() && loaded_models_.count(active_model_name_);
}

std::shared_ptr<ModelInfo> InferenceEngine::active_model_info() const {
    if (!active_model_name_.empty() && loaded_models_.count(active_model_name_)) {
        return loaded_models_.at(active_model_name_)->model_info;
    }
    return nullptr;
}

std::vector<std::string> InferenceEngine::loaded_model_names() const {
    std::vector<std::string> names;
    for (const auto& [name, _] : loaded_models_) {
        names.push_back(name);
    }
    return names;
}

void InferenceEngine::set_action_selection_mode(ActionSelectionMode mode) {
    action_selection_mode_ = mode;
    const char* mode_str = "greedy";
    if (mode == ActionSelectionMode::STOCHASTIC) mode_str = "stochastic";
    else if (mode == ActionSelectionMode::MEAN) mode_str = "mean";
    RCLCPP_INFO(logger_, "Action selection mode set to: %s", mode_str);
}

}  // namespace physicar_deepracer
