// joy_teleop_node.cpp — 1:1 port of joy_teleop_node.py
//
// Copyright 2026 AICASTLE Inc.
// Licensed under the Apache License, Version 2.0

#include <algorithm>
#include <chrono>
#include <cmath>
#include <fstream>
#include <functional>
#include <map>
#include <mutex>
#include <set>
#include <string>
#include <filesystem>

#include "rclcpp/rclcpp.hpp"
#include "rcl_interfaces/msg/set_parameters_result.hpp"
#include "sensor_msgs/msg/joy.hpp"
#include "std_msgs/msg/float64.hpp"
#include "builtin_interfaces/msg/duration.hpp"

#include "physicar_interfaces/msg/joy_teleop_status.hpp"
#include "physicar_interfaces/msg/teleop_status.hpp"
#include "physicar_interfaces/srv/get_joy_mapping.hpp"
#include "physicar_interfaces/srv/set_joy_mapping.hpp"

#include "nlohmann/json.hpp"

using json = nlohmann::json;
using namespace std::chrono_literals;
using std::placeholders::_1;
using std::placeholders::_2;

static constexpr const char * MAPPING_FILE =
  "/opt/physicar/userdata/joy_mapping.json";

static const std::set<std::string> INT_KEYS = {
  "axis_speed", "axis_steering", "axis_pan", "axis_tilt",
  "deadman_button", "estop_button", "center_camera_button",
  "camera_assist_button",
};
static const std::set<std::string> FLOAT_KEYS = {
  "max_speed", "max_steering", "max_pan", "max_tilt", "deadzone", "rate",
};
static const std::set<std::string> BOOL_KEYS = {
  "invert_speed", "invert_steering", "invert_pan", "invert_tilt",
};

static bool is_known_key(const std::string & k) {
  return INT_KEYS.count(k) || FLOAT_KEYS.count(k) || BOOL_KEYS.count(k);
}

static double clip(double v, double lim) {
  if (v > lim) return lim;
  if (v < -lim) return -lim;
  return v;
}

static double deg2rad(double d) { return d * M_PI / 180.0; }

// ── Value wrapper (int | float | bool) ──

struct Val {
  enum Type { INT, FLOAT, BOOL } type;
  int64_t i = 0;
  double f = 0.0;
  bool b = false;

  Val() : type(INT) {}
  Val(int64_t v) : type(INT), i(v), f(static_cast<double>(v)) {}
  Val(double v) : type(FLOAT), f(v) {}
  Val(bool v) : type(BOOL), b(v) {}

  int64_t as_int() const { return (type == INT) ? i : static_cast<int64_t>(f); }
  double as_float() const { return (type == FLOAT) ? f : static_cast<double>(i); }
  bool as_bool() const { return (type == BOOL) ? b : (i != 0); }

  json to_json() const {
    switch (type) {
      case INT: return i;
      case FLOAT: return f;
      case BOOL: return b;
    }
    return nullptr;
  }
};

static Val coerce(const std::string & key, const json & value) {
  if (INT_KEYS.count(key)) {
    int64_t iv = value.get<int64_t>();
    if (iv < -1) throw std::runtime_error("must be >= -1");
    return Val(iv);
  }
  if (FLOAT_KEYS.count(key)) {
    double fv = value.get<double>();
    if (fv < 0.0) throw std::runtime_error("must be >= 0");
    return Val(fv);
  }
  if (BOOL_KEYS.count(key)) {
    return Val(value.get<bool>());
  }
  throw std::runtime_error("unknown mapping key: " + key);
}


