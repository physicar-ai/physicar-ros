#include <chrono>
#include <cmath>
#include <filesystem>
#include <fstream>
#include <functional>
#include <mutex>
#include <string>
#include <vector>

#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/compressed_image.hpp>
#include <sensor_msgs/msg/laser_scan.hpp>
#include <std_msgs/msg/float64.hpp>

#include <physicar_interfaces/msg/deepracer_inference.hpp>
#include <physicar_interfaces/srv/deepracer_load_model.hpp>
#include <physicar_interfaces/srv/deepracer_unload_model.hpp>
#include <physicar_interfaces/srv/deepracer_control.hpp>
#include <physicar_interfaces/srv/deepracer_status.hpp>
#include <physicar_interfaces/srv/deepracer_set_config.hpp>

#include <opencv2/imgcodecs.hpp>
#include <opencv2/imgproc.hpp>
#include <nlohmann/json.hpp>

#include "physicar_deepracer/constants.hpp"
#include "physicar_deepracer/model_loader.hpp"
#include "physicar_deepracer/inference_engine.hpp"

namespace fs = std::filesystem;
using namespace std::chrono_literals;
using namespace physicar_deepracer;

// ---------------------------------------------------------------------------
// DeepracerConfig
// ---------------------------------------------------------------------------
struct DeepracerConfig {
    std::string action_selection = DEFAULT_ACTION_SELECTION;
    double pan = DEFAULT_PAN;
    double tilt = DEFAULT_TILT;
    double speed_percent = DEFAULT_SPEED_PERCENT;
    std::string source = "default";

    nlohmann::json to_json() const {
        return {
            {"action_selection", action_selection},
            {"pan", pan},
            {"tilt", tilt},
            {"speed_percent", speed_percent}
        };
    }

    static DeepracerConfig from_json(const nlohmann::json& data,
                                     const std::string& src = "unknown") {
        DeepracerConfig cfg;
        cfg.source = src;

        // action_selection
        if (data.contains("action_selection") && data["action_selection"].is_string()) {
            std::string v = data["action_selection"].get<std::string>();
            if (v == "greedy" || v == "stochastic" || v == "mean") {
                cfg.action_selection = v;
            }
        }

        auto get_f = [&](const std::string& key, double def, double lo, double hi) -> double {
            if (!data.contains(key) || !data[key].is_number()) return def;
            double v = data[key].get<double>();
            return std::clamp(v, lo, hi);
        };

        cfg.pan = get_f("pan", DEFAULT_PAN, -90.0, 90.0);
        cfg.tilt = get_f("tilt", DEFAULT_TILT, -90.0, 90.0);
        cfg.speed_percent = get_f("speed_percent", DEFAULT_SPEED_PERCENT, 0.0, 200.0);
        return cfg;
    }
};

