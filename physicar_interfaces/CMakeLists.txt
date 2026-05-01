cmake_minimum_required(VERSION 3.8)
project(physicar_interfaces)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(rosidl_default_generators REQUIRED)
find_package(std_msgs REQUIRED)
find_package(action_msgs REQUIRED)
find_package(builtin_interfaces REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/CalibrationStatus.msg"
  "msg/Audio.msg"
  "msg/DeepracerInference.msg"
  "msg/JoyTeleopStatus.msg"
  "msg/TeleopStatus.msg"
  "srv/SetCalibration.srv"
  "srv/GetCalibration.srv"
  "srv/DeepracerLoadModel.srv"
  "srv/DeepracerUnloadModel.srv"
  "srv/DeepracerControl.srv"
  "srv/DeepracerStatus.srv"
  "srv/DeepracerSetConfig.srv"
  "srv/ToolList.srv"
  "srv/ToolGet.srv"
  "srv/ToolCall.srv"
  "srv/ToolSet.srv"
  "srv/ToolDelete.srv"
  "srv/ToolReset.srv"
  "srv/GetJoyMapping.srv"
  "srv/SetJoyMapping.srv"
  DEPENDENCIES std_msgs action_msgs builtin_interfaces
)

ament_export_dependencies(rosidl_default_runtime)

ament_package()
