/**
 * Scan Filter Node for PhysiCar (C++ port)
 *
 * Filters invalid LaserScan readings (inf, nan, out of range) to improve
 * downstream odometry estimation (rf2o_laser_odometry).
 *
 * Subscribes: /scan
 * Publishes:  /scan_filtered
 */

#include <cmath>
#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/laser_scan.hpp>

class ScanFilterNode : public rclcpp::Node
{
public:
  ScanFilterNode() : Node("scan_filter")
  {
    declare_parameter("input_topic", std::string("/scan"));
    declare_parameter("output_topic", std::string("/scan_filtered"));
    declare_parameter("range_margin", 0.1);

    auto input_topic = get_parameter("input_topic").as_string();
    auto output_topic = get_parameter("output_topic").as_string();
    range_margin_ = get_parameter("range_margin").as_double();

    // Sensor QoS (best effort, volatile, depth 1)
    auto sensor_qos = rclcpp::SensorDataQoS().keep_last(1);

    pub_ = create_publisher<sensor_msgs::msg::LaserScan>(output_topic, sensor_qos);
    sub_ = create_subscription<sensor_msgs::msg::LaserScan>(
      input_topic, sensor_qos,
      [this](const sensor_msgs::msg::LaserScan::SharedPtr msg) { scan_callback(msg); });

    RCLCPP_INFO(get_logger(), "Scan filter: %s -> %s", input_topic.c_str(), output_topic.c_str());
  }

private:
  void scan_callback(const sensor_msgs::msg::LaserScan::SharedPtr msg)
  {
    auto filtered = std::make_unique<sensor_msgs::msg::LaserScan>();
    filtered->header = msg->header;
    filtered->angle_min = msg->angle_min;
    filtered->angle_max = msg->angle_max;
    filtered->angle_increment = msg->angle_increment;
    filtered->time_increment = msg->time_increment;
    filtered->scan_time = msg->scan_time;
    filtered->range_min = msg->range_min;
    filtered->range_max = msg->range_max;

    const float effective_max = msg->range_max - static_cast<float>(range_margin_);
    const size_t n = msg->ranges.size();
    filtered->ranges.resize(n);

    for (size_t i = 0; i < n; ++i) {
      const float r = msg->ranges[i];
      filtered->ranges[i] =
        (std::isfinite(r) && r > msg->range_min && r < effective_max) ? r : 0.0f;
    }

    if (!msg->intensities.empty()) {
      filtered->intensities = msg->intensities;
    }

    pub_->publish(std::move(filtered));
  }

  rclcpp::Publisher<sensor_msgs::msg::LaserScan>::SharedPtr pub_;
  rclcpp::Subscription<sensor_msgs::msg::LaserScan>::SharedPtr sub_;
  double range_margin_;
};

int main(int argc, char * argv[])
{
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<ScanFilterNode>());
  rclcpp::shutdown();
  return 0;
}
