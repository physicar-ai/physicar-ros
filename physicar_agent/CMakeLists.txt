cmake_minimum_required(VERSION 3.5)
project(physicar_agent)

find_package(ament_cmake REQUIRED)
find_package(ament_cmake_python REQUIRED)

# Install Python package (symlinked with --symlink-install)
ament_python_install_package(${PROJECT_NAME})

# Node executable
install(PROGRAMS
  scripts/agent_node
  DESTINATION lib/${PROJECT_NAME}
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