// ---------------------------------------------------------------------------
// DeepracerNode
// ---------------------------------------------------------------------------
class DeepracerNode : public rclcpp::Node {
public:
    DeepracerNode()
        : Node("deepracer"),
          model_loader_(this->get_logger()),
          inference_engine_(this->get_logger()) {

        RCLCPP_INFO(get_logger(), "DeepRacer node starting...");

        // Parameters (fallback values, JSON file takes priority)
        declare_parameter("action_selection", DEFAULT_ACTION_SELECTION);
        declare_parameter("pan", DEFAULT_PAN);
        declare_parameter("tilt", DEFAULT_TILT);

        // Load configuration (JSON file > ROS params > defaults)
        load_config();
        apply_config();

        // Callback groups
        service_cb_group_ = create_callback_group(
            rclcpp::CallbackGroupType::MutuallyExclusive);
        sensor_cb_group_ = create_callback_group(
            rclcpp::CallbackGroupType::Reentrant);

        // Camera pan/tilt publishers
        pan_pub_ = create_publisher<std_msgs::msg::Float64>("/camera/pan", 10);
        tilt_pub_ = create_publisher<std_msgs::msg::Float64>("/camera/tilt", 10);

        // Speed / steering publishers
        speed_pub_ = create_publisher<std_msgs::msg::Float64>("/speed", 10);
        steering_pub_ = create_publisher<std_msgs::msg::Float64>("/steering", 10);

        // Inference result publisher
        inference_pub_ = create_publisher<physicar_interfaces::msg::DeepracerInference>(
            "/deepracer/inference", 10);

        // Services
        load_model_srv_ = create_service<physicar_interfaces::srv::DeepracerLoadModel>(
            "deepracer/load_model",
            std::bind(&DeepracerNode::load_model_callback, this,
                      std::placeholders::_1, std::placeholders::_2),
            rclcpp::ServicesQoS(), service_cb_group_);

        unload_model_srv_ = create_service<physicar_interfaces::srv::DeepracerUnloadModel>(
            "deepracer/unload_model",
            std::bind(&DeepracerNode::unload_model_callback, this,
                      std::placeholders::_1, std::placeholders::_2),
            rclcpp::ServicesQoS(), service_cb_group_);

        control_srv_ = create_service<physicar_interfaces::srv::DeepracerControl>(
            "deepracer/control",
            std::bind(&DeepracerNode::control_callback, this,
                      std::placeholders::_1, std::placeholders::_2),
            rclcpp::ServicesQoS(), service_cb_group_);

        status_srv_ = create_service<physicar_interfaces::srv::DeepracerStatus>(
            "deepracer/status",
            std::bind(&DeepracerNode::status_callback, this,
                      std::placeholders::_1, std::placeholders::_2),
            rclcpp::ServicesQoS(), service_cb_group_);

        set_config_srv_ = create_service<physicar_interfaces::srv::DeepracerSetConfig>(
            "deepracer/set_config",
            std::bind(&DeepracerNode::set_config_callback, this,
                      std::placeholders::_1, std::placeholders::_2),
            rclcpp::ServicesQoS(), service_cb_group_);

        // Sensor QoS (match camera/lidar publishers which use BEST_EFFORT)
        rclcpp::QoS sensor_qos(1);
        sensor_qos.reliability(rclcpp::ReliabilityPolicy::BestEffort);
        sensor_qos.history(rclcpp::HistoryPolicy::KeepLast);

        rclcpp::SubscriptionOptions sub_opts;
        sub_opts.callback_group = sensor_cb_group_;

        // Subscribers
        image_sub_ = create_subscription<sensor_msgs::msg::CompressedImage>(
            "/camera/image_raw/compressed", sensor_qos,
            std::bind(&DeepracerNode::image_callback, this, std::placeholders::_1),
            sub_opts);

        scan_sub_ = create_subscription<sensor_msgs::msg::LaserScan>(
            "/scan_filtered", sensor_qos,
            std::bind(&DeepracerNode::scan_callback, this, std::placeholders::_1),
            sub_opts);

        // Inference timer (runs at max rate)
        inference_timer_ = create_wall_timer(
            std::chrono::duration<double>(inference_interval_),
            std::bind(&DeepracerNode::inference_loop, this),
            sensor_cb_group_);

        // Camera-hold timer: 5Hz while inference is running
        camera_hold_timer_ = create_wall_timer(
            200ms,
            std::bind(&DeepracerNode::camera_hold_tick, this),
            sensor_cb_group_);

        RCLCPP_INFO(get_logger(), "DeepRacer node ready");
        RCLCPP_INFO(get_logger(), "Models path: %s", MODELS_BASE_PATH.c_str());
        auto models = model_loader_.list_models();
        std::string models_str;
        for (const auto& m : models) {
            if (!models_str.empty()) models_str += ", ";
            models_str += m;
        }
        RCLCPP_INFO(get_logger(), "Available models: [%s]", models_str.c_str());
    }

private:
    // -----------------------------------------------------------------------
    // Service callbacks
    // -----------------------------------------------------------------------
    void load_model_callback(
        const physicar_interfaces::srv::DeepracerLoadModel::Request::SharedPtr request,
        physicar_interfaces::srv::DeepracerLoadModel::Response::SharedPtr response) {

        RCLCPP_INFO(get_logger(), "Loading model: %s", request->model_name.c_str());

        try {
            std::lock_guard<std::mutex> lock(lock_);

            auto [success, message, model_info] =
                model_loader_.load_model(request->model_name);

            if (!success) {
                response->success = false;
                response->message = message;
                response->model_path = "";
                response->action_space_json = "[]";
                return;
            }

            auto [load_ok, load_msg] = inference_engine_.load_model(model_info);
            if (!load_ok) {
                response->success = false;
                response->message = load_msg;
                response->model_path = "";
                response->action_space_json = "[]";
                return;
            }

            loaded_models_[model_info->name] = model_info;

            response->success = true;
            response->message = load_msg;
            response->model_path = model_info->tflite_path;

            nlohmann::json action_space_list = nlohmann::json::array();
            for (const auto& a : model_info->action_space) {
                action_space_list.push_back({
                    {"speed", a.speed},
                    {"steering_angle", a.steering_angle}
                });
            }
            response->action_space_json = action_space_list.dump();

            std::string loaded_str;
            for (const auto& [n, _] : loaded_models_) {
                if (!loaded_str.empty()) loaded_str += ", ";
                loaded_str += n;
            }
            RCLCPP_INFO(get_logger(), "Loaded models: [%s]", loaded_str.c_str());

        } catch (const std::exception& e) {
            RCLCPP_ERROR(get_logger(), "Exception in load_model: %s", e.what());
            response->success = false;
            response->message = std::string("Internal error: ") + e.what();
            response->model_path = "";
            response->action_space_json = "[]";
        }
    }

