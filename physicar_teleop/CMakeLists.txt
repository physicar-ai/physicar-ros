cmake_minimum_required(VERSION 3.10)
project(physicar_teleop)

find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)
find_package(rclcpp REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(std_msgs REQUIRED)
find_package(physicar_interfaces REQUIRED)
find_package(nlohmann_json REQUIRED)

# C++ node
add_executable(joy_teleop_node src/joy_teleop_node.cpp)
target_link_libraries(joy_teleop_node
  rclcpp::rclcpp ${sensor_msgs_TARGETS} ${std_msgs_TARGETS} ${physicar_interfaces_TARGETS}
  nlohmann_json::nlohmann_json)

install(TARGETS joy_teleop_node
  DESTINATION lib/${PROJECT_NAME}
)

ament_python_install_package(${PROJECT_NAME})

install(PROGRAMS
  scripts/joy_teleop_node.py
  DESTINATION lib/${PROJECT_NAME}
)

install(DIRECTORY config
  DESTINATION share/${PROJECT_NAME}
)

install(
  FILES resource/${PROJECT_NAME}
  DESTINATION share/ament_index/resource_index/packages
)
install(
  FILES package.xml
  DESTINATION share/${PROJECT_NAME}
)

ament_package()
