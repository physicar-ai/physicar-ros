/**
 * Topic Watchdog Node for PhysiCar (C++ port)
 *
 * Monitors critical sensor topics. If a topic stops publishing for longer
 * than its configured timeout, kills the responsible process (launch will
 * respawn it because every node has respawn=True).
 *
 * Restart policy:
 * - Startup grace period: ignore stale topics for the first N seconds.
 * - Cooldown: don't kill the same target twice within `cooldown` seconds.
 * - Sim mode: detect /clock backward jumps (world switch) and kill immediately.
 */

#include <chrono>
#include <csignal>
#include <cstdlib>
#include <fstream>
#include <functional>
#include <string>
#include <unordered_map>
#include <vector>

#include <rclcpp/rclcpp.hpp>
#include <nav_msgs/msg/odometry.hpp>
#include <rosgraph_msgs/msg/clock.hpp>
#include <sensor_msgs/msg/battery_state.hpp>
#include <sensor_msgs/msg/camera_info.hpp>
#include <sensor_msgs/msg/imu.hpp>
#include <sensor_msgs/msg/laser_scan.hpp>

using namespace std::chrono;
using Clock = std::chrono::steady_clock;

struct WatchEntry
{
  std::string topic;
  double timeout;
  std::string kill_pattern;
};

// Real-mode watch list
static const std::vector<WatchEntry> REAL_WATCH_TOPICS = {
  {"/camera/camera_info", 5.0, "camera_ros/lib/camera_ros/camera_node"},
  {"/scan", 3.0, "rplidar_node"},
  {"/scan_filtered", 3.0, "scan_filter_node"},
  {"/odom", 3.0, "ekf_filter_node"},
  {"/battery_state", 15.0, "physicar_driver_node"},
  {"/imu", 5.0, "physicar_driver_node"},
};

// Sim-mode watch list
static const std::vector<WatchEntry> SIM_WATCH_TOPICS = {
  {"/scan", 10.0, "ros_gz_bridge"},
  {"/odom/laser", 5.0, "rf2o_laser_odometry_node"},
  {"/odom", 15.0, "ekf_filter_node"},
};

class TopicWatchdog : public rclcpp::Node
{
public:
  TopicWatchdog() : Node("topic_watchdog")
  {
    declare_parameter("startup_grace_sec", 30.0);
    declare_parameter("cooldown_sec", 30.0);
    declare_parameter("check_period_sec", 2.0);
    declare_parameter("enabled", true);
    declare_parameter("mode", std::string("real"));

    enabled_ = get_parameter("enabled").as_bool();
    if (!enabled_) {
      RCLCPP_INFO(get_logger(), "[Watchdog] disabled (enabled=False)");
      return;
    }

    auto mode = get_parameter("mode").as_string();
    const auto & entries = (mode == "sim") ? SIM_WATCH_TOPICS : REAL_WATCH_TOPICS;

    startup_grace_ = get_parameter("startup_grace_sec").as_double();
    cooldown_ = get_parameter("cooldown_sec").as_double();
    auto check_period = get_parameter("check_period_sec").as_double();

    start_time_ = Clock::now();
    my_pid_ = getpid();

    // Best-effort QoS for sensors
    auto qos_be = rclcpp::SensorDataQoS().keep_last(1);
    // Reliable QoS for other topics
    auto qos_rel = rclcpp::QoS(1).reliable();

    for (const auto & e : entries) {
      topics_.push_back(e);
      last_msg_time_[e.topic] = start_time_;

      // Choose QoS: best-effort for LaserScan and Imu topics
      bool is_sensor = (e.topic.find("/scan") != std::string::npos ||
                        e.topic == "/imu");
      auto & qos = is_sensor ? qos_be : qos_rel;

      // Generic subscription — we only need the callback, not the data
      subs_.push_back(create_generic_subscription(
        e.topic,
        get_msg_type(e.topic),
        qos,
        [this, topic = e.topic](std::shared_ptr<rclcpp::SerializedMessage>) {
          last_msg_time_[topic] = Clock::now();
        }));
    }

    timer_ = create_wall_timer(
      duration<double>(check_period),
      std::bind(&TopicWatchdog::check, this));

    RCLCPP_INFO(get_logger(),
      "[Watchdog] mode=%s, monitoring %zu topics (grace=%.0fs, cooldown=%.0fs)",
      mode.c_str(), topics_.size(), startup_grace_, cooldown_);

    // Sim mode: monitor /clock for backward time jumps
    if (mode == "sim") {
      clock_sub_ = create_subscription<rosgraph_msgs::msg::Clock>(
        "/clock", qos_be,
        std::bind(&TopicWatchdog::on_clock, this, std::placeholders::_1));
    }
  }

private:
  static std::string get_msg_type(const std::string & topic)
  {
    if (topic.find("/camera_info") != std::string::npos) return "sensor_msgs/msg/CameraInfo";
    if (topic.find("/scan") != std::string::npos) return "sensor_msgs/msg/LaserScan";
    if (topic == "/imu") return "sensor_msgs/msg/Imu";
    if (topic.find("/odom") != std::string::npos) return "nav_msgs/msg/Odometry";
    if (topic.find("/battery") != std::string::npos) return "sensor_msgs/msg/BatteryState";
    if (topic == "/clock") return "rosgraph_msgs/msg/Clock";
    return "std_msgs/msg/Empty";
  }