    void unload_model_callback(
        const physicar_interfaces::srv::DeepracerUnloadModel::Request::SharedPtr request,
        physicar_interfaces::srv::DeepracerUnloadModel::Response::SharedPtr response) {

        const auto& name = request->model_name;
        RCLCPP_INFO(get_logger(), "Unloading model: %s", name.empty() ? "(all)" : name.c_str());

        try {
            std::lock_guard<std::mutex> lock(lock_);

            // Stop inference if unloading active model or all models
            auto active = inference_engine_.active_model_name();
            if (name.empty() || name == active) {
                if (inference_running_) {
                    inference_running_ = false;
                    publish_float64(speed_pub_, 0.0);
                    publish_float64(steering_pub_, 0.0);
                    RCLCPP_INFO(get_logger(), "Stopped inference (active model unloaded)");
                }
            }

            auto [success, message] = inference_engine_.unload_model(name);
            if (success) {
                if (name.empty()) {
                    loaded_models_.clear();
                } else {
                    loaded_models_.erase(name);
                }
            }
            response->success = success;
            response->message = message;

        } catch (const std::exception& e) {
            RCLCPP_ERROR(get_logger(), "Exception in unload_model: %s", e.what());
            response->success = false;
            response->message = std::string("Internal error: ") + e.what();
        }
    }

    void control_callback(
        const physicar_interfaces::srv::DeepracerControl::Request::SharedPtr request,
        physicar_interfaces::srv::DeepracerControl::Response::SharedPtr response) {

        std::lock_guard<std::mutex> lock(lock_);

        if (request->start) {
            if (!inference_engine_.is_loaded()) {
                response->success = false;
                response->message = "No model loaded. Call load_model first.";
                return;
            }
            set_camera_position();
            inference_running_ = true;
            response->success = true;
            response->message = "Inference started";
            RCLCPP_INFO(get_logger(), "Inference started");
        } else {
            inference_running_ = false;
            publish_float64(speed_pub_, 0.0);
            publish_float64(steering_pub_, 0.0);
            reset_camera_position();
            response->success = true;
            response->message = "Inference stopped";
            RCLCPP_INFO(get_logger(), "Inference stopped");
        }
    }