class JoyTeleopNode : public rclcpp::Node {
public:
  JoyTeleopNode() : Node("physicar_joy_teleop") {
    // ── Parameter defaults ──
    declare_parameter("max_speed", 2.0);
    declare_parameter("max_steering", 20.0);
    declare_parameter("max_pan", 30.0);
    declare_parameter("max_tilt", 30.0);
    declare_parameter("axis_speed", 1);
    declare_parameter("axis_steering", 0);
    declare_parameter("axis_pan", 3);
    declare_parameter("axis_tilt", 4);
    declare_parameter("invert_speed", false);
    declare_parameter("invert_steering", false);
    declare_parameter("invert_pan", false);
    declare_parameter("invert_tilt", false);
    declare_parameter("deadzone", 0.05);
    declare_parameter("deadman_button", 4);
    declare_parameter("camera_assist_button", 5);
    declare_parameter("estop_button", 1);
    declare_parameter("center_camera_button", 7);
    declare_parameter("rate", 30.0);

    // Load defaults into mapping_
    for (auto & k : INT_KEYS)
      mapping_[k] = Val(get_parameter(k).as_int());
    for (auto & k : FLOAT_KEYS)
      mapping_[k] = Val(get_parameter(k).as_double());
    for (auto & k : BOOL_KEYS)
      mapping_[k] = Val(get_parameter(k).as_bool());

    mapping_source_ = "default";
    mapping_units_ = "degrees";

    load_mapping_from_file();
    migrate_legacy_radians();

    // ── enabled parameter ──
    declare_parameter("enabled", true);
    enabled_ = get_parameter("enabled").as_bool();
    param_cb_handle_ = add_on_set_parameters_callback(
      std::bind(&JoyTeleopNode::on_param_set, this, _1));

    // ── Publishers ──
    pub_speed_ = create_publisher<std_msgs::msg::Float64>("/teleop/speed", 10);
    pub_steer_ = create_publisher<std_msgs::msg::Float64>("/teleop/steering", 10);
    pub_pan_ = create_publisher<std_msgs::msg::Float64>("/teleop/camera/pan", 10);
    pub_tilt_ = create_publisher<std_msgs::msg::Float64>("/teleop/camera/tilt", 10);

    // JoyTeleopStatus — TRANSIENT_LOCAL (latched)
    auto joy_status_qos = rclcpp::QoS(1)
      .reliable()
      .transient_local();
    pub_status_ = create_publisher<physicar_interfaces::msg::JoyTeleopStatus>(
      "~/status", joy_status_qos);

    // TeleopStatus — RELIABLE, KEEP_LAST(1), VOLATILE
    auto teleop_status_qos = rclcpp::QoS(1).reliable();
    pub_teleop_status_ = create_publisher<physicar_interfaces::msg::TeleopStatus>(
      "/teleop/status", teleop_status_qos);

    // ── Subscription ──
    sub_joy_ = create_subscription<sensor_msgs::msg::Joy>(
      "/joy", 10, std::bind(&JoyTeleopNode::on_joy, this, _1));

    // ── Services ──
    srv_get_ = create_service<physicar_interfaces::srv::GetJoyMapping>(
      "~/get_mapping", std::bind(&JoyTeleopNode::srv_get, this, _1, _2));
    srv_set_ = create_service<physicar_interfaces::srv::SetJoyMapping>(
      "~/set_mapping", std::bind(&JoyTeleopNode::srv_set, this, _1, _2));

    // ── Timer ──
    restart_timer();

    // First status emit
    publish_status(true);

    RCLCPP_INFO(get_logger(),
      "Joy teleop ready (source=%s, enabled=%s, deadman=button %ld, "
      "camera_assist=button %ld, estop=button %ld, "
      "max_speed=%.1f m/s, max_steering=%.1f°)",
      mapping_source_.c_str(), enabled_ ? "true" : "false",
      mapping_["deadman_button"].as_int(),
      mapping_["camera_assist_button"].as_int(),
      mapping_["estop_button"].as_int(),
      mapping_["max_speed"].as_float(),
      mapping_["max_steering"].as_float());
  }

private:
  // ── Timer ──
  void restart_timer() {
    if (timer_) timer_->cancel();
    double rate = std::max(1.0, mapping_["rate"].as_float());
    timer_ = create_wall_timer(
      std::chrono::duration<double>(1.0 / rate),
      std::bind(&JoyTeleopNode::tick, this));
  }

  // ── Persistence ──
  void load_mapping_from_file() {
    if (!std::filesystem::is_regular_file(MAPPING_FILE)) return;
    std::ifstream ifs(MAPPING_FILE);
    if (!ifs.is_open()) {
      RCLCPP_ERROR(get_logger(), "%s cannot be opened; using defaults", MAPPING_FILE);
      return;
    }
    json data;
    try { data = json::parse(ifs); }
    catch (const json::parse_error & e) {
      RCLCPP_ERROR(get_logger(), "%s parse error (%s); using defaults", MAPPING_FILE, e.what());
      return;
    }
    if (!data.is_object()) {
      RCLCPP_ERROR(get_logger(), "%s top-level is not a JSON object; using defaults", MAPPING_FILE);
      return;
    }

    // units
    if (data.contains("units") && data["units"].is_string()) {
      auto u = data["units"].get<std::string>();
      if (u == "degrees" || u == "radians") mapping_units_ = u;
      else mapping_units_ = "radians";
    } else {
      mapping_units_ = "radians";
    }

    // Validate all first, then commit
    std::map<std::string, Val> validated;
    for (auto & [k, v] : data.items()) {
      if (!is_known_key(k)) continue;
      try {
        validated[k] = coerce(k, v);
      } catch (const std::exception & e) {
        RCLCPP_ERROR(get_logger(), "%s invalid %s (%s); using defaults", MAPPING_FILE, k.c_str(), e.what());
        return;
      }
    }
    for (auto & [k, v] : validated) mapping_[k] = v;
    mapping_source_ = "file";
    RCLCPP_INFO(get_logger(), "Loaded mapping from %s", MAPPING_FILE);
  }

