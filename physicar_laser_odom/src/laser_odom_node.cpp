// Copyright 2026 AICASTLE Inc.
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

/// @file laser_odom_node.cpp
/// @brief ROS2 node for 2D laser scan odometry using Point-to-Line ICP.
///
/// Subscribes to /scan_filtered (LaserScan) and publishes /odom/laser (Odometry).
/// The laser→base TF is read once from the TF tree.
/// This node replaces rf2o_laser_odometry with a cleaner, Apache-2.0 implementation.

#include <memory>

#include <rclcpp/rclcpp.hpp>
#include <sensor_msgs/msg/laser_scan.hpp>
#include <nav_msgs/msg/odometry.hpp>
#include <geometry_msgs/msg/transform_stamped.hpp>

#include <tf2/utils.h>
#include <tf2_ros/buffer.h>
#include <tf2_ros/transform_listener.h>
#include <tf2_ros/transform_broadcaster.h>
#include <tf2_geometry_msgs/tf2_geometry_msgs.hpp>

#include "physicar_laser_odom/scan_matcher.hpp"

namespace physicar {

class LaserOdomNode : public rclcpp::Node {
 public:
  LaserOdomNode() : Node("laser_odom_node") {
    // Parameters
    declare_parameter<std::string>("laser_scan_topic", "/scan");
    declare_parameter<std::string>("odom_topic", "/odom/laser");
    declare_parameter<std::string>("base_frame_id", "base_footprint");
    declare_parameter<std::string>("odom_frame_id", "odom");
    declare_parameter<bool>("publish_tf", false);
    declare_parameter<int>("max_iterations", 30);
    declare_parameter<double>("max_correspondence_dist", 0.5);
    declare_parameter<double>("convergence_threshold", 1e-5);

    laser_topic_ = get_parameter("laser_scan_topic").as_string();
    odom_topic_  = get_parameter("odom_topic").as_string();
    base_frame_  = get_parameter("base_frame_id").as_string();
    odom_frame_  = get_parameter("odom_frame_id").as_string();
    publish_tf_  = get_parameter("publish_tf").as_bool();

    matcher_.max_iterations = get_parameter("max_iterations").as_int();
    matcher_.max_correspondence_dist = get_parameter("max_correspondence_dist").as_double();
    matcher_.convergence_threshold = get_parameter("convergence_threshold").as_double();

    // TF
    tf_buffer_ = std::make_shared<tf2_ros::Buffer>(get_clock());
    tf_listener_ = std::make_shared<tf2_ros::TransformListener>(*tf_buffer_);
    if (publish_tf_)
      tf_broadcaster_ = std::make_unique<tf2_ros::TransformBroadcaster>(this);

    // Publishers
    odom_pub_ = create_publisher<nav_msgs::msg::Odometry>(odom_topic_, 5);

    // Subscribers (best-effort for sensor data)
    auto qos = rclcpp::QoS(rclcpp::KeepLast(1)).best_effort().durability_volatile();
    scan_sub_ = create_subscription<sensor_msgs::msg::LaserScan>(
        laser_topic_, qos,
        std::bind(&LaserOdomNode::on_scan, this, std::placeholders::_1));

    RCLCPP_INFO(get_logger(),
        "LaserOdomNode started — listening on [%s], publishing to [%s]",
        laser_topic_.c_str(), odom_topic_.c_str());
  }

