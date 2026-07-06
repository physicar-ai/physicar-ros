cmake_minimum_required(VERSION 3.5)
project(physicar_bringup)

find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(rclcpp REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(nav_msgs REQUIRED)
find_package(rosgraph_msgs REQUIRED)
find_package(geometry_msgs REQUIRED)
find_package(std_msgs REQUIRED)
find_package(std_srvs REQUIRED)

# Optional: custom interfaces
find_package(physicar_interfaces QUIET)

# ── C++ nodes ──
add_executable(scan_filter_node src/scan_filter_node.cpp)
ament_target_dependencies(scan_filter_node rclcpp sensor_msgs)

add_executable(topic_watchdog_node src/topic_watchdog_node.cpp)
ament_target_dependencies(topic_watchdog_node rclcpp sensor_msgs nav_msgs rosgraph_msgs)

add_executable(physicar_driver_node src/physicar_driver_node.cpp)
ament_target_dependencies(physicar_driver_node
  rclcpp sensor_msgs nav_msgs geometry_msgs std_msgs std_srvs)
if(physicar_interfaces_FOUND)
  ament_target_dependencies(physicar_driver_node physicar_interfaces)
  target_compile_definitions(physicar_driver_node PRIVATE HAS_CUSTOM_INTERFACES=1)
endif()

install(TARGETS
  scan_filter_node
  topic_watchdog_node
  physicar_driver_node
  DESTINATION lib/${PROJECT_NAME}
)

# Install Python package (symlinked with --symlink-install)
ament_python_install_package(${PROJECT_NAME})

# Node executables (entry points)
install(PROGRAMS
  scripts/cmd_vel_adapter_node.py
  DESTINATION lib/${PROJECT_NAME}
)

# Data files (launch, config, params, sounds, shell scripts)
install(
  DIRECTORY launch config param sounds scripts
  DESTINATION share/${PROJECT_NAME}
  PATTERN "*.pyc" EXCLUDE
)

# Package marker
install(
  FILES resource/${PROJECT_NAME}
  DESTINATION share/ament_index/resource_index/packages
)
install(
  FILES package.xml
  DESTINATION share/${PROJECT_NAME}
)

ament_package()