    void status_callback(
        const physicar_interfaces::srv::DeepracerStatus::Request::SharedPtr /*request*/,
        physicar_interfaces::srv::DeepracerStatus::Response::SharedPtr response) {

        std::lock_guard<std::mutex> lock(lock_);

        response->model_loaded = inference_engine_.is_loaded();
        response->inference_running = inference_running_;

        auto active_info = inference_engine_.active_model_info();
        if (active_info) {
            response->model_name = active_info->name;
            response->model_path = active_info->tflite_path;
            response->action_count = static_cast<int32_t>(active_info->action_space.size());
            nlohmann::json action_space_list = nlohmann::json::array();
            for (const auto& a : active_info->action_space) {
                action_space_list.push_back({
                    {"speed", a.speed},
                    {"steering_angle", a.steering_angle}
                });
            }
            response->action_space_json = action_space_list.dump();
        } else {
            response->model_name = "";
            response->model_path = "";
            response->action_count = 0;
            response->action_space_json = "[]";
        }

        response->loaded_models_json =
            nlohmann::json(inference_engine_.loaded_model_names()).dump();
        response->inference_rate = static_cast<float>(inference_rate_actual_);
        response->inference_count = inference_engine_.inference_count();

        auto last = inference_engine_.last_result();
        if (last.has_value()) {
            nlohmann::json obj = {
                {"index", last->action_index},
                {"speed", last->speed},
                {"steering_angle", last->steering_angle}
            };
            response->last_action = obj.dump();
        } else {
            response->last_action = "";
        }

        // Configuration fields
        response->action_selection_mode = config_.action_selection;
        response->config_source = config_.source;
        response->camera_pan = static_cast<float>(config_.pan);
        response->camera_tilt = static_cast<float>(config_.tilt);
        response->speed_percent = static_cast<float>(config_.speed_percent);
    }

    void set_config_callback(
        const physicar_interfaces::srv::DeepracerSetConfig::Request::SharedPtr request,
        physicar_interfaces::srv::DeepracerSetConfig::Response::SharedPtr response) {

        std::string key = request->key;
        std::transform(key.begin(), key.end(), key.begin(), ::tolower);

        std::lock_guard<std::mutex> lock(lock_);

        if (key == "all") {
            load_config();
            apply_config();
            response->success = true;
            response->message = "Config reloaded from " + config_.source;

        } else if (key == "action_selection") {
            std::string value = request->string_value;
            std::transform(value.begin(), value.end(), value.begin(), ::tolower);
            if (value != "greedy" && value != "stochastic" && value != "mean") {
                response->success = false;
                response->message = "Invalid action_selection: '" + value +
                    "'. Use 'greedy', 'stochastic', or 'mean'";
                return;
            }
            config_.action_selection = value;
            apply_config();
            response->success = true;
            response->message = "action_selection set to '" + value + "'";

        } else if (key == "pan") {
            double value = request->float_value;
            if (value < -30.0 || value > 30.0) {
                response->success = false;
                response->message = "pan " + std::to_string(value) +
                    " out of range [-30.0, 30.0]";
                return;
            }
            config_.pan = value;
            response->success = true;
            response->message = "pan set to " + std::to_string(value) + "°";

        } else if (key == "tilt") {
            double value = request->float_value;
            if (value < -30.0 || value > 30.0) {
                response->success = false;
                response->message = "tilt " + std::to_string(value) +
                    " out of range [-30.0, 30.0]";
                return;
            }
            config_.tilt = value;
            response->success = true;
            response->message = "tilt set to " + std::to_string(value) + "°";

        } else if (key == "speed_percent") {
            double value = request->float_value;
            if (value < 50.0 || value > 150.0) {
                response->success = false;
                response->message = "speed_percent " + std::to_string(value) +
                    " out of range [50, 150]";
                return;
            }
            config_.speed_percent = value;
            response->success = true;
            response->message = "speed_percent set to " +
                std::to_string(static_cast<int>(value)) + "%";

        } else {
            response->success = false;
            response->message = "Unknown key: '" + key +
                "'. Valid keys: action_selection, pan, tilt, speed_percent, all";
            return;
        }

        // Save to file if requested
        if (request->save_to_file) {
            if (save_config()) {
                response->message += " (saved to " + CONFIG_FILE_PATH + ")";
            } else {
                response->message += " (save failed)";
            }
        }

        RCLCPP_INFO(get_logger(), "%s", response->message.c_str());
    }