  void migrate_legacy_radians() {
    if (mapping_units_ == "degrees") return;
    double steer = mapping_["max_steering"].as_float();
    if (steer >= 2.0) {
      mapping_units_ = "degrees";
      save_mapping_to_file();
      return;
    }
    for (auto & key : {"max_steering", "max_pan", "max_tilt"}) {
      double v = mapping_[key].as_float();
      if (v > 0.0)
        mapping_[key] = Val(std::round(v * 180.0 / M_PI * 10.0) / 10.0);
    }
    mapping_units_ = "degrees";
    RCLCPP_INFO(get_logger(),
      "Migrated legacy radian limits to degrees: "
      "max_steering=%.1f°, max_pan=%.1f°, max_tilt=%.1f°",
      mapping_["max_steering"].as_float(),
      mapping_["max_pan"].as_float(),
      mapping_["max_tilt"].as_float());
    auto [ok, msg] = save_mapping_to_file();
    if (!ok) RCLCPP_WARN(get_logger(), "Could not persist migrated mapping: %s", msg.c_str());
  }

  std::pair<bool, std::string> save_mapping_to_file() {
    try {
      std::filesystem::create_directories(std::filesystem::path(MAPPING_FILE).parent_path());
      std::string tmp = std::string(MAPPING_FILE) + ".tmp";
      json payload;
      for (auto & [k, v] : mapping_) payload[k] = v.to_json();
      payload["units"] = mapping_units_;
      std::ofstream ofs(tmp);
      if (!ofs.is_open()) return {false, "Cannot open tmp file"};
      ofs << payload.dump(2) << "\n";
      ofs.close();
      std::filesystem::rename(tmp, MAPPING_FILE);
      mapping_source_ = "file";
      return {true, std::string("Saved to ") + MAPPING_FILE};
    } catch (const std::exception & e) {
      return {false, std::string("Save failed: ") + e.what()};
    }
  }

  json mapping_to_json() const {
    json j;
    for (auto & [k, v] : mapping_) j[k] = v.to_json();
    return j;
  }

  // ── Parameter callback ──
  rcl_interfaces::msg::SetParametersResult on_param_set(
      const std::vector<rclcpp::Parameter> & params) {
    for (auto & p : params) {
      if (p.get_name() == "enabled" && p.get_type() == rclcpp::ParameterType::PARAMETER_BOOL) {
        if (enabled_ && !p.as_bool()) {
          auto z = std_msgs::msg::Float64();
          z.data = 0.0;
          pub_speed_->publish(z);
          pub_steer_->publish(z);
        }
        enabled_ = p.as_bool();
        RCLCPP_INFO(get_logger(), "enabled = %s", enabled_ ? "true" : "false");
        publish_status(true);
      }
    }
    rcl_interfaces::msg::SetParametersResult result;
    result.successful = true;
    return result;
  }

  // ── Service handlers ──
  void srv_get(
      const physicar_interfaces::srv::GetJoyMapping::Request::SharedPtr,
      physicar_interfaces::srv::GetJoyMapping::Response::SharedPtr resp) {
    resp->success = true;
    resp->message = "";
    resp->source = mapping_source_;
    resp->mapping_json = mapping_to_json().dump();
  }

