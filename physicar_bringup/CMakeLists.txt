cmake_minimum_required(VERSION 3.5)
project(physicar_bringup)

find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)

# Install Python package (symlinked with --symlink-install)
ament_python_install_package(${PROJECT_NAME})

# Node executables (entry points)
install(PROGRAMS
  scripts/physicar_driver_node.py
  scripts/scan_filter_node.py
  scripts/audio_node.py
  scripts/cmd_vel_adapter_node.py
  scripts/topic_watchdog_node.py
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
