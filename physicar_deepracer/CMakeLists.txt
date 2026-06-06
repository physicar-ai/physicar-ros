cmake_minimum_required(VERSION 3.8)
project(physicar_deepracer)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(rclcpp REQUIRED)
find_package(std_msgs REQUIRED)
find_package(sensor_msgs REQUIRED)
find_package(physicar_interfaces REQUIRED)
find_package(OpenCV REQUIRED)

# Find TensorFlow Lite (installed to /usr/local by default)
find_library(TFLITE_LIB tensorflowlite
  HINTS /usr/local/lib ${TFLITE_ROOT}/lib
)
find_path(TFLITE_INCLUDE_DIR tensorflow/lite/interpreter.h
  HINTS /usr/local/include ${TFLITE_ROOT}/include
)

if(NOT TFLITE_LIB OR NOT TFLITE_INCLUDE_DIR)
  message(FATAL_ERROR
    "TensorFlow Lite C++ library not found.\n"
    "Install it with: bash scripts/install_tflite.sh\n"
    "Or set -DTFLITE_ROOT=/path/to/tflite")
endif()

message(STATUS "TFLite lib: ${TFLITE_LIB}")
message(STATUS "TFLite include: ${TFLITE_INCLUDE_DIR}")

# C++ deepracer_node executable
add_executable(deepracer_node
  src/deepracer_node.cpp
  src/model_loader.cpp
  src/inference_engine.cpp
)

target_include_directories(deepracer_node PRIVATE
  include
  ${TFLITE_INCLUDE_DIR}
  ${OpenCV_INCLUDE_DIRS}
)

target_link_libraries(deepracer_node
  ${TFLITE_LIB}
  ${OpenCV_LIBS}
)

ament_target_dependencies(deepracer_node
  rclcpp
  std_msgs
  sensor_msgs
  physicar_interfaces
)

install(TARGETS deepracer_node
  DESTINATION lib/${PROJECT_NAME}
)

# Install launch files
install(DIRECTORY
  launch
  DESTINATION share/${PROJECT_NAME}/
)

# Install config files
install(DIRECTORY
  config
  DESTINATION share/${PROJECT_NAME}/
)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  ament_lint_auto_find_test_dependencies()
endif()

ament_package()