  void srv_set(
      const physicar_interfaces::srv::SetJoyMapping::Request::SharedPtr req,
      physicar_interfaces::srv::SetJoyMapping::Response::SharedPtr resp) {
    try {
      std::map<std::string, Val> updates;
      if (!req->mapping_json.empty()) {
        auto bulk = json::parse(req->mapping_json);
        if (!bulk.is_object()) throw std::runtime_error("mapping_json must be a JSON object");
        for (auto & [k, v] : bulk.items()) {
          if (!is_known_key(k)) throw std::runtime_error("unknown key: " + k);
          updates[k] = coerce(k, v);
        }
      } else if (!req->key.empty()) {
        auto & k = req->key;
        if (!is_known_key(k)) throw std::runtime_error("unknown key: " + k);
        if (INT_KEYS.count(k))
          updates[k] = coerce(k, json(req->int_value));
        else if (FLOAT_KEYS.count(k))
          updates[k] = coerce(k, json(req->float_value));
        else
          updates[k] = coerce(k, json(req->bool_value));
      } else {
        throw std::runtime_error("either key or mapping_json must be set");
      }

      for (auto & [k, v] : updates) mapping_[k] = v;
      if (updates.count("rate")) restart_timer();

      std::string msg = "Updated " + std::to_string(updates.size()) + " key(s)";
      if (req->save_to_file) {
        auto [ok, save_msg] = save_mapping_to_file();
        if (!ok) {
          resp->success = false;
          resp->message = save_msg;
          resp->mapping_json = mapping_to_json().dump();
          return;
        }
        msg += "; " + save_msg;
      } else {
        if (mapping_source_ != "file") mapping_source_ = "override";
      }
      resp->success = true;
      resp->message = msg;
      resp->mapping_json = mapping_to_json().dump();
    } catch (const std::exception & e) {
      resp->success = false;
      resp->message = e.what();
      resp->mapping_json = mapping_to_json().dump();
    }
  }

  // ── Helpers ──
  double axis(const sensor_msgs::msg::Joy & joy, int idx, bool invert) const {
    if (idx < 0 || idx >= static_cast<int>(joy.axes.size())) return 0.0;
    double v = static_cast<double>(joy.axes[idx]);
    double dz = mapping_.at("deadzone").as_float();
    if (v > -dz && v < dz) v = 0.0;
    if (invert) v = -v;
    return v;
  }

  bool btn(const sensor_msgs::msg::Joy & joy, int idx) const {
    if (idx < 0 || idx >= static_cast<int>(joy.buttons.size())) return false;
    return joy.buttons[idx] != 0;
  }

  void publish_status(bool force = false) {
    // Joy-specific status (latched, only on change)
    auto joy_msg = physicar_interfaces::msg::JoyTeleopStatus();
    joy_msg.enabled = enabled_;
    if (force || joy_msg.enabled != published_status_.enabled) {
      pub_status_->publish(joy_msg);
      published_status_ = joy_msg;
    }

    // Generic teleop status (every tick)
    auto teleop_msg = physicar_interfaces::msg::TeleopStatus();
    teleop_msg.source = "joy";
    teleop_msg.drive_engaged = enabled_ && drive_engaged_;
    teleop_msg.camera_engaged = enabled_ && camera_engaged_;
    teleop_msg.estop_latched = estop_latched_;
    int64_t timeout_ns = static_cast<int64_t>(teleop_status_timeout_sec_ * 1e9);
    teleop_msg.timeout.sec = static_cast<int32_t>(timeout_ns / 1000000000LL);
    teleop_msg.timeout.nanosec = static_cast<uint32_t>(timeout_ns % 1000000000LL);
    pub_teleop_status_->publish(teleop_msg);
  }

  // ── Callbacks ──
  void on_joy(const sensor_msgs::msg::Joy::SharedPtr msg) {
    last_joy_ = *msg;
    has_joy_ = true;
    last_joy_time_ = get_clock()->now();

    // ESTOP: active while held
    bool estop_now = btn(*msg, static_cast<int>(mapping_["estop_button"].as_int()));
    if (estop_now != estop_latched_) {
      estop_latched_ = estop_now;
      RCLCPP_WARN(get_logger(), "Teleop ESTOP %s", estop_latched_ ? "ON" : "OFF");
    }

    // Edge-trigger camera recenter
    bool center_now = btn(*msg, static_cast<int>(mapping_["center_camera_button"].as_int()));
    if (center_now && !prev_center_) {
      pan_ = 0.0;
      tilt_ = 0.0;
    }
    prev_center_ = center_now;
  }