    // -----------------------------------------------------------------------
    // Configuration
    // -----------------------------------------------------------------------
    void load_config() {
        if (fs::exists(CONFIG_FILE_PATH)) {
            try {
                std::ifstream f(CONFIG_FILE_PATH);
                auto data = nlohmann::json::parse(f);
                if (!data.is_object()) {
                    throw std::runtime_error("config file is not a JSON object");
                }
                config_ = DeepracerConfig::from_json(data, "json_file");
                RCLCPP_INFO(get_logger(), "Loaded config from %s",
                            CONFIG_FILE_PATH.c_str());
            } catch (const std::exception& e) {
                RCLCPP_ERROR(get_logger(),
                    "Failed to load %s (%s); using ROS params",
                    CONFIG_FILE_PATH.c_str(), e.what());
                config_ = config_from_params();
            }
        } else {
            RCLCPP_INFO(get_logger(),
                "Config file not found: %s, using ROS parameters",
                CONFIG_FILE_PATH.c_str());
            config_ = config_from_params();
        }
    }

    DeepracerConfig config_from_params() {
        DeepracerConfig cfg;
        cfg.action_selection = get_parameter("action_selection").as_string();
        cfg.pan = get_parameter("pan").as_double();
        cfg.tilt = get_parameter("tilt").as_double();
        cfg.source = "ros_params";
        return cfg;
    }

    void apply_config() {
        if (config_.action_selection == "stochastic") {
            inference_engine_.set_action_selection_mode(ActionSelectionMode::STOCHASTIC);
        } else if (config_.action_selection == "mean") {
            inference_engine_.set_action_selection_mode(ActionSelectionMode::MEAN);
        } else {
            inference_engine_.set_action_selection_mode(ActionSelectionMode::GREEDY);
        }

        RCLCPP_INFO(get_logger(), "Config applied (source: %s):", config_.source.c_str());
        RCLCPP_INFO(get_logger(), "  action_selection: %s", config_.action_selection.c_str());
        RCLCPP_INFO(get_logger(), "  pan: %.1f°", config_.pan);
        RCLCPP_INFO(get_logger(), "  tilt: %.1f°", config_.tilt);
        RCLCPP_INFO(get_logger(), "  speed_percent: %.0f", config_.speed_percent);
    }

    bool save_config() {
        try {
            auto config_dir = fs::path(CONFIG_FILE_PATH).parent_path();
            fs::create_directories(config_dir);
            std::ofstream f(CONFIG_FILE_PATH);
            f << config_.to_json().dump(4);
            config_.source = "json_file";
            RCLCPP_INFO(get_logger(), "Config saved to %s", CONFIG_FILE_PATH.c_str());
            return true;
        } catch (const std::exception& e) {
            RCLCPP_ERROR(get_logger(), "Failed to save config: %s", e.what());
            return false;
        }
    }

    // -----------------------------------------------------------------------
    // Camera position
    // -----------------------------------------------------------------------
    void set_camera_position() {
        double pan_rad = config_.pan * M_PI / 180.0;
        double tilt_rad = config_.tilt * M_PI / 180.0;
        publish_float64(pan_pub_, pan_rad);
        publish_float64(tilt_pub_, tilt_rad);
        RCLCPP_INFO(get_logger(),
            "Camera position set: pan=%.1f° (%.3f rad), tilt=%.1f° (%.3f rad)",
            config_.pan, pan_rad, config_.tilt, tilt_rad);
    }

    void reset_camera_position() {
        publish_float64(pan_pub_, 0.0);
        publish_float64(tilt_pub_, 0.0);
        RCLCPP_INFO(get_logger(), "Camera position reset to neutral (0°, 0°)");
    }

    void camera_hold_tick() {
        if (!inference_running_) return;
        double pan_rad = config_.pan * M_PI / 180.0;
        double tilt_rad = config_.tilt * M_PI / 180.0;
        publish_float64(pan_pub_, pan_rad);
        publish_float64(tilt_pub_, tilt_rad);
    }

    // -----------------------------------------------------------------------
    // Sensor callbacks
    // -----------------------------------------------------------------------
    void image_callback(const sensor_msgs::msg::CompressedImage::SharedPtr msg) {
        if (inference_running_) {
            try {
                cv::Mat raw_data(1, static_cast<int>(msg->data.size()), CV_8UC1,
                                 const_cast<uint8_t*>(msg->data.data()));
                latest_image_ = cv::imdecode(raw_data, cv::IMREAD_COLOR);
            } catch (const std::exception& e) {
                RCLCPP_ERROR(get_logger(), "Image decode error: %s", e.what());
                return;
            }
        }
        // Always update stamp (used for freshness check)
        image_stamp_ = rclcpp::Time(msg->header.stamp);
        image_stamp_valid_ = true;
    }