 private:
  void on_scan(const sensor_msgs::msg::LaserScan::SharedPtr scan) {
    // Convert scan to points
    auto points = scan_to_points(
        scan->ranges.data(),
        static_cast<int>(scan->ranges.size()),
        scan->angle_min, scan->angle_increment,
        scan->range_min, scan->range_max);

    if (points.size() < 20) return;

    // Get laser→base transform (once)
    if (!got_laser_tf_) {
      got_laser_tf_ = lookup_laser_tf(scan->header.frame_id);
      if (!got_laser_tf_) return;
    }

    // Transform points from laser frame to base frame
    for (auto& p : points)
      p = laser_to_base_.transform(p);

    if (!has_prev_scan_) {
      prev_points_ = std::move(points);
      prev_stamp_ = scan->header.stamp;
      has_prev_scan_ = true;
      return;
    }

    // Run ICP: match current (source) to previous (target)
    auto result = matcher_.match(prev_points_, points);

    if (!result.converged && result.iterations >= matcher_.max_iterations) {
      RCLCPP_WARN_THROTTLE(get_logger(), *get_clock(), 2000,
          "ICP did not converge (iter=%d, fitness=%.4f)",
          result.iterations, result.fitness);
    }

    // Accumulate pose
    pose_ = pose_.compose(result.delta);

    // Compute velocities
    const double dt = (rclcpp::Time(scan->header.stamp) - rclcpp::Time(prev_stamp_)).seconds();
    double vx = 0, vyaw = 0;
    if (dt > 1e-6 && dt < 1.0) {
      vx = result.delta.x / dt;  // forward velocity (local frame)
      vyaw = result.delta.yaw / dt;
    }

    // Publish odometry
    publish_odom(scan->header.stamp, vx, vyaw);

    // Shift
    prev_points_ = std::move(points);
    prev_stamp_ = scan->header.stamp;
  }

  void publish_odom(const rclcpp::Time& stamp, double vx, double vyaw) {
    tf2::Quaternion q;
    q.setRPY(0, 0, pose_.yaw);

    nav_msgs::msg::Odometry odom;
    odom.header.stamp = stamp;
    odom.header.frame_id = odom_frame_;
    odom.child_frame_id = base_frame_;
    odom.pose.pose.position.x = pose_.x;
    odom.pose.pose.position.y = pose_.y;
    odom.pose.pose.orientation = tf2::toMsg(q);
    odom.twist.twist.linear.x = vx;
    odom.twist.twist.angular.z = vyaw;
    odom_pub_->publish(odom);

    if (publish_tf_) {
      geometry_msgs::msg::TransformStamped tf;
      tf.header.stamp = stamp;
      tf.header.frame_id = odom_frame_;
      tf.child_frame_id = base_frame_;
      tf.transform.translation.x = pose_.x;
      tf.transform.translation.y = pose_.y;
      tf.transform.rotation = tf2::toMsg(q);
      tf_broadcaster_->sendTransform(tf);
    }
  }

  bool lookup_laser_tf(const std::string& laser_frame) {
    try {
      auto tf = tf_buffer_->lookupTransform(
          base_frame_, laser_frame, tf2::TimePointZero);
      laser_to_base_.x = tf.transform.translation.x;
      laser_to_base_.y = tf.transform.translation.y;
      laser_to_base_.yaw = tf2::getYaw(tf.transform.rotation);
      RCLCPP_INFO(get_logger(),
          "Laser TF [%s → %s]: x=%.3f y=%.3f yaw=%.1f°",
          laser_frame.c_str(), base_frame_.c_str(),
          laser_to_base_.x, laser_to_base_.y,
          laser_to_base_.yaw * 180.0 / M_PI);
      return true;
    } catch (const tf2::TransformException& ex) {
      RCLCPP_WARN_THROTTLE(get_logger(), *get_clock(), 2000,
          "Waiting for TF [%s → %s]: %s",
          laser_frame.c_str(), base_frame_.c_str(), ex.what());
      return false;
    }
  }

  // ROS interface
  std::string laser_topic_, odom_topic_, base_frame_, odom_frame_;
  bool publish_tf_ = false;

  rclcpp::Subscription<sensor_msgs::msg::LaserScan>::SharedPtr scan_sub_;
  rclcpp::Publisher<nav_msgs::msg::Odometry>::SharedPtr odom_pub_;
  std::shared_ptr<tf2_ros::Buffer> tf_buffer_;
  std::shared_ptr<tf2_ros::TransformListener> tf_listener_;
  std::unique_ptr<tf2_ros::TransformBroadcaster> tf_broadcaster_;

  // Algorithm
  ScanMatcher matcher_;
  std::vector<Point2D> prev_points_;
  builtin_interfaces::msg::Time prev_stamp_;
  bool has_prev_scan_ = false;
  bool got_laser_tf_ = false;
  Pose2D laser_to_base_;
  Pose2D pose_;  // accumulated pose in odom frame
};

}  // namespace physicar

int main(int argc, char** argv) {
  rclcpp::init(argc, argv);
  rclcpp::spin(std::make_shared<physicar::LaserOdomNode>());
  rclcpp::shutdown();
  return 0;
}