  void tick() {
    if (!has_joy_ || !enabled_) {
      drive_engaged_ = false;
      camera_engaged_ = false;
      publish_status();
      return;
    }

    // Stale-joy guard (>0.5s gap = controller dropped)
    auto now = get_clock()->now();
    auto age_ns = (now - last_joy_time_).nanoseconds();
    if (age_ns > 500000000LL) {
      if (drive_engaged_ || camera_engaged_) {
        RCLCPP_WARN(get_logger(), "/joy stale > 0.5s — releasing drive/camera engagement");
      }
      drive_engaged_ = false;
      camera_engaged_ = false;
      publish_status();
      return;
    }

    bool prev_drive = drive_engaged_;
    bool prev_camera = camera_engaged_;

    drive_engaged_ = btn(last_joy_, static_cast<int>(mapping_["deadman_button"].as_int()));
    camera_engaged_ = btn(last_joy_, static_cast<int>(mapping_["camera_assist_button"].as_int()));

    auto pub_f64 = [](rclcpp::Publisher<std_msgs::msg::Float64>::SharedPtr & pub, double v) {
      auto m = std_msgs::msg::Float64();
      m.data = v;
      pub->publish(m);
    };

    // ── Drive (only while LB held and ESTOP not latched) ──
    if (drive_engaged_ && !estop_latched_) {
      double spd = axis(last_joy_, static_cast<int>(mapping_["axis_speed"].as_int()),
                        mapping_["invert_speed"].as_bool());
      double stg = axis(last_joy_, static_cast<int>(mapping_["axis_steering"].as_int()),
                        mapping_["invert_steering"].as_bool());
      double max_speed = mapping_["max_speed"].as_float();
      double speed = clip(spd * max_speed, max_speed);
      double max_steer_deg = mapping_["max_steering"].as_float();
      double steering_deg = clip(stg * max_steer_deg, max_steer_deg);
      pub_f64(pub_speed_, speed);
      pub_f64(pub_steer_, deg2rad(steering_deg));
    } else if (prev_drive && !drive_engaged_) {
      // Released — emit one zero
      pub_f64(pub_speed_, 0.0);
      pub_f64(pub_steer_, 0.0);
    } else if (estop_latched_ && drive_engaged_) {
      pub_f64(pub_speed_, 0.0);
    }

    // ── Camera (only while RB held; position mode) ──
    if (camera_engaged_) {
      double pan_in = axis(last_joy_, static_cast<int>(mapping_["axis_pan"].as_int()),
                           mapping_["invert_pan"].as_bool());
      double tilt_in = axis(last_joy_, static_cast<int>(mapping_["axis_tilt"].as_int()),
                            mapping_["invert_tilt"].as_bool());
      double max_pan = mapping_["max_pan"].as_float();
      double max_tilt = mapping_["max_tilt"].as_float();
      pan_ = clip(pan_in * max_pan, max_pan);
      tilt_ = clip(tilt_in * max_tilt, max_tilt);
      pub_f64(pub_pan_, deg2rad(pan_));
      pub_f64(pub_tilt_, deg2rad(tilt_));
    } else if (prev_camera && !camera_engaged_) {
      pub_f64(pub_pan_, deg2rad(pan_));
      pub_f64(pub_tilt_, deg2rad(tilt_));
    }

    publish_status();
  }

  // ── Members ──
  std::map<std::string, Val> mapping_;
  std::string mapping_source_;
  std::string mapping_units_;
  bool enabled_ = true;
  double teleop_status_timeout_sec_ = 0.5;

  // State
  sensor_msgs::msg::Joy last_joy_;
  bool has_joy_ = false;
  rclcpp::Time last_joy_time_{0, 0, RCL_ROS_TIME};
  bool drive_engaged_ = false;
  bool camera_engaged_ = false;
  bool estop_latched_ = false;
  bool prev_center_ = false;
  double pan_ = 0.0;
  double tilt_ = 0.0;
  physicar_interfaces::msg::JoyTeleopStatus published_status_;

  // ROS handles
  rclcpp::Publisher<std_msgs::msg::Float64>::SharedPtr pub_speed_, pub_steer_, pub_pan_, pub_tilt_;
  rclcpp::Publisher<physicar_interfaces::msg::JoyTeleopStatus>::SharedPtr pub_status_;
  rclcpp::Publisher<physicar_interfaces::msg::TeleopStatus>::SharedPtr pub_teleop_status_;
  rclcpp::Subscription<sensor_msgs::msg::Joy>::SharedPtr sub_joy_;
  rclcpp::Service<physicar_interfaces::srv::GetJoyMapping>::SharedPtr srv_get_;
  rclcpp::Service<physicar_interfaces::srv::SetJoyMapping>::SharedPtr srv_set_;
  rclcpp::TimerBase::SharedPtr timer_;
  rclcpp::node_interfaces::OnSetParametersCallbackHandle::SharedPtr param_cb_handle_;
};


int main(int argc, char ** argv) {
  rclcpp::init(argc, argv);
  auto node = std::make_shared<JoyTeleopNode>();
  rclcpp::spin(node);
  rclcpp::shutdown();
  return 0;
}