    void scan_callback(const sensor_msgs::msg::LaserScan::SharedPtr msg) {
        latest_scan_ = msg;
        scan_stamp_ = rclcpp::Time(msg->header.stamp);
        scan_stamp_valid_ = true;
    }

    // -----------------------------------------------------------------------
    // Inference loop
    // -----------------------------------------------------------------------
    void inference_loop() {
        if (!inference_running_) return;

        double current_time = now().seconds();

        // Check image availability
        if (latest_image_.empty() || !image_stamp_valid_) {
            if (!image_missing_logged_) {
                RCLCPP_WARN(get_logger(), "No camera image received yet, waiting...");
                image_missing_logged_ = true;
            }
            return;
        }

        // Check image freshness
        double image_age = (now() - image_stamp_).seconds();
        if (image_age > image_timeout_) {
            if (inference_running_) {
                RCLCPP_ERROR(get_logger(),
                    "Camera image timeout (%.1fs > %.1fs). Auto-stopping inference.",
                    image_age, image_timeout_);
                inference_running_ = false;
                publish_float64(speed_pub_, 0.0);
                publish_float64(steering_pub_, 0.0);
            }
            return;
        }
        image_missing_logged_ = false;

        // Check LiDAR availability (only if model uses LiDAR)
        auto active_info = inference_engine_.active_model_info();
        bool model_has_lidar = active_info && active_info->has_lidar;
        bool use_fallback_lidar = false;

        if (model_has_lidar) {
            if (!latest_scan_ || !scan_stamp_valid_) {
                use_fallback_lidar = true;
                if (!lidar_missing_logged_) {
                    RCLCPP_WARN(get_logger(),
                        "No LiDAR data received, using fallback (max distance)");
                    lidar_missing_logged_ = true;
                }
            } else {
                double lidar_age = (now() - scan_stamp_).seconds();
                if (lidar_age > lidar_timeout_) {
                    use_fallback_lidar = true;
                    if (!lidar_missing_logged_) {
                        RCLCPP_WARN(get_logger(),
                            "LiDAR data stale (%.1fs), using fallback", lidar_age);
                        lidar_missing_logged_ = true;
                    }
                } else {
                    lidar_missing_logged_ = false;
                }
            }
        }

        std::lock_guard<std::mutex> lock(lock_);
        if (!inference_running_) return;

        try {
            std::optional<InferenceResult> result;

            if (!model_has_lidar) {
                // Camera-only model
                auto image_input = inference_engine_.preprocess_image(latest_image_);
                result = inference_engine_.run_inference(image_input);
            } else if (use_fallback_lidar) {
                // Camera+LiDAR model, but LiDAR missing: use fallback
                std::vector<float> lidar_fallback(
                    LIDAR_SECTORS, static_cast<float>(LIDAR_MAX_DIST));
                auto image_input = inference_engine_.preprocess_image(latest_image_);
                result = inference_engine_.run_inference(image_input, &lidar_fallback);
            } else {
                // Camera+LiDAR model, normal path
                std::vector<float> ranges(latest_scan_->ranges.begin(),
                                          latest_scan_->ranges.end());
                result = inference_engine_.infer_from_raw(
                    latest_image_, &ranges,
                    latest_scan_->angle_min, latest_scan_->angle_increment);
            }

            if (!result.has_value()) return;

            // Publish speed (m/s, multiplied by speed_percent)
            double speed = result->speed * (config_.speed_percent / 100.0);
            publish_float64(speed_pub_, speed);

            // Publish steering (degrees -> radians)
            double steering_rad = result->steering_angle * M_PI / 180.0;
            publish_float64(steering_pub_, steering_rad);

            // Publish inference result message
            auto inference_msg = physicar_interfaces::msg::DeepracerInference();
            inference_msg.header.stamp = this->now();
            inference_msg.speed = static_cast<float>(result->speed);
            inference_msg.steering_angle = static_cast<float>(result->steering_angle);
            inference_msg.probabilities = result->probabilities;
            inference_pub_->publish(inference_msg);

            // Update timing
            double elapsed = current_time - last_inference_time_;
            if (elapsed > 0.0) {
                inference_rate_actual_ = 1.0 / elapsed;
            }
            last_inference_time_ = current_time;

        } catch (const std::exception& e) {
            RCLCPP_ERROR(get_logger(), "Inference loop error: %s", e.what());
        }
    }