  void on_clock(const rosgraph_msgs::msg::Clock::SharedPtr msg)
  {
    double sec = msg->clock.sec + msg->clock.nanosec * 1e-9;
    double prev = last_sim_sec_;
    last_sim_sec_ = sec;

    if (prev < 1.0 || sec >= prev) return;

    auto now = Clock::now();
    if (duration<double>(now - start_time_).count() < startup_grace_) return;

    RCLCPP_WARN(get_logger(),
      "[Watchdog] /clock jumped backward (%.1fs -> %.1fs) — world switch detected",
      prev, sec);

    std::unordered_map<std::string, bool> killed;
    for (const auto & e : topics_) {
      if (killed.count(e.kill_pattern)) continue;
      auto it = last_kill_time_.find(e.kill_pattern);
      if (it != last_kill_time_.end() &&
          duration<double>(now - it->second).count() < cooldown_) continue;
      if (kill_process(e.kill_pattern)) {
        last_kill_time_[e.kill_pattern] = now;
        killed[e.kill_pattern] = true;
      }
    }
    for (const auto & e : topics_) {
      last_msg_time_[e.topic] = now;
    }
  }

  void check()
  {
    auto now = Clock::now();
    if (duration<double>(now - start_time_).count() < startup_grace_) return;

    for (const auto & e : topics_) {
      double stale = duration<double>(now - last_msg_time_[e.topic]).count();
      if (stale < e.timeout) continue;

      auto it = last_kill_time_.find(e.kill_pattern);
      if (it != last_kill_time_.end() &&
          duration<double>(now - it->second).count() < cooldown_) continue;

      RCLCPP_WARN(get_logger(),
        "[Watchdog] %s stale for %.1fs (timeout=%.0fs) -> killing '%s'",
        e.topic.c_str(), stale, e.timeout, e.kill_pattern.c_str());

      if (kill_process(e.kill_pattern)) {
        last_kill_time_[e.kill_pattern] = now;
        for (const auto & t : topics_) {
          if (t.kill_pattern == e.kill_pattern) {
            last_msg_time_[t.topic] = now;
          }
        }
      }
    }
  }

  static bool exe_matches(pid_t pid, const std::string & pattern)
  {
    // Check /proc/PID/exe (binary path)
    char buf[512];
    std::string link = "/proc/" + std::to_string(pid) + "/exe";
    ssize_t len = ::readlink(link.c_str(), buf, sizeof(buf) - 1);
    if (len > 0) {
      buf[len] = '\0';
      if (std::string(buf).find(pattern) != std::string::npos) return true;
    }
    // Fallback: check /proc/PID/cmdline (includes node name remapping etc.)
    std::string cmdline_path = "/proc/" + std::to_string(pid) + "/cmdline";
    std::ifstream ifs(cmdline_path);
    if (ifs) {
      std::string cmdline((std::istreambuf_iterator<char>(ifs)),
                           std::istreambuf_iterator<char>());
      if (cmdline.find(pattern) != std::string::npos) return true;
    }
    return false;
  }

  bool kill_process(const std::string & pattern)
  {
    // Use pgrep to find candidate PIDs, then verify via /proc/PID/exe
    std::string cmd = "pgrep -f '" + pattern + "' 2>/dev/null";
    FILE * fp = popen(cmd.c_str(), "r");
    if (!fp) return false;

    std::vector<pid_t> pids;
    char buf[32];
    while (fgets(buf, sizeof(buf), fp)) {
      pid_t pid = static_cast<pid_t>(std::atoi(buf));
      if (pid > 0 && pid != my_pid_ && exe_matches(pid, pattern)) {
        pids.push_back(pid);
      }
    }
    pclose(fp);

    if (pids.empty()) {
      RCLCPP_WARN(get_logger(), "[Watchdog] no process matching '%s'", pattern.c_str());
      return false;
    }

    for (pid_t pid : pids) {
      if (::kill(pid, SIGTERM) == 0) {
        RCLCPP_INFO(get_logger(), "[Watchdog] SIGTERM pid=%d (%s)", pid, pattern.c_str());
      }
    }
    return true;
  }

  bool enabled_;
  double startup_grace_;
  double cooldown_;
  pid_t my_pid_;
  Clock::time_point start_time_;
  double last_sim_sec_ = 0.0;

  std::vector<WatchEntry> topics_;
  std::unordered_map<std::string, Clock::time_point> last_msg_time_;
  std::unordered_map<std::string, Clock::time_point> last_kill_time_;

  std::vector<std::shared_ptr<rclcpp::GenericSubscription>> subs_;
  rclcpp::Subscription<rosgraph_msgs::msg::Clock>::SharedPtr clock_sub_;
  rclcpp::TimerBase::SharedPtr timer_;
};

int main(int argc, char * argv[])
{
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<TopicWatchdog>());
  rclcpp::shutdown();
  return 0;
}
