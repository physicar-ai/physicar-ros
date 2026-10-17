cmake_minimum_required(VERSION 3.10)
project(physicar_laser_odom)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic -O2)
endif()

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(nav_msgs REQUIRED)
find_package(std_srvs REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(tf2 REQUIRED)
find_package(tf2_ros REQUIRED)
find_package(tf2_geometry_msgs REQUIRED)
find_package(Eigen3 REQUIRED)

add_executable(laser_odom_node src/laser_odom_node.cpp)
target_include_directories(laser_odom_node PRIVATE
  include
  ${EIGEN3_INCLUDE_DIRS}
)
target_link_libraries(laser_odom_node PRIVATE
  rclcpp::rclcpp
  ${sensor_msgs_TARGETS}
  ${nav_msgs_TARGETS}
  ${std_srvs_TARGETS}
  ${geometry_msgs_TARGETS}
  tf2::tf2
  tf2_ros::tf2_ros
  tf2_geometry_msgs::tf2_geometry_msgs
)

install(TARGETS laser_odom_node
  DESTINATION lib/${PROJECT_NAME}
)

ament_package()