    // -----------------------------------------------------------------------
    // Utility
    // -----------------------------------------------------------------------
    void publish_float64(
        rclcpp::Publisher<std_msgs::msg::Float64>::SharedPtr pub, double value) {
        auto msg = std_msgs::msg::Float64();
        msg.data = value;
        pub->publish(msg);
    }

    // -----------------------------------------------------------------------
    // Members
    // -----------------------------------------------------------------------
    ModelLoader model_loader_;
    InferenceEngine inference_engine_;
    DeepracerConfig config_;

    // State
    std::atomic<bool> inference_running_{false};
    std::map<std::string, std::shared_ptr<ModelInfo>> loaded_models_;
    std::mutex lock_;

    // Latest sensor data
    cv::Mat latest_image_;
    sensor_msgs::msg::LaserScan::SharedPtr latest_scan_;
    rclcpp::Time image_stamp_{0, 0, RCL_ROS_TIME};
    rclcpp::Time scan_stamp_{0, 0, RCL_ROS_TIME};
    bool image_stamp_valid_ = false;
    bool scan_stamp_valid_ = false;

    // Timing
    double last_inference_time_ = 0.0;
    double inference_interval_ = 1.0 / MAX_INFERENCE_RATE_HZ;
    double inference_rate_actual_ = 0.0;

    // Sensor timeout tracking
    double image_timeout_ = 2.0;
    double lidar_timeout_ = 5.0;
    bool image_missing_logged_ = false;
    bool lidar_missing_logged_ = false;

    // Callback groups
    rclcpp::CallbackGroup::SharedPtr service_cb_group_;
    rclcpp::CallbackGroup::SharedPtr sensor_cb_group_;

    // Publishers
    rclcpp::Publisher<std_msgs::msg::Float64>::SharedPtr pan_pub_;
    rclcpp::Publisher<std_msgs::msg::Float64>::SharedPtr tilt_pub_;
    rclcpp::Publisher<std_msgs::msg::Float64>::SharedPtr speed_pub_;
    rclcpp::Publisher<std_msgs::msg::Float64>::SharedPtr steering_pub_;
    rclcpp::Publisher<physicar_interfaces::msg::DeepracerInference>::SharedPtr inference_pub_;

    // Subscribers
    rclcpp::Subscription<sensor_msgs::msg::CompressedImage>::SharedPtr image_sub_;
    rclcpp::Subscription<sensor_msgs::msg::LaserScan>::SharedPtr scan_sub_;

    // Services
    rclcpp::Service<physicar_interfaces::srv::DeepracerLoadModel>::SharedPtr load_model_srv_;
    rclcpp::Service<physicar_interfaces::srv::DeepracerUnloadModel>::SharedPtr unload_model_srv_;
    rclcpp::Service<physicar_interfaces::srv::DeepracerControl>::SharedPtr control_srv_;
    rclcpp::Service<physicar_interfaces::srv::DeepracerStatus>::SharedPtr status_srv_;
    rclcpp::Service<physicar_interfaces::srv::DeepracerSetConfig>::SharedPtr set_config_srv_;

    // Timers
    rclcpp::TimerBase::SharedPtr inference_timer_;
    rclcpp::TimerBase::SharedPtr camera_hold_timer_;
};

// ===========================================================================
// main
// ===========================================================================
int main(int argc, char** argv) {
    rclcpp::init(argc, argv);

    try {
        auto node = std::make_shared<DeepracerNode>();

        rclcpp::executors::MultiThreadedExecutor executor(
            rclcpp::ExecutorOptions(), 4);
        executor.add_node(node);

        executor.spin();
        rclcpp::shutdown();
    } catch (const std::exception& e) {
        RCLCPP_FATAL(rclcpp::get_logger("deepracer"), "Fatal: %s", e.what());
        rclcpp::shutdown();
        return 1;
    }

    return 0;
}
